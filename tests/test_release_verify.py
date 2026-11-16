"""Real OpenSSH signatures and hostile small tar fixtures in private temp dirs."""
import copy
import gzip
import io
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

from release_fixtures import NOW, file_record, pointer, sample, sha
from rsinst import release as r, release_verify as v


def command(args):
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class SignedInput(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.isfile(v.SSH_KEYGEN): raise unittest.SkipTest('OpenSSH ssh-keygen needed')
        cls.temp = tempfile.TemporaryDirectory(prefix='ragspark-u21-test-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        for name in ('publisher', 'other'):
            command([v.SSH_KEYGEN, '-q', '-t', 'ed25519', '-N', '', '-C', 'TEST-ONLY', '-f', str(cls.root / name)])
        pub = (cls.root / 'publisher.pub').read_text().split()
        cls.public = ' '.join(pub[:2])
        cls.trust = cls.root / 'allowed'
        cls.trust.write_text('ragspark-release namespaces="ragspark-release-v1,ragspark-channel-v1" ' + cls.public + '\n')
        cls.trust.chmod(0o600)

    def signed(self, raw, namespace='ragspark-release-v1', key='publisher'):
        folder = Path(tempfile.mkdtemp(dir=self.root))
        self.addCleanup(shutil.rmtree, folder)
        filename = folder / 'metadata.json'; filename.write_bytes(raw)
        command([v.SSH_KEYGEN, '-Y', 'sign', '-f', str(self.root / key), '-n', namespace, str(filename)])
        return filename, Path(str(filename) + '.sig')

    def test_valid_manifest_and_channel(self):
        raw = r.canonical(sample()); filename, sig = self.signed(raw)
        self.assertEqual(v.load_manifest(filename, sig, self.trust), sample())
        p = pointer(raw); channel, csig = self.signed(r.canonical(p), 'ragspark-channel-v1')
        checked, state = v.load_channel(channel, csig, self.trust, NOW)
        self.assertEqual(checked, p)
        self.assertEqual(v.load_manifest(filename, sig, self.trust, pointer=checked), sample())
        self.assertEqual(state['sha256'], sha(channel.read_bytes()))

    def test_tampering_rejected_before_json_parsing(self):
        filename, sig = self.signed(r.canonical(sample()))
        filename.write_bytes(filename.read_bytes() + b' ')
        with mock.patch.object(r, 'manifest') as parse:
            with self.assertRaises(r.InvalidRelease): v.load_manifest(filename, sig, self.trust)
            parse.assert_not_called()

    def test_wrong_key_namespace_and_identity(self):
        raw = r.canonical(sample())
        for key, ns in [('other', 'ragspark-release-v1'), ('publisher', 'ragspark-channel-v1')]:
            filename, sig = self.signed(raw, ns, key)
            with self.subTest(key=key, ns=ns), self.assertRaises(r.InvalidRelease): v.load_manifest(filename, sig, self.trust)
        trust = self.root / 'different-identity'; trust.write_text('someone-else ' + self.public + '\n'); trust.chmod(0o600)
        filename, sig = self.signed(raw)
        with self.assertRaises(r.InvalidRelease): v.load_manifest(filename, sig, trust)

    def test_revocation_is_enforced(self):
        revoked = self.root / 'revoked'; revoked.write_text(self.public + '\n'); revoked.chmod(0o600)
        filename, sig = self.signed(r.canonical(sample()))
        with self.assertRaises(r.InvalidRelease): v.load_manifest(filename, sig, self.trust, revoked)

    def test_signed_invalid_json_and_semantics_rejected(self):
        for raw in (b'{"format":1,"format":1}', b'{"format":NaN}', b'{', b'{}', r.canonical(dict(sample(), pair_protocol=2))):
            filename, sig = self.signed(raw)
            with self.subTest(raw=raw[:20]), self.assertRaises(r.InvalidRelease): v.load_manifest(filename, sig, self.trust)

    def test_signature_size_and_armor(self):
        raw = r.canonical(sample())
        for sig in (b'x', b'x' * (v.MAX_SIGNATURE + 1), b'-----BEGIN SSH SIGNATURE-----\n!\n-----END SSH SIGNATURE-----'):
            with self.subTest(size=len(sig)), self.assertRaises(r.InvalidRelease):
                v.verify_signature(raw, sig, self.trust, 'ragspark-release-v1')

    def test_non_ed25519_signature_rejected(self):
        key = self.root / 'rsa'
        command([v.SSH_KEYGEN, '-q', '-t', 'rsa', '-b', '2048', '-N', '', '-f', str(key)])
        filename, sig = self.signed(r.canonical(sample()), key='rsa')
        trust = self.root / 'rsa-allowed'; trust.write_text('ragspark-release ' + (self.root / 'rsa.pub').read_text()); trust.chmod(0o600)
        with self.assertRaises(r.InvalidRelease): v.load_manifest(filename, sig, trust)

    def test_verifier_failure_and_timeout_fail_closed(self):
        raw = r.canonical(sample()); _, sig = self.signed(raw)
        for error in (FileNotFoundError(), subprocess.TimeoutExpired('ssh-keygen', 15)):
            with self.subTest(error=type(error).__name__), mock.patch.object(v.subprocess, 'run', side_effect=error):
                with self.assertRaises(r.InvalidRelease): v.verify_signature(raw, sig.read_bytes(), self.trust, 'ragspark-release-v1')

    def test_insecure_trust_source_rejected(self):
        filename, sig = self.signed(r.canonical(sample()))
        unsafe = self.root / 'unsafe'; unsafe.write_bytes(self.trust.read_bytes()); unsafe.chmod(0o666)
        with self.assertRaises(r.InvalidRelease): v.load_manifest(filename, sig, unsafe)
        symlink = self.root / 'trust-link'; symlink.symlink_to(self.trust)
        with self.assertRaises(r.InvalidRelease): v.load_manifest(filename, sig, symlink)
        # A writable non-sticky trust ancestor is rejected even if its file is safe.
        parent = self.root / 'shared'; parent.mkdir(); bad = parent / 'allowed'; bad.write_bytes(self.trust.read_bytes()); bad.chmod(0o600)
        parent.chmod(0o777)
        with self.assertRaises(r.InvalidRelease): v.load_manifest(filename, sig, bad)
        parent.chmod(0o700)

    def test_missing_revocation_file_fails_closed(self):
        filename, sig = self.signed(r.canonical(sample()))
        with self.assertRaises(r.InvalidRelease): v.load_manifest(filename, sig, self.trust, self.root / 'missing-revocations')


class FileInputs(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_size_limits(self):
        f = self.root / 'data'; f.write_bytes(b'ab')
        self.assertEqual(v.bounded_read(f, 2), b'ab')
        with self.assertRaises(r.InvalidRelease): v.bounded_read(f, 1)
        f.write_bytes(b'')
        with self.assertRaises(r.InvalidRelease): v.bounded_read(f, 10)

    def test_symlink_hardlink_directory_and_fifo_rejected(self):
        regular = self.root / 'plain'; regular.write_bytes(b'x')
        sym = self.root / 'symlink'; sym.symlink_to(regular)
        directory = self.root / 'directory'; directory.mkdir()
        directory_symlink = self.root / 'dirlink'; directory_symlink.symlink_to(directory)
        (directory / 'file').write_bytes(b'x')
        fifo = self.root / 'fifo'; os.mkfifo(fifo)
        for p in (sym, directory, fifo, directory_symlink / 'file'):
            with self.subTest(p=p.name), self.assertRaises(r.InvalidRelease): v.bounded_read(p, 10)
        hard = self.root / 'hardlink'; os.link(regular, hard)
        with self.assertRaises(r.InvalidRelease): v.bounded_read(hard, 10)

    def test_trust_owner_check(self):
        f = self.root / 'trust'; f.write_bytes(b'x'); f.chmod(0o600)
        with mock.patch.object(v.os, 'geteuid', return_value=os.getuid() + 123):
            with self.assertRaises(r.InvalidRelease): v.bounded_read(f, 10, trusted=True)


class Archives(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); (self.root / 'payload').mkdir()
        self.a = copy.deepcopy(sample()['artifacts'][0])

    def tar(self, members, format=tarfile.USTAR_FORMAT):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w', format=format) as t:
            for item, data in members: t.addfile(item, io.BytesIO(data))
        return buffer.getvalue()

    def member(self, name='installer/example.py', data=b'pass\n', **attrs):
        m = tarfile.TarInfo(name); m.size = len(data); m.mode = 0o644
        for k, value in attrs.items(): setattr(m, k, value)
        return m, data

    def write(self, raw, kind='code'):
        a = copy.deepcopy(self.a)
        if kind == 'model':
            a.update(kind='model', repo='fixture/model', revision='f' * 40)
        payload = gzip.compress(raw, mtime=0) if kind == 'code' else raw
        a.update(bytes=len(payload), sha256=sha(payload))
        filename = self.root / a['path']; filename.write_bytes(payload)
        return a, filename

    def reject(self, raw, kind='code'):
        a, _ = self.write(raw, kind)
        with self.assertRaises(r.InvalidRelease): v.verify_artifact(self.root, a)

    def test_regular_inventory_with_directories_and_empty_file(self):
        self.a['files'].append(file_record('installer/empty.py', b''))
        parent = self.member('installer', b'', type=tarfile.DIRTYPE, mode=0o755)
        raw = self.tar([parent, self.member(), self.member('installer/empty.py', b'')])
        a, _ = self.write(raw)
        before = set(self.root.rglob('*'))
        result = v.verify_artifact(self.root, a)
        self.assertEqual(result['files'], 2); self.assertEqual(result['inspection'], 'file-inventory')
        self.assertEqual(set(self.root.rglob('*')), before)  # no extraction

    def test_model_ustar_prefix_and_unicode(self):
        name = 'long/' * 24 + 'веса.bin'
        self.a['files'] = [file_record(name)]
        a, _ = self.write(self.tar([self.member(name)]), 'model')
        self.assertEqual(v.verify_artifact(self.root, a)['files'], 1)

    def test_archive_digest_length_and_internal_digest(self):
        raw = self.tar([self.member()]); a, file = self.write(raw)
        for delta in ({'bytes': a['bytes'] + 1}, {'sha256': '0' * 64}):
            with self.subTest(delta=delta), self.assertRaises(r.InvalidRelease): v.verify_artifact(self.root, dict(a, **delta))
        self.reject(self.tar([self.member(data=b'fail\n')]))
        self.reject(self.tar([self.member(data=b'larger\n')]))

    def test_path_escape_and_duplicates(self):
        for name in ('../escape', '/tmp/escape', 'installer/../escape', './installer/example.py', 'installer//example.py'):
            with self.subTest(name=name): self.reject(self.tar([self.member(name)]))
        self.reject(self.tar([self.member(), self.member()]))
        self.reject(self.tar([self.member('installer//', b'', type=tarfile.DIRTYPE, mode=0o755), self.member()]))

    def test_links_special_files_and_permissions(self):
        for typ in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.GNUTYPE_SPARSE):
            with self.subTest(typ=typ): self.reject(self.tar([self.member(type=typ, linkname='/etc/passwd')]))
        for attrs in ({'mode': 0o4755}, {'mode': 0o666}, {'uid': 123}, {'gid': 123}):
            with self.subTest(attrs=attrs): self.reject(self.tar([self.member(**attrs)]))

    def test_unknown_missing_and_directory_collisions(self):
        self.reject(self.tar([]))
        self.reject(self.tar([self.member('installer/extra.py')]))
        self.reject(self.tar([self.member('installer/example.py', b'', type=tarfile.DIRTYPE, mode=0o755)]))
        self.reject(self.tar([self.member('other', b'', type=tarfile.DIRTYPE, mode=0o755), self.member()]))

    def test_extensions_rejected_before_reading_payload(self):
        # A hostile PAX size must not trigger tarfile's allocation/extension parsing.
        for typ in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK):
            m = tarfile.TarInfo('extension'); m.type = typ; m.size = 1 << 30
            with self.subTest(typ=typ): self.reject(m.tobuf(format=tarfile.USTAR_FORMAT))

    def test_truncation_checksum_and_trailing_data(self):
        raw = self.tar([self.member()])
        self.reject(raw[:511]); self.reject(raw[:514])
        damaged = bytearray(raw); damaged[0] ^= 1; self.reject(bytes(damaged))
        self.reject(raw + b'not padding')
        self.reject(raw + self.tar([self.member('installer/hidden.py')]))
        self.reject(raw + bytes(20480))
        pad = bytearray(raw); pad[512 + 5] = 1; self.reject(bytes(pad))

    def test_bad_compression_even_with_matching_outer_hash(self):
        a, file = self.write(self.tar([self.member()]))
        for payload in (b'not gzip', file.read_bytes()[:-5], file.read_bytes()[:20] + b'x' * 40):
            file.write_bytes(payload); bad = dict(a, bytes=len(payload), sha256=sha(payload))
            with self.subTest(length=len(payload)), self.assertRaises(r.InvalidRelease): v.verify_artifact(self.root, bad)

    def test_hash_only_image_is_not_reported_as_content_validated(self):
        a = copy.deepcopy(sample()['artifacts'][1]); a['path'] = 'payload/image.tar'
        data = b'fixture image bytes, not an imported image'
        a.update(bytes=len(data), sha256=sha(data)); (self.root / a['path']).write_bytes(data)
        result = v.verify_artifact(self.root, a)
        self.assertIsNone(result['files']); self.assertEqual(result['inspection'], 'archive-bytes-only')

    def test_artifact_parent_symlink_rejected(self):
        outside = self.root / 'elsewhere'; outside.mkdir()
        (self.root / 'payload' / 'link').symlink_to(outside)
        a = copy.deepcopy(self.a); a['path'] = 'payload/link/code.tar.gz'
        data = gzip.compress(self.tar([self.member()]), mtime=0)
        (outside / 'code.tar.gz').write_bytes(data); a.update(bytes=len(data), sha256=sha(data))
        with self.assertRaises(r.InvalidRelease): v.verify_artifact(self.root, a)


if __name__ == '__main__': unittest.main()
