"""tools/acceptance.py against a mock RAGFlow API: passes when it should, fails when it should."""
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))

import mock_ragflow  # noqa: E402


class Acceptance(unittest.TestCase):
    def run_tool(self, mode, *extra):
        srv, state, url = mock_ragflow.serve(mode)
        try:
            env = dict(os.environ, RAGFLOW_API=url, ADMIN_DEFAULT_PASSWORD="secret",
                       DEFAULT_SUPERUSER_EMAIL="admin@ragspark.local",
                       PYTHONPATH=os.path.join(ROOT, "tests", "fake"))
            p = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "acceptance.py"),
                                "--dir", os.path.join(ROOT, "acceptance")] + list(extra),
                               env=env, capture_output=True, text=True, timeout=120)
            return p.returncode, p.stdout + p.stderr, state
        finally:
            srv.shutdown()

    def test_passes(self):
        code, out, state = self.run_tool("good")
        self.assertEqual(code, 0, out)
        self.assertIn("acceptance passed", out)
        self.assertEqual(len(state.chats), 1)
        chat = list(state.chats.values())[0]
        self.assertEqual(chat["vector_similarity_weight"], 0.7)
        self.assertEqual(chat["similarity_threshold"], 0.1)
        self.assertIn("{knowledge}", chat["prompt_config"]["system"])

    def test_rerun_replaces_example(self):
        srv, state, url = mock_ragflow.serve("good")
        try:
            env = dict(os.environ, RAGFLOW_API=url, ADMIN_DEFAULT_PASSWORD="secret",
                       PYTHONPATH=os.path.join(ROOT, "tests", "fake"))
            for _ in range(2):
                p = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "acceptance.py"),
                                    "--dir", os.path.join(ROOT, "acceptance")], env=env,
                                   capture_output=True, text=True, timeout=120)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertEqual(len(state.chats), 1)
            self.assertEqual(len(state.datasets), 1)
        finally:
            srv.shutdown()

    def test_cleanup(self):
        code, out, state = self.run_tool("good", "--cleanup")
        self.assertEqual(code, 0, out)
        self.assertEqual((len(state.chats), len(state.datasets)), (0, 0))

    def test_invented_answer_fails(self):
        code, out, _ = self.run_tool("leak")
        self.assertEqual(code, 1)
        self.assertIn("Австралии", out)

    def test_cjk_fails(self):
        code, out, _ = self.run_tool("cjk")
        self.assertEqual(code, 1)
        self.assertIn("CJK", out)

    def test_english_names(self):
        code, out, state = self.run_tool("good", "--lang", "en")
        self.assertEqual(code, 0, out)
        self.assertEqual(list(state.chats.values())[0]["name"], "RAGSpark sample")


if __name__ == "__main__":
    unittest.main()
