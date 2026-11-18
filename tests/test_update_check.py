"""Update planning on disposable installations and a loopback publisher."""
from contextlib import redirect_stdout, redirect_stderr
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from release_fixtures import NOW, file_record, pointer, sample, sha
from rsinst import cli, paths, release as r, release_inventory, stackmode, ui, update_check as check
from rsinst import update_bootstrap, update_transaction as txn
from rsinst.install import CLI_SCRIPT
from maintenance import main as gateway
from rsinst import update_host as host, update_source as source

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'build/release'))
from source import pack


def command(*args):
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class UpdateCheck(unittest.TestCase):
    def setUp(self):
        prior = os.umask(0o077)
        self.addCleanup(os.umask, prior)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prefix = Path(self.tmp.name)
        self.old = self.prefix / 'opt/ragspark'
        self.state = self.prefix / 'var/lib/ragspark-update'
        self.trust = self.state / 'trust'
        self.trust.mkdir(parents=True)
        self.state.chmod(0o700)
        self.bundle = self.prefix / 'bundle'
        self.bundle.mkdir()
        self.key = self.prefix / 'test-key'
        command('ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', self.key)
        (self.trust / 'allowed_signers').write_text('ragspark-release ' + self.key.with_suffix('.pub').read_text())
        (self.trust / 'allowed_signers').chmod(0o600)
        self.release = sample()
        self.code, self.image, self.model = self.release['artifacts']
        service = {'image': self.image['image_ref'], 'command': [
            'vllm', 'serve', self.model['repo'], '--revision', self.model['revision']]}
        self.services = {s: service.copy() for s in ('vllm-llm', 'vllm-embed', 'vllm-rerank')}
        self.compose = {'name': 'ragspark', 'services': self.services}
        role = self.release['profiles'][0]['roles']['head']
        role.update(images={k: 'image' for k in self.services},
                    models={k: 'weights' for k in ['llm', 'embedding', 'rerank']})
        self.config = {'version': 'abc0123', 'schema': 1, 'status': 'installed',
                       'config': {'llm': 'fixture-llm', 'lang': 'ru'}}
        data = {'compose/compose.yaml': json.dumps(self.compose).encode(),
                'compose/llm/fixture-llm.yaml': b'services: {}\n',
                'installer/ragspark.py': b'print("fixture CLI")\n', 'README.md': b'old'}
        self.files = []
        for name, content in data.items():
            file = self.old / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(content)
            file.chmod(0o644)
            self.files.append(file_record(name, content))
        self.current = release_inventory.legacy_baseline('abc0123' + 'e'*33, self.files)
        (self.trust / 'baseline.json').write_bytes(r.canonical({'source': self.current, 'files': self.files}))
        (self.trust / 'baseline.json').chmod(0o600)
        (self.old / 'compose/.env').write_text('COMPOSE_FILE=compose.yaml:llm/fixture-llm.yaml\n')
        (self.old / 'VERSION').write_text('abc0123\n')
        (self.old / 'manifest.json').write_bytes(r.canonical(self.config))
        candidate = self.prefix / 'candidate'
        shutil.copytree(self.old, candidate)
        (candidate / 'README.md').write_text('new')
        records = self.files[:-1] + [file_record('README.md', b'new')]
        self.code.update(pack(candidate, records, self.bundle))
        self.release['transitions'][0]['source'] = self.current
        self.sign_release()
        self.patch(paths, 'PREFIX', self.tmp.name)
        self.patch(host, 'hardware', mock.Mock(return_value=3 << 30))
        self.patch(host, 'installed_image', mock.Mock(return_value=True))
        self.patch(host, 'installed_model', mock.Mock(return_value=True))
        self.output = self.patch(host, 'output', mock.Mock(side_effect=self.compose_command))
        self.prev_lang = ui.lang()
        self.addCleanup(ui.set_lang, self.prev_lang)

    def patch(self, obj, name, value):
        patcher = mock.patch.object(obj, name, value)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def sign(self, folder, name, raw, namespace):
        path = folder / name
        path.write_bytes(raw)
        path.with_suffix(path.suffix + '.sig').unlink(missing_ok=True)
        command('ssh-keygen', '-Y', 'sign', '-f', self.key, '-n', namespace, path)

    def sign_release(self):
        self.sign(self.bundle, 'release.json', r.canonical(self.release), 'ragspark-release-v1')

    def compose_command(self, args, cwd):
        self.assertEqual(args[:2], ['docker', 'compose'])
        self.assertIn('config', args)
        if '--environment' in args:
            return (cwd / '.env').read_bytes()
        return (cwd / 'compose.yaml').read_bytes()

    def run_cli(self, local=True, lang='ru'):
        out = io.StringIO()
        args = ['update', '--check', '--lang', lang]
        if local:
            args += ['--from', str(self.bundle)]
        with mock.patch.dict(os.environ, {'RAGSPARK_ALLOW_NONROOT': '1'}), redirect_stdout(out):
            status = cli.main(args)
        self.assertFalse((self.state / 'check-stage').exists())
        return status, out.getvalue()

    def test_offline_verified_reuse_and_no_installation_changes(self):
        def snapshot():
            return {str(f): (f.read_bytes(), f.stat().st_mtime_ns)
                    for f in self.old.rglob('*') if f.is_file()}
        before = snapshot()
        with mock.patch.object(source, 'remote', side_effect=AssertionError('offline network')):
            status, msg = self.run_cli()
        self.assertEqual(status, 0, msg)
        self.assertIn('abc0123 → выпуск 0.2.0', msg)
        self.assertIn('image, weights', msg)
        self.assertIn('Пиковое', msg)
        self.assertEqual(before, snapshot())
        self.assertEqual((self.state / 'accepted.json').stat().st_mode & 0o777, 0o600)
        status, msg = self.run_cli(lang='en')
        self.assertIn('Installed abc0123', msg)
        self.assertEqual(status, 0)

    @unittest.skipUnless(shutil.which('docker'), 'requires Compose plugin')
    def test_real_compose_plan(self):
        with mock.patch.object(host, 'output', REAL_OUTPUT):
            status, msg = self.run_cli()
        self.assertEqual(status, 0, msg)

    def test_missing_local_artifact_and_corrupt_archive(self):
        host.installed_model.return_value = False
        self.assertEqual(self.run_cli()[0], 3)
        path = self.bundle / self.code['path']
        path.write_bytes(b'x' * self.code['bytes'])
        self.assertIn('digest mismatch', self.run_cli()[1])

    def test_bad_signature_and_unconfigured_channel(self):
        (self.bundle / 'release.json.sig').write_bytes(b'bad')
        self.assertEqual(self.run_cli()[0], 3)
        self.assertFalse((self.state / 'accepted.json').exists())
        self.assertEqual(self.run_cli(local=False)[0], 5)

    def test_offline_floor_and_same_sequence_substitution(self):
        self.assertEqual(self.run_cli()[0], 0)
        for seq in (1, 2):
            self.release.update(sequence=seq, version='0.1.1')
            self.sign_release()
            self.assertEqual(self.run_cli()[0], 3)

    def test_installed_signed_release_and_identity_conflict(self):
        self.sign(self.state, 'installed-release.json', r.canonical(self.release), 'ragspark-release-v1')
        (self.state / 'installed-release.json').chmod(0o600)
        (self.state / 'installed-release.json.sig').chmod(0o600)
        (self.old / 'README.md').write_text('new')
        (self.old / 'VERSION').write_text('0.2.0')
        self.config['version'] = '0.2.0'
        (self.old / 'manifest.json').write_bytes(r.canonical(self.config))
        status, msg = self.run_cli()
        self.assertEqual(status, 0, msg)
        self.assertIn('уже установлен', msg)
        self.release['source_commit'] = 'f'*40
        self.sign_release()
        self.assertEqual(self.run_cli()[0], 3)

    def test_drift_unknown_transition_node_and_busy(self):
        (self.old / 'README.md').write_text('changed')
        self.assertIn('README.md', self.run_cli()[1])
        (self.old / 'README.md').write_text('old')
        self.release.update(sequence=3, version='0.3.0')
        self.release['transitions'][0]['source'] = dict(self.current, legacy_commit='1'*40)
        self.sign_release()
        self.assertIn('transition', self.run_cli()[1])
        with source.workspace(self.state):
            with self.assertRaises(source.Busy), source.workspace(self.state):
                pass
        self.config['config']['role'] = 'node'
        (self.old / 'manifest.json').write_bytes(r.canonical(self.config))
        self.assertIn('головном', self.run_cli()[1])

    def test_undeclared_service_unit_or_model_changes(self):
        def changed(args, cwd):
            if cwd == self.old / 'compose' or '--environment' in args:
                return self.compose_command(args, cwd)
            cfg = json.loads((cwd/'compose.yaml').read_bytes())
            cfg['services']['vllm-llm']['shm_size'] = 123
            return json.dumps(cfg).encode()
        self.output.side_effect = changed
        self.assertIn('restart missing', self.run_cli()[1])
        self.output.side_effect = self.compose_command
        with mock.patch.object(host, 'units', side_effect=[{'UNIT':'old'}, {'UNIT':'new'}]):
            self.assertIn('systemd', self.run_cli()[1])
        self.release['artifacts'][2]['revision'] = 'a'*40
        self.sign_release()
        (self.state/'accepted.json').unlink()
        self.assertIn('effective model', self.run_cli()[1])

    def test_insufficient_space_before_transfer(self):
        usage = shutil.disk_usage(self.state)._replace(free=1)
        with mock.patch.object(shutil, 'disk_usage', return_value=usage), mock.patch.object(source, 'stage_artifact') as stage:
            self.assertIn('insufficient disk', self.run_cli()[1])
            stage.assert_not_called()

    def test_stage_verifies_private_copy(self):
        path = self.bundle / self.code['path']
        with source.workspace(self.state) as stage:
            copied = source.stage_artifact(stage, self.bundle, self.code)
            path.write_bytes(b'other')
            self.assertEqual(sha(copied.read_bytes()), self.code['sha256'])

    def test_https_discovery_failure_and_cached_pointer(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=self.bundle))
        self.addCleanup(server.server_close)
        cert = self.prefix / 'test.pem'
        key = self.prefix / 'test-tls.key'
        command('openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj',
                '/CN=localhost', '-addext', 'subjectAltName=IP:127.0.0.1', '-out', cert, '-keyout', key)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        url = 'https://127.0.0.1:' + str(server.server_port)
        (self.trust/'stable-url').write_text(url + '/stable.json')
        (self.trust/'stable-url').chmod(0o600)
        self.code['urls'] = [url + '/' + self.code['path']]
        self.sign_release()
        p = pointer(r.canonical(self.release))
        p.update(manifest_url=url+'/release.json', signature_url=url+'/release.json.sig')
        self.sign(self.bundle, 'stable.json', r.canonical(p), 'ragspark-channel-v1')
        with mock.patch.object(source.time, 'time', return_value=NOW):
            self.assertEqual(self.run_cli(False)[0], 5)  # untrusted test TLS cert
            with mock.patch.dict(os.environ, {'SSL_CERT_FILE': str(cert)}):
                status, msg = self.run_cli(False)
                self.assertEqual(status, 0, msg)
                self.assertIn('Скачано', msg)
                p['sequence'] += 1
                self.sign(self.bundle, 'stable.json', r.canonical(p), 'ragspark-channel-v1')
                (self.bundle/'release.json').unlink()
                self.assertEqual(self.run_cli(False)[0], 5)
                self.assertEqual(json.loads((self.state/'accepted.json').read_bytes())['channel']['sequence'], p['sequence'])

    def test_cli_rejects_incompatible_modes_and_recovery_source(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as err:
            cli.build_parser().parse_args(['update', '--check', '--rollback'])
        self.assertEqual(err.exception.code, 2)
        with mock.patch.dict(os.environ, {'RAGSPARK_ALLOW_NONROOT': '1'}), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['update', '--resume', '--from', '.']), 2)
            self.assertEqual(cli.main(['update', '--from', '.']), 5)


    def setup_bootstrap(self):
        (self.prefix / 'etc/systemd/system').mkdir(parents=True)
        wrapper = self.prefix / paths.CLI_PATH.lstrip('/')
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text(CLI_SCRIPT.format(install_dir=paths.INSTALL_DIR))
        wrapper.chmod(0o755)
        self.config['files'] = [str(wrapper)]
        manifest = self.old / 'manifest.json'
        manifest.write_bytes(r.canonical(self.config))
        manifest.chmod(0o600)

        self.rows = []
        self.unit_loaded = False
        self.unexpected_prestart = False
        self.calls = []

        def inspect(args, cwd=None):
            self.calls.append(args)
            if args[0] == 'systemctl':
                status = {'LoadState': 'loaded', 'ActiveState': 'inactive', 'SubState': 'dead'}
                if self.unit_loaded:
                    for prop, value in update_bootstrap.jobs()[args[-1]]:
                        if value:
                            status[prop] = f'{{ argv[]={update_bootstrap.unit_exec(value)} ; }}'
                    if self.unexpected_prestart and args[-1] == paths.UNIT_NAME:
                        status['ExecStartPre'] = '{ argv[]=/usr/bin/false ; }'
                return ''.join(f'{key}={val}\n' for key, val in status.items()).encode()
            if 'ps' in args:
                return json.dumps(self.rows).encode()
            if '--services' in args:
                return '\n'.join(self.services).encode()
            return self.compose_command(args, cwd)

        self.output.side_effect = inspect
        with mock.patch.dict(os.environ, {'RAGSPARK_ALLOW_NONROOT': '1'}):
            update_bootstrap.bootstrap(ROOT / 'installer')
        self.assertTrue((self.state / 'engine/maintenance.py').is_file())
        self.assertEqual(wrapper.read_text(), update_bootstrap.WRAPPER)
        override = self.prefix / 'etc/systemd/system/ragspark.service.d/90-ragspark-update.conf'
        self.assertIn('ExecStartPre=\n', override.read_text())
        self.assertIn(str(override), json.loads(manifest.read_bytes())['files'])

        def answer(url, timeout):
            self.assertTrue(url.endswith('/api/v1/system/healthz'))
            return True

        self.patch(stackmode, 'http_ok', answer)
        self.rows = [{'Service': name, 'ID': str(i)*64, 'State': 'running', 'Health': 'healthy'}
                     for i, name in enumerate(self.services, 1)]

    def through_gateway(self, *args):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {'RAGSPARK_ALLOW_NONROOT': '1'}), redirect_stdout(out):
            status = gateway(list(args))
        return status, out.getvalue()

    def test_signed_gateway_apply_and_rollback_preserve_running_container_ids(self):
        self.setup_bootstrap()
        result, msg = self.through_gateway('update', '--from', str(self.bundle), '--yes')
        self.assertEqual(result, 3, msg)
        self.assertIn('loaded systemd command differs', msg)
        self.assertFalse((self.state / txn.TRANSACTION).exists())
        self.unit_loaded = True
        self.unexpected_prestart = True
        result, msg = self.through_gateway('update', '--from', str(self.bundle), '--yes')
        self.assertEqual(result, 3, msg)
        self.assertIn('ExecStartPre', msg)
        self.assertFalse((self.state / txn.TRANSACTION).exists())
        self.unexpected_prestart = False
        keep = list(self.rows)
        old_config = json.loads((self.old / 'manifest.json').read_bytes())
        result, msg = self.through_gateway('update', '--from', str(self.bundle), '--yes')
        self.assertEqual(result, 0, msg)
        self.assertEqual((self.old / 'README.md').read_bytes(), b'new')
        self.assertEqual((self.old / 'VERSION').read_text(), '0.2.0\n')
        self.assertEqual(json.loads((self.old / 'manifest.json').read_bytes()), dict(old_config, version='0.2.0'))
        bundle_release = (self.bundle / 'release.json').read_bytes()
        self.assertEqual((self.state / 'installed-release.json').read_bytes(), bundle_release)
        self.assertEqual(self.rows, keep)
        self.assertTrue(all('up' not in call and 'stop' not in call and 'restart' not in call
                            for call in self.calls))
        result, msg = self.through_gateway('update', '--from', str(self.bundle), '--check')
        self.assertEqual(result, 0, msg)
        self.assertIn('уже установлен', msg)
        result, msg = self.through_gateway('update', '--rollback', '--yes')
        self.assertEqual(result, 0, msg)
        self.assertEqual((self.old / 'README.md').read_bytes(), b'old')
        self.assertEqual(json.loads((self.old / 'manifest.json').read_bytes()), old_config)
        self.assertFalse((self.state / 'installed-release.json').exists())
        self.assertEqual(self.rows, keep)

    def test_gate_blocks_lifecycle_after_interruption_and_recovers_without_input(self):
        self.setup_bootstrap()
        self.unit_loaded = True
        real_publish = txn.publish_receipt

        def interrupted(root, operation, side, proof, _checkpoint):
            def crash(checkpoint):
                if checkpoint == 'target:installed-release.json':
                    raise SystemExit(99)
            real_publish(root, operation, side, proof, crash)

        with mock.patch.object(txn, 'publish_receipt', side_effect=interrupted):
            with self.assertRaises(SystemExit) as aborted:
                self.through_gateway('update', '--from', str(self.bundle), '--yes')
            self.assertEqual(aborted.exception.code, 99)
        self.assertTrue((self.state / 'installed-release.json').exists())
        self.assertFalse((self.state / 'installed-release.json.sig').exists())
        for invocation in (('start',), ('backup', '--scheduled'), ('unit-start', '/usr/bin/docker', 'head'),
                           ('unit-stop', '/usr/bin/docker', 'head'), ('update', '--check')):
            self.assertEqual(self.through_gateway(*invocation)[0], 7)
        shutil.rmtree(self.bundle)
        result, msg = self.through_gateway('update', '--resume')
        self.assertEqual(result, 0, msg)
        self.assertEqual((self.old / 'VERSION').read_text(), '0.2.0\n')
        self.assertTrue((self.state / 'installed-release.json.sig').exists())

    def repack(self, changed):
        shutil.rmtree(self.bundle / 'payload')
        with tempfile.TemporaryDirectory(dir=self.prefix) as directory:
            staging = Path(directory)
            files = []
            for record in self.code['files']:
                name = record['path']
                origin = self.old / name
                data = changed.get(name, origin.read_bytes())
                dest = staging / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                files.append(file_record(name, data, mode=record['mode']))
            self.code.update(pack(staging, files, self.bundle))
        self.sign_release()

    def test_broken_candidate_cli_triggers_verified_rollback(self):
        self.setup_bootstrap()
        self.unit_loaded = True
        self.repack({'installer/ragspark.py': b'def broken(\n'})
        result, msg = self.through_gateway('update', '--from', str(self.bundle), '--yes')
        self.assertEqual(result, 6, msg)
        self.assertEqual((self.old / 'installer/ragspark.py').read_text(), 'print("fixture CLI")\n')
        self.assertEqual((self.old / 'VERSION').read_text(), 'abc0123\n')
        self.assertEqual(txn.load_plan(self.state / txn.TRANSACTION)['phase'], 'reverted')
        self.assertFalse((self.state / 'installed-release.json').exists())

    def test_two_signed_updates_and_rollback_restore_previous_signed_receipt(self):
        self.setup_bootstrap()
        self.unit_loaded = True
        result, msg = self.through_gateway('update', '--from', str(self.bundle), '--yes')
        self.assertEqual(result, 0, msg)
        previous = (self.state / 'installed-release.json').read_bytes()
        self.release['transitions'][0]['source'] = {'version': '0.2.0', 'manifest_sha256': sha(previous)}
        self.release.update(version='0.3.0', sequence=3)
        self.repack({'README.md': b'third'})
        result, msg = self.through_gateway('update', '--from', str(self.bundle), '--yes')
        self.assertEqual(result, 0, msg)
        self.assertEqual((self.old / 'README.md').read_bytes(), b'third')
        self.assertEqual((self.old / 'VERSION').read_text(), '0.3.0\n')
        result, msg = self.through_gateway('update', '--rollback', '--yes')
        self.assertEqual(result, 0, msg)
        self.assertEqual((self.old / 'README.md').read_bytes(), b'new')
        self.assertEqual((self.old / 'VERSION').read_text(), '0.2.0\n')
        self.assertEqual((self.state / 'installed-release.json').read_bytes(), previous)
        self.assertEqual(json.loads((self.state / 'accepted.json').read_bytes())['release']['sequence'], 3)

    def test_shared_lifecycle_lock_can_enter_nested_unit_but_blocks_update(self):
        self.setup_bootstrap()
        self.unit_loaded = True
        with txn.lock(self.state / 'maintenance.lock', exclusive=False):
            with mock.patch('maintenance.subprocess.run', return_value=mock.Mock(returncode=0)) as run:
                self.assertEqual(self.through_gateway('unit-start', '/usr/bin/docker', 'head')[0], 0)
                self.assertEqual(run.call_args_list[0].args[0][-1], 'prestart')
                self.assertEqual(run.call_args_list[1].args[0][:3], [Path('/usr/bin/docker'), 'compose', 'up'])
                self.assertTrue(run.call_args_list[1].kwargs['pass_fds'])
            self.assertEqual(self.through_gateway('update', '--from', str(self.bundle), '--yes')[0], 4)


