"""A small stand-in for RAGFlow 0.27.2's model registry, enough for tools/ragflow_models.py.

Request and response shapes follow api/apps/restful_apis/ (provider_api, models_api,
chat_api, dataset_api, document_api) in RAGFlow 0.27.2. `served` plays the vLLM
servers: base_url -> the model names they list.
"""
import json
import threading
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "tok-models"


def new_id():
    return uuid.uuid4().hex


class State:
    def __init__(self, served):
        self.served = served                    # base_url -> [model names]
        self.provider = False
        self.instances = {}                     # name -> {"id", "instance_name", "base_url"}
        self.models = {}                        # id -> {"id", "instance", "name", "type", "status", ...}
        self.defaults = {}                      # model type -> model id
        self.chats, self.datasets, self.docs = {}, {}, {}
        self.writes = []                        # every request that changes something

    def model(self, instance, name):
        for m in self.models.values():
            if m["instance"] == instance and m["name"] == name:
                return m
        return None

    def resolve(self, ref):
        """A tenant-model id or a composite name "model@instance@VLLM" -> model record."""
        if ref in self.models:
            return self.models[ref]
        parts = (ref or "").split("@")
        return self.model(parts[1], parts[0]) if len(parts) == 3 else None

    # helpers for tests
    def add_chat(self, name, ref):
        cid = new_id()
        m = self.resolve(ref)
        self.chats[cid] = {"id": cid, "name": name, "llm_id": ref, "tenant_llm_id": m["id"] if m else None}
        return cid

    def add_dataset(self, name, layout, docs=()):
        kid = new_id()
        self.datasets[kid] = {"id": kid, "name": name, "parser_config": {"layout_recognize": layout, "chunk_token_num": 512}}
        self.docs[kid] = [{"id": new_id(), "name": n, "parser_config": dict(pc)} for n, pc in docs]
        return kid


# RAGFlow 0.27.2 builds parser_config in its API responses from a schema without these
HIDDEN = ("raptor", "graphrag")


def public(record):
    out = dict(record)
    out["parser_config"] = {k: v for k, v in record["parser_config"].items() if k not in HIDDEN}
    return out


def _types(name):
    return ["embedding"] if "bge-m3" in name else ["rerank"] if "reranker" in name else ["chat"]


