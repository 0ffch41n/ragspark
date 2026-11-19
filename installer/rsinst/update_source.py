"""Authenticated release discovery and private, bounded staging for update --check."""
import fcntl
import hashlib
import http.client
import os
import shutil
import stat
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from . import release as r, release_verify as v


class PreparationError(Exception):
    pass


class Busy(Exception):
    pass


class SecureRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 4

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        r.https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@contextmanager
def remote(url):
    r.https(url)
    try:
        with urllib.request.build_opener(SecureRedirect()).open(url, timeout=30) as response:
            r.https(response.geturl())
            yield response
    except urllib.error.HTTPError as exc:
        exc.close()
        raise PreparationError('HTTPS transfer failed (HTTP %d)' % exc.code) from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as exc:
        raise PreparationError('HTTPS transfer failed') from exc


def metadata(url, limit):
    with remote(url) as stream:
        data = stream.read(limit + 1)
    r.need(0 < len(data) <= limit, 'metadata size limit')
    return data


@contextmanager
def workspace(root):
    # Provisioned alongside the trust store, before discovery. Never accept a
    # lock file supplied by the bundle. Lock the private directory inode itself.
    if not root.is_dir():
        raise PreparationError('release trust store is not configured')
    fd = os.open(root, os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        r.need(st.st_uid == os.geteuid() and stat.S_IMODE(st.st_mode) == 0o700,
               'update state directory must be private (0700)')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy() from None
        stage = root / 'check-stage'
        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir(mode=0o700)
        try:
            yield stage
        finally:
            shutil.rmtree(stage)
    finally:
        os.close(fd)


def persist(root, state):
    with tempfile.NamedTemporaryFile(dir=root, delete=False) as out:
        name = out.name
        try:
            out.write(r.canonical(state))
            out.flush()
            os.fsync(out.fileno())
        except BaseException:
            os.unlink(name)
            raise
    try:
        os.replace(name, root / 'accepted.json')
        directory = os.open(root, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def floor(record, previous):
    if previous is None:
        return
    r.fields(previous, 'sequence version manifest_sha256')
    before = r.integer(previous['sequence'], 1)
    version = r.version(previous['version'])
    r.digest(previous['manifest_sha256'])
    if record['sequence'] == before:
        r.need(record == previous, 'release sequence reused')
    else:
        r.need(record['sequence'] > before and r.version(record['version']) > version,
               'release rollback or version reused')


def discover(root, bundle, *, expected_manifest=None):
    trust = root / 'trust'
    key = trust / 'allowed_signers'
    if not os.path.lexists(key):
        raise PreparationError('release signing key is not configured')
    revoked = trust / 'revoked'
    revoked = revoked if os.path.lexists(revoked) else None
    cache = root / 'accepted.json'
    state = {'channel': None, 'release': None}
    if os.path.lexists(cache):
        state = r.parse(v.bounded_read(cache, r.MAX_CHANNEL, trusted=True))
        r.fields(state, 'channel release')
        r.need(state['release'] is not None, 'invalid security cache')
    channel = state['channel']
    if bundle is None:
        endpoint = trust / 'stable-url'
        if not os.path.lexists(endpoint):
            raise PreparationError('stable channel URL is not configured')
        url = r.https(v.bounded_read(endpoint, 8192, trusted=True).decode().strip())
        raw = metadata(url, r.MAX_CHANNEL)
        sig = metadata(url + '.sig', v.MAX_SIGNATURE)
        v.verify_signature(raw, sig, key, 'ragspark-channel-v1', revoked)
        pointer, channel = r.channel(raw, time.time(), channel)
        # A verified pointer already establishes the floor, even if fetching
        # its release subsequently loses the connection.
        announced = {'sequence': pointer['release_sequence'], 'version': pointer['version'],
                     'manifest_sha256': pointer['manifest_sha256']}
        floor(announced, state['release'])
        persist(root, {'channel': channel, 'release': announced})
        raw = metadata(pointer['manifest_url'], pointer['manifest_bytes'])
        sig = metadata(pointer['signature_url'], v.MAX_SIGNATURE)
    else:
        raw = v.bounded_read(bundle / 'release.json', r.MAX_MANIFEST)
        sig = v.bounded_read(bundle / 'release.json.sig', v.MAX_SIGNATURE)
    v.verify_signature(raw, sig, key, 'ragspark-release-v1', revoked)
    release = r.manifest(raw)
    if bundle is None:
        r.bind_channel(pointer, raw, release)
    identity = {'sequence': release['sequence'], 'version': release['version'],
                'manifest_sha256': hashlib.sha256(raw).hexdigest()}
    if expected_manifest is not None:
        r.need(identity['manifest_sha256'] == r.digest(expected_manifest), 'pair release digest differs')
    floor(identity, state['release'])
    if bundle is not None and state['release'] != identity:
        persist(root, {'channel': channel, 'release': identity})
    return release, identity, key, revoked, (raw, sig)


def stage_artifact(stage, bundle, artifact):
    name = artifact['path']
    dest = stage / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    sources = [None] if bundle is not None else artifact['urls']
    r.need(bool(sources), 'artifact has no download URL: ' + artifact['id'])
    for idx, url in enumerate(sources):
        source = v.regular_file(bundle / name) if bundle is not None else remote(url)
        try:
            with source as src, dest.open('wb') as out:
                remaining = artifact['bytes']
                while remaining:
                    part = src.read(min(v.CHUNK, remaining))
                    r.need(bool(part), 'truncated artifact: ' + artifact['id'])
                    out.write(part)
                    remaining -= len(part)
                r.need(not src.read(1), 'oversized artifact: ' + artifact['id'])
            break
        except PreparationError:
            if idx + 1 == len(sources):
                raise
    v.verify_artifact(stage, artifact)
    return dest
