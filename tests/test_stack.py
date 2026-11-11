"""Stack mode (stage 5): the relay, the compose files of both units and `ragspark watch`'s decisions."""
import asyncio
import glob
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "installer"))
sys.path.insert(0, os.path.join(ROOT, "compose"))

import relay  # noqa: E402
from rsinst import install, watch  # noqa: E402

try:
    import yaml

    class ComposeLoader(yaml.SafeLoader):
        """SafeLoader that reads compose's merge tags (!override, !reset) as plain values."""

    def _plain(loader, node):
        if isinstance(node, yaml.MappingNode):
            return loader.construct_mapping(node, deep=True)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node, deep=True)
        return loader.construct_scalar(node)

    for _tag in ("!override", "!reset"):
        ComposeLoader.add_constructor(_tag, _plain)
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False


class Relay(unittest.TestCase):
    def test_bytes_pass_both_ways_and_a_missing_target_closes_the_call(self):
        async def scenario():
            async def echo(reader, writer):
                data = await reader.read(1 << 16)
                writer.write(b"echo:" + data)
                await writer.drain()
                writer.close()
            target = await asyncio.start_server(echo, "127.0.0.1", 0)
            port = target.sockets[0].getsockname()[1]
            ready = asyncio.get_running_loop().create_future()
            task = asyncio.ensure_future(relay.serve(0, "127.0.0.1", port, ready))
            lport = await ready
            reader, writer = await asyncio.open_connection("127.0.0.1", lport)
            writer.write("привет".encode())
            await writer.drain()
            got = await asyncio.wait_for(reader.read(1 << 16), 5)
            writer.close()
            target.close()
            await target.wait_closed()
            reader, writer = await asyncio.open_connection("127.0.0.1", lport)  # target gone now
            gone = await asyncio.wait_for(reader.read(1 << 16), 5)
            writer.close()
            task.cancel()
            return got, gone
        got, gone = asyncio.run(scenario())
        self.assertEqual(got, "echo:привет".encode())
        self.assertEqual(gone, b"")


class ModelRepos(unittest.TestCase):
    def test_the_serve_line_may_start_in_the_entrypoint(self):
        cfg = {"services": {
            "vllm-llm-rank": {"entrypoint": ["vllm", "serve", "org/big", "--revision", "r1"],
                              "command": ["--node-rank", "0"]},
            "vllm-llm": {"entrypoint": ["python3", "-I", "/relay.py"], "command": ["8000", "10.0.0.1", "8000"]},
            "vllm-embed": {"entrypoint": ["/usr/bin/env"], "command": ["vllm", "serve", "org/embed"]}}}
        self.assertEqual(install.model_repos(cfg), [("org/embed", ""), ("org/big", "r1")])