def make_handler(state):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, obj, headers=None):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(200)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def ok(self, data=None, headers=None):
            self.send({"code": 0, "message": "success", "data": data}, headers)

        def fail(self, message):
            self.send({"code": 102, "message": message})

        def route(self, method):
            u = urllib.parse.urlparse(self.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            p = urllib.parse.unquote(u.path)
            n = int(self.headers.get("Content-Length") or 0)
            b = json.loads(self.rfile.read(n) or b"{}") if n else {}
            if method == "GET" and p.startswith("/mock/documents/"):    # for tests/fake DocumentService only
                doc_id = p.rsplit("/", 1)[1]
                doc = next((d for docs in state.docs.values() for d in docs if d["id"] == doc_id), None)
                return self.send(doc)
            if method == "POST" and p == "/api/v1/auth/login":
                return self.ok({"email": b.get("email")}, {"Authorization": TOKEN})
            if self.headers.get("Authorization") != TOKEN:
                return self.send({"code": 401, "message": "unauthorized"})
            if method != "GET" and not p.endswith("/connection"):
                state.writes.append("%s %s" % (method, p))
            page, size = int(q.get("page", 1)), int(q.get("page_size", 30))

            def paged(rows):
                return rows[(page - 1) * size:page * size]

            pre = "/api/v1/providers/VLLM"
            if method == "PUT" and p == "/api/v1/providers":
                state.provider = True
                return self.ok()
            if method == "POST" and p == pre + "/connection":
                return self.ok() if b.get("base_url") in state.served else self.fail("connection refused")
            if method == "GET" and p == pre + "/models":
                return self.ok([{"name": m, "model_types": _types(m)} for m in state.served.get(q.get("base_url"), [])])
            if p == pre + "/instances":
                if method == "GET":
                    return self.ok([{"id": i["id"], "instance_name": i["instance_name"]} for i in state.instances.values()])
                inst = {"id": new_id(), "instance_name": b["instance_name"], "base_url": b["base_url"]}
                state.instances[inst["instance_name"]] = inst
                for info in b.get("model_info", []):
                    mid = new_id()
                    state.models[mid] = {"id": mid, "instance": inst["instance_name"], "name": info["model_name"],
                                         "type": info["model_type"][0], "status": "active"}
                return self.ok()
            if p.startswith(pre + "/instances/"):
                rest = p[len(pre + "/instances/"):]
                key, _, tail = rest.partition("/")
                inst = state.instances.get(key) or next((i for i in state.instances.values() if i["id"] == key), None)
                if not inst:
                    return self.fail("No instance found")
                name = inst["instance_name"]
                if method == "GET" and not tail:
                    return self.ok(dict(inst))
                if tail == "models" and method == "GET":
                    return self.ok([{"name": m["name"], "model_type": [m["type"]], "status": m["status"]}
                                    for m in state.models.values() if m["instance"] == name])
                if tail == "models" and method == "POST":
                    if state.model(name, b["model_name"]):
                        return self.fail("Model '%s' already exists" % b["model_name"])
                    mid = new_id()
                    t = b["model_type"]
                    state.models[mid] = {"id": mid, "instance": name, "name": b["model_name"],
                                         "type": t[0] if isinstance(t, list) else t, "status": "active"}
                    return self.ok()
                if tail.startswith("models/") and method == "PATCH":
                    m = state.model(name, tail[len("models/"):])
                    if not m:
                        return self.fail("model not found")
                    m["status"] = b.get("status", m["status"])
                    return self.ok()
            if method == "GET" and p == "/api/v1/models":
                return self.ok([{"model_id": m["id"], "name": m["name"], "instance_name": m["instance"],
                                 "model_type": [m["type"]], "status": m["status"]} for m in state.models.values()])
            if p == "/api/v1/models/default":
                if method == "GET":
                    rows = []
                    for t, mid in state.defaults.items():
                        m = state.models[mid]
                        rows.append({"model_type": t, "model_id": mid, "model_name": m["name"], "model_instance": m["instance"]})
                    return self.ok(rows)
                m = state.models.get(b.get("model_id"))
                if not m or m["status"] != "active":
                    return self.fail("Model isn't available")
                state.defaults[b["model_type"]] = m["id"]
                return self.ok()
            if p == "/api/v1/chats" and method == "GET":
                # RAGFlow 0.27.2 lists chats without tenant_llm_id (None) although the record has it
                return self.ok(paged([dict(c, tenant_llm_id=None) for c in state.chats.values()]))
            if p.startswith("/api/v1/chats/") and method == "PATCH":
                chat = state.chats.get(p.rsplit("/", 1)[1])
                m = state.resolve(b.get("llm_id"))
                if not chat or not m or m["status"] != "active":
                    return self.fail("`llm_id` %s doesn't exist" % b.get("llm_id"))
                chat["llm_id"], chat["tenant_llm_id"] = b["llm_id"], m["id"]
                return self.ok(chat)
            if p == "/api/v1/datasets" and method == "GET":
                return self.ok(paged([public(kb) for kb in state.datasets.values()]))
            if p.startswith("/api/v1/datasets/"):
                rest = p[len("/api/v1/datasets/"):].split("/")
                kb = state.datasets.get(rest[0])
                if not kb:
                    return self.fail("no dataset")
                if len(rest) == 1 and method == "PUT":
                    kb["parser_config"].update(b.get("parser_config") or {})       # deep_merge
                    return self.ok()
                if len(rest) == 2 and method == "GET":
                    docs = state.docs.get(kb["id"], [])
                    return self.ok({"docs": paged([public(d) for d in docs]), "total": len(docs)})
                if len(rest) == 3 and method == "PATCH":
                    doc = next((d for d in state.docs[kb["id"]] if d["id"] == rest[2]), None)
                    cfg = b.get("parser_config") or {}
                    doc["parser_config"].update(cfg)
                    if not cfg.get("raptor") and doc["parser_config"].get("raptor"):
                        del doc["parser_config"]["raptor"]          # DocumentService.update_parser_config does this
                    return self.ok()
            return self.send({"code": 404, "message": "mock: no route %s %s" % (method, p)})

        def do_GET(self):
            self.route("GET")

        def do_POST(self):
            self.route("POST")

        def do_PUT(self):
            self.route("PUT")

        def do_PATCH(self):
            self.route("PATCH")

    return H


def serve(served):
    """Start in a thread; returns (server, state, base_url)."""
    state = State(served)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, state, "http://127.0.0.1:%d" % srv.server_address[1]
