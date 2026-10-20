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
        self.assertEqual(sorted(c["name"] for c in state.chats.values()), ["RAGSpark — пример", "Ассистент"])
        self.assertIn(1, state.levels)                                       # one question at level Low
        self.assertEqual(len(state.deleted_sessions), len(state.levels))     # no test sessions left behind
        chat = [c for c in state.chats.values() if c["name"] == "RAGSpark — пример"][0]
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
            self.assertEqual(len(state.chats), 2)                       # example replaced, assistant kept
            self.assertEqual(len(state.datasets), 1)
        finally:
            srv.shutdown()

    def test_cleanup(self):
        code, out, state = self.run_tool("good", "--cleanup")
        self.assertEqual(code, 0, out)
        self.assertEqual([c["name"] for c in state.chats.values()], ["Ассистент"])   # the assistant stays
        self.assertEqual(len(state.datasets), 0)

    def test_assistant_only(self):
        code, out, state = self.run_tool("good", "--assistant-only")
        self.assertEqual(code, 0, out)
        self.assertEqual([c["name"] for c in state.chats.values()], ["Ассистент"])
        self.assertEqual(len(state.datasets), 0)

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
        self.assertEqual(sorted(c["name"] for c in state.chats.values()), ["Assistant", "RAGSpark sample"])


class ModeEval(unittest.TestCase):
    """tools/mode_eval.py against the same mock, after the acceptance test made the example chat."""

    def test_compares_levels_and_cleans_up(self):
        import json
        import tempfile
        with open(os.path.join(ROOT, "acceptance", "sample-ru.json"), encoding="utf-8") as f:
            questions = json.load(f)["questions"]
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(questions, tmp, ensure_ascii=False)
        tmp.close()
        self.addCleanup(os.remove, tmp.name)
        QUESTIONS = tmp.name
        srv, state, url = mock_ragflow.serve("good")
        try:
            env = dict(os.environ, RAGFLOW_API=url, ADMIN_DEFAULT_PASSWORD="secret",
                       PYTHONPATH=os.path.join(ROOT, "tests", "fake"))
            p = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "acceptance.py"),
                                "--dir", os.path.join(ROOT, "acceptance")], env=env, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            levels_before = len(state.levels)
            p = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "mode_eval.py"), "--modes", "0,1",
                                "--questions", QUESTIONS, "--trace"],
                               env=env, capture_output=True, text=True, timeout=120)
            out = p.stdout + p.stderr
            self.assertEqual(p.returncode, 0, out)
            self.assertEqual(state.levels[levels_before:], [0, 0, 0, 1, 1, 1])
            self.assertRegex(out, r"Naive\s+2/2\s+1/1")
            self.assertRegex(out, r"Low\s+2/2\s+1/1")                    # <think> block stripped
            self.assertIn("| [Hybrid search] Kept 2 of 2 passage(s).", out)   # --trace shows the progress log
            self.assertEqual(len(state.deleted_sessions), levels_before + 6)

            # --dataset: a temporary chat with RAGSpark's settings, deleted afterwards
            p = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "mode_eval.py"), "--modes", "0",
                                "--dataset", "RAGSpark — пример", "--questions", QUESTIONS],
                               env=env, capture_output=True, text=True, timeout=120)
            out = p.stdout + p.stderr
            self.assertEqual(p.returncode, 0, out)
            self.assertRegex(out, r"Naive\s+2/2\s+1/1")
            self.assertEqual(len(state.chats), 2)                            # the temporary chat is gone
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
