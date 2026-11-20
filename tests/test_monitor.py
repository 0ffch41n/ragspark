"""Monitor checks, incident transitions and the optional timer lifecycle."""
import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "installer"))

from rsinst import monitor, paths, ui  # noqa: E402
from rsinst.manifest import Manifest  # noqa: E402


class Monitor(unittest.TestCase):
    def setUp(self):
        self.addCleanup(os.umask, os.umask(0o022))
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for mock in (patch.object(paths, "PREFIX", directory.name),
                     patch.object(ui, "_state", dict(ui._state, lang="en", log=None, pending=[]))):
            mock.start()
            self.addCleanup(mock.stop)
        self.manifest = Manifest()
        self.manifest.data["status"] = "installed"
        self.manifest.save()

    def test_debounce_then_report_and_clear_once(self):
        path = Path(paths.host(monitor.STATE))
        path.parent.mkdir(parents=True)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            monitor.record({"mon_disk"})
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(monitor.read_state()["active"], [])
            monitor.record({"mon_web"})
            self.assertEqual(output.getvalue(), "")
            monitor.record({"mon_web"})
            monitor.record({"mon_web", "mon_disk"})
            monitor.record(set())
            monitor.record(set())
        self.assertEqual(output.getvalue().count("check RAGFlow"), 1)
        self.assertEqual(output.getvalue().count("recovered RAGFlow"), 1)
        self.assertEqual(len(output.getvalue().splitlines()), 2)
        self.assertEqual(monitor.read_state()["active"], [])

    def test_head_checks_models_web_and_backups(self):
        self.manifest.data["backup_timer"] = paths.BACKUP_TIMER
        rows = [("vllm-embed", "running", "healthy"), ("vllm-llm", "running", "starting"),
                ("vllm-rerank", "running", "healthy")]
        with patch.object(monitor.control, "services", return_value=rows), \
             patch.object(monitor.control, "ragflow_health", return_value={"status": "nok"}), \
             patch.object(monitor, "_disk_ok", return_value=True), \
             patch.object(monitor.backup, "last_copy", return_value=("old", 49)):
            self.assertEqual(monitor.failures(self.manifest), {"mon_web", "mon_models", "mon_backup"})

    def test_head_checks_configured_replication(self):
        sender = Path(monitor.backup_remote.config_path())
        sender.parent.mkdir(parents=True)
        sender.touch()
        status = sender.with_name("sender-status.json")
        rows = [(name, "running", "healthy") for name in ("vllm-embed", "vllm-rerank", "vllm-llm")]
        with patch.object(monitor.control, "services", return_value=rows), \
             patch.object(monitor.control, "ragflow_health", return_value={"status": "ok"}), \
             patch.object(monitor, "_disk_ok", return_value=True):
            self.assertEqual(monitor.failures(self.manifest), {"mon_replica"})
            status.write_text('{"ok": true, "time": %s}' % time.time())
            self.assertEqual(monitor.failures(self.manifest), set())

    def test_stack_node_checks_qsfp_and_head_api(self):
        self.manifest.config.update(role="node", llm="deepseek-v4-flash-vision")
        rows = [("vllm-embed", "running", "healthy"), ("vllm-rerank", "running", "healthy"),
                ("vllm-llm-rank", "running", "")]
        with patch.object(monitor.control, "services", return_value=rows), \
             patch.object(monitor.control, "ragflow_health") as web, \
             patch.object(monitor, "_disk_ok", return_value=True), \
             patch.object(monitor, "run", return_value=(0, "")) as ping, \
             patch.object(monitor.stackmode, "http_ok", return_value=True) as head:
            self.assertEqual(monitor.failures(self.manifest), set())
            web.assert_not_called()
            self.assertEqual(ping.call_args.args[0][-3:], ["-I", "192.168.100.11", "192.168.100.10"])
            head.return_value = False
            ping.return_value = (1, "")
            self.assertEqual(monitor.failures(self.manifest), {"mon_peer", "mon_models"})

    def test_timer_is_opt_in_idempotent_and_stops_without_reloading(self):
        with patch.object(monitor, "run", return_value=(0, "")) as run:
            self.assertEqual(monitor.schedule(self.manifest), 0)
            self.assertEqual(monitor.schedule(self.manifest), 0)
            self.assertTrue(monitor.stop(self.manifest))
            self.assertEqual([call.args[0] for call in run.call_args_list], [
                ["systemctl", "enable", "--no-reload", "--now", monitor.TIMER],
                ["systemctl", "enable", "--no-reload", "--now", monitor.TIMER],
                ["systemctl", "disable", "--no-reload", "--now", monitor.TIMER],
                ["systemctl", "stop", monitor.SERVICE],
            ])
        self.assertEqual(self.manifest.data["monitor_timer"], monitor.TIMER)
        self.assertIn(paths.host("/etc/systemd/system/" + monitor.SERVICE), self.manifest.data["files"])


if __name__ == "__main__":
    unittest.main()
