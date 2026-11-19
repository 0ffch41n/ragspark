"""Enrollment lifecycle tests with real files and no system account changes."""

import base64
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'installer'))
from rsinst import cli, pair_setup as pair, pair_transport as transport, paths, release as r, ui
from rsinst.manifest import Manifest


LOCAL = {'head_ip': '192.168.100.10', 'node_ip': '192.168.100.11'}
BINDING = dict(LOCAL, id='ab' * 16)
KEY = 'ssh-ed25519 ' + base64.b64encode(transport.ED25519_HEADER + b'a' * 32).decode('ascii')
OTHER_KEY = 'ssh-ed25519 ' + base64.b64encode(transport.ED25519_HEADER + b'b' * 32).decode('ascii')
OFFER = pair.record(BINDING, KEY)


class PairSetup(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)
        self.base = self.work / 'pair'
        self.base.mkdir(mode=0o755)
        self.auth = self.base / 'account/.ssh/authorized_keys'
        for context in (patch.object(paths, 'PREFIX', str(self.work)),
                        patch.object(ui, '_state', {'lang': 'en', 'yes': True, 'log': None, 'pending': []})):
            context.start()
            self.addCleanup(context.stop)

        # An installed tree has administrator-owned, read-only source files.
        source = Path(__file__).resolve().parents[1] / 'installer'
        installer = Path(paths.install_dir()) / 'installer'
        (installer / 'rsinst').mkdir(parents=True)
        for folder in (installer, installer.parent, installer.parent.parent, installer / 'rsinst'):
            folder.chmod(0o755)
        for name in pair.RECEIVER_FILES:
            (installer / name).write_bytes((source / name).read_bytes())
            (installer / name).chmod(0o644)

    def test_codes_reject_wrong_type_invalid_or_extra_fields(self):
        good = pair.token('RSI', OFFER)
        with patch.object(sys, 'stdin', io.StringIO(good + '\n')), redirect_stdout(io.StringIO()):
            self.assertEqual(pair.read_token('RSI', ''), OFFER)
        for bad in (pair.token('RSR', OFFER), pair.token('RSI', dict(OFFER, protocol=2)),
                    pair.token('RSI', dict(OFFER, extra=True)), 'RSI' + 'X' * 1024, 'RSI-X'):
            with self.subTest(bad=bad[:10]), patch.object(sys, 'stdin', io.StringIO(bad)), \
                    redirect_stdout(io.StringIO()), self.assertRaises(r.InvalidRelease):
                pair.read_token('RSI', '')

    @unittest.skipUnless(shutil.which('ssh-keygen'), 'requires OpenSSH key generation')
    def test_head_publishes_enrollment_only_after_authenticated_reply(self):
        offer = pair.create_invitation(self.base, LOCAL)
        self.assertEqual(pair.create_invitation(self.base, LOCAL), offer)
        self.assertEqual((self.base / 'pending/id_ed25519').stat().st_mode & 0o777, 0o600)
        receipt = pair.record(offer['pair'], OTHER_KEY)
        expected = {'pair': offer['pair'], 'version': 'e09da62', 'unit_state': 'active'}

        def reply(directory):
            self.assertEqual(directory, self.base / 'pending')
            self.assertFalse((self.base / 'head').exists())
            self.assertEqual((directory / 'known_hosts').read_text(),
                             transport.known_host(LOCAL['node_ip'], OTHER_KEY))
            return expected

        with patch.object(transport, 'query_head', side_effect=transport.PairUnavailable('disconnected')):
            with self.assertRaises(transport.PairUnavailable):
                pair.finish_head(self.base, receipt)
        self.assertTrue((self.base / 'pending').exists())
        self.assertFalse((self.base / 'head').exists())
        with patch.object(transport, 'query_head', side_effect=reply) as query:
            wrong = pair.record(dict(offer['pair'], id='ff' * 16), OTHER_KEY)
            with self.assertRaisesRegex(r.InvalidRelease, 'does not match'):
                pair.finish_head(self.base, wrong)
            query.assert_not_called()
            self.assertEqual(pair.finish_head(self.base, receipt), expected)
        self.assertFalse((self.base / 'pending').exists())
        pair.remove_head_identity(self.base)
        self.assertFalse((self.base / 'head').exists())

    def test_node_retries_partial_authorization_and_revokes_the_key(self):
        replace = pair.txn.replace

        def interrupt(path, data, mode):
            if path == self.auth:
                raise OSError('interrupted before key publication')
            replace(path, data, mode)

        previous_umask = os.umask(0o077)
        self.addCleanup(os.umask, previous_umask)
        with patch.object(pair, 'managed_account'), patch.object(pair.txn, 'replace', side_effect=interrupt):
            with self.assertRaises(OSError):
                pair.authorize_node(self.base, OFFER)
        self.assertFalse(self.auth.exists())
        with patch.object(pair, 'managed_account') as account:
            with self.assertRaisesRegex(r.InvalidRelease, 'revoke'):
                pair.authorize_node(self.base, pair.record(BINDING, OTHER_KEY))
            account.assert_not_called()
            pair.authorize_node(self.base, OFFER)
            pair.authorize_node(self.base, OFFER)
        self.assertEqual(self.auth.read_text(), transport.authorized_key(LOCAL['head_ip'], KEY))
        for path in (self.auth.parent, self.auth.parent.parent, self.base / 'receiver',
                     self.base / 'receiver/rsinst'):
            self.assertEqual(path.stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.base / 'node.json').stat().st_mode & 0o777, 0o644)
        self.assertEqual((self.base / 'accepted.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(pair.read_json(self.base / 'node.json'), BINDING)
        with redirect_stdout(io.StringIO()) as out:
            pair.show_status(self.base, 'node')
        self.assertIn('Read-only status is allowed', out.getvalue())
        pair.remove_node_grant(self.base)
        pair.remove_node_grant(self.base)
        self.assertFalse(self.auth.exists())
        self.assertFalse((self.base / 'node.json').exists())
        self.assertFalse((self.base / 'accepted.json').exists())

    def test_unmanaged_system_account_is_not_adopted(self):
        account = SimpleNamespace(pw_uid=411, pw_gid=411, pw_dir=str(pair.ACCOUNT_HOME), pw_shell='/bin/sh')
        with patch.object(pair.pwd, 'getpwnam', return_value=account), \
                patch.object(pair, 'checked_command') as command:
            with self.assertRaisesRegex(r.InvalidRelease, 'outside RAGSpark'):
                pair.managed_account(self.base)
            command.assert_not_called()
        self.assertFalse((self.base / 'account.json').exists())

    def test_interrupted_account_creation_preserves_exact_identity(self):
        account = SimpleNamespace(pw_uid=411, pw_gid=411, pw_dir=str(pair.ACCOUNT_HOME), pw_shell='/bin/sh')
        with patch.object(pair.pwd, 'getpwnam', side_effect=KeyError), \
                patch.object(pair, 'checked_command', side_effect=r.InvalidRelease('interrupted')):
            with self.assertRaisesRegex(r.InvalidRelease, 'interrupted'):
                pair.managed_account(self.base)
        self.assertEqual(pair.read_json(self.base / 'account.json'), {'uid': None})
        with patch.object(pair.pwd, 'getpwnam', return_value=account), \
                patch.object(pair.os, 'getgrouplist', return_value=[411, 999]):
            with self.assertRaisesRegex(r.InvalidRelease, 'supplementary'):
                pair.managed_account(self.base)
        with patch.object(pair.pwd, 'getpwnam', return_value=account), \
                patch.object(pair.os, 'getgrouplist', return_value=[411]):
            pair.managed_account(self.base)
        self.assertEqual(pair.read_json(self.base / 'account.json'), {'uid': 411})

    def test_valid_enrollment_prompts_show_fingerprints_in_both_languages(self):
        expected = {'pair': BINDING, 'version': 'e09da62', 'unit_state': 'active'}
        manifest = Manifest({'status': 'installed', 'config': {}})
        (self.base / 'pending').mkdir()
        for lang in ('en', 'ru'):
            with (
                self.subTest(lang=lang),
                patch.object(pair, 'endpoints', return_value=LOCAL),
                patch.object(pair, 'read_key', return_value=OTHER_KEY),
                patch.object(ui, 'confirm', return_value=True) as confirm,
                patch.object(pair, 'authorize_node') as authorize,
                patch.object(pair, 'finish_head', return_value=expected) as finish,
            ):
                ui.set_lang(lang)
                invitation = pair.token('RSI', OFFER)
                with patch.object(sys, 'stdin', io.StringIO(invitation + '\n')), \
                        redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(pair.enroll(self.base, 'node', 'accept', manifest), 0)
                self.assertIn(pair.fingerprint(KEY), confirm.call_args.args[0])
                authorize.assert_called_once_with(self.base, OFFER)
                receipt = pair.record(BINDING, OTHER_KEY)
                self.assertIn(pair.fingerprint(OTHER_KEY), output.getvalue())
                self.assertIn(pair.token('RSR', receipt), output.getvalue().splitlines())
                with patch.object(sys, 'stdin', io.StringIO(pair.token('RSR', receipt) + '\n')), \
                        redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(pair.enroll(self.base, 'head', 'finish', manifest), 0)
                self.assertIn(pair.fingerprint(OTHER_KEY), confirm.call_args.args[0])
                finish.assert_called_once_with(self.base, receipt)
                self.assertIn('e09da62', output.getvalue())

    def test_accept_checks_role_and_addresses_before_publishing_a_key(self):
        args = cli.build_parser().parse_args(['pair', 'accept'])
        config = dict(LOCAL, llm='deepseek-v4-flash-vision', role='head')
        manifest = Manifest({'status': 'installed', 'config': config})
        with redirect_stdout(io.StringIO()), patch.object(pair, 'authorize_node') as authorize:
            self.assertEqual(pair.command(args, manifest), 1)
            config['role'] = 'node'
            wrong = pair.record(dict(BINDING, head_ip='192.168.100.12'), KEY)
            with patch.object(pair, 'endpoints', return_value=LOCAL), \
                    patch.object(pair, 'read_token', return_value=wrong):
                self.assertEqual(pair.command(args, manifest), 1)
            authorize.assert_not_called()


if __name__ == '__main__':
    unittest.main()
