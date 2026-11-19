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
from rsinst import backup, update_bootstrap, update_services as maintenance, update_transaction as txn
from rsinst.install import CLI_SCRIPT
from maintenance import main as gateway
from rsinst import update_host as host, update_source as source
from rsinst import pair_transport, pair_update_transport, update_bundle, update_node
from rsinst import watch

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
        self.assertIn('node role requires an installed stack model', self.run_cli()[1])

    def configure_stack_release(self):
        rank = {'image': self.image['image_ref'], 'command': self.services['vllm-llm']['command']}
        embedding = dict(self.model, id='embed', path='payload/models/embed.tar',
                         repo='fixture/embedding', revision='1' * 40)
        rerank = dict(self.model, id='rerank', path='payload/models/rerank.tar',
                      repo='fixture/rerank', revision='2' * 40)
        self.release['artifacts'] += [embedding, rerank]
        head = {stackmode.RANK: rank, 'ragflow': {'image': self.image['image_ref']}}
        node = {stackmode.RANK: rank}
        for service, model in [('vllm-embed', embedding), ('vllm-rerank', rerank)]:
            node[service] = {'image': self.image['image_ref'], 'command': [
                'vllm', 'serve', model['repo'], '--revision', model['revision']]}
        profile = self.release['profiles'][0]
        profile.update(mode='stack', id='fixture-stack')
        requirements = profile['roles']['head']['requirements']
        profile['roles'] = {
            'head': {'requirements': requirements, 'images': dict.fromkeys(head, 'image'),
                     'models': {'llm': 'weights'}},
            'node': {'requirements': dict(requirements, min_total_ram_bytes=111 << 30),
                     'images': dict.fromkeys(node, 'image'),
                     'models': {'llm': 'weights', 'embedding': 'embed', 'rerank': 'rerank'}},
        }
        data = {
            'compose/compose.yaml': json.dumps({'services': head}).encode(),
            'compose/stack.yaml': b'services: {}\n',
            'compose/node/compose.yaml': json.dumps({'services': node}).encode(),
            'compose/node/llm/fixture-llm.yaml': b'services: {}\n',
        }
        for name, raw in data.items():
            dest = self.old / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(raw)
            dest.chmod(0o644)
        self.files = [f for f in self.files if f['path'] not in data]
        self.files += [file_record(name, raw) for name, raw in data.items()]
        self.current = release_inventory.legacy_baseline('abc0123' + 'e' * 33, self.files)
        (self.trust / 'baseline.json').write_bytes(r.canonical({'source': self.current, 'files': self.files}))
        self.release['transitions'][0].update(source=self.current, profile='fixture-stack',
                                               restart={'head': [], 'node': []})
        self.code['files'] = self.files
        self.repack({'README.md': b'new'})
        (self.old / 'compose/.env').write_text('COMPOSE_FILE=compose.yaml:stack.yaml:llm/fixture-llm.yaml\n')
        (self.old / 'compose/node/.env').write_text('COMPOSE_FILE=compose.yaml:llm/fixture-llm.yaml\n')
        self.config['config'].update(head_ip='192.0.2.10', node_ip='192.0.2.11', http_port=18080)
        self.expected_services = {'compose': head, 'node': node}
        self.rows = {directory: [
            {'ID': str(index) * 64, 'Service': service, 'State': 'running',
             'Health': '' if directory == 'node' and service == stackmode.RANK else 'healthy'}
            for index, service in enumerate(services, 1)
        ] for directory, services in self.expected_services.items()}
        self.health = self.patch(stackmode, 'http_ok', mock.Mock(return_value=True))

        def inspect(args, cwd):
            if 'ps' in args:
                self.assertIn('--all', args)
                return json.dumps(self.rows[cwd.name]).encode()
            if '--services' in args:
                return '\n'.join(self.expected_services[cwd.name]).encode()
            return self.compose_command(args, cwd)

        self.output.side_effect = inspect

    def test_stack_check_uses_only_local_role_dependencies_and_endpoints(self):
        self.configure_stack_release()
        roles = self.release['profiles'][0]['roles']
        for role, directory, models, urls in [
            ('head', 'compose', ['weights'],
             ['http://127.0.0.1:18080/api/v1/system/healthz', 'http://192.0.2.10:8000/health']),
            ('node', 'node', ['embed', 'rerank', 'weights'],
             ['http://192.0.2.11:8001/health', 'http://192.0.2.11:8002/health']),
        ]:
            with self.subTest(role=role):
                host.installed_model.reset_mock()
                self.health.reset_mock()
                self.config['config']['role'] = role
                (self.old / 'manifest.json').write_bytes(r.canonical(self.config))
                status, msg = self.run_cli(lang='en')
                self.assertEqual(status, 0, msg)
                self.assertIn('local role ' + role, msg)
                self.assertIn('Local services ready: ' + ', '.join(sorted(self.expected_services[directory])), msg)
                host.hardware.assert_called_with(roles[role]['requirements'])
                self.assertEqual([call.args[1]['id'] for call in host.installed_model.call_args_list], models)
                self.assertEqual([call.args[0] for call in self.health.call_args_list], urls)
        self.config['config']['role'] = 'head'
        (self.old / 'manifest.json').write_bytes(r.canonical(self.config))
        self.health.side_effect = lambda url, timeout: not url.endswith(':8000/health')
        self.assertIn('DeepSeek health check failed', self.run_cli(lang='en')[1])
        self.assertEqual((self.old / 'VERSION').read_text(), 'abc0123\n')
        self.assertFalse((self.state / txn.TRANSACTION).exists())

    def test_stack_check_rejects_missing_node_dependency_or_unready_node(self):
        self.configure_stack_release()
        self.config['config']['role'] = 'node'
        (self.old / 'manifest.json').write_bytes(r.canonical(self.config))
        host.installed_model.side_effect = lambda _cache, model: model['id'] != 'rerank'
        with mock.patch.object(source, 'stage_artifact') as stage:
            status, msg = self.run_cli(lang='en')
            self.assertEqual(status, 3, msg)
            self.assertIn('locally verified dependencies: rerank', msg)
            stage.assert_not_called()
        self.health.side_effect = lambda url, timeout: not url.endswith(':8002/health')
        host.installed_model.side_effect = None
        status, msg = self.run_cli(lang='en')
        self.assertEqual(status, 3, msg)
        self.assertIn('node model health check failed: rerank', msg)
        self.rows['node'] = [row for row in self.rows['node'] if row['Service'] != stackmode.RANK]
        self.assertIn('stack is not ready', self.run_cli(lang='en')[1])

    def test_stack_check_rejects_node_compose_changes_and_stack_apply(self):
        self.configure_stack_release()
        self.config['config']['role'] = 'node'
        (self.old / 'manifest.json').write_bytes(r.canonical(self.config))
        compose = json.loads((self.old / 'compose/node/compose.yaml').read_bytes())
        compose['services']['vllm-embed']['command'] += ['--max-num-seqs', '4']
        self.repack({'compose/node/compose.yaml': json.dumps(compose).encode()})
        self.assertIn('service restart missing', self.run_cli(lang='en')[1])
        apply = mock.Mock()
        self.config['config']['role'] = 'head'
        with self.assertRaisesRegex(r.InvalidRelease, 'single-node'):
            check.check(self.state, self.old, self.bundle, self.config['config'], apply)
        apply.assert_not_called()

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


    def setup_bootstrap(self, role='head'):
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
                    for prop, value in update_bootstrap.jobs(role)[args[-1]]:
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

    def assert_stack_runner(self, role, relative):
        self.configure_stack_release()
        self.config['config']['role'] = role
        self.setup_bootstrap(role)
        directory = self.old / relative
        self.output.assert_any_call(['docker', 'compose', 'ps', '--all', '--format', 'json'], cwd=directory)

        drop_in = self.prefix / 'etc/systemd/system/ragspark.service.d/90-ragspark-update.conf'
        content = drop_in.read_text()
        for action in ('start', 'stop'):
            self.assertIn(f'unit-{action} /usr/bin/docker {role}\n', content)
        self.unit_loaded = True
        update_bootstrap.ensure_ready(role)
        other = 'head' if role == 'node' else 'node'
        with self.assertRaisesRegex(r.InvalidRelease, 'loaded systemd command differs'):
            update_bootstrap.ensure_ready(other)

        with mock.patch('maintenance.subprocess.run', return_value=mock.Mock(returncode=0)) as run:
            status, msg = self.through_gateway('unit-start', '/usr/bin/docker', role)
            self.assertEqual(status, 0, msg)
            run.assert_called()
            self.assertEqual(run.call_args.kwargs['cwd'], directory)
            self.assertEqual(run.call_args.args[0][:3], [Path('/usr/bin/docker'), 'compose', 'up'])

    def test_bootstrap_stack_head(self):
        self.assert_stack_runner('head', 'compose')

    def test_bootstrap_stack_node(self):
        self.assert_stack_runner('node', 'compose/node')

    def through_gateway(self, *args):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {'RAGSPARK_ALLOW_NONROOT': '1'}), redirect_stdout(out):
            status = gateway(list(args))
        return status, out.getvalue()

    def configure_node_preparation(self):
        self.configure_stack_release()
        self.config['config']['role'] = 'node'
        (self.old / 'manifest.json').write_bytes(r.canonical(self.config))
        inbox = self.state / update_node.INBOX
        self.bundle.rename(inbox)
        self.bundle = inbox
        self.operation_id = 'a' * 32
        self.preparation = self.state / update_node.PREPARATION

    def node_operation(self, action, operation=None, digest=None):
        return self.through_gateway('update-node', action, operation or self.operation_id,
                                    digest or sha((self.bundle / 'release.json').read_bytes()))

    def test_node_prepares_one_signed_release_and_keeps_the_running_installation(self):
        self.configure_node_preparation()
        metadata = (self.old / 'manifest.json').read_bytes()
        digest = sha((self.bundle / 'release.json').read_bytes())
        status, msg = self.node_operation('prepare')
        self.assertEqual(status, 0, msg)
        expected = {'operation': self.operation_id, 'phase': 'prepared',
                    'target': {'version': '0.2.0', 'manifest_sha256': digest}}
        self.assertEqual(json.loads(msg), expected)
        journal = txn.operation_path(self.preparation)
        plan = txn.load_plan(journal)
        self.assertEqual(plan['containers'], {r['Service']: r['ID'] for r in self.rows['node']})
        self.assertEqual((journal / 'previous/README.md').read_bytes(), b'old')
        self.assertEqual((journal / 'target/README.md').read_bytes(), b'new')
        self.assertEqual((journal / 'target/installed-release.json').read_bytes(),
                         (self.bundle / 'release.json').read_bytes())
        self.assertEqual((self.old / 'README.md').read_bytes(), b'old')
        self.assertEqual((self.old / 'VERSION').read_text(), 'abc0123\n')
        self.assertEqual((self.old / 'manifest.json').read_bytes(), metadata)
        self.assertFalse(txn.pending(self.state))
        with mock.patch('maintenance.subprocess.run', return_value=mock.Mock(returncode=0)):
            self.assertEqual(self.through_gateway('watch')[0], 0)
        with mock.patch.object(source, 'discover', side_effect=AssertionError('unexpected restaging')):
            self.assertEqual(self.node_operation('prepare'), (0, msg))
            self.assertEqual(self.node_operation('status'), (0, msg))
        self.assertIn('saved pair operation differs', self.node_operation('prepare', operation='b' * 32)[1])
        self.assertIn('saved pair release differs', self.node_operation('status', digest='b' * 64)[1])
        status, msg = self.node_operation('cancel')
        self.assertEqual(status, 0, msg)
        expected['phase'] = 'reverted'
        self.assertEqual(json.loads(msg), expected)
        self.assertEqual(self.node_operation('cancel'), (0, msg))
        self.assertEqual(self.node_operation('prepare'), (0, msg))
        self.assertEqual(self.node_operation('prepare', operation='b' * 32)[0], 0)

    def test_node_rejects_wrong_digest_tampering_and_unsupported_actions(self):
        self.configure_node_preparation()
        status, msg = self.node_operation('prepare', digest='b' * 64)
        self.assertNotEqual(status, 0)
        self.assertIn('pair release digest differs', msg)
        self.assertFalse((self.state / 'accepted.json').exists())
        self.assertNotEqual(self.node_operation('publish')[0], 0)
        self.assertNotEqual(self.node_operation('prepare', operation='../invalid')[0], 0)
        self.assertEqual(json.loads(self.node_operation('status')[1])['phase'], 'absent')
        signature = self.bundle / 'release.json.sig'
        signed = signature.read_bytes()
        signature.write_bytes(b'tampered')
        self.assertIn('signature', self.node_operation('prepare')[1])
        signature.write_bytes(signed)
        (self.bundle / self.code['path']).write_bytes(b'x' * self.code['bytes'])
        self.assertIn('digest mismatch', self.node_operation('prepare')[1])
        self.assertFalse(self.preparation.exists())

    def test_node_preparation_refuses_other_roles_and_service_transitions(self):
        self.configure_node_preparation()
        self.config['config']['role'] = 'head'
        (self.old / 'manifest.json').write_bytes(r.canonical(self.config))
        self.assertIn('runs on the node', self.node_operation('prepare')[1])
        self.config['config']['role'] = 'node'
        (self.old / 'manifest.json').write_bytes(r.canonical(self.config))
        self.release['transitions'][0].update(impact='services', backup='required',
                                               restart={'head': [], 'node': [stackmode.RANK]})
        self.sign_release()
        self.assertIn('requires a code-only transition', self.node_operation('prepare')[1])
        self.assertFalse(self.preparation.exists())

    def node_transfer_packet(self):
        receipt = ((self.bundle / 'release.json').read_bytes(), (self.bundle / 'release.json.sig').read_bytes())
        packet = io.BytesIO()
        update_bundle.send(packet, self.bundle, receipt)
        return packet.getvalue(), sha(receipt[0])

    def receive_node_packet(self, stream, digest):
        with mock.patch.object(update_node.sys, 'stdin', mock.Mock(buffer=stream)):
            return self.node_operation('receive', digest=digest)

    def test_node_receives_one_verified_bundle_and_retries_after_interruption(self):
        self.configure_node_preparation()
        packet, digest = self.node_transfer_packet()
        shutil.rmtree(self.bundle)
        transfer = self.state / 'pair-transfer'
        transfer.mkdir(mode=0o700)
        (transfer / 'interrupted').write_bytes(b'old partial transfer')
        status, msg = self.receive_node_packet(io.BytesIO(packet[:-7]), digest)
        self.assertNotEqual(status, 0)
        self.assertIn('truncated pair bundle', msg)
        self.assertFalse(self.preparation.exists())
        self.assertFalse(transfer.exists())
        self.assertFalse((self.state / 'accepted.json').exists())
        receive = update_bundle.receive

        def receive_with_lifecycle_available(*args):
            with txn.lock(self.state / 'maintenance.lock'):
                return receive(*args)

        with mock.patch.object(update_bundle, 'receive', side_effect=receive_with_lifecycle_available):
            status, msg = self.receive_node_packet(io.BytesIO(packet), digest)
        self.assertEqual(status, 0, msg)
        self.assertEqual(json.loads(msg)['phase'], 'prepared')
        self.assertEqual((txn.operation_path(self.preparation) / 'target/README.md').read_bytes(), b'new')
        self.assertEqual((self.old / 'README.md').read_bytes(), b'old')
        self.assertFalse(transfer.exists())
        self.assertEqual(self.receive_node_packet(io.BytesIO(packet), digest), (0, msg))
        self.assertEqual(self.node_operation('status', digest=digest), (0, msg))

    def test_node_rejects_tampered_or_oversized_transfer_before_preparing(self):
        self.configure_node_preparation()
        packet, digest = self.node_transfer_packet()
        _, raw_size, sig_size = update_bundle.HEADER.unpack(packet[:update_bundle.HEADER.size])
        signature_start = update_bundle.HEADER.size + raw_size
        bad_signature = packet[:signature_start] + b'x' + packet[signature_start + 1:]
        large = update_bundle.HEADER.pack(update_bundle.MAGIC, r.MAX_MANIFEST + 1, sig_size)
        bad_code = packet[:-1] + bytes([packet[-1] ^ 1])
        for payload, expected_error, read_bytes in [
            (large, 'invalid integer', update_bundle.HEADER.size),
            (bad_signature, 'signature', signature_start + sig_size),
            (bad_code, 'pair code digest mismatch', len(packet)),
            (packet + b'extra', 'unexpected data', len(packet) + 1),
        ]:
            with self.subTest(error=expected_error):
                stream = io.BytesIO(payload)
                status, msg = self.receive_node_packet(stream, digest)
                self.assertNotEqual(status, 0)
                self.assertIn(expected_error, msg)
                self.assertEqual(stream.tell(), read_bytes)
                self.assertFalse(self.preparation.exists())
                self.assertFalse((self.state / 'accepted.json').exists())
                self.assertFalse((self.state / 'pair-transfer').exists())

    def test_qsfp_client_delivers_signed_bundle_to_the_fixed_runner(self):
        self.configure_node_preparation()
        config = self.config['config']
        binding = {'id': 'e' * 32, 'head_ip': config['head_ip'], 'node_ip': config['node_ip']}
        key = ' '.join(self.key.with_suffix('.pub').read_text().split()[:2])
        pair_dir = Path(paths.host(str(pair_transport.PAIR_DIR)))
        head = pair_dir / 'head'
        identity = head / 'update/id_ed25519'
        identity.parent.mkdir(parents=True)
        identity.touch(mode=0o600)
        (head / 'peer.json').write_bytes(r.canonical(binding))
        (head / 'known_hosts').write_text(pair_transport.known_host(binding['node_ip'], key))
        (pair_dir / 'node.json').write_bytes(r.canonical(binding))
        (pair_dir / 'update.json').write_bytes(r.canonical(dict(
            protocol=1, pair=binding, key=key, purpose=pair_update_transport.GRANT_PURPOSE)))
        receipt = tuple((self.bundle / name).read_bytes() for name in ('release.json', 'release.json.sig'))
        digest = sha(receipt[0])

        def ssh(argv, stdin, timeout):
            self.assertNotIn('-n', argv)
            self.assertIn('StrictHostKeyChecking=yes', argv)
            self.assertIn('IdentityFile=' + str(identity), argv)
            self.assertIn('UserKnownHostsFile=' + str(head / 'known_hosts'), argv)
            self.assertEqual(argv[-2:], [pair_update_transport.USER + '@' + binding['node_ip'],
                                         pair_update_transport.COMMAND])
            with mock.patch.dict(os.environ, {'SUDO_USER': pair_update_transport.USER}), \
                    mock.patch.object(sys, 'stdin', mock.Mock(buffer=stdin)):
                status, msg = self.through_gateway('update-peer')
            self.assertEqual(status, 0, msg)
            return msg.encode()

        with mock.patch.object(pair_transport, '_read_ssh', side_effect=ssh):
            result = pair_update_transport.query('receive', self.operation_id, digest,
                                                 bundle=self.bundle, receipt=receipt, directory=head)
            self.assertEqual(result['phase'], 'prepared')
            self.assertEqual(pair_update_transport.query('status', self.operation_id, digest, directory=head), result)
            self.assertEqual(pair_update_transport.query('cancel', self.operation_id, digest, directory=head)['phase'],
                             'reverted')
        self.assertNotEqual(self.through_gateway('update-peer')[0], 0)

    def configure_pair_update(self):
        self.configure_stack_release()
        self.config['config']['role'] = 'head'
        (self.old / 'manifest.json').write_bytes(r.canonical(self.config))
        self.node_tree = self.prefix / 'node-tree'
        shutil.copytree(self.old, self.node_tree)
        node_config = dict(self.config, config=dict(self.config['config'], role='node'))
        (self.node_tree / 'manifest.json').write_bytes(r.canonical(node_config))
        self.node_state = self.prefix / 'node-state'
        shutil.copytree(self.state, self.node_state)
        self.peer = {'id': 'e' * 32, 'head_ip': self.config['config']['head_ip'],
                     'node_ip': self.config['config']['node_ip']}
        self.patch(pair_transport, 'head_binding', mock.Mock(return_value=self.peer))
        self.patch(update_bootstrap, 'ensure_ready', mock.Mock())
        self.remote = self.patch(pair_update_transport, 'query', mock.Mock(side_effect=self.forward_pair))

    def forward_pair(self, action, operation, digest, *, directory, expected_pair, bundle=None, receipt=None):
        self.assertEqual(expected_pair, self.peer)
        stream = None
        if action == 'receive':
            stream = io.BytesIO()
            update_bundle.send(stream, bundle, receipt)
            stream.seek(0)
        output = io.StringIO()
        with redirect_stdout(output):
            status = update_node.command(self.node_state, self.node_tree, [action, operation, digest], stream=stream)
        self.assertEqual(status, 0, output.getvalue())
        return json.loads(output.getvalue())

    def pair_start(self):
        return self.through_gateway('update', '--from', str(self.bundle), '--yes', '--lang', 'en')

    def test_pair_resume_after_node_prepares_without_reopening_bundle(self):
        self.configure_pair_update()

        def drop(action, *args, **kwargs):
            if action == 'receive':
                self.assertEqual((self.old / 'VERSION').read_text(), 'abc0123\n')
                self.forward_pair(action, *args, **kwargs)
                raise pair_transport.PairUnavailable('reply lost after preparation')
            return self.forward_pair(action, *args, **kwargs)

        self.remote.side_effect = drop
        status, msg = self.pair_start()
        self.assertEqual(status, 7, msg)
        self.assertIn('reply lost', msg)
        head = txn.load_plan(txn.operation_path(self.state))
        self.assertEqual(head['phase'], 'prepared')
        self.assertIsNone(head['pair']['decision'])
        self.assertEqual((self.node_tree / 'VERSION').read_text(), 'abc0123\n')
        shutil.rmtree(self.bundle)
        self.remote.side_effect = self.forward_pair
        self.remote.reset_mock()
        status, msg = self.through_gateway('update', '--resume', '--yes', '--lang', 'en')
        self.assertEqual(status, 0, msg)
        self.assertEqual([call.args[0] for call in self.remote.call_args_list], ['status', 'apply', 'commit'])
        for root, tree in ((self.state, self.old), (self.node_state, self.node_tree)):
            self.assertEqual(txn.load_plan(txn.operation_path(root))['phase'], 'committed')
            self.assertEqual((tree / 'VERSION').read_text(), '0.2.0\n')

    def test_pair_saved_commit_survives_lost_acknowledgement(self):
        self.configure_pair_update()

        def drop(action, *args, **kwargs):
            reply = self.forward_pair(action, *args, **kwargs)
            if action == 'commit':
                head = txn.load_plan(txn.operation_path(self.state))
                self.assertEqual(head['phase'], 'verified')
                self.assertEqual(head['pair']['decision'], 'commit')
                raise pair_transport.PairUnavailable('commit acknowledgement lost')
            return reply

        self.remote.side_effect = drop
        status, msg = self.pair_start()
        self.assertEqual(status, 7, msg)
        self.assertEqual(txn.load_plan(txn.operation_path(self.node_state))['phase'], 'committed')
        self.assertEqual(self.through_gateway('update', '--rollback', '--yes')[0], 3)
        with mock.patch.object(watch, 'fuse_only') as fuse:
            self.assertEqual(self.through_gateway('watch')[0], 0)
            fuse.assert_called_once_with('head')
        self.remote.side_effect = self.forward_pair
        self.remote.reset_mock()
        status, msg = self.through_gateway('update', '--resume', '--yes', '--lang', 'en')
        self.assertEqual(status, 0, msg)
        self.assertEqual([call.args[0] for call in self.remote.call_args_list], ['commit'])
        self.assertEqual(txn.load_plan(txn.operation_path(self.state))['phase'], 'committed')

    def test_pair_rollback_before_decision_restores_both_nodes(self):
        self.configure_pair_update()

        def drop(action, *args, **kwargs):
            reply = self.forward_pair(action, *args, **kwargs)
            if action == 'apply':
                raise pair_transport.PairUnavailable('apply acknowledgement lost')
            return reply

        self.remote.side_effect = drop
        status, msg = self.pair_start()
        self.assertEqual(status, 7, msg)
        self.assertEqual(txn.load_plan(txn.operation_path(self.node_state))['phase'], 'verified')
        self.assertEqual((self.node_tree / 'README.md').read_bytes(), b'new')
        self.assertEqual((self.old / 'README.md').read_bytes(), b'old')
        self.remote.side_effect = self.forward_pair
        status, msg = self.through_gateway('update', '--rollback', '--yes', '--lang', 'en')
        self.assertEqual(status, 0, msg)
        for root, tree in ((self.state, self.old), (self.node_state, self.node_tree)):
            journal = txn.load_plan(txn.operation_path(root))
            self.assertEqual(journal['phase'], 'reverted')
            self.assertEqual((tree / 'README.md').read_bytes(), b'old')
            self.assertFalse((root / 'installed-release.json').exists())

    def test_node_remembers_cancellation_of_an_unreceived_release(self):
        self.configure_node_preparation()
        packet, digest = self.node_transfer_packet()
        status, msg = self.node_operation('cancel', digest=digest)
        self.assertEqual(status, 0, msg)
        self.assertEqual(json.loads(msg)['phase'], 'reverted')
        self.assertEqual(self.receive_node_packet(io.BytesIO(packet), digest), (0, msg))
        self.assertFalse(self.preparation.exists())
        self.assertEqual((self.old / 'VERSION').read_text(), 'abc0123\n')

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


    def configure_service_release(self, mutate=None):
        """A real signed package and journal with a disposable simulated stack."""
        for name in (maintenance.WEB, *backup.DATA):
            self.services[name] = {'image': self.image['image_ref']}
        self.services[maintenance.WEB].update(
            ports=[{'target': 80, 'published': '18080'}], restart='on-failure:5',
            networks={'default': None, 'backend': None})
        self.compose['networks'] = {'default': {'driver': 'bridge'}, 'backend': {'internal': True}}
        role = self.release['profiles'][0]['roles']['head']
        role['images'] = {name: 'image' for name in self.services}
        raw = json.dumps(self.compose).encode()
        (self.old / 'compose/compose.yaml').write_bytes(raw)
        self.files = [file_record('compose/compose.yaml', raw) if rec['path'] == 'compose/compose.yaml' else rec
                      for rec in self.files]
        self.current = release_inventory.legacy_baseline('abc0123' + 'e'*33, self.files)
        (self.trust / 'baseline.json').write_bytes(r.canonical({'source': self.current, 'files': self.files}))
        self.release['transitions'][0].update(source=self.current, impact='services', backup='required',
                                               restart={'head': ['vllm-llm']})
        new = json.loads(raw)
        new['services']['vllm-llm']['command'] += ['--max-num-seqs', '4']
        if mutate:
            mutate(new)
        self.repack({'compose/compose.yaml': json.dumps(new).encode()})
        self.setup_bootstrap()
        self.unit_loaded = True
        for volume in backup.VOLUMES:
            (self.prefix / volume).mkdir()
        self.patch(backup, 'mountpoint', lambda volume: str(self.prefix / volume))
        self.patch(backup, 'parsing', mock.Mock(return_value=0))
        self.patch(backup, 'wait_parsing', mock.Mock(return_value=True))
        self.patch(backup, 'ready', mock.Mock(return_value=True))
        self.events = []

        def verified(_config):
            self.assertEqual((self.old / 'VERSION').read_text().strip(), 'abc0123')
            self.events.append('backup-verified')

        self.patch(backup, 'stop', mock.Mock(side_effect=lambda _data: self.events.append('stop-data')))
        self.patch(maintenance, 'verified_backup', mock.Mock(side_effect=verified))
        self.patch(maintenance, 'systemctl', lambda action, name: self.events.append((action, name)))
        self.compose_actions = self.patch(maintenance, 'run_compose', mock.Mock(side_effect=self.service_compose))
        inspect = self.output.side_effect

        def simulated(args, cwd=None):
            if args[0] == 'systemctl' and args[-1].endswith('.timer'):
                active = 'active' if args[-1] == paths.BACKUP_TIMER else 'inactive'
                return f'LoadState=loaded\nActiveState={active}\n'.encode()
            if args[:2] == ['docker', 'inspect']:
                return b'{}' if 'PortBindings' in args[3] else self.image['config_digest'].encode()
            if '-q' in args:
                return b'e' * 64
            if any(str(v).endswith('/private-web.yaml') for v in args) and 'config' in args:
                out = json.loads(inspect(args, cwd))
                out['services']['ragflow'].pop('ports', None)
                return json.dumps(out).encode()
            return inspect(args, cwd)

        self.output.side_effect = simulated

    def service_compose(self, tree, config, args, overlay=None, timeout=2400):
        if args[0] == 'up' and args[-1] == 'ragflow':
            action = 'create-private' if overlay else 'publish-web'
            self.events.append(action)
            if not overlay:
                self.assertIn(maintenance.status(self.state / txn.TRANSACTION),
                              ('opening-target', 'opening-previous', 'reopening-source'))
        elif args[0] == 'up' and args[-1] == 'vllm-llm':
            self.events.append('up-model')
        elif args == ['stop', 'ragflow']:
            self.events.append('close-web')
        elif args == ['stop', 'vllm-llm']:
            self.events.append('stop-model')
        elif args == ['start', 'ragflow']:
            self.events.append('start-private')
        elif args[0] == 'exec':
            self.events.append('verify-' + args[2])

    def apply_service_release(self):
        return self.through_gateway('update', '--from', str(self.bundle), '--yes')

    def test_service_update_backs_up_before_publication_and_checks_private_web(self):
        self.configure_service_release()
        code, out = self.apply_service_release()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.events, [('stop', paths.BACKUP_TIMER), 'close-web', 'stop-data',
                                       'backup-verified', 'close-web', 'stop-model', 'up-model', 'verify-vllm-llm',
                                       'create-private', 'start-private', 'verify-ragflow', 'publish-web',
                                       ('start', paths.BACKUP_TIMER)])
        self.assertTrue((self.state / 'installed-release.json.sig').exists())
        self.assertFalse(txn.pending(self.state))
        self.assertEqual(self.through_gateway('update', '--rollback', '--yes')[0], 3)

    def test_service_update_writes_and_verifies_real_backup_archives(self):
        self.configure_service_release()
        # Use the real backup writer with small real files and a synthetic
        # read-only Compose image metadata query.
        real_backup = VERIFIED_BACKUP
        for volume in backup.VOLUMES:
            (self.prefix / volume / 'marker').write_text(volume + '\n')
        env = self.old / 'compose/.env'
        with env.open('a') as stream:
            for key in backup.DATA_ENV:
                stream.write(f'{key}=fixture\n')
        self.patch(backup, '_describe', lambda name, _manifest:
                   {'name': name, 'format': 1, 'release': 'abc0123',
                    'images': {k: self.image['image_ref'] for k in ('ragflow', *backup.DATA)}})
        maintenance.verified_backup.side_effect = real_backup
        code, out = self.apply_service_release()
        self.assertEqual(code, 0, out)
        archive, = maintenance.backup_root().glob('ragspark-*-before-update')
        info = json.loads((archive / 'backup.json').read_text())
        self.assertIsNone(backup.verify(str(archive), info))
        for volume in backup.VOLUMES:
            self.assertEqual((self.prefix / volume / 'marker').read_text(), volume + '\n')

    def test_service_update_rejects_changes_to_data_image_before_stopping(self):
        self.configure_service_release(lambda cfg: cfg['services']['mysql'].update(image='different'))
        code, out = self.apply_service_release()
        self.assertEqual(code, 3, out)
        self.assertEqual(self.events, [])
        self.assertFalse((self.state / txn.TRANSACTION).exists())

    def test_cancellation_while_parsing_preserves_running_web(self):
        self.configure_service_release()
        backup.wait_parsing.return_value = False
        code, out = self.apply_service_release()
        self.assertEqual(code, 6, out)
        self.assertEqual(self.events, [('stop', paths.BACKUP_TIMER), ('start', paths.BACKUP_TIMER)])
        self.assertEqual((self.old / 'VERSION').read_text(), 'abc0123\n')
        self.assertFalse(txn.pending(self.state))

    def test_service_probe_failure_reverts_before_publication(self):
        self.configure_service_release()

        def fail_model(tree, config, args, overlay=None, timeout=2400):
            self.service_compose(tree, config, args, overlay, timeout)
            if args[:3] == ['exec', '-T', 'vllm-llm'] and (tree / 'VERSION').read_text() == '0.2.0\n':
                raise source.PreparationError('target probe failed')

        self.compose_actions.side_effect = fail_model
        code, out = self.apply_service_release()
        self.assertEqual(code, 6, out)
        self.assertEqual(self.events.count('publish-web'), 1)
        self.assertEqual(self.events.count('backup-verified'), 1)
        self.assertEqual((self.old / 'VERSION').read_text(), 'abc0123\n')
        self.assertFalse(txn.pending(self.state))

    def test_service_opening_failure_disallows_rollback_and_resumes_forward(self):
        self.configure_service_release()

        def fail_open(tree, config, args, overlay=None, timeout=2400):
            self.service_compose(tree, config, args, overlay, timeout)
            if args[0] == 'up' and args[-1] == 'ragflow' and overlay is None:
                raise source.PreparationError('exposure uncertain')

        self.compose_actions.side_effect = fail_open
        self.assertEqual(self.apply_service_release()[0], 7)
        self.assertTrue(txn.pending(self.state))
        self.assertEqual(maintenance.status(self.state / txn.TRANSACTION), 'opening-target')
        self.assertEqual(self.through_gateway('update', '--rollback', '--yes')[0], 3)
        self.compose_actions.side_effect = self.service_compose
        self.assertEqual(self.through_gateway('update', '--resume')[0], 0)
        self.assertEqual((self.old / 'VERSION').read_text(), '0.2.0\n')
        self.assertEqual(self.events.count('backup-verified'), 1)
        self.assertFalse(txn.pending(self.state))


