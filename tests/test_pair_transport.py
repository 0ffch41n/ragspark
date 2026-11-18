"""Tests for the read-only pair status protocol and its SSH boundary."""

import base64
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'installer'))
from rsinst import pair_transport as pair, release as r


PAIR = {'id': 'a' * 32, 'head_ip': '192.168.100.10', 'node_ip': '192.168.100.11'}
KEY = 'ssh-ed25519 ' + base64.b64encode(pair.ED25519_HEADER + bytes(range(32))).decode()
STATUS = {'protocol': 1, 'role': 'node', 'pair': PAIR, 'version': 'e09da62',
          'unit_state': 'active'}
CONNECTION = '192.168.100.10 63740 192.168.100.11 22'


class PairValidation(unittest.TestCase):
    def test_reject_invalid_enrollments(self):
        for wrong in (dict(PAIR, head_ip='0.0.0.0'), dict(PAIR, head_ip='127.0.0.1'),
                      dict(PAIR, head_ip='192.168.100.11'), dict(PAIR, node_ip='224.0.0.1'),
                      dict(PAIR, node_ip='192.168.100.11\nssh-ed25519 AAAA'),
                      dict(PAIR, id='wrong'), dict(PAIR, user='root')):
            with self.subTest(wrong=wrong), self.assertRaises(r.InvalidRelease):
                pair.binding(wrong)

    def test_key_lines_are_confined_to_one_peer_and_command(self):
        self.assertEqual(pair.known_host(PAIR['node_ip'], KEY), f'{PAIR["node_ip"]} {KEY}\n')
        self.assertEqual(pair.authorized_key(PAIR['head_ip'], KEY),
                         'restrict,from="192.168.100.10",command="/usr/bin/python3 -I -B '
                         '/var/lib/ragspark-pair/receiver/pair_status.py" ' + KEY + '\n')
        for key in (KEY + '\n' + KEY, KEY + ' comment', 'ssh-rsa ' + KEY.split()[1],
                    'ssh-ed25519 ' + 'A' * 68):
            with self.subTest(key=key), self.assertRaises(r.InvalidRelease):
                pair.authorized_key(PAIR['head_ip'], key)

    @unittest.skipUnless(shutil.which('ssh'), 'requires OpenSSH client')
    def test_openssh_effective_settings(self):
        folder = Path('/var/lib/ragspark-pair/head')
        args = pair.ssh_args(PAIR, folder)
        self.assertEqual(args[-2:], ['ragspark-pair@192.168.100.11', 'status'])
        result = subprocess.check_output([args[0], '-G', *args[1:]],
                                         stderr=subprocess.DEVNULL, text=True)
        lines = result.splitlines()
        self.assertEqual([line for line in lines if line.startswith('identityfile ')],
                         ['identityfile ' + str(folder / 'id_ed25519')])
        effective = dict(line.split(' ', 1) for line in lines)
        for name, value in dict(user='ragspark-pair', hostname=PAIR['node_ip'],
                                bindaddress=PAIR['head_ip'], identityfile=str(folder / 'id_ed25519'),
                                identityagent='none', identitiesonly='yes', batchmode='yes',
                                stricthostkeychecking='true', hostkeyalgorithms='ssh-ed25519',
                                globalknownhostsfile='/dev/null',
                                userknownhostsfile=str(folder / 'known_hosts'),
                                updatehostkeys='false', forwardagent='no').items():
            with self.subTest(option=name):
                self.assertEqual(effective[name], value)


