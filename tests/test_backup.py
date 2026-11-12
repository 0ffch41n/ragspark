"""`ragspark backup` and `ragspark restore` (stage 6): real tar on scratch directories, Docker faked."""
import argparse
import datetime
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import io
import tarfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "installer"))

from rsinst import backup, cli, paths, ui  # noqa: E402
from rsinst.manifest import Manifest  # noqa: E402

REAL_RUN = backup.run

ENV = ("COMPOSE_FILE=compose.yaml:llm/qwen-3.8-27b-nvfp4.yaml\nRAGSPARK_HTTP_PORT=80\n"
       "DEFAULT_SUPERUSER_EMAIL=admin@ragspark.local\nADMIN_DEFAULT_PASSWORD=a1\nRAGFLOW_SECRET_KEY=k1\n"
       "ELASTIC_PASSWORD=e1\nMYSQL_PASSWORD=m1\nMYSQL_DBNAME=rag_flow\nMINIO_USER=rag_flow\nMINIO_PASSWORD=o1\nREDIS_PASSWORD=r1\n")


def tree(path):
    """{relative path: (mode, sha256 or 'dir')} of a directory."""
    out = {}
    for base, dirs, files in os.walk(path):
        for d in dirs:
            p = os.path.join(base, d)
            out[os.path.relpath(p, path)] = (stat.S_IMODE(os.lstat(p).st_mode), "dir")
        for f in files:
            p = os.path.join(base, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, path)] = (stat.S_IMODE(os.lstat(p).st_mode), hashlib.sha256(fh.read()).hexdigest())
    return out


