"""tools/ragflow_models.py against a mock of RAGFlow's model registry: first setup, rerun,
and a change of the LLM (and back) with its chats, defaults and scan datasets."""
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import mock_providers  # noqa: E402

LLM_URL, EMBED_URL, RERANK_URL = "http://vllm-llm:8000/v1", "http://vllm-embed:8000/v1", "http://vllm-rerank:8000/v1"
OLD, NEW = "qwen3.8-27b", "qwen3.6-35b-a3b"


def serving(llm):
    return {LLM_URL: [llm], EMBED_URL: ["deepvk/USER-bge-m3"], RERANK_URL: ["BAAI/bge-reranker-v2-m3"]}


class RagflowModels(unittest.TestCase):
    def setUp(self):
        self.srv, self.state, self.url = mock_providers.serve(serving(OLD))
        self.addCleanup(self.srv.server_close)                  # cleanups run last-in first-out
        self.addCleanup(self.srv.shutdown)

    def run_tool(self, llm, *extra, vision="1", **env_extra):
        env = dict(os.environ, RAGFLOW_API=self.url, ADMIN_DEFAULT_PASSWORD="secret",
                   DEFAULT_SUPERUSER_EMAIL="admin@ragspark.local", RAGSPARK_LLM_NAME=llm,
                   RAGSPARK_LLM_CONTEXT="65536", RAGSPARK_LLM_TOOLS="1", RAGSPARK_LLM_VISION=vision,
                   PYTHONPATH=os.path.join(ROOT, "tests", "fake"), **env_extra)
        p = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "ragflow_models.py")] + list(extra),
                           env=env, capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout + p.stderr

    def install(self):
        """A first setup with the default LLM, the installer's two chats and a scan dataset."""
        code, out = self.run_tool(OLD)
        self.assertEqual(code, 0, out)
        s = self.state
        old_chat = s.model("ragspark-llm", OLD)["id"]
        self.sample = s.add_chat("RAGSpark — пример", old_chat)            # the installer stores the model id
        self.assistant = s.add_chat("Ассистент", old_chat)
        self.other = s.add_chat("my own", "someone-else@other@VLLM")        # not RAGSpark's: left alone
        self.scans = s.add_dataset("scans", "%s@ragspark-vision@VLLM" % OLD, docs=[
            ("a.pdf", {"layout_recognize": "%s@ragspark-vision@VLLM" % OLD, "raptor": {"use_raptor": True}}),
            ("b.pdf", {"layout_recognize": "Plain Text"})])
        self.plain = s.add_dataset("plain", "Plain Text")
        s.writes.clear()

    def test_first_setup_then_rerun_changes_nothing(self):
        self.install()
        s = self.state
        self.assertEqual(sorted(s.instances), ["ragspark-embed", "ragspark-llm", "ragspark-rerank", "ragspark-vision"])
        self.assertEqual(sorted(s.defaults), ["chat", "embedding", "rerank"])     # vision is registered only
        code, out = self.run_tool(OLD)
        self.assertEqual(code, 0, out)
        self.assertEqual(s.writes, [], out)

    def test_change_the_llm_and_back(self):
        self.install()
        s = self.state
        s.served[LLM_URL] = [NEW]                               # vllm-llm now serves the other model

        code, out = self.run_tool(NEW, "--check")               # read-only: reports, changes nothing
        self.assertEqual(code, 0, out)
        self.assertEqual(s.writes, [], out)
        for line in ("would add model %s to instance ragspark-llm" % NEW, "chat 'RAGSpark — пример' would move",
                     "dataset 'scans' would parse scans with", "document 'a.pdf' in 'scans'",
                     "%s - would be disabled in instance ragspark-vision" % OLD):
            self.assertIn(line, out)

        code, out = self.run_tool(NEW)
        self.assertEqual(code, 0, out)
        new_chat, new_vision = s.model("ragspark-llm", NEW), s.model("ragspark-vision", NEW)
        self.assertEqual((new_chat["type"], new_vision["type"]), ("chat", "vision"))
        self.assertEqual(s.defaults["chat"], new_chat["id"])
        for cid in (self.sample, self.assistant):
            self.assertEqual(s.chats[cid]["tenant_llm_id"], new_chat["id"])
        self.assertEqual(s.chats[self.other]["llm_id"], "someone-else@other@VLLM")
        new_ref = "%s@ragspark-vision@VLLM" % NEW
        self.assertEqual(s.datasets[self.scans]["parser_config"]["layout_recognize"], new_ref)
        self.assertEqual(s.datasets[self.scans]["parser_config"]["chunk_token_num"], 512)    # merged, not replaced
        a, b = s.docs[self.scans]
        self.assertEqual(a["parser_config"], {"layout_recognize": new_ref, "raptor": {"use_raptor": True}})
        self.assertEqual(b["parser_config"], {"layout_recognize": "Plain Text"})
        self.assertEqual(s.datasets[self.plain]["parser_config"]["layout_recognize"], "Plain Text")
        for inst in ("ragspark-llm", "ragspark-vision"):
            self.assertEqual(s.model(inst, OLD)["status"], "inactive", inst)
        self.assertEqual(sorted(s.defaults), ["chat", "embedding", "rerank"])

        s.writes.clear()
        code, out = self.run_tool(NEW)                          # rerun after the change: nothing left to do
        self.assertEqual((code, s.writes), (0, []), out)

        s.served[LLM_URL] = [OLD]                               # and back: the old records are enabled again
        old_chat = s.model("ragspark-llm", OLD)
        code, out = self.run_tool(OLD)
        self.assertEqual(code, 0, out)
        self.assertIn("enabled again in instance ragspark-llm", out)
        self.assertEqual(old_chat["status"], "active")
        self.assertEqual(s.defaults["chat"], old_chat["id"])
        for cid in (self.sample, self.assistant):
            self.assertEqual(s.chats[cid]["tenant_llm_id"], old_chat["id"])
        self.assertEqual(s.docs[self.scans][0]["parser_config"]["layout_recognize"], "%s@ragspark-vision@VLLM" % OLD)
        self.assertEqual(s.model("ragspark-llm", NEW)["status"], "inactive")
        self.assertEqual(len([m for m in s.models.values() if m["instance"] == "ragspark-llm"]), 2)   # no duplicates

    def test_another_login_during_a_change(self):
        """RAGFlow keeps one session per user: a superuser login elsewhere - the web interface - while
        the tool runs ends the tool's session (VALIDATION section 18). It logs in again and finishes."""
        self.install()
        self.state.served[LLM_URL] = [NEW]
        before = self.state.requests
        self.assertEqual(self.run_tool(NEW)[0], 0)
        total = self.state.requests - before                    # the requests of an undisturbed change
        for at in (1, total // 2, total - 1):
            with self.subTest(at=at):
                self.setUp()                                    # a fresh RAGFlow
                self.install()
                s = self.state
                s.served[LLM_URL] = [NEW]
                s.end_session_after = s.requests + at
                code, out = self.run_tool(NEW)
                self.assertEqual(code, 0, out)
                self.assertIn("another login ended the RAGFlow session", out)
                new_chat = s.model("ragspark-llm", NEW)
                self.assertEqual(s.defaults["chat"], new_chat["id"])
                for cid in (self.sample, self.assistant):
                    self.assertEqual(s.chats[cid]["tenant_llm_id"], new_chat["id"])
                self.assertEqual(s.docs[self.scans][0]["parser_config"]["layout_recognize"],
                                 "%s@ragspark-vision@VLLM" % NEW)
                for inst in ("ragspark-llm", "ragspark-vision"):
                    self.assertEqual(s.model(inst, OLD)["status"], "inactive", inst)

    def test_a_session_that_keeps_ending_fails_without_looping(self):
        self.install()
        s = self.state
        s.served[LLM_URL] = [NEW]
        s.end_session_always = True                             # one login again per request, then it gives up
        code, out = self.run_tool(NEW)
        self.assertNotEqual(code, 0, out)
        self.assertIn("HTTP 401", out)

    def test_without_ragflow_services_documents_keep_their_settings(self):
        """The API hides a document's RAPTOR settings; if they cannot be read, the document is not touched."""
        self.install()
        s = self.state
        s.served[LLM_URL] = [NEW]
        code, out = self.run_tool(NEW, RAGSPARK_FAKE_NO_SERVICES="1")
        self.assertEqual(code, 1, out)
        self.assertIn("cannot read its RAPTOR settings", out)
        a = s.docs[self.scans][0]
        self.assertEqual(a["parser_config"], {"layout_recognize": "%s@ragspark-vision@VLLM" % OLD,
                                              "raptor": {"use_raptor": True}})
        self.assertEqual(s.datasets[self.scans]["parser_config"]["layout_recognize"], "%s@ragspark-vision@VLLM" % NEW)
        self.assertEqual(s.chats[self.sample]["tenant_llm_id"], s.model("ragspark-llm", NEW)["id"])

    def test_a_model_the_server_does_not_list_is_not_added(self):
        self.install()
        code, out = self.run_tool(NEW)                          # vllm-llm still serves the old model
        self.assertEqual(code, 1, out)
        self.assertIn("model not served", out)
        self.assertIsNone(self.state.model("ragspark-llm", NEW))
        self.assertEqual(self.state.model("ragspark-llm", OLD)["status"], "active")
        self.assertEqual(self.state.chats[self.sample]["tenant_llm_id"], self.state.model("ragspark-llm", OLD)["id"])

    def test_the_embedding_model_is_never_changed_in_place(self):
        self.install()
        s = self.state
        s.models[s.model("ragspark-embed", "deepvk/USER-bge-m3")["id"]]["name"] = "other/embedder"
        code, out = self.run_tool(OLD)
        self.assertEqual(code, 1, out)
        self.assertIn("does not change this model in place", out)
        self.assertIsNone(s.model("ragspark-embed", "deepvk/USER-bge-m3"))
        self.assertEqual(s.model("ragspark-embed", "other/embedder")["status"], "active")

    def test_a_vision_default_set_by_hand_follows_the_change(self):
        self.install()
        s = self.state
        s.defaults["vision"] = s.model("ragspark-vision", OLD)["id"]
        s.served[LLM_URL] = [NEW]
        code, out = self.run_tool(NEW)
        self.assertEqual(code, 0, out)
        self.assertEqual(s.defaults["vision"], s.model("ragspark-vision", NEW)["id"])
        self.assertIn("set by hand", out)

    def test_an_llm_without_vision_warns_about_the_vision_instance(self):
        self.install()
        code, out = self.run_tool(OLD, vision="0")
        self.assertEqual(code, 0, out)
        self.assertIn("the current LLM file has no vision", out)


if __name__ == "__main__":
    unittest.main()