@unittest.skipUnless(HAVE_YAML, "needs python3-yaml")
class ComposeFiles(unittest.TestCase):
    def load(self, *parts):
        with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
            return yaml.load(f, Loader=ComposeLoader)

    def stack_llms(self):
        cat = self.load("catalog", "models.yaml")
        return [e for e in cat["models"]["llm"] if "stack" in (e.get("modes") or []) and e.get("compose")]

    def test_the_node_runs_the_same_embedding_and_reranker_as_single_mode(self):
        single = self.load("compose", "compose.yaml")["services"]
        node = self.load("compose", "node", "compose.yaml")["services"]
        for name in ("vllm-embed", "vllm-rerank"):
            self.assertEqual(node[name]["image"], single[name]["image"], name)
            self.assertEqual(node[name]["command"], single[name]["command"], name)

    def test_both_halves_extend_one_definition(self):
        llms = self.stack_llms()
        self.assertTrue(llms)
        for e in llms:
            head = self.load("compose", "llm", e["id"] + ".yaml")["services"]["vllm-llm-rank"]
            node = self.load("compose", "node", "llm", e["id"] + ".yaml")["services"]["vllm-llm-rank"]
            # extends paths are relative to each project's directory (compose/ and compose/node/)
            self.assertEqual(os.path.normpath(os.path.join(ROOT, "compose", head["extends"]["file"])),
                             os.path.normpath(os.path.join(ROOT, "compose", "node", node["extends"]["file"])))
            self.assertEqual(head["extends"]["service"], node["extends"]["service"])
            self.assertEqual(head["command"][:2], ["--node-rank", "0"])
            self.assertEqual(node["command"], ["--node-rank", "1", "--headless"])
            rank = self.load("compose", "ranks", e["id"] + ".yaml")["services"][head["extends"]["service"]]
            entry = rank["entrypoint"]
            self.assertEqual(entry[:3], ["vllm", "serve", e["hf_repo"]])
            self.assertEqual(entry[entry.index("--served-model-name") + 1], e["served_name"])
            self.assertIn("--revision", entry)
            self.assertIn("@sha256:", rank["image"])
            self.assertEqual(rank["restart"], "always")              # vLLM exits with 0 when it loses its partner
            self.assertNotIn("--node-rank", entry)
            self.assertNotIn("NCCL_IB_HCA", rank["environment"])     # one half of the cable only
            self.assertNotIn("NCCL_IB_GID_INDEX", rank["environment"])
            env = self.load("compose", "llm", e["id"] + ".yaml")["services"]["ragflow"]["environment"]
            self.assertEqual(e["served_name"], env["RAGSPARK_LLM_NAME"])
            self.assertEqual(e["context"]["default"], int(entry[entry.index("--max-model-len") + 1]))
            self.assertEqual(str(e["context"]["default"]), env["RAGSPARK_LLM_CONTEXT"])
            self.assertEqual(e["tool_calling"], env["RAGSPARK_LLM_TOOLS"] == "1")
            self.assertEqual(e["vision"], env["RAGSPARK_LLM_VISION"] == "1")
            self.assertEqual(e["nodes"], 2)
            self.assertTrue(e["summary"]["en"].strip() and e["summary"]["ru"].strip())
            self.assertTrue(str(e["license"]).strip() and not str(e["license"]).startswith("TBD"))
            self.assertIsInstance(e["validated"], bool)

    def test_relays_need_no_gpu_and_run_an_image_the_head_has(self):
        stack = self.load("compose", "stack.yaml")["services"]
        rank_images = {self.load("compose", "ranks", os.path.basename(p))["services"]["rank"]["image"]
                       for p in glob.glob(os.path.join(ROOT, "compose", "ranks", "*.yaml"))}
        for name in ("vllm-llm", "vllm-embed", "vllm-rerank"):
            svc = stack[name]
            self.assertEqual(svc["entrypoint"], ["python3", "-I", "/relay.py"], name)
            self.assertIn(svc["image"], rank_images, name)
            self.assertEqual(svc["user"], "65534:65534", name)
            self.assertEqual(svc["command"][0], "8000", name)
        for dep in stack["ragflow"]["depends_on"].values():
            self.assertEqual(dep["condition"], "service_started")


class WatchHead(unittest.TestCase):
    def test_restart_after_two_failed_requests_while_healthy(self):
        state, action = watch.decide_head({}, ("running", 100, "healthy"), lambda: False)
        self.assertIsNone(action)
        state, action = watch.decide_head(state, ("running", 100, "healthy"), lambda: False)
        self.assertEqual(action, "restart")
        self.assertEqual(state["fails"], 0)

    def test_one_answer_resets_the_count(self):
        state, _ = watch.decide_head({}, ("running", 100, "healthy"), lambda: False)
        state, action = watch.decide_head(state, ("running", 100, "healthy"), lambda: True)
        state, action = watch.decide_head(state, ("running", 100, "healthy"), lambda: False)
        self.assertIsNone(action)

    def test_no_request_while_starting_or_stopped(self):
        asked = []
        for rank in (None, ("running", 100, "starting"), ("exited", 100, "unhealthy"), ("restarting", 100, "")):
            state, action = watch.decide_head({"fails": 1}, rank, lambda: asked.append(1) or False)
            self.assertIsNone(action)
            self.assertEqual(state["fails"], 0)
        self.assertEqual(asked, [])


