"""Real archives and real rsync/rrsync in scratch dirs; no network or Docker stack changes."""
import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from test_backup import Store
from rsinst import backup, backup_remote as remote, backup_setup, cli, paths


class Transfer(Store):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(remote, "VAULT", "/srv/node-backups")
        p.start()
        self.addCleanup(p.stop)
        self.incoming, self.vault = paths.host(remote.INCOMING), paths.host(remote.VAULT)
        self.base = paths.host(remote.BASE)
        for d in (self.incoming, self.vault, self.base + "/receipts"):
            os.makedirs(d, mode=0o700)
        _, self.copy = self.take()
        self.name = os.path.basename(self.copy)
        self.digest = backup.sha256(os.path.join(self.copy, "backup.json"))
        self.c = {"peer": "192.168.100.11", "source": "192.168.100.10", "key": "/key", "known_hosts": "/hosts"}

    def stage(self):
        dest = os.path.join(self.incoming, self.name)
        shutil.copytree(self.copy, dest)
        return dest

    def test_partial_upload_is_not_published(self):
        dest = self.stage()
        os.unlink(os.path.join(dest, "backup.json"))
        self.assertEqual(remote.receive(), 0)
        self.assertEqual(os.listdir(self.vault), [])

    def test_verified_upload_gets_root_vault_and_receipt(self):
        self.stage()
        self.assertEqual(remote.receive(), 0)
        final = os.path.join(self.vault, self.name)
        with open(os.path.join(final, "backup.json")) as f:
            self.assertIsNone(backup.verify(final, json.load(f)))
        self.assertFalse(os.path.exists(os.path.join(self.incoming, self.name)))
        receipt = os.path.join(self.base, "receipts", self.name + ".json")
        self.assertEqual(json.loads(Path(receipt).read_text())["sha256"], self.digest)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(remote.dispatch("receipt " + self.name), 0)
        self.assertEqual(json.loads(out.getvalue())["sha256"], self.digest)

    def test_node_can_list_received_copies_without_making_one(self):
        self.stage()
        self.assertEqual(remote.receive(), 0)
        Path(self.base, "receiver.json").write_text("{}")
        paths.set_role("node")
        args = cli.build_parser().parse_args(["backup", "--list"])
        out = io.StringIO()
        calls = list(self.calls)
        with contextlib.redirect_stdout(out):
            self.assertEqual(backup.backup(args), 0)
        self.assertIn(self.name, out.getvalue())
        self.assertEqual(calls, self.calls)

    def test_receiver_prunes_its_own_history_and_receipts(self):
        import datetime
        day = datetime.datetime(2026, 1, 1)
        names = ["ragspark-" + (day + datetime.timedelta(days=i)).strftime("%Y%m%d-%H%M%S") for i in range(35)]
        names.append("ragspark-20250101-000000-before-restore")
        for name in names:
            os.mkdir(os.path.join(self.vault, name))
            Path(self.vault, name, "backup.json").write_text('{"format": 1}')
            Path(self.base, "receipts", name + ".json").write_text("{}")
        self.assertEqual(remote.receive(), 0)
        self.assertEqual(set(os.listdir(self.vault)), backup.to_keep(names))
        self.assertEqual({n[:-5] for n in os.listdir(self.base + "/receipts")}, backup.to_keep(names))

    def test_corrupt_upload_is_not_published(self):
        dest = self.stage()
        with open(os.path.join(dest, "env"), "a") as f:
            f.write("broken")
        self.assertEqual(remote.receive(), 1)
        self.assertEqual(os.listdir(self.vault), [])
        self.assertEqual(os.listdir(self.base + "/receipts"), [])

    def test_existing_vault_cannot_be_overwritten(self):
        self.stage()
        self.assertEqual(remote.receive(), 0)
        target = os.path.join(self.vault, self.name, "backup.json")
        before = Path(target).read_bytes()
        dest = self.stage()
        info = json.loads(Path(dest, "backup.json").read_text())
        info["host"] = "different"
        Path(dest, "backup.json").write_text(json.dumps(info))
        self.assertEqual(remote.receive(), 1)
        self.assertEqual(Path(target).read_bytes(), before)

    def test_identical_retry_is_idempotent(self):
        self.stage()
        self.assertEqual(remote.receive(), 0)
        self.stage()
        self.assertEqual(remote.receive(), 0)
        self.assertEqual(os.listdir(self.vault), [self.name])

    def test_root_snapshot_does_not_follow_symlinks(self):
        dest = self.stage()
        os.unlink(os.path.join(dest, "env"))
        os.symlink("/etc/passwd", os.path.join(dest, "env"))
        self.assertEqual(remote.receive(), 1)
        self.assertEqual(os.listdir(self.vault), [])

    def test_root_snapshot_rejects_directory_symlinks(self):
        os.symlink(self.copy, os.path.join(self.incoming, self.name))
        self.assertEqual(remote.receive(), 1)
        self.assertEqual(os.listdir(self.vault), [])

    def test_remote_commands_are_allowlisted(self):
        for command in ("", "bash", "receipt ../../etc/passwd", "receipt " + self.name + "; id",
                        "receipt ragspark-20269999-000000", "cat /etc/shadow"):
            self.assertEqual(remote.dispatch(command), 2)
        with mock.patch.object(remote.os, "execv") as call, mock.patch.object(remote.os, "umask"):
            remote.dispatch("rsync --server --sender . /")
            self.assertEqual(call.call_args.args[1][1:3], ["-wo", "-no-del"])

    def test_receipt_requires_expected_digest(self):
        r = {"name": self.name, "sha256": "0" * 64}
        with mock.patch.object(remote, "run", return_value=(0, json.dumps(r))):
            self.assertFalse(remote.receipt(self.c, self.name, self.digest))

    def test_lost_transfer_keeps_local_copy_and_retry_is_independent(self):
        with mock.patch.object(remote, "config", return_value=self.c), \
             mock.patch.object(remote, "receipt", return_value=False), \
             mock.patch.object(remote, "_upload", return_value=False):
            self.assertFalse(remote.sync(self.root, wait_seconds=0))
        self.assertTrue(os.path.isfile(os.path.join(self.copy, "backup.json")))
        calls = list(self.calls)
        with mock.patch.object(remote, "config", return_value=self.c), \
             mock.patch.object(remote, "receipt", side_effect=[False, True]), \
             mock.patch.object(remote, "_upload", return_value=True):
            self.assertTrue(remote.sync(self.root, wait_seconds=0))
        self.assertEqual(self.calls, calls)       # no extra Docker stop/start on sync

    def test_upload_without_verified_receipt_is_not_success(self):
        with mock.patch.object(remote, "config", return_value=self.c), \
             mock.patch.object(remote, "receipt", return_value=False), \
             mock.patch.object(remote, "_upload", return_value=True):
            self.assertFalse(remote.sync(self.root, wait_seconds=0))

    def test_upload_uses_checksums_and_sends_manifest_last(self):
        calls = []
        with mock.patch.object(remote, "run", side_effect=lambda cmd, **kw: (calls.append(cmd), (0, ""))[1]):
            self.assertTrue(remote._upload(self.c, self.copy, self.name))
        self.assertIn("--checksum", calls[0])
        self.assertIn("--exclude=backup.json", calls[0])
        self.assertEqual(calls[1][-2], os.path.join(self.copy, "backup.json"))
        self.assertNotIn("--delete", " ".join(" ".join(c) for c in calls))

    def test_sync_requires_configuration_when_requested_explicitly(self):
        with mock.patch.object(remote, "config", return_value=None):
            self.assertTrue(remote.sync(self.root))
            self.assertFalse(remote.sync(self.root, required=True))

    @unittest.skipUnless(shutil.which("rrsync") and shutil.which("rsync"), "real rsync/rrsync required")
    def test_real_rrsync_transfer_and_restrictions(self):
        fake_ssh = os.path.join(self.tmp, "transport.py")
        wrapper = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools", "backup_receive.py")
        Path(fake_ssh).write_text("#!/usr/bin/env python3\nimport os, sys, shlex\nos.environ['SSH_ORIGINAL_COMMAND'] = shlex.join(sys.argv[2:])\nos.execv(sys.executable, [sys.executable, '-I', %r])\n" % wrapper)
        os.chmod(fake_ssh, 0o700)
        env = dict(os.environ, RAGSPARK_PREFIX=self.tmp)
        dest = "dummy:" + self.name + "/"
        first = ["rsync", "-rt", "--exclude=backup.json", "-e", fake_ssh, self.copy + "/", dest]
        p = subprocess.run(first, env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(remote.receive(), 0)
        self.assertEqual(os.listdir(self.vault), [])
        second = ["rsync", "-rt", "-e", fake_ssh, os.path.join(self.copy, "backup.json"), dest]
        p = subprocess.run(second, env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(remote.receive(), 0)
        for args in (["--delete", self.copy + "/", dest], [dest, self.tmp + "/forbidden/"]):
            p = subprocess.run(["rsync", "-rt", "-e", fake_ssh] + args, env=env,
                               capture_output=True, text=True, timeout=20)
            self.assertNotEqual(p.returncode, 0)


class Parser(unittest.TestCase):
    def test_sync_and_receive_are_exclusive(self):
        self.assertTrue(cli.build_parser().parse_args(["backup", "--sync"]).sync)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["backup", "--sync", "--receive"])

class Setup(Store):
    def test_sender_key_stays_private_and_pins_provided_host_key(self):
        from rsinst.manifest import Manifest
        hostkeys = os.path.join(self.tmp, "peer-known-hosts")
        Path(hostkeys).write_text("192.168.100.11 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITest\n")
        def keygen(cmd, **kw):
            key = cmd[-1]
            Path(key).write_text("test-only-private-key")
            Path(key + ".pub").write_text("ssh-ed25519 AAAATest sender\n")
            return 0, ""
        with mock.patch.object(backup_setup, "run", side_effect=keygen), \
             mock.patch.object(backup_setup, "_timers"), contextlib.redirect_stdout(io.StringIO()):
            backup_setup.sender(Manifest.load(), "192.168.100.11", "192.168.100.10", hostkeys)
        c = remote.config()
        self.assertEqual(os.stat(c["key"]).st_mode & 0o777, 0o600)
        self.assertEqual(Path(c["known_hosts"]).read_text(), Path(hostkeys).read_text())
        args = remote.ssh_args(c)
        self.assertIn("StrictHostKeyChecking=yes", args)
        self.assertIn("192.168.100.10", args)

    def test_unverified_or_wrong_host_key_file_is_rejected(self):
        from rsinst.manifest import Manifest
        hostkeys = os.path.join(self.tmp, "peer-known-hosts")
        Path(hostkeys).write_text("192.168.31.190 ssh-ed25519 AAAATest\n")
        with self.assertRaises(ValueError):
            backup_setup.sender(Manifest.load(), "192.168.100.11", "192.168.100.10", hostkeys)
        self.assertFalse(os.path.exists(remote.config_path()))

    def test_receiver_sets_forced_command_and_root_managed_authorized_keys(self):
        from types import SimpleNamespace
        from rsinst.manifest import Manifest
        public = os.path.join(self.tmp, "sender.pub")
        Path(public).write_text("ssh-ed25519 AAAATest sender\n")
        home = paths.host(remote.BASE + "/receiver")
        user = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid(), pw_dir=home, pw_shell="/bin/sh")
        paths.set_role("node")
        os.makedirs(paths.host("/srv"), exist_ok=True)
        old_umask = os.umask(0o077)
        self.addCleanup(os.umask, old_umask)
        os.makedirs(paths.compose_dir(), exist_ok=True)
        Manifest.load().save()
        with mock.patch.object(backup_setup.pwd, "getpwnam", side_effect=[KeyError(), user, user]), \
             mock.patch.object(backup_setup.os, "getgrouplist", return_value=[os.getgid()]), \
             mock.patch.object(backup_setup, "run", return_value=(0, "")), \
             mock.patch.object(backup_setup, "_timers"):
            backup_setup.receiver(Manifest.load(), "192.168.100.10", public)
            backup_setup.receiver(Manifest.load(), "192.168.100.10", public)   # repeat setup
        auth = Path(home, ".ssh", "authorized_keys").read_text()
        self.assertIn('restrict,from="192.168.100.10",command="/usr/bin/python3 -I ', auth)
        self.assertEqual(os.stat(paths.host(remote.VAULT)).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(home).st_uid, os.geteuid())
        parent = os.path.dirname(paths.host(remote.INCOMING))
        self.assertEqual(os.stat(parent).st_mode & 0o777, 0o701)
        self.assertEqual(os.stat(paths.host(remote.INCOMING)).st_mode & 0o777, 0o700)

    def test_incoming_parent_preserves_existing_safe_permissions(self):
        parent = Path(self.tmp, "parent")
        parent.mkdir(mode=0o755)
        for mode in (0o700, 0o701, 0o750, 0o755):
            parent.chmod(mode)
            backup_setup._incoming_parent(str(parent))
            self.assertEqual(parent.stat().st_mode & 0o777, mode | 0o001)

    def test_incoming_parent_rejects_unsafe_paths_unchanged(self):
        parent = Path(self.tmp, "parent")
        parent.mkdir()
        for mode in (0o770, 0o707):
            parent.chmod(mode)
            with self.assertRaises(ValueError):
                backup_setup._incoming_parent(str(parent))
            self.assertEqual(parent.stat().st_mode & 0o777, mode)
        parent.chmod(0o700)
        link = Path(self.tmp, "link")
        link.symlink_to(parent)
        with self.assertRaises(ValueError):
            backup_setup._incoming_parent(str(link))
        self.assertEqual(parent.stat().st_mode & 0o777, 0o700)
        with mock.patch.object(backup_setup.os, "geteuid", return_value=os.geteuid() + 1):
            with self.assertRaises(ValueError):
                backup_setup._incoming_parent(str(parent))
        self.assertEqual(parent.stat().st_mode & 0o777, 0o700)

    def test_remote_timer_commands_have_no_stack_restart(self):
        from rsinst.manifest import Manifest
        ran = []
        with mock.patch.object(backup_setup, "run", side_effect=lambda c, **kw: (ran.append(c), (0, ""))[1]):
            backup_setup._timers(Manifest.load(), "sync")
        unit = Path(paths.host("/etc/systemd/system/" + remote.SYNC_SERVICE)).read_text()
        self.assertIn("backup --sync", unit)
        self.assertNotIn("daemon-reload", str(ran))


if __name__ == "__main__":
    unittest.main()
