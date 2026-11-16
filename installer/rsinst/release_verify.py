"""Bounded, read-only checks for signed releases and staged artifacts (U2.1).

No network, Docker, extraction, key provisioning or service control. Trust paths
are explicit administrator inputs, never taken from a manifest. A successful
check is not permission to execute/apply; later stages own private staging.
"""
import base64
from contextlib import contextmanager
import gzip
import hashlib
import os
from pathlib import Path
import stat
import struct
import subprocess
import tarfile
import tempfile
import zlib

from . import release as r

MAX_SIGNATURE = 16 << 10
MAX_TRUST = 256 << 10
MAX_REVOCATIONS = 4 << 20
CHUNK = 1 << 20
SSH_KEYGEN = '/usr/bin/ssh-keygen'


@contextmanager
def regular_file(filename, trusted=False):
    """Open component-by-component without following symlinks (including parents)."""
    name = os.fspath(filename)
    r.need(type(name) is str and name.startswith('/'), 'absolute source path required')
    r.path(name[1:])
    directory = fd = None
    try:
        directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        for component in name[1:].split('/')[:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
            os.close(directory); directory = child
            if trusted:
                s = os.fstat(directory)
                shared_tmp = s.st_uid == 0 and s.st_mode & stat.S_ISVTX
                r.need(s.st_uid in (0, os.geteuid()) and (not s.st_mode & 0o022 or shared_tmp), 'unsafe trust directory')
        fd = os.open(name.split('/')[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
        s = os.fstat(fd)
        r.need(stat.S_ISREG(s.st_mode) and s.st_nlink == 1, 'expected a single-link regular source file')
        if trusted:
            r.need(s.st_uid == os.geteuid() and not s.st_mode & 0o022, 'unsafe trust file ownership/permissions')
        f = os.fdopen(fd, 'rb'); fd = None
        with f:
            yield f
    except OSError:
        raise r.InvalidRelease('cannot securely read source file') from None
    finally:
        if fd is not None: os.close(fd)
        if directory is not None: os.close(directory)


def bounded_read(filename, maximum, trusted=False):
    with regular_file(filename, trusted) as f:
        before = os.fstat(f.fileno())
        r.need(0 < before.st_size <= maximum, 'file size limit')
        raw = f.read(maximum + 1)
        after = os.fstat(f.fileno())
        r.need(len(raw) == before.st_size and _identity(before) == _identity(after), 'source changed while reading')
        return raw


def _identity(s):
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns


def _ssh_string(raw):
    r.need(len(raw) >= 4, 'invalid SSH signature encoding')
    size = struct.unpack('>I', raw[:4])[0]
    r.need(size <= len(raw) - 4, 'invalid SSH signature length')
    return raw[4:4 + size], raw[4 + size:]


def _signature_header(raw, namespace):
    """Bounded envelope/algorithm check; only ssh-keygen establishes authenticity."""
    r.need(type(raw) is bytes and 0 < len(raw) <= MAX_SIGNATURE, 'signature size limit')
    lines = raw.strip().splitlines()
    r.need(len(lines) >= 3 and lines[0] == b'-----BEGIN SSH SIGNATURE-----'
           and lines[-1] == b'-----END SSH SIGNATURE-----', 'invalid SSH signature armor')
    try:
        data = base64.b64decode(b''.join(lines[1:-1]), validate=True)
    except ValueError:
        raise r.InvalidRelease('invalid SSH signature base64') from None
    r.need(data[:10] == b'SSHSIG\0\0\0\1', 'unsupported SSH signature format')
    pub, data = _ssh_string(data[10:])
    keytype, pub = _ssh_string(pub)
    key, tail = _ssh_string(pub)
    r.need(keytype == b'ssh-ed25519' and len(key) == 32 and not tail, 'Ed25519 signature required')
    ns, data = _ssh_string(data)
    reserved, data = _ssh_string(data)
    algorithm, data = _ssh_string(data)
    sig, data = _ssh_string(data)
    sigtype, sig = _ssh_string(sig)
    sigbytes, tail = _ssh_string(sig)
    r.need(ns == namespace.encode('ascii') and not reserved and algorithm in (b'sha256', b'sha512')
           and sigtype == b'ssh-ed25519' and len(sigbytes) == 64 and not tail and not data,
           'invalid SSH signature namespace/algorithm')


def verify_signature(raw, signature, trust_file, namespace, revocation_file=None):
    """Verify bounded exact bytes, using a snapshot of an external trust store."""
    r.choice(namespace, ('ragspark-release-v1', 'ragspark-channel-v1'))
    limit = r.MAX_MANIFEST if namespace == 'ragspark-release-v1' else r.MAX_CHANNEL
    r.need(type(raw) is bytes and 0 < len(raw) <= limit, 'metadata size limit')
    _signature_header(signature, namespace)
    trust = bounded_read(trust_file, MAX_TRUST, trusted=True)
    revoked = bounded_read(revocation_file, MAX_REVOCATIONS, trusted=True) if revocation_file is not None else None
    # Public metadata only; all temporary files still stay private.
    with tempfile.TemporaryDirectory(prefix='ragspark-signature-') as tmp:
        root = Path(tmp)
        for name, value in (('allowed', trust), ('signature', signature), ('revoked', revoked)):
            if value is not None:
                fd = os.open(root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as f: f.write(value)
        cmd = [SSH_KEYGEN, '-Y', 'verify', '-f', str(root / 'allowed'), '-I', 'ragspark-release',
               '-n', namespace, '-s', str(root / 'signature')]
        if revoked is not None: cmd += ['-r', str(root / 'revoked')]
        try:
            result = subprocess.run(cmd, input=raw, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'}, timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise r.InvalidRelease('signature verifier unavailable or timed out') from None
        r.need(result.returncode == 0, 'signature not trusted')


def load_manifest(filename, signature_file, trust_file, revocation_file=None, pointer=None):
    raw = bounded_read(filename, r.MAX_MANIFEST)
    signature = bounded_read(signature_file, MAX_SIGNATURE)
    verify_signature(raw, signature, trust_file, 'ragspark-release-v1', revocation_file)
    data = r.manifest(raw)
    if pointer is not None: r.bind_channel(pointer, raw, data)
    return data


def load_channel(filename, signature_file, trust_file, now, previous=None, revocation_file=None):
    raw = bounded_read(filename, r.MAX_CHANNEL)
    signature = bounded_read(signature_file, MAX_SIGNATURE)
    verify_signature(raw, signature, trust_file, 'ragspark-channel-v1', revocation_file)
    return r.channel(raw, now, previous)


class _Budget:
    def __init__(self, stream, maximum):
        self.stream, self.remaining = stream, maximum

    def read(self, size):
        r.need(0 <= size <= CHUNK, 'invalid stream read size')
        value = self.stream.read(min(size, self.remaining + 1))
        self.remaining -= len(value)
        r.need(self.remaining >= 0, 'expanded archive limit')
        return value

    def exact(self, size):
        value = self.read(size)
        r.need(len(value) == size, 'truncated archive')
        return value


def _inspect_tar(stream, a):
    expected = {f['path']: f for f in a['files']}
    directories = set()
    for name in expected:
        parts = name.split('/')
        directories.update('/'.join(parts[:i]) for i in range(1, len(parts)))
    # Each legitimate path may have one header, with at most one partial data block.
    r.need(len(expected) + len(directories) <= 100000, 'archive entry count limit')
    budget = a['expanded_bytes'] + (len(expected) + len(directories)) * 1024 + 10240
    reader = _Budget(stream, budget)
    seen, found = set(), set()
    while True:
        block = reader.exact(512)
        if block == bytes(512):
            r.need(reader.exact(512) == bytes(512), 'invalid tar end marker')
            # Accept ordinary tar record padding, never a second archive/hidden payload.
            tail = reader.read(10240)
            r.need(len(tail) < 10240 and len(tail) % 512 == 0 and not tail.strip(b'\0'), 'invalid tar trailing bytes')
            break
        r.need(len(seen) < len(expected) + len(directories), 'archive header count limit')
        # Reject extension records before TarInfo can consume their unbounded payloads.
        r.need(block[257:265] == b'ustar\x0000' and block[156:157] in (b'0', b'\0', b'5'), 'unsupported tar header/type (USTAR required)')
        try:
            item = tarfile.TarInfo.frombuf(block, 'utf-8', 'strict')
        except (tarfile.TarError, UnicodeError, ValueError, OverflowError):
            raise r.InvalidRelease('invalid tar header') from None
        try:
            raw_name = block[:100].split(b'\0', 1)[0].decode('utf-8')
            prefix = block[345:500].split(b'\0', 1)[0].decode('utf-8')
        except UnicodeError:
            raise r.InvalidRelease('invalid tar path encoding') from None
        if prefix: raw_name = prefix + '/' + raw_name
        if item.isdir() and raw_name.endswith('/'): raw_name = raw_name[:-1]
        name = r.path(raw_name)
        r.need(name == item.name, 'noncanonical tar path')
        r.need(name not in seen, 'duplicate archive path'); seen.add(name)
        r.need(item.uid == 0 and item.gid == 0 and not item.linkname, 'unexpected tar owner/link')
        r.integer(item.size); r.integer(item.mtime)
        if item.isdir():
            r.need(name in directories and item.size == 0 and item.mode == 0o755, 'invalid archive directory')
            continue
        r.need(name in expected, 'unexpected archive file')
        entry = expected[name]
        r.need(item.size == entry['bytes'] and item.mode == int(entry['mode'], 8), 'archive file metadata mismatch')
        sha, left = hashlib.sha256(), item.size
        while left:
            part = reader.exact(min(left, CHUNK)); left -= len(part); sha.update(part)
        r.need(sha.hexdigest() == entry['sha256'], 'archive file digest mismatch')
        padding = reader.exact((-item.size) % 512)
        r.need(not padding.strip(b'\0'), 'nonzero tar data padding')
        found.add(name)
    r.need(found == set(expected), 'archive inventory incomplete')
    return len(found)


def verify_artifact(bundle_directory, metadata):
    """Inspect exact inventory without extraction; image result is bytes-only."""
    a = r.artifact(metadata)
    filename = Path(bundle_directory) / a['path']
    with regular_file(filename) as f:
        before = os.fstat(f.fileno())
        r.need(before.st_size == a['bytes'], 'artifact length mismatch')
        sha = hashlib.sha256()
        remaining = a['bytes']
        while remaining:
            block = f.read(min(CHUNK, remaining))
            r.need(bool(block), 'truncated artifact')
            sha.update(block); remaining -= len(block)
        r.need(not f.read(1) and sha.hexdigest() == a['sha256'], 'artifact digest mismatch')
        count = None
        if a['kind'] != 'image':
            f.seek(0)
            try:
                if a['kind'] == 'code':
                    with gzip.GzipFile(fileobj=f, mode='rb') as stream:
                        count = _inspect_tar(stream, a)
                else:
                    count = _inspect_tar(f, a)
            except (OSError, EOFError, tarfile.TarError, ValueError, zlib.error) as e:
                if isinstance(e, r.InvalidRelease): raise
                raise r.InvalidRelease('invalid archive stream') from None
        r.need(_identity(before) == _identity(os.fstat(f.fileno())), 'artifact changed while checking')
    return {'id': a['id'], 'sha256': a['sha256'], 'bytes': a['bytes'], 'files': count,
            'inspection': 'archive-bytes-only' if a['kind'] == 'image' else 'file-inventory'}
