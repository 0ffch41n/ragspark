"""Installer tests: messages, helpers, and a full install/uninstall with fake system tools.

  python3 -m unittest discover -s tests -v

The end-to-end test needs a standalone Docker Compose binary to validate the
real compose files (set RAGSPARK_TEST_COMPOSE); without it that test is skipped.
"""
import glob
import json
import os
import re
import shutil
import string
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "installer"))

from rsinst import i18n, install, manifest, sysutil  # noqa: E402


def placeholders(text):
    return sorted({f for _, f, _, _ in string.Formatter().parse(text) if f})


class Messages(unittest.TestCase):
    def test_both_languages_and_same_placeholders(self):
        for key, pair in i18n.MESSAGES.items():
            self.assertEqual(len(pair), 2, key)
            en, ru = pair
            self.assertTrue(en.strip() and ru.strip(), key)
            self.assertEqual(placeholders(en), placeholders(ru), key)

    def test_every_key_used_in_code_exists(self):
        used = set()
        for path in glob.glob(os.path.join(ROOT, "installer", "rsinst", "*.py")):
            with open(path, encoding="utf-8") as f:
                text = f.read()
            used.update(re.findall(r"\bt\(\"([a-z0-9_]+)\"", text))
            used.update(re.findall(r"\"((?:st|plan)_[a-z]+)\"", text))
        missing = sorted(k for k in used if k not in i18n.MESSAGES)
        self.assertEqual(missing, [])

    def test_russian_text_is_russian(self):
        for key, (en, ru) in i18n.MESSAGES.items():
            if re.search(r"[A-Za-z]{4,}", en) and not re.search(r"^[\W\d{}_A-Za-z:./`-]+$", ru):
                self.assertRegex(ru, r"[А-Яа-яЁё]", key)


class Helpers(unittest.TestCase):
    def test_version_tuple(self):
        self.assertEqual(sysutil.version_tuple("29.6.2"), (29, 6, 2))
        self.assertEqual(sysutil.version_tuple("v2.39.4-desktop.1"), (2, 39, 4))
        self.assertEqual(sysutil.version_tuple("5.2"), (5, 2, 0))
        self.assertEqual(sysutil.version_tuple("x"), ())

    def test_env_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, ".env")
            with open(p, "w") as f:
                f.write("# comment\nA=1\nB=two\n")
            sysutil.set_env(p, "B", "three")
            sysutil.set_env(p, "C", "4")
            self.assertEqual(sysutil.read_env(p), {"A": "1", "B": "three", "C": "4"})
            with open(p) as f:
                self.assertTrue(f.read().startswith("# comment\n"))
            self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)

    def test_make_dirs_records_only_new(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["RAGSPARK_PREFIX"] = d
            try:
                import importlib
                from rsinst import paths
                importlib.reload(paths)
                importlib.reload(manifest)
                m = manifest.Manifest()
                os.makedirs(os.path.join(d, "opt"))
                manifest.make_dirs(paths.install_dir(), m)
                self.assertEqual(m.data["created_dirs"], [paths.install_dir()])
                target = os.path.join(d, "srv", "ragspark", "hf-cache")
                manifest.make_dirs(target, m)
                self.assertEqual(m.data["created_dirs"][1:], [os.path.join(d, "srv"),
                                                              os.path.join(d, "srv", "ragspark"), target])
                self.assertTrue(os.path.exists(paths.manifest_path()))
            finally:
                os.environ.pop("RAGSPARK_PREFIX")
                importlib.reload(paths)
                importlib.reload(manifest)


class WeightsCheck(unittest.TestCase):
    """The offline weights check that runs in the vLLM container, run here directly."""

    def check(self, hub, repo="org/model", revision=""):
        p = subprocess.run([sys.executable, "-c", install.WEIGHTS_SCRIPT, repo, "local", revision],
                           env=dict(os.environ, HF_HUB_CACHE=hub), capture_output=True, text=True)
        return p.returncode, p.stdout.strip()

    def test_local_check_reads_the_cache_only(self):
        hub = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, hub, ignore_errors=True)
        base = os.path.join(hub, "models--org--model")
        self.assertEqual(self.check(hub), (3, "MISSING refs/main"))
        os.makedirs(os.path.join(base, "refs"))
        with open(os.path.join(base, "refs", "main"), "w") as f:
            f.write("abc123")
        self.assertEqual(self.check(hub)[0], 3)                            # no snapshot directory
        snap = os.path.join(base, "snapshots", "abc123")
        os.makedirs(snap)
        for name, data in (("config.json", "{}"), ("model.safetensors.index.json",
                           json.dumps({"weight_map": {"a": "m-1.safetensors", "b": "m-2.safetensors"}}))):
            with open(os.path.join(snap, name), "w") as f:
                f.write(data)
        open(os.path.join(snap, "m-1.safetensors"), "w").close()
        os.symlink(os.path.join(hub, "missing-blob"), os.path.join(snap, "m-2.safetensors"))
        self.assertEqual(self.check(hub), (3, "MISSING m-2.safetensors"))  # dangling link: blob gone
        os.remove(os.path.join(snap, "m-2.safetensors"))
        open(os.path.join(snap, "m-2.safetensors"), "w").close()
        self.assertEqual(self.check(hub), (0, "COMPLETE " + snap))         # no checksum files needed

    def test_pinned_revision_ignores_refs_main(self):
        hub = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, hub, ignore_errors=True)
        base = os.path.join(hub, "models--org--model")
        os.makedirs(os.path.join(base, "refs"))
        with open(os.path.join(base, "refs", "main"), "w") as f:
            f.write("newer")                                               # main moved on
        snap = os.path.join(base, "snapshots", "abc123")
        os.makedirs(snap)
        with open(os.path.join(snap, "config.json"), "w") as f:
            f.write("{}")
        open(os.path.join(snap, "model.safetensors"), "w").close()
        self.assertEqual(self.check(hub, revision="abc123"), (0, "COMPLETE " + snap))
        self.assertEqual(self.check(hub, revision="def456"), (3, "MISSING snapshot def456"))
        shutil.rmtree(os.path.join(base, "refs"))                          # a pinned download writes no ref
        self.assertEqual(self.check(hub, revision="abc123"), (0, "COMPLETE " + snap))

    def test_model_repos_reads_the_pinned_revision(self):
        cfg = {"services": {
            "vllm-llm": {"command": ["vllm", "serve", "org/llm", "--revision", "abc123", "--served-model-name", "x"]},
            "vllm-embed": {"command": ["vllm", "serve", "org/embed", "--runner", "pooling"]},
            "ragflow": {"command": ["--enable-adminserver"]}}}
        self.assertEqual(install.model_repos(cfg), [("org/embed", ""), ("org/llm", "abc123")])