VERIFIED_BACKUP = maintenance.verified_backup
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
                env = (old / 'compose/env.example').read_text()
                import re
                for name, resolved in profiles.resolve(candidate, llm, mode).items():
                    with self.subTest(mode=mode, role=name):
                        artifacts = {svc: {'image_ref': ref} for svc, ref in resolved['images'].items()}
                        artifacts.update(resolved['models'])
                        pins = {field: {key: key for key in resolved[field]}
                                for field in ('images', 'models')}
                        directory = Path('compose/node' if name == 'node' else 'compose')
                        files = stackmode.compose_file(llm, name)
                        (old / directory / '.env').write_text(
                            re.sub('^COMPOSE_FILE=.*$', 'COMPOSE_FILE=' + files, env, flags=re.M))
                        transition = {'impact': 'code', 'restart': {name: []}}
                        profile = {'roles': {name: pins}}
                        host.impact(old, candidate, llm, mode, name, transition, profile, artifacts, set())
                        (candidate / directory / '.env').unlink()
                        if mode == 'stack' and name == 'head':
                            with self.assertRaisesRegex(r.InvalidRelease, 'restart'):
                                host.impact(old, candidate, llm, mode, name, transition,
                                            profile, artifacts, {'compose/relay.py'})

    def test_real_compose_scheduler_only_and_private_web_overlay(self):
        with tempfile.TemporaryDirectory() as directory:
            old = Path(directory) / 'old'
            target = Path(directory) / 'target'
            for tree in (old, target):
                shutil.copytree(ROOT / 'compose', tree / 'compose')
                env = tree / 'compose/.env'
                env.write_bytes((tree / 'compose/env.example').read_bytes())
            name = 'qwen-3.8-27b-nvfp4'
            config = {'llm': name}
            files = ['compose.yaml', 'llm/' + name + '.yaml']
            source_file = target / 'compose' / files[1]
            text = source_file.read_text()
            source_file.write_text(text.replace('      - --max-num-batched-tokens',
                                                '      - --max-num-seqs\n      - "4"\n      - --max-num-batched-tokens'))
            existing = host.render(old / 'compose', files)
            candidate = host.render(target / 'compose', files)
            self.assertTrue(existing['services']['ragflow']['ports'])
            maintenance.compatible(existing, candidate, {'compose/' + files[1]}, ['vllm-llm'], name)
            overlay = Path(directory) / 'private-web.yaml'
            overlay.write_bytes(maintenance.PRIVATE_WEB)
            maintenance.prove_private(target, config, overlay)