REAL_OUTPUT = host.output


class Reuse(unittest.TestCase):
    def test_hf_blobs_and_whole_snapshot(self):
        model = sample()['artifacts'][2]
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / 'hub/models--fixture--model'
            snap = repo / 'snapshots' / model['revision']
            snap.mkdir(parents=True)
            blobs = repo / 'blobs'
            blobs.mkdir()
            (blobs/'hash').write_bytes(b'pass\n')
            (snap/'config.json').symlink_to('../../blobs/hash')
            self.assertTrue(host.installed_model(Path(tmp), model))
            (snap/'extra.py').write_bytes(b'')
            self.assertFalse(host.installed_model(Path(tmp), model))
            (snap/'extra.py').unlink()
            (blobs/'hash').write_bytes(b'other')
            self.assertFalse(host.installed_model(Path(tmp), model))

    def test_image_identity_needs_pinned_digest_platform_and_config(self):
        image = sample()['artifacts'][1]
        row = dict(Id=image['config_digest'], Os='linux', Architecture='arm64', RepoDigests=[image['image_ref']])
        for override, expected in [({}, True), ({'Architecture':'amd64'}, False), ({'Id':'wrong'}, False),
                                   ({'RepoDigests':[]}, False)]:
            with mock.patch.object(host, 'output', return_value=json.dumps([dict(row, **override)]).encode()):
                self.assertEqual(host.installed_image(image), expected)


