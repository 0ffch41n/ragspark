"""`ragspark llm`: install with fake system tools, then change the LLM and back, and the rollback.
Like the installer's end-to-end test it needs a compose binary (RAGSPARK_TEST_COMPOSE)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "installer"))

from rsinst import sysutil  # noqa: E402

try:
    import yaml  # noqa: F401
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False

DEFAULT, FAST = "qwen-3.8-27b-nvfp4", "qwen-3.6-35b-a3b-nvfp4"
LLM_UP = "compose up -d --wait --wait-timeout 2400 --no-deps vllm-llm"
STACK_UP = "compose up -d --wait --wait-timeout 600"


@unittest.skipUnless(os.environ.get("RAGSPARK_TEST_COMPOSE"), "set RAGSPARK_TEST_COMPOSE to a compose binary")
class SwitchLlm(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state = os.path.join(self.tmp, "state.json")
        vllm = "vllm/vllm-openai:v0.27.1-aarch64@sha256:" + "1c8e60a0841b333c700488cb029d3664807249da0c071e862191b00fe34b228c"
        json.dump({"images": [vllm], "calls": [], "containers": False,
                   "weights_complete": ["deepvk/USER-bge-m3"]}, open(self.state, "w"))
        os.makedirs(os.path.join(self.tmp, "opt"))
        os.makedirs(os.path.join(self.tmp, "proc", "sys", "vm"))            # drop_caches lands here
        self.env = dict(os.environ, RAGSPARK_PREFIX=self.tmp, RAGSPARK_ALLOW_NONROOT="1",
                        FAKE_STATE=self.state, FAKE_COMPOSE=os.environ["RAGSPARK_TEST_COMPOSE"],
                        PATH=os.path.join(ROOT, "tests", "fakebin") + os.pathsep + os.environ["PATH"],
                        LANG="ru_RU.UTF-8", RAGSPARK_RETRY_DELAY="0")
        code, out = self.ragspark("install", "--yes", "--force", "--no-network", "--models-dir", "/srv/ragspark/hf-cache")
        self.assertEqual(code, 0, out)
        self.inst = os.path.join(self.tmp, "opt", "ragspark")
        self.envfile = os.path.join(self.inst, "compose", ".env")

    def ragspark(self, *args):
        p = subprocess.run([sys.executable, os.path.join(ROOT, "installer", "ragspark.py")] + list(args),
                           env=self.env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout + p.stderr

    def fake(self):
        return json.load(open(self.state))

    def set_fake(self, **kw):
        s = self.fake()
        s.update(kw)
        json.dump(s, open(self.state, "w"))

    def llm_file(self):
        return sysutil.read_env(self.envfile)["COMPOSE_FILE"]

    def manifest(self):
        return json.load(open(os.path.join(self.inst, "manifest.json")))

    def test_change_and_back(self):
        before = len(self.fake()["calls"])
        code, out = self.ragspark("llm", "2", "--yes")                      # number 2 of the menu
        self.assertEqual(code, 0, out)
        self.assertEqual(self.llm_file(), "compose.yaml:llm/%s.yaml" % FAST)
        m = self.manifest()
        self.assertEqual(m["config"]["llm"], FAST)
        self.assertIn("unsloth/Qwen3.6-35B-A3B-NVFP4-Fast", m["models_downloaded"])   # uninstall removes it
        calls = self.fake()["calls"][before:]
        download = next(i for i, c in enumerate(calls) if "Qwen3.6-35B-A3B-NVFP4-Fast online" in c)
        stop = calls.index("compose stop vllm-llm")
        llm_up = calls.index(LLM_UP)
        stack_up = calls.index(STACK_UP)
        register = next(i for i, c in enumerate(calls) if c.startswith("compose exec") and i > stack_up)
        self.assertLess(download, stop)                                     # weights while the old LLM still answers
        self.assertLess(stop, llm_up)                                       # the LLM alone: RAGFlow keeps running
        self.assertLess(llm_up, stack_up)                                   # then ragflow is recreated
        self.assertLess(stack_up, register)
        self.assertIn("%s готова; перезапускаю RAGFlow с ней" % FAST, out)
        self.assertEqual(open(os.path.join(self.tmp, "proc", "sys", "vm", "drop_caches")).read(), "3\n")
        self.assertIn("Теперь работает %s" % FAST, out)
        self.assertIn("Вернуть: sudo ragspark llm %s" % DEFAULT, out)
        log = open(os.path.join(self.inst, "install.log"), encoding="utf-8").read()
        self.assertIn("Смена языковой модели", log)
        for key in ("MYSQL_PASSWORD", "ELASTIC_PASSWORD", "ADMIN_DEFAULT_PASSWORD"):
            self.assertNotIn(sysutil.read_env(self.envfile)[key], log, key)  # `compose -f ... config` is not logged

        before = len(self.fake()["calls"])
        code, out = self.ragspark("llm", DEFAULT, "--yes")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.llm_file(), "compose.yaml:llm/%s.yaml" % DEFAULT)
        calls = self.fake()["calls"][before:]
        self.assertFalse([c for c in calls if " online " in c], calls)       # its weights are still there

    def test_rollback_when_the_new_llm_does_not_start(self):
        self.set_fake(up_fails_for=FAST)
        before = len(self.fake()["calls"])
        code, out = self.ragspark("llm", FAST, "--yes")
        self.assertEqual(code, 1, out)
        self.assertEqual(self.llm_file(), "compose.yaml:llm/%s.yaml" % DEFAULT)
        self.assertEqual(self.manifest()["config"]["llm"], DEFAULT)
        self.assertIn("%s не запустилась" % FAST, out)
        self.assertIn("%s снова работает" % DEFAULT, out)
        calls = self.fake()["calls"][before:]
        self.assertEqual(calls.count(LLM_UP), 2)                            # the new LLM, then the old one again
        self.assertEqual(calls.count(STACK_UP), 1)                          # ragflow only after the old one is back
        self.assertEqual(calls.count("compose stop vllm-llm"), 2)

    def test_ragflow_failing_after_the_llm_started_is_reported_without_rollback(self):
        self.set_fake(stack_fails_for=FAST)
        before = len(self.fake()["calls"])
        code, out = self.ragspark("llm", FAST, "--yes")
        self.assertEqual(code, 1, out)
        self.assertIn("%s работает, но RAGFlow не запустился снова" % FAST, out)
        self.assertEqual(self.llm_file(), "compose.yaml:llm/%s.yaml" % FAST)   # the LLM is fine: no rollback
        calls = self.fake()["calls"][before:]
        self.assertEqual(calls.count(LLM_UP), 1)
        self.assertEqual(calls.count("compose stop vllm-llm"), 1)

    def test_without_a_terminal_and_yes_nothing_changes(self):
        before = len(self.fake()["calls"])
        code, out = self.ragspark("llm", FAST)
        self.assertEqual(code, 1, out)
        self.assertEqual(self.llm_file(), "compose.yaml:llm/%s.yaml" % DEFAULT)
        calls = self.fake()["calls"][before:]
        self.assertNotIn("compose stop vllm-llm", calls)
        self.assertFalse([c for c in calls if "Qwen3.6" in c], calls)

    def test_show_same_and_unknown(self):
        code, out = self.ragspark("llm")
        self.assertEqual(code, 0, out)
        self.assertIn("Языковая модель сейчас: %s" % DEFAULT, out)
        if HAVE_YAML:
            self.assertIn("1) %s — работает сейчас" % DEFAULT, out)
        before = len(self.fake()["calls"])
        code, out = self.ragspark("llm", DEFAULT)                           # the running one: registration only
        self.assertEqual(code, 0, out)
        calls = self.fake()["calls"][before:]
        self.assertNotIn("compose stop vllm-llm", calls)
        self.assertEqual(len([c for c in calls if c.startswith("compose exec")]), 1)
        code, out = self.ragspark("llm", "nope")
        self.assertEqual(code, 1, out)
        self.assertIn("Неизвестная модель nope", out)


if __name__ == "__main__":
    unittest.main()