class WatchNode(unittest.TestCase):
    def test_restart_once_when_the_head_went_away(self):
        rank = ("running", 1000, "")
        state, action = watch.decide_node({}, rank, True, 2000)          # pair formed: armed
        self.assertIsNone(action)
        state, action = watch.decide_node(state, rank, False, 2010)      # head lost
        self.assertIsNone(action)
        state, action = watch.decide_node(state, rank, False, 2010 + watch.GRACE_S - 1)
        self.assertIsNone(action)
        state, action = watch.decide_node(state, rank, False, 2010 + watch.GRACE_S)
        self.assertEqual(action, "restart")
        state, action = watch.decide_node(state, rank, False, 2010 + 10 * watch.GRACE_S)
        self.assertIsNone(action)                                         # disarmed until the head is back

    def test_a_short_gap_does_not_restart(self):
        rank = ("running", 1000, "")
        state, _ = watch.decide_node({}, rank, True, 2000)
        state, _ = watch.decide_node(state, rank, False, 2010)
        state, action = watch.decide_node(state, rank, True, 2030)
        state, action = watch.decide_node(state, rank, False, 2040 + watch.GRACE_S - 1)
        self.assertIsNone(action)

    def test_a_forming_pair_is_left_alone(self):
        # Boot: both halves start, the head is unreachable for minutes - never touch the node's half.
        state = {"rank_started": 500, "head_seen_ok": True}               # left over from before the reboot
        rank = ("running", 3000, "")
        for now in range(3000, 3000 + 20 * watch.GRACE_S, 30):
            state, action = watch.decide_node(state, rank, False, now)
            self.assertIsNone(action)

    def test_a_local_restart_since_the_loss_counts_as_recovered(self):
        state, _ = watch.decide_node({}, ("running", 1000, ""), True, 2000)
        state, _ = watch.decide_node(state, ("running", 1000, ""), False, 2010)
        state, action = watch.decide_node(state, ("running", 2050, ""), False, 2010 + 2 * watch.GRACE_S)
        self.assertIsNone(action)                                         # it restarted by itself: wait for the head

    def test_stopped_half_is_dockers_business(self):
        self.assertIsNone(watch.decide_node({"head_seen_ok": True, "head_lost_at": 1}, None, False, 10 ** 6)[1])
        self.assertIsNone(watch.decide_node({"head_seen_ok": True, "head_lost_at": 1}, ("exited", 1, ""), False, 10 ** 6)[1])

    def test_started_epoch(self):
        self.assertEqual(watch.started_epoch("1970-01-01T00:01:40.123456789Z"), 100)
        self.assertEqual(watch.started_epoch("0001-01-01T00:00:00Z"), watch.started_epoch("0001-01-01T00:00:00Z"))
        self.assertEqual(watch.started_epoch(""), 0)


if __name__ == "__main__":
    unittest.main()


class Roles(unittest.TestCase):
    def setUp(self):
        from rsinst import paths
        self.paths = paths
        self.addCleanup(paths.set_role, "head")

    def test_the_node_runs_compose_in_its_own_directory_and_project(self):
        p = self.paths
        p.set_role("node")
        self.assertTrue(p.compose_dir().endswith(os.path.join("compose", "node")))
        self.assertEqual(p.project(), "ragspark-node")
        p.set_role("anything else")
        self.assertTrue(p.compose_dir().endswith("compose"))
        self.assertEqual(p.project(), "ragspark")

    def test_compose_files_per_mode_and_role(self):
        from rsinst import stackmode
        big, small = "deepseek-v4-flash-vision", "qwen-3.8-27b-nvfp4"
        self.assertTrue(stackmode.is_stack(big))
        self.assertFalse(stackmode.is_stack(small))
        self.assertFalse(stackmode.is_stack(""))
        self.assertEqual(stackmode.compose_file(big), "compose.yaml:stack.yaml:llm/%s.yaml" % big)
        self.assertEqual(stackmode.compose_file(big, "node"), "compose.yaml:llm/%s.yaml" % big)
        self.assertEqual(stackmode.compose_file(small), "compose.yaml:llm/%s.yaml" % small)
        self.assertEqual(stackmode.compose_args(big),
                         ["-f", "compose.yaml", "-f", "stack.yaml", "-f", "llm/%s.yaml" % big])
        for llm, role in ((big, "head"), (big, "node"), (small, "head")):
            base = os.path.join(ROOT, "compose", *(["node"] if role == "node" else []))
            for f in stackmode.compose_file(llm, role).split(":"):
                self.assertTrue(os.path.exists(os.path.join(base, f)), (llm, role, f))
        self.assertEqual(stackmode.addresses({}), ("192.168.100.10", "192.168.100.11"))
        self.assertEqual(stackmode.addresses({"node_ip": "10.0.0.2"}), ("192.168.100.10", "10.0.0.2"))


