"""A small stand-in for the RAGFlow 0.27.2 API, enough for tools/acceptance.py.

Request and response shapes follow api/apps/restful_apis/ in RAGFlow 0.27.2.
MODE changes the chat's behaviour so tests can see the tool fail:
  good  - correct answers with sources, "no answer" text for the unrelated question
  leak  - the unrelated question gets an invented answer
  cjk   - an answer contains Chinese characters
"""
import json
import re
import threading
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "tok-123"
SAMPLE_TEXT = ("Резервная копия базы знаний создаётся автоматически каждую среду в 02:30. "
               "Температура воздуха в серверной не должна превышать 27 °C.")


class State:
    def __init__(self, mode="good"):
        self.mode = mode
        self.datasets, self.docs, self.chats = {}, {}, {}
        self.polls = 0
        self.log = []


def make_handler(state):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, obj, code=200, headers=None):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def ok(self, data=None):
            self.send({"code": 0, "message": "", "data": data})

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        def authed(self):
            if self.headers.get("Authorization") != TOKEN:
                self.send({"code": 401, "message": "unauthorized"})
                return False
            return True

        def route(self, method):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            p = u.path
            raw = self.body()
            state.log.append("%s %s" % (method, p))
            if method == "POST" and p == "/api/v1/auth/login":
                b = json.loads(raw)
                if b.get("password") == "ENC:secret":
                    return self.send({"code": 0, "data": {}}, headers={"Authorization": TOKEN})
                return self.send({"code": 109, "message": "Email and password do not match!"})
            if not self.authed():
                return
            if method == "GET" and p == "/api/v1/models":
                return self.ok([{"model_id": "m-rerank", "name": "BAAI/bge-reranker-v2-m3",
                                 "instance_name": "ragspark-rerank", "model_type": "rerank"}])
            if p == "/api/v1/chats":
                if method == "GET":
                    name = (q.get("name") or [""])[0]
                    return self.ok([c for c in state.chats.values() if not name or c["name"] == name])
                if method == "DELETE":
                    for i in json.loads(raw)["ids"]:
                        state.chats.pop(i, None)
                    return self.ok({"success_count": 1})
                if method == "POST":
                    b = json.loads(raw)
                    for ds in b.get("dataset_ids", []):
                        if not any(d["chunk_count"] for d in state.docs.values() if d["dataset_id"] == ds):
                            return self.send({"code": 102, "message": "The dataset %s doesn't own parsed file" % ds})
                    if b.get("tenant_rerank_id") != "m-rerank":
                        return self.send({"code": 102, "message": "`tenant_rerank_id` must be a valid tenant model id"})
                    cid = uuid.uuid4().hex
                    chat = dict(b, id=cid, rerank_id="BAAI/bge-reranker-v2-m3@ragspark-rerank@VLLM")
                    state.chats[cid] = chat
                    return self.ok(chat)
            if p == "/api/v1/datasets":
                if method == "GET":
                    name = (q.get("name") or [""])[0]
                    return self.ok([d for d in state.datasets.values() if not name or d["name"] == name])
                if method == "DELETE":
                    for i in json.loads(raw)["ids"]:
                        state.datasets.pop(i, None)
                    return self.ok({})
                if method == "POST":
                    b = json.loads(raw)
                    assert b["parser_config"]["layout_recognize"] == "Plain Text", b
                    did = uuid.uuid4().hex
                    state.datasets[did] = dict(b, id=did)
                    return self.ok(state.datasets[did])
            m = re.match(r"^/api/v1/datasets/(\w+)/documents$", p)
            if m and method == "POST":
                assert b"filename=\"sample-ru.pdf\"" in raw and b"%PDF" in raw
                doc = {"id": uuid.uuid4().hex, "dataset_id": m.group(1), "name": "sample-ru.pdf",
                       "run": "UNSTART", "progress": 0, "chunk_count": 0}
                state.docs[doc["id"]] = doc
                return self.ok([doc])
            if m and method == "GET":
                state.polls += 1
                for d in state.docs.values():
                    if d["run"] == "RUNNING" and state.polls >= 2:
                        d.update(run="DONE", progress=1.0, chunk_count=2)
                return self.ok({"total": len(state.docs), "docs": [d for d in state.docs.values()
                                                                  if d["dataset_id"] == m.group(1)]})
            if re.match(r"^/api/v1/datasets/\w+/documents/parse$", p) and method == "POST":
                for i in json.loads(raw)["document_ids"]:
                    state.docs[i]["run"] = "RUNNING"
                return self.ok(None)
            if re.match(r"^/api/v1/datasets/\w+/documents/\w+/chunks$", p) and method == "GET":
                chunks = [{"content": SAMPLE_TEXT[:80]}, {"content": SAMPLE_TEXT[80:]}]
                return self.ok({"total": 2, "chunks": chunks if (q.get("page") or ["1"])[0] == "1" else []})
            if p == "/api/v1/chat/completions" and method == "POST":
                b = json.loads(raw)
                chat = state.chats[b["chat_id"]]
                question = b["question"]
                ref = {"chunks": [{"content": SAMPLE_TEXT, "document_name": "sample-ru.pdf"}], "doc_aggs": []}
                if "Австрал" in question:
                    if state.mode == "leak":
                        return self.ok({"answer": "Столица Австралии — Канберра.", "reference": ref})
                    return self.ok({"answer": chat["prompt_config"]["empty_response"],
                                    "reference": {"chunks": [], "doc_aggs": []}})
                if "резерв" in question.lower():
                    answer = "Резервная копия создаётся каждую среду в 02:30 [ID:0]."
                else:
                    answer = "Не выше 27 °C [ID:0]."
                if state.mode == "cjk":
                    answer += " 相关信息"
                return self.ok({"answer": answer, "reference": ref})
            self.send({"code": 404, "message": "mock: no route %s %s" % (method, p)}, 404)

        def do_GET(self):
            self.route("GET")

        def do_POST(self):
            self.route("POST")

        def do_DELETE(self):
            self.route("DELETE")

    return H


def serve(mode="good"):
    """Start in a thread; returns (server, state, base_url)."""
    state = State(mode)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, state, "http://127.0.0.1:%d" % srv.server_address[1]