class PairStatus(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        peer = self.folder / 'peer.json'
        peer.write_bytes(r.canonical(PAIR))
        peer.chmod(0o600)
        self.hosts = self.folder / 'known_hosts'
        self.hosts.write_text(pair.known_host(PAIR['node_ip'], KEY))
        self.hosts.chmod(0o600)
        self.key = self.folder / 'id_ed25519'
        self.key.touch(mode=0o600)

    def test_valid_reply_is_bound_to_local_configuration(self):
        for version in ('e09da62', '0.25.2'):
            expected = dict(STATUS, version=version)
            with patch.object(pair, '_read_ssh', return_value=r.canonical(expected)) as read:
                self.assertEqual(pair.query_head(self.folder), expected)
                read.assert_called_once_with(pair.ssh_args(PAIR, self.folder))

    def test_bad_pins_modes_and_links_are_rejected_before_ssh(self):
        with patch.object(pair, '_read_ssh') as read:
            self.hosts.write_text(pair.known_host(PAIR['head_ip'], KEY))
            with self.assertRaisesRegex(r.InvalidRelease, 'pinned'):
                pair.query_head(self.folder)
            self.hosts.write_text(pair.known_host(PAIR['node_ip'], KEY) * 2)
            with self.assertRaises(r.InvalidRelease):
                pair.query_head(self.folder)
            self.hosts.write_text(pair.known_host(PAIR['node_ip'], KEY))
            self.key.chmod(0o644)
            with self.assertRaisesRegex(r.InvalidRelease, '0600'):
                pair.query_head(self.folder)
            self.key.unlink()
            self.key.symlink_to(self.hosts)
            with self.assertRaises(r.InvalidRelease):
                pair.query_head(self.folder)
            read.assert_not_called()

    def test_incompatible_or_malformed_status_is_refused(self):
        for wrong in (dict(STATUS, protocol=2), dict(STATUS, protocol=True),
                      dict(STATUS, role='head'), dict(STATUS, pair=dict(PAIR, id='b' * 32)),
                      dict(STATUS, pair=dict(PAIR, node_ip='192.168.100.12')),
                      dict(STATUS, version='latest'), dict(STATUS, version='01.2.3'),
                      dict(STATUS, unit_state='complete'), dict(STATUS, extra='unknown')):
            with self.subTest(wrong=wrong), patch.object(pair, '_read_ssh', return_value=r.canonical(wrong)):
                with self.assertRaises(r.InvalidRelease):
                    pair.query_head(self.folder)

    def test_receiver_demands_exact_command_and_both_cable_endpoints(self):
        with patch.object(pair, '_read_public_node_file', return_value=r.canonical(PAIR)) as read:
            for command in ('', 'id', 'status ', 'status; id', 'status /tmp/file'):
                with self.subTest(command=command), self.assertRaisesRegex(r.InvalidRelease, 'command'):
                    pair.node_status(command, CONNECTION)
            read.assert_not_called()
            for connection in ('', CONNECTION.replace(PAIR['head_ip'], '192.168.31.243'),
                               CONNECTION.replace(PAIR['node_ip'], '192.168.31.190')):
                with self.subTest(connection=connection), self.assertRaisesRegex(r.InvalidRelease, 'cable'):
                    pair.node_status('status', connection)
            self.assertEqual(read.call_count, 3)
            read.assert_called_with(pair.NODE_BINDING)

    def test_forced_entrypoint_refuses_an_unrelated_command(self):
        entrypoint = Path(__file__).resolve().parents[1] / 'installer' / 'pair_status.py'
        result = subprocess.run([sys.executable, '-I', '-B', str(entrypoint)],
                                env=dict(os.environ, SSH_ORIGINAL_COMMAND='echo sensitive',
                                         SSH_CONNECTION=CONNECTION),
                                capture_output=True, timeout=5, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b'')
        self.assertNotIn(b'sensitive', result.stderr)

    def test_receiver_checks_only_public_files_and_one_systemd_unit(self):
        def read(path):
            return {pair.NODE_BINDING: r.canonical(PAIR), pair.NODE_VERSION: b'e09da62\n'}[path]

        with patch.object(pair, '_read_public_node_file', side_effect=read), \
                patch.object(pair.subprocess, 'run') as run:
            run.return_value = subprocess.CompletedProcess([], 0, b'active\n')
            self.assertEqual(r.parse(pair.node_status('status', CONNECTION)), STATUS)
            run.assert_called_once()
            self.assertEqual(run.call_args.args[0], ['/usr/bin/systemctl', 'show',
                             '--property=ActiveState', '--value', 'ragspark.service'])
            run.return_value = subprocess.CompletedProcess([], 1, b'private output')
            expected = dict(STATUS, unit_state='unknown')
            self.assertEqual(r.parse(pair.node_status('status', CONNECTION)), expected)

    def test_receiver_refuses_user_owned_or_writable_status_source(self):
        self.hosts.chmod(0o666)
        with self.assertRaisesRegex(r.InvalidRelease, 'unsafe node status file'):
            pair._read_public_node_file(self.hosts)
        self.hosts.chmod(0o600)
        if os.getuid() != 0:
            with self.assertRaisesRegex(r.InvalidRelease, 'unsafe node status file'):
                pair._read_public_node_file(self.hosts)


class PairSSHReply(unittest.TestCase):
    def run_child(self, code):
        return pair._read_ssh([sys.executable, '-I', '-B', '-c', code])

    def test_reads_bounded_stdout_and_drops_stderr(self):
        self.assertEqual(self.run_child("import sys; sys.stderr.write('private output'); print('ok')"), b'ok\n')

    def test_stderr_is_not_in_diagnostic(self):
        with self.assertRaisesRegex(pair.PairUnavailable, '^pair SSH failed;'):
            self.run_child("import sys; sys.stderr.write('private output'); sys.exit(255)")

    def test_oversized_reply_is_rejected_without_waiting_for_eof(self):
        with self.assertRaisesRegex(pair.PairUnavailable, 'size limit'):
            self.run_child(f"import os,time; os.write(1,b'x'*{pair.MAX_REPLY + 1}); time.sleep(10)")

    def test_partial_reply_and_hanging_exit_time_out(self):
        with patch.object(pair, 'SSH_TIMEOUT', 0.3):
            for code in ("import os,time; os.write(1,b'partial'); time.sleep(10)",
                         "import os,time; os.close(1); time.sleep(10)"):
                with self.subTest(code=code), self.assertRaisesRegex(pair.PairUnavailable, 'timed out'):
                    self.run_child(code)


if __name__ == '__main__':
    unittest.main()