class CableChecks(unittest.TestCase):
    """preflight.check_cable reads /sys and /dev through the test prefix."""

    def setUp(self):
        import shutil
        import tempfile
        from unittest import mock
        from rsinst import paths, preflight
        self.preflight = preflight
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patcher = mock.patch.object(paths, "PREFIX", self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        lines = ("1: lo    inet 127.0.0.1/8 scope host lo\\\n"
                 "3: enp1s0f1np1    inet 192.168.100.10/24 brd 192.168.100.255 scope global enp1s0f1np1\\\n")
        patcher = mock.patch.object(preflight, "run", return_value=(0, lines))
        patcher.start()
        self.addCleanup(patcher.stop)

    def sys(self, rel, text):
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text + "\n")

    def report(self, ip="192.168.100.10"):
        from rsinst import ui
        r = self.preflight.Report()
        with open(os.devnull, "w") as null, __import__("contextlib").redirect_stdout(null):
            iface = self.preflight.check_cable(r, ip)
        return iface, [k for k, _ in r.rows], ui

    def link(self, speed="200000", mtu="9000", state="up", ports=2):
        for name, value in (("operstate", state), ("speed", speed), ("mtu", mtu)):
            self.sys("sys/class/net/enp1s0f1np1/" + name, value)
        os.makedirs(os.path.join(self.tmp, "dev", "infiniband"), exist_ok=True)
        for i in range(ports):
            self.sys("sys/class/infiniband/roce%d/ports/1/state" % i, "4: ACTIVE")

    def test_a_good_cable(self):
        self.link()
        self.assertEqual(self.report()[:2], ("enp1s0f1np1", ["OK", "OK"]))

    def test_no_address_no_rdma_slow_or_half(self):
        self.assertEqual(self.report("10.9.9.9")[:2], (None, ["FAIL"]))
        self.link(speed="13000")
        self.assertEqual(self.report()[1], ["WARN", "OK"])                 # the ~13 Gb/s state after a re-plug
        self.link(mtu="1500")
        self.assertEqual(self.report()[1], ["WARN", "OK"])
        os.remove(os.path.join(self.tmp, "sys/class/infiniband/roce1/ports/1/state"))
        self.link(ports=1)
        self.assertEqual(self.report()[1], ["OK", "WARN"])                 # one half of the cable
        os.rmdir(os.path.join(self.tmp, "dev", "infiniband"))
        self.assertEqual(self.report()[1], ["OK", "FAIL"])
        self.link(state="down")
        self.assertEqual(self.report()[1][0], "FAIL")


@unittest.skipUnless(HAVE_YAML, "needs python3-yaml")
class Menu(unittest.TestCase):
    def test_the_head_is_offered_both_modes_and_the_node_only_stack(self):
        cat, files, head = install.llm_offer()
        self.assertEqual(head, ["qwen-3.8-27b-nvfp4", "qwen-3.6-35b-a3b-nvfp4", "deepseek-v4-flash-vision"])
        self.assertEqual(install.llm_offer("node")[2], ["deepseek-v4-flash-vision"])
        from rsinst import catalog, ui
        ui.set_lang("en")
        facts = catalog.facts(catalog.entry(cat, "llm", "deepseek-v4-flash-vision"), 121)
        self.assertIn("2 units, ~92 GiB each", facts)
        self.assertIn("MIT", facts)