@unittest.skipUnless(shutil.which('docker'), 'requires Compose plugin')
class CatalogCompose(unittest.TestCase):
    def test_effective_service_plans_from_actual_catalog(self):
        import profiles
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            old = temp / 'installed'
            candidate = temp / 'candidate'
            for root in (old, candidate):
                shutil.copytree(ROOT / 'compose', root / 'compose')
                shutil.copytree(ROOT / 'catalog', root / 'catalog')
            for mode, llm in [('single', 'qwen-3.8-27b-nvfp4'), ('stack', 'deepseek-v4-flash-vision')]:
                head_llm = profiles.resolve(candidate, llm, mode)['head']
                artifacts = {}
                role = {'images': {}, 'models': {}}
                for name, ref in head_llm['images'].items():
                    role['images'][name] = name
                    artifacts[name] = {'image_ref': ref}
                for name, rec in head_llm['models'].items():
                    role['models'][name] = name
                    artifacts[name] = rec
                env = (old / 'compose/env.example').read_text()
                files = 'compose.yaml:' + ('stack.yaml:' if mode == 'stack' else '') + 'llm/' + llm + '.yaml'
                import re
                env = re.sub('^COMPOSE_FILE=.*$', 'COMPOSE_FILE=' + files, env, flags=re.M)
                (old / 'compose/.env').write_text(env)
                transition = {'impact': 'code', 'restart': {'head': []}}
                profile = {'roles': {'head': role}}
                host.impact(old, candidate, llm, mode, 'head', transition, profile, artifacts, set())
                (candidate / 'compose/.env').unlink()
                if mode == 'stack':
                    with self.assertRaisesRegex(r.InvalidRelease, 'restart'):
                        host.impact(old, candidate, llm, mode, 'head', transition, profile, artifacts,
                                    {'compose/relay.py'})