@unittest.skipUnless(os.environ.get("RAGSPARK_TEST_COMPOSE"), "set RAGSPARK_TEST_COMPOSE to a compose binary")
class EndToEnd(unittest.TestCase):
    """install -> status -> uninstall in a scratch prefix with fake docker/systemctl/nvidia-smi."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state = os.path.join(self.tmp, "state.json")
        vllm = "vllm/vllm-openai:v0.27.1-aarch64@sha256:" + "1c8e60a0841b333c700488cb029d3664807249da0c071e862191b00fe34b228c"
        # The vLLM image is "already on the machine"; everything else must be pulled.
        json.dump({"images": [vllm], "calls": [], "containers": False,
                   "weights_complete": ["deepvk/USER-bge-m3"]}, open(self.state, "w"))
        pre = os.path.join(self.tmp, "srv", "ragspark", "hf-cache", "hub", "models--deepvk--USER-bge-m3")
        os.makedirs(pre)                                   # weights that existed before the install
        os.makedirs(os.path.join(self.tmp, "opt"))
        self.env = dict(os.environ, RAGSPARK_PREFIX=self.tmp, RAGSPARK_ALLOW_NONROOT="1",
                        FAKE_STATE=self.state, FAKE_COMPOSE=os.environ["RAGSPARK_TEST_COMPOSE"],
                        PATH=os.path.join(ROOT, "tests", "fakebin") + os.pathsep + os.environ["PATH"],
                        LANG="ru_RU.UTF-8", RAGSPARK_RETRY_DELAY="0")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ragspark(self, *args):
        p = subprocess.run([sys.executable, os.path.join(ROOT, "installer", "ragspark.py")] + list(args),
                           env=self.env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout + p.stderr

    def test_install_status_uninstall(self):
        code, out = self.ragspark("install", "--yes", "--force", "--no-network", "--models-dir", "/srv/ragspark/hf-cache")
        self.assertEqual(code, 0, out)
        self.assertIn("Приёмочная проверка", out)
        inst = os.path.join(self.tmp, "opt", "ragspark")
        m = json.load(open(os.path.join(inst, "manifest.json")))
        self.assertEqual(m["status"], "installed")
        self.assertEqual(m["config"]["lang"], "ru")
        self.assertEqual(len(m["images_pulled"]), 5)                      # es, mysql, silo, valkey, ragflow
        self.assertEqual(sorted(m["models_downloaded"]), ["BAAI/bge-reranker-v2-m3", "Inferact/Qwen3.8-27B-NVFP4"])
        revisions = json.load(open(self.state))["weights_revisions"]       # every model pinned to a commit
        self.assertEqual(len(revisions), 3)
        for repo, rev in revisions.items():
            self.assertRegex(rev, r"^[0-9a-f]{40}$", repo)
        env = sysutil.read_env(os.path.join(inst, "compose", ".env"))
        self.assertEqual(env["DEFAULT_SUPERUSER_EMAIL"], "admin@ragspark.local")
        self.assertEqual(env["RAGSPARK_LANG"], "ru")
        self.assertEqual(len(env["MYSQL_PASSWORD"]), 32)
        self.assertEqual(os.stat(os.path.join(inst, "compose", ".env")).st_mode & 0o777, 0o600)
        with open(os.path.join(inst, "install.log"), encoding="utf-8") as f:
            log = f.read()
        self.assertIn("compose config", log)
        for key in ("MYSQL_PASSWORD", "ELASTIC_PASSWORD", "ADMIN_DEFAULT_PASSWORD", "RAGFLOW_SECRET_KEY"):
            self.assertNotIn(env[key], log, key)                           # no secrets in the log
        self.assertEqual(os.stat(os.path.join(inst, "install.log")).st_mode & 0o777, 0o600)
        unit = open(os.path.join(self.tmp, "etc", "systemd", "system", "ragspark.service")).read()
        self.assertIn("WorkingDirectory=/opt/ragspark/compose", unit)
        self.assertIn("/opt/ragspark/installer/ragspark.py prestart", unit)
        self.assertTrue(os.access(os.path.join(self.tmp, "usr", "local", "bin", "ragspark"), os.X_OK))
        calls = json.load(open(self.state))["calls"]
        self.assertTrue(any(c.startswith("compose up -d --wait") for c in calls))
        sysd = open(self.state + ".systemctl").read()
        self.assertLess(sysd.index("daemon-reload"), sysd.index("start ragspark.service"))

        code, out = self.ragspark("install", "--yes", "--force", "--no-network")
        self.assertEqual(code, 1)                                          # already installed

        code, out = self.ragspark("status")
        self.assertIn("ragflow", out)

        code, out = self.ragspark("uninstall", "--yes")
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.exists(inst))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "etc", "systemd", "system", "ragspark.service")))
        hub = os.path.join(self.tmp, "srv", "ragspark", "hf-cache", "hub")
        self.assertEqual(sorted(os.listdir(hub)), ["models--deepvk--USER-bge-m3"])   # pre-existing kept
        left = json.load(open(self.state))["images"]
        self.assertEqual(len(left), 1)                                     # only the pre-existing vLLM image

    def set_state(self, **kw):
        with open(self.state) as f:
            s = json.load(f)
        s.update(kw)
        with open(self.state, "w") as f:
            json.dump(s, f)

    def test_prestart_stops_only_unready_containers(self):
        self.set_state(ps_rows=["ragspark-vllm-llm-1\trunning\tUp 3 seconds (health: starting)",
                                "ragspark-es01-1\trunning\tUp 2 minutes (healthy)",
                                "ragspark-vllm-embed-1\trestarting\tRestarting (1) 2 seconds ago",
                                "ragspark-ragflow-1\trunning\tUp 5 seconds (unhealthy)"])
        code, out = self.ragspark("prestart")
        self.assertEqual(code, 0, out)
        stopped = json.load(open(self.state)).get("stopped", [])
        self.assertEqual(stopped, ["ragspark-ragflow-1", "ragspark-vllm-embed-1", "ragspark-vllm-llm-1"])
        self.set_state(ps_rows=["ragspark-es01-1\trunning\tUp 2 minutes (healthy)"], stopped=[])
        self.ragspark("prestart")
        self.assertEqual(json.load(open(self.state))["stopped"], [])

    def test_existing_install_dir_blocks_even_with_force(self):
        os.makedirs(os.path.join(self.tmp, "opt", "ragspark", "mine"))
        code, out = self.ragspark("install", "--yes", "--force", "--no-network")
        self.assertEqual(code, 1)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "opt", "ragspark", "mine")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "opt", "ragspark", "manifest.json")))

    def test_old_volumes_block_even_with_force(self):
        self.set_state(volumes=["ragspark_mysql", "ragspark_esdata"])
        code, out = self.ragspark("install", "--yes", "--force", "--no-network")
        self.assertEqual(code, 1)
        self.assertIn("ragspark_mysql ragspark_esdata", out)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "opt", "ragspark")))

    def test_uninstall_keeps_everything_when_down_fails(self):
        code, out = self.ragspark("install", "--yes", "--force", "--no-network")
        self.assertEqual(code, 0, out)
        self.set_state(down_fails=True)
        code, out = self.ragspark("uninstall", "--yes")
        self.assertEqual(code, 1)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "opt", "ragspark", "compose", ".env")))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "opt", "ragspark", "manifest.json")))

    def test_uninstall_leaves_foreign_files_in_models_dir(self):
        # The installer creates the models directory here (it does not exist yet) ...
        shutil.rmtree(os.path.join(self.tmp, "srv"))
        code, out = self.ragspark("install", "--yes", "--force", "--no-network")
        self.assertEqual(code, 0, out)
        cache = os.path.join(self.tmp, "srv", "ragspark", "hf-cache")
        # ... and someone later puts their own files into it.
        os.makedirs(os.path.join(cache, "hub", "datasets--someone--data"))
        os.makedirs(os.path.join(cache, "hub", ".locks"))
        code, out = self.ragspark("uninstall", "--yes")
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.isdir(os.path.join(cache, "hub", "datasets--someone--data")))
        self.assertFalse(os.path.exists(os.path.join(cache, "hub", ".locks")))
        rmi = [c for c in json.load(open(self.state))["calls"] if c.startswith("rmi ")]
        self.assertEqual(len(rmi), 5)            # the pre-existing vLLM image is not touched
        self.assertTrue(all("@sha256:" in c for c in rmi))

    def test_skip_acceptance_still_sets_up_the_assistant(self):
        code, out = self.ragspark("install", "--yes", "--force", "--no-network", "--skip-acceptance")
        self.assertEqual(code, 0, out)
        calls = json.load(open(self.state))["calls"]
        self.assertFalse(any(c.startswith("compose cp") for c in calls))    # no sample copied
        tool = [c for c in calls if c.startswith("compose exec")]
        self.assertEqual(len(tool), 2, tool)                                # models, then the assistant
        self.assertIn("--assistant-only", tool[-1])
        self.assertIn("Чат «Ассистент»", out)
        self.assertNotIn("RAGSpark — пример", out)

    def test_interrupted_install_resumes(self):
        # Fail at the image step: compose config works, but every pull fails.
        fakebin = os.path.join(self.tmp, "bin")
        os.makedirs(fakebin)
        wrapper = os.path.join(fakebin, "docker")
        real = os.path.join(ROOT, "tests", "fakebin", "docker")
        open(wrapper, "w").write("#!/bin/sh\n[ \"$1\" = pull ] && [ ! -e \"$FAKE_STATE.netok\" ] && exit 1\n"
                                 "exec %s \"$@\"\n" % real)
        os.chmod(wrapper, 0o755)
        self.env["PATH"] = fakebin + os.pathsep + self.env["PATH"]
        code, out = self.ragspark("install", "--yes", "--force", "--no-network")
        self.assertEqual(code, 1)
        m = json.load(open(os.path.join(self.tmp, "opt", "ragspark", "manifest.json")))
        self.assertEqual(m["status"], "installing")
        env_before = open(os.path.join(self.tmp, "opt", "ragspark", "compose", ".env")).read()
        open(self.state + ".netok", "w").close()                          # the network is back
        code, out = self.ragspark("install", "--yes", "--force", "--no-network")
        self.assertEqual(code, 0, out)
        env_after = open(os.path.join(self.tmp, "opt", "ragspark", "compose", ".env")).read()
        self.assertEqual(env_before, env_after)                            # passwords kept


if __name__ == "__main__":
    unittest.main()
