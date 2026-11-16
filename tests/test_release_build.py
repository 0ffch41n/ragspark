"""Publisher commands on disposable Git objects and an installed-tree fixture."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'build/release'))
import package
from rsinst import release as r, release_inventory, release_verify

TOOL = ROOT / 'build/release/package.py'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def run(*args, cwd=None, ok=True):
    p = subprocess.run(args, cwd=cwd, capture_output=True)
    if ok and p.returncode:
        raise AssertionError(p.stderr.decode())
    return p


def write_json(path, data):
    path.write_bytes(r.canonical(data))
    return path


class Code(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.temp = Path(t.name)
        self.repo = self.temp / 'repo'
        run('git', 'init', '-q', str(self.repo))
        (self.repo / 'tools').mkdir()
        self.file = self.repo / 'tools/start.py'
        self.file.write_bytes(b'fixture\n')
        self.file.chmod(0o755)
        self.commit = self.record()

    def record(self):
        run('git', 'add', '-A', cwd=self.repo)
        run('git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@invalid', '-c', 'commit.gpgsign=false',
            'commit', '-qm', 'test fixture', cwd=self.repo)
        return run('git', 'rev-parse', 'HEAD', cwd=self.repo).stdout.decode().strip()

    def test_git_objects_reproduce_archive(self):
        blobs = []
        for name in ('first', 'second'):
            files = package.snapshot(self.repo, self.commit, self.temp / name)
            out = self.temp / (name + '-out')
            out.mkdir()
            desc = package.pack(self.temp / name, files, out)
            release_verify.verify_artifact(out, desc)
            blobs.append((desc, (out / desc['path']).read_bytes()))
            self.file.write_text('local edits')
            (self.repo / 'tools/.env').write_text('private fixture')
            self.file.chmod(0o644)
        self.assertEqual(blobs[0], blobs[1])
        self.assertEqual(blobs[0][0]['files'], [
            {'path': 'tools/start.py', 'bytes': 8, 'mode': '0755', 'sha256': sha(b'fixture\n')}])

    def test_tracked_private_file_or_symlink_rejected(self):
        for i, path in enumerate(['credentials.txt', 'secrets', '.env', 'link']):
            target = self.repo / 'tools' / path
            if path == 'link':
                target.symlink_to('../unrelated')
            else:
                target.write_bytes(b'test only')
            commit = self.record()
            with self.subTest(path=path), self.assertRaises(r.InvalidRelease):
                package.snapshot(self.repo, commit, self.temp / str(i))
            target.unlink()

    def test_committed_baseline_and_git_free_compare(self):
        ref = json.loads((ROOT/'build/release/baselines/e09da62.json').read_bytes())
        self.assertEqual(release_inventory.inventory_sha(ref['files']), ref['source']['inventory_sha256'])
        # Build a reference using source objects, then take the Git repo away.
        reference = run(sys.executable, TOOL, 'baseline', '--repo', self.repo, '--commit', self.commit, '--files').stdout
        base = json.loads(reference)
        filename = self.temp/'reference.json'
        filename.write_bytes(reference)
        installed = self.temp/'installed'
        package.snapshot(self.repo, self.commit, installed)
        (installed/'tools/start.py').chmod(0o755)
        (installed/'VERSION').write_text(self.commit[:7])
        write_json(installed/'manifest.json', {'version':self.commit[:7]})
        shutil.rmtree(self.repo)
        args = [sys.executable, TOOL, 'compare', '--baseline', filename, '--installed', installed,
                '--commit', self.commit, '--inventory-sha256', base['source']['inventory_sha256']]
        self.assertTrue(json.loads(run(*args).stdout)['matches'])
        (installed/'VERSION').write_text('invalid')
        self.assertEqual(run(*args, ok=False).returncode, 3)
        base['files'][0]['mode'] = '0644'
        write_json(filename, base)
        self.assertEqual(run(*args, ok=False).returncode, 3)

    def test_installed_drift_and_marker_mismatch(self):
        installed = self.temp / 'installed'
        files = package.snapshot(self.repo, self.commit, installed)
        f = installed / 'tools/start.py'
        f.chmod(0o755)
        source = release_inventory.legacy_baseline(self.commit, files)
        (installed / 'VERSION').write_text(self.commit[:7] + '\n')
        write_json(installed / 'manifest.json', {'version': self.commit[:7]})
        self.assertTrue(release_inventory.compare(installed, files, source)['matches'])
        # Runtime data outside the managed code roots is intentionally out of scope.
        (installed/'backup.lock').write_text('local')
        (installed/'tools/__pycache__').mkdir()
        self.assertTrue(release_inventory.compare(installed, files, source)['matches'])
        with self.assertRaises(r.InvalidRelease):
            release_inventory.compare(installed, files, dict(source, inventory_sha256='0' * 64))
        f.write_bytes(b'changed\n')
        (installed / 'tools/extra').symlink_to('/nonexistent')
        write_json(installed / 'manifest.json', {'version': 'stale', 'password': 'fixture-only'})
        report = release_inventory.compare(installed, files, source)
        self.assertEqual(report['differences'], {'tools/start.py': 'sha256', 'tools/extra': 'unexpected',
                                               'manifest.json': 'version-mismatch'})
        self.assertNotIn('fixture-only', str(report))
        f.chmod(0o644)
        self.assertEqual(release_inventory.compare(installed, files, source)['differences']['tools/start.py'], 'mode')
        f.unlink()
        self.assertEqual(release_inventory.compare(installed, files, source)['differences']['tools/start.py'], 'missing')
        f.symlink_to(installed / 'manifest.json')
        self.assertEqual(release_inventory.compare(installed, files, source)['differences']['tools/start.py'], 'type')


class ComposeBuild(unittest.TestCase):
    """Real Compose config; no Docker daemon calls. Dependency bytes below are synthetic."""
    def setUp(self):
        if not shutil.which('docker') and not os.environ.get('RAGSPARK_TEST_COMPOSE'):
            self.skipTest('requires Docker Compose')
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.tmp = Path(t.name)
        self.commit = run('git', 'rev-parse', 'HEAD', cwd=ROOT).stdout.decode().strip()
        self.base = [sys.executable, str(TOOL)]
        self.opts = ['--repo', str(ROOT), '--commit', self.commit]
        self.compose = (['--compose-bin', os.environ['RAGSPARK_TEST_COMPOSE']]
                        if os.environ.get('RAGSPARK_TEST_COMPOSE') else [])

    def pins(self, model, mode):
        return json.loads(run(*self.base, 'pins', *self.opts, *self.compose, '--mode', mode, '--llm', model).stdout)

    def test_real_catalog_matrix(self):
        for model in ('qwen-3.8-27b-nvfp4', 'qwen-3.6-35b-a3b-nvfp4', 'deepseek-v4-flash-vision'):
            mode = 'stack' if model.startswith('deepseek') else 'single'
            roles = self.pins(model, mode)
            self.assertEqual(len(roles['head']['images']), 9 if mode == 'stack' else 8)
            self.assertTrue(roles['head']['images']['mysql'].startswith('docker.io/library/mysql:'))
            for role in roles.values():
                for pin in role['models'].values(): r.commit(pin['revision'])
            if mode == 'stack':
                self.assertEqual(len(roles['node']['images']), 3)
                self.assertEqual(set(roles['head']['models']), {'llm'})
                self.assertEqual(roles['node']['models']['llm'], roles['head']['models']['llm'])
            else:
                self.assertEqual(set(roles['head']['models']), {'llm','embedding','rerank'})

    def test_catalog_and_model_revision_conflicts(self):
        from profiles import resolve
        tree = self.tmp / 'snapshot'
        package.snapshot(ROOT, self.commit, tree)
        catalog = tree / 'catalog/models.yaml'
        original = catalog.read_text()
        llm = 'qwen-3.8-27b-nvfp4'
        cmd = [self.compose[-1]] if self.compose else ['docker', 'compose']
        catalog.write_text(original.replace('hf_repo: unsloth/Qwen3.8-27B-NVFP4',
                                            'hf_repo: fixture/unrelated'))
        with self.assertRaisesRegex(r.InvalidRelease, 'differs from catalog'):
            resolve(tree, llm, 'single', cmd)
        catalog.write_text(original)
        overlay = tree / ('compose/llm/' + llm + '.yaml')
        overlay.write_text(overlay.read_text().replace('--revision', '--unused-flag'))
        with self.assertRaisesRegex(r.InvalidRelease, 'expected one pinned model'):
            resolve(tree, llm, 'single', cmd)

    def dependencies(self, role):
        # Model fixture tar carries real test bytes; registry refs merely label fake image bytes.
        deps = []
        store = self.tmp / 'store'
        (store / 'payload').mkdir(parents=True)
        for i, ref in enumerate(sorted(set(role['images'].values()))):
            name = 'image-' + str(i)
            data = name.encode()
            path = 'payload/' + name + '.tar'
            (store / path).write_bytes(data)
            deps.append(dict(id=name, kind='image', path=path, bytes=len(data), sha256=sha(data),
                             expanded_bytes=len(data), urls=[], image_ref=ref, platform='linux/arm64',
                             config_digest='sha256:' + sha(data)))
        for name, pin in role['models'].items():
            data = b'123'
            item = tarfile.TarInfo('config.json')
            item.size = len(data)
            item.mode = 0o644
            path = 'payload/' + name + '.tar'
            with tarfile.open(store / path, 'w', format=tarfile.USTAR_FORMAT) as t:
                t.addfile(item, io.BytesIO(data))
            raw = (store / path).read_bytes()
            deps.append(dict(id=name, kind='model', path=path, bytes=len(raw), sha256=sha(raw),
                             expanded_bytes=len(data), urls=[], **pin,
                             files=[dict(path='config.json', bytes=len(data), sha256=sha(data), mode='0644')]))
        return deps, store

    def test_publisher_roundtrip_and_reproducibility(self):
        llm = 'qwen-3.8-27b-nvfp4'
        pins = self.pins(llm, 'single')
        deps, store = self.dependencies(pins['head'])
        # An irrelevant artifact need not be downloaded or present in the store.
        other = dict(deps[-1], repo='fixture/unselected', id='unused', path='payload/unused.tar')
        deps.append(other)
        desc = write_json(self.tmp / 'deps.json', deps)
        baseline = json.loads(run(*self.base, 'baseline', *self.opts).stdout)
        requirement = dict(python_min=[3,10,0], docker_min=[24,0,0], compose_min=[2,24,0],
                           driver_min=[580,0,0], min_total_ram_bytes=1)
        recipe = dict(version='0.1.0', sequence=1, created_at='2026-01-01T00:00:00Z', code_urls=[],
                      profiles=[dict(id='single-test', mode='single', llm=llm, requirements={'head':requirement})],
                      transitions=[dict(source=baseline, profile='single-test', impact='code', restart={'head':[]},
                                        backup='none', migration='none', rollback='compatible')])
        recipe_file = write_json(self.tmp / 'recipe.json', recipe)
        args = [*self.base, 'build', *self.opts, *self.compose, '--recipe', recipe_file, '--dependencies', desc,
                '--store', store, '--output']
        a, b = self.tmp / 'a', self.tmp / 'b'
        for out in (a,b): run(*args, out)
        for path in ('release.json', 'payload/code.tar.gz'):
            self.assertEqual((a/path).read_bytes(), (b/path).read_bytes())
        manifest = r.manifest((a/'release.json').read_bytes())
        self.assertNotIn('unused', {x['id'] for x in manifest['artifacts']})
        self.assertEqual(manifest['profiles'][0]['llm'], llm)
        key = self.tmp / 'test-key'
        run('ssh-keygen','-q','-t','ed25519','-N','','-f',str(key))
        trust = self.tmp / 'allowed'
        trust.write_text('ragspark-release ' + Path(str(key)+'.pub').read_text())
        trust.chmod(0o600)
        run('ssh-keygen','-Y','sign','-f',str(key),'-n','ragspark-release-v1',str(a/'release.json'))
        self.assertEqual(release_verify.load_manifest(a/'release.json', a/'release.json.sig', trust), manifest)
        self.assertNotEqual(run(*args,a,ok=False).returncode, 0)
        (store/deps[0]['path']).write_bytes(b'corrupt')
        bad = self.tmp / 'bad'
        self.assertNotEqual(run(*args,bad,ok=False).returncode, 0)
        self.assertFalse(bad.exists())
        deps.pop(0)
        write_json(desc, deps)
        self.assertNotEqual(run(*args,bad,ok=False).returncode, 0)
        self.assertFalse(bad.exists())