class Store(unittest.TestCase):
    """An installed head with four volume directories; docker compose recorded, not run."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        p = mock.patch.object(paths, "PREFIX", self.tmp)
        p.start()
        self.addCleanup(p.stop)
        paths.set_role("head")
        self.addCleanup(paths.set_role, "head")
        ui.set_yes(True)
        self.addCleanup(ui.set_yes, False)
        compose_dir = paths.compose_dir()
        os.makedirs(compose_dir)
        with open(os.path.join(compose_dir, ".env"), "w") as f:
            f.write(ENV)
        os.chmod(os.path.join(compose_dir, ".env"), 0o600)
        with open(os.path.join(paths.install_dir(), "VERSION"), "w") as f:
            f.write("abc1234\n")
        m = Manifest()
        m.data["status"] = "installed"
        m.config.update(llm="qwen-3.8-27b-nvfp4", lang="en")
        m.save()
        self.vols = {}
        for v in backup.VOLUMES:
            d = os.path.join(self.tmp, "docker", v, "_data")
            os.makedirs(os.path.join(d, "sub"))
            with open(os.path.join(d, "a.db"), "w") as f:
                f.write("data of %s\n" % v)
            with open(os.path.join(d, "sub", "b.bin"), "wb") as f:
                f.write(os.urandom(4096))
            os.chmod(os.path.join(d, "sub", "b.bin"), 0o640)
            self.vols[v] = d
        self.calls = []
        self.parsing = iter([0] * 100)
        patches = [
            mock.patch.object(backup, "mountpoint", side_effect=lambda v: self.vols.get(v)),
            mock.patch.object(backup, "states", side_effect=lambda svcs: {s: ("running", "healthy") for s in svcs}),
            mock.patch.object(backup, "compose", side_effect=self.compose),
            mock.patch.object(backup, "ready", return_value=True),
            mock.patch.object(backup, "ragflow_image", return_value="ragflow:r7"),
            mock.patch.object(backup, "images", return_value={s: s + ":pinned" for s in (backup.WEB,) + backup.DATA}),
            mock.patch.object(backup, "run", side_effect=self.run_cmd),
            mock.patch.object(backup, "_unit_starting", return_value=False),
            mock.patch.object(backup, "parsing", side_effect=lambda: next(self.parsing)),
            mock.patch.object(backup, "POLL_S", 0),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.root = os.path.join(self.tmp, "srv", "ragspark", "backups")

    def run_cmd(self, cmd, **kw):
        if cmd[0] in ("docker", "systemctl"):
            return 0, ""
        return REAL_RUN(cmd, **kw)

    def compose(self, args, **kw):
        self.calls.append(" ".join(args))
        return 0, ""

    def take(self, wait=0):
        return backup.take(Manifest.load(), self.root, wait)


class Retention(unittest.TestCase):
    def test_seven_daily_four_weekly_and_every_safety_copy(self):
        day = datetime.datetime(2026, 9, 1, 3, 30)
        names = ["ragspark-" + (day + datetime.timedelta(days=i)).strftime("%Y%m%d-%H%M%S") for i in range(60)]
        names.append("ragspark-20260815-120000-before-restore")
        keep = backup.to_keep(names)
        regular = sorted((n for n in keep if not backup.is_safety(n)), reverse=True)
        newest = sorted((n for n in names if not backup.is_safety(n)), reverse=True)
        self.assertEqual(regular[:7], newest[:7])
        weeks = {backup.stamp(n).isocalendar()[:2] for n in regular}
        latest_weeks = sorted({backup.stamp(n).isocalendar()[:2] for n in newest}, reverse=True)[:4]
        self.assertEqual(weeks, set(latest_weeks))
        self.assertLessEqual(len(regular), 7 + 4)
        self.assertIn("ragspark-20260815-120000-before-restore", keep)
        self.assertEqual(backup.to_keep(["notes.txt", "ragspark-x"]), set())


class Backup(Store):
    def test_a_complete_copy_with_ragflow_stopped_first_and_started_last(self):
        code, path = self.take()
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, ["stop ragflow", "stop redis mysql minio es01",
                                      "start redis mysql minio es01", "start ragflow"])
        with open(os.path.join(path, "backup.json")) as f:
            info = json.load(f)
        self.assertIsNone(backup.verify(path, info))
        self.assertEqual(sorted(info["volumes"]), sorted(backup.VOLUMES))
        self.assertEqual((info["release"], info["llm"], info["ragflow_image"]), ("abc1234", "qwen-3.8-27b-nvfp4", "ragflow:r7"))
        self.assertIn("downtime_s", info)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(path, "env")).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.root).st_mode), 0o700)
        self.assertEqual([r[0] for r in backup.copies(self.root)], [os.path.basename(path)])

    def test_a_failed_copy_starts_ragflow_again_and_leaves_nothing(self):
        real = backup.archive

        def failing(src, dest, options):
            if "minio" in dest:
                return 2, "tar: disk full"
            return real(src, dest, options)
        with mock.patch.object(backup, "archive", side_effect=failing):
            code, path = self.take()
        self.assertEqual((code, path), (1, None))
        self.assertEqual(self.calls[-2:], ["start redis mysql minio es01", "start ragflow"])
        self.assertEqual(os.listdir(self.root), [])

    def test_waits_while_documents_are_parsed(self):
        self.parsing = iter([2, 1, 0, 0])
        code, _ = self.take(wait=1)
        self.assertEqual(code, 0)

    def test_still_parsing_means_no_copy_and_nothing_stopped(self):
        self.parsing = iter([3] * 100)
        code, path = self.take(wait=0)
        self.assertEqual((code, path), (1, None))
        self.assertEqual(self.calls, [])

    def test_a_stopped_stack_is_copied_and_left_stopped(self):
        with mock.patch.object(backup, "states", return_value={}):
            code, _ = self.take()
        self.assertEqual((code, self.calls), (0, []))

    def test_old_copies_are_pruned_and_unfinished_ones_removed(self):
        os.makedirs(self.root)
        day = datetime.datetime.now() - datetime.timedelta(days=90)
        for i in range(30):
            d = os.path.join(self.root, "ragspark-" + (day + datetime.timedelta(days=i)).strftime("%Y%m%d-%H%M%S"))
            os.makedirs(d)
            with open(os.path.join(d, "backup.json"), "w") as f:
                json.dump({"format": 1, "volumes": {}}, f)
        os.makedirs(os.path.join(self.root, "ragspark-20250101-000000-before-restore"))
        with open(os.path.join(self.root, "ragspark-20250101-000000-before-restore", "backup.json"), "w") as f:
            json.dump({"format": 1}, f)
        os.makedirs(os.path.join(self.root, ".incomplete-ragspark-20250102-000000"))
        code, _ = self.take()
        self.assertEqual(code, 0)
        left = sorted(os.listdir(self.root))
        self.assertNotIn(".incomplete-ragspark-20250102-000000", left)
        self.assertIn("ragspark-20250101-000000-before-restore", left)
        self.assertLessEqual(len([n for n in left if backup.stamp(n) and not backup.is_safety(n)]), 7 + 4)

    def test_a_second_run_waits_for_nobody(self):
        os.makedirs(self.root)
        lock = backup._lock(self.root)
        self.addCleanup(lock.close)
        code, path = self.take()
        self.assertEqual((code, path, self.calls), (1, None, []))


class Restore(Store):
    def restore(self, name):
        return backup.restore(argparse.Namespace(name=name, dir=self.root))

    def test_round_trip_with_the_previous_state_saved(self):
        _, path = self.take()
        saved = {v: tree(d) for v, d in self.vols.items()}
        with open(os.path.join(self.vols["mysql"], "a.db"), "w") as f:
            f.write("changed after the copy\n")
        os.remove(os.path.join(self.vols["minio"], "sub", "b.bin"))
        with open(os.path.join(self.vols["esdata"], "new.seg"), "w") as f:
            f.write("new\n")
        changed = {v: tree(d) for v, d in self.vols.items()}
        env_path = os.path.join(paths.compose_dir(), ".env")
        with open(env_path, "w") as f:
            f.write(ENV.replace("MYSQL_PASSWORD=m1", "MYSQL_PASSWORD=m2"))
        self.calls.clear()

        self.assertEqual(self.restore(os.path.basename(path)), 0)
        self.assertEqual({v: tree(d) for v, d in self.vols.items()}, saved)
        with open(env_path) as f:
            self.assertIn("MYSQL_PASSWORD=m1\n", f.read())
        # the passwords changed, so the services are created again with .env, not merely started
        self.assertEqual(self.calls, ["stop ragflow", "stop redis mysql minio es01",
                                      "up -d --no-deps redis mysql minio es01", "up -d --no-deps ragflow"])
        safety = [r for r in backup.copies(self.root) if backup.is_safety(r[0])]
        self.assertEqual(len(safety), 1)
        restored_safety = os.path.join(self.tmp, "check")
        os.makedirs(restored_safety)
        backup.extract(os.path.join(safety[0][1], safety[0][2]["volumes"]["mysql"]["file"]), restored_safety)
        self.assertEqual(tree(restored_safety), changed["mysql"])

    def test_same_passwords_only_start_the_services(self):
        _, path = self.take()
        self.calls.clear()
        self.assertEqual(self.restore(path), 0)                          # a path works as well as a name
        self.assertEqual(self.calls[2:], ["start redis mysql minio es01", "start ragflow"])

    def test_a_damaged_copy_changes_nothing(self):
        _, path = self.take()
        info = json.load(open(os.path.join(path, "backup.json")))
        with open(os.path.join(path, info["volumes"]["esdata"]["file"]), "ab") as f:
            f.write(b"x")
        before = {v: tree(d) for v, d in self.vols.items()}
        self.calls.clear()
        self.assertEqual(self.restore(os.path.basename(path)), 1)
        self.assertEqual(self.calls, [])
        self.assertEqual({v: tree(d) for v, d in self.vols.items()}, before)

    def test_a_failed_unpack_puts_the_previous_state_back(self):
        _, path = self.take()
        with open(os.path.join(self.vols["mysql"], "a.db"), "w") as f:
            f.write("current state\n")
        before = {v: tree(d) for v, d in self.vols.items()}
        real, seen = backup.extract, []

        def flaky(src, directory):
            seen.append(src)
            if len(seen) == 2:                                           # the second volume of the copy
                backup.clear(directory)
                return 2, "tar: unexpected end of file"
            return real(src, directory)
        with mock.patch.object(backup, "extract", side_effect=flaky):
            self.assertEqual(self.restore(os.path.basename(path)), 1)
        self.assertEqual({v: tree(d) for v, d in self.vols.items()}, before)
        self.assertEqual(self.calls[-2:], ["up -d --no-deps redis mysql minio es01", "up -d --no-deps ragflow"])

    def test_not_a_copy(self):
        self.assertEqual(self.restore("ragspark-20990101-000000"), 1)
        self.assertEqual(self.calls, [])


class FailureCases(Store):
    def restore(self, name):
        return backup.restore(argparse.Namespace(name=name, dir=self.root))

    def test_mysql_runtime_socket_is_omitted_and_hard_links_are_copied(self):
        os.symlink("/var/run/mysqld/mysqld.sock", os.path.join(self.vols["mysql"], "mysql.sock"))
        os.link(os.path.join(self.vols["esdata"], "a.db"), os.path.join(self.vols["esdata"], "hard.db"))
        code, path = self.take()
        self.assertEqual(code, 0)
        with open(os.path.join(path, "backup.json")) as stream:
            info = json.load(stream)
        self.assertIsNone(backup.verify(path, info))
        dest = os.path.join(self.tmp, "inspect")
        os.makedirs(dest)
        backup.extract(os.path.join(path, info["volumes"]["mysql"]["file"]), dest)
        self.assertFalse(os.path.lexists(os.path.join(dest, "mysql.sock")))
        backup.extract(os.path.join(path, info["volumes"]["esdata"]["file"]), dest)
        self.assertEqual(backup.sha256(os.path.join(dest, "a.db")), backup.sha256(os.path.join(dest, "hard.db")))

    def test_cannot_query_parsing_means_nothing_stopped(self):
        self.parsing = iter([None])
        code, path = self.take()
        self.assertEqual((code, path, self.calls), (1, None, []))

    def test_parsing_race_restarts_web_without_a_copy(self):
        self.parsing = iter([0, 1])
        code, path = self.take()
        self.assertEqual((code, path), (1, None))
        self.assertNotIn("stop redis mysql minio es01", self.calls)
        self.assertEqual(self.calls[-1], "start ragflow")

    def test_ctrl_c_during_archive_restarts_services(self):
        with mock.patch.object(backup, "archive", side_effect=KeyboardInterrupt):
            code, path = self.take()
        self.assertEqual((code, path), (1, None))
        self.assertEqual(self.calls[-1], "start ragflow")

    def test_live_volume_is_never_archived(self):
        def busy(cmd, **kw):
            return (0, "container-id") if cmd[:2] == ["docker", "ps"] else self.run_cmd(cmd, **kw)
        with mock.patch.object(backup, "run", side_effect=busy), mock.patch.object(backup, "archive") as archive:
            code, _ = self.take()
        self.assertEqual(code, 1)
        archive.assert_not_called()

    def test_all_directories_share_a_lock(self):
        os.makedirs(self.root)
        lock = backup._lock(self.root)
        self.addCleanup(lock.close)
        code, path = backup.take(Manifest.load(), self.root + "-other", 0)
        self.assertEqual((code, path, self.calls), (1, None, []))

    def test_missing_volume_in_manifest_is_rejected_before_stop(self):
        _, path = self.take()
        f = os.path.join(path, "backup.json")
        with open(f) as stream:
            info = json.load(stream)
        del info["volumes"]["mysql"]
        with open(f, "w") as stream:
            json.dump(info, stream)
        self.calls.clear()
        self.assertEqual(self.restore(path), 1)
        self.assertEqual(self.calls, [])

    def test_traversal_and_links_rejected_even_with_matching_checksums(self):
        _, path = self.take()
        f = os.path.join(path, "backup.json")
        with open(f) as stream:
            info = json.load(stream)
        for name, link in (("../outside", False), ("/etc/outside", False), ("link", True)):
            dest = os.path.join(path, "mysql.tar.gz")
            with tarfile.open(dest, "w:gz") as tf:
                member = tarfile.TarInfo(name)
                if link:
                    member.type = tarfile.SYMTYPE
                    member.linkname = "/etc"
                    tf.addfile(member)
                else:
                    member.size = 3
                    tf.addfile(member, io.BytesIO(b"bad"))
            info["volumes"]["mysql"].update(file="mysql.tar.gz", sha256=backup.sha256(dest))
            with open(f, "w") as stream:
                json.dump(info, stream)
            self.calls.clear()
            self.assertEqual(self.restore(path), 1)
            self.assertEqual(self.calls, [])

    def test_incompatible_images_are_rejected_before_stop(self):
        _, path = self.take()
        self.calls.clear()
        with mock.patch.object(backup, "images", return_value={"mysql": "different"}):
            self.assertEqual(self.restore(path), 1)
        self.assertEqual(self.calls, [])

    def test_failed_restored_health_check_rolls_back(self):
        _, path = self.take()
        with open(os.path.join(self.vols["mysql"], "a.db"), "w") as f:
            f.write("current data")
        before = {v: tree(d) for v, d in self.vols.items()}
        with mock.patch.object(backup, "ready", side_effect=[False, True, True]):
            self.assertEqual(self.restore(path), 1)
        self.assertEqual({v: tree(d) for v, d in self.vols.items()}, before)

    def test_failed_rollback_leaves_services_stopped(self):
        _, path = self.take()
        self.calls.clear()
        with mock.patch.object(backup, "extract", return_value=(2, "read error")):
            self.assertEqual(self.restore(path), 1)
        self.assertFalse(any(c.startswith(("start ", "up ")) for c in self.calls))

    def test_invalid_dates_are_not_copies(self):
        self.assertIsNone(backup.stamp("ragspark-20269999-999999"))

    def test_multiple_copies_per_day_keep_seven_distinct_days(self):
        names = ["ragspark-202611%02d-%02d0000" % (day, hour) for day in range(1, 13) for hour in range(10)]
        keep = backup.to_keep(names)
        self.assertTrue(all("ragspark-202611%02d-090000" % d in keep for d in range(6, 13)))



class Commands(Store):
    def test_list_and_parser(self):
        self.take()
        args = cli.build_parser().parse_args(["backup", "--list", "--dir", self.root])
        self.assertEqual(backup.backup(args), 0)
        args = cli.build_parser().parse_args(["restore", "ragspark-20260101-000000"])
        self.assertEqual(args.name, "ragspark-20260101-000000")
        self.assertIn("backup", cli.COMMANDS)
        self.assertIn("restore", cli.COMMANDS)

    def test_the_node_keeps_no_data(self):
        paths.set_role("node")
        args = cli.build_parser().parse_args(["backup"])
        self.assertEqual(backup.backup(args), 1)
        self.assertEqual(self.calls, [])

    def test_schedule_on_and_off(self):
        ran = []
        with mock.patch.object(backup, "run", side_effect=lambda cmd, **kw: (ran.append(cmd), (0, ""))[1]):
            m = Manifest.load()
            self.assertTrue(backup.schedule(m, True))
            timer = paths.host(paths.BACKUP_TIMER_PATH)
            self.assertTrue(os.path.isfile(timer))
            with open(paths.host(paths.BACKUP_SERVICE_PATH)) as f:
                self.assertIn("ragspark.py backup --scheduled", f.read())
            with open(timer) as f:
                self.assertIn("OnCalendar=*-*-* 03:30:00", f.read())
            m = Manifest.load()
            self.assertEqual(m.data["backup_timer"], paths.BACKUP_TIMER)
            self.assertIn(timer, m.data["files"])
            self.assertIn(["systemctl", "enable", "--now", paths.BACKUP_TIMER], ran)
            self.assertNotIn(["systemctl", "daemon-reload"], ran)              # new files need no reload
            self.assertTrue(backup.schedule(m, False))
            self.assertFalse(os.path.exists(timer))
            m = Manifest.load()
            self.assertIsNone(m.data["backup_timer"])
            self.assertNotIn(timer, m.data["files"])


if __name__ == "__main__":
    unittest.main()
