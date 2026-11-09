#!/usr/bin/env python3
"""ragflow_models.py - register RAGSpark's vLLM models in RAGFlow 0.27 (idempotent).

Runs INSIDE the RAGFlow container, so it uses RAGFlow's own password
encryption and needs nothing on the host. From the compose/ directory:

  sudo docker compose exec -T ragflow \
    sh -c 'cd /ragflow && PYTHONPATH=/ragflow .venv/bin/python - --check' < ../tools/ragflow_models.py

  --check        read-only: verify connectivity, report what would change
  (no flag)      create missing instances, set default models

An instance's address is never changed; a mismatch is reported instead.
Everything comes from the container environment (compose/.env and the LLM
file): the superuser DEFAULT_SUPERUSER_EMAIL / ADMIN_DEFAULT_PASSWORD, and
the LLM RAGSPARK_LLM_NAME, RAGSPARK_LLM_CONTEXT, RAGSPARK_LLM_TOOLS and
RAGSPARK_LLM_VISION. A spec entry with "default": false is registered but
never made the default model of its type.

Changing the LLM (`ragspark llm`): the chat and vision instances serve the
model of the current LLM file. When an instance holds another model, the
current one is added to it, the default model, every chat and every dataset
and document that parses scans with the old model move to the current one,
and the old model is disabled (kept, so switching back re-enables it).
RAGFlow cannot rename a model, and chats refer to it by its record id.
A document's RAPTOR settings are read through RAGFlow's own services in
this container: the API leaves them out of its records, and an update
without them deletes them. Every change goes through the API.
The embedding and rerank models are never changed in place: the search
index depends on the embedding model.
Request formats were captured from RAGFlow 0.27.2.
"""
import argparse, base64, http.cookiejar, json, os, sys, urllib.error, urllib.parse, urllib.request

PROVIDER = "VLLM"
# Host names are the compose service names (compose/compose.yaml).
DEFAULT_SPEC = [
    {"instance": "ragspark-llm", "base_url": "http://vllm-llm:8000/v1",
     "model": os.environ.get("RAGSPARK_LLM_NAME", "qwen3.8-27b"), "type": "chat",
     "max_tokens": int(os.environ.get("RAGSPARK_LLM_CONTEXT", "65536")),
     "is_tools": os.environ.get("RAGSPARK_LLM_TOOLS", "1") == "1"},
    {"instance": "ragspark-embed", "base_url": "http://vllm-embed:8000/v1",
     "model": "deepvk/USER-bge-m3", "type": "embedding", "max_tokens": 8192, "is_tools": False},
    {"instance": "ragspark-rerank", "base_url": "http://vllm-rerank:8000/v1",
     "model": "BAAI/bge-reranker-v2-m3", "type": "rerank", "max_tokens": 8192, "is_tools": False},
]
# An LLM file sets RAGSPARK_LLM_VISION=1 when its checkpoint has a vision tower and
# vLLM serves image input. The same served model is then registered a second time,
# as a vision model, for parsing scanned PDFs: a dataset's PDF parser (layout
# recognizer) "<model>@ragspark-vision@VLLM". It is deliberately not the default
# vision model: rag/app/naive.py uses the tenant's default vision model to describe
# figures in every DeepDOC parse, which would slow down every upload.
if os.environ.get("RAGSPARK_LLM_VISION", "0") == "1":
    DEFAULT_SPEC.append(
        {"instance": "ragspark-vision", "base_url": "http://vllm-llm:8000/v1",
         "model": os.environ.get("RAGSPARK_LLM_NAME", "qwen3.8-27b"), "type": "vision",
         "max_tokens": int(os.environ.get("RAGSPARK_LLM_CONTEXT", "65536")), "is_tools": False,
         "default": False})

API = os.environ.get("RAGFLOW_API", "http://127.0.0.1:9380").rstrip("/")
_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_jar))
_auth = {"value": ""}


def encrypt_password(password):
    """RAGFlow's own algorithm; prefer its function, fall back to the same steps."""
    try:
        from api.utils.crypt import crypt
        return crypt(password)
    except Exception:
        from Cryptodome.PublicKey import RSA
        from Cryptodome.Cipher import PKCS1_v1_5
        key = RSA.importKey(open("/ragflow/conf/public.pem").read(), "Welcome")
        data = base64.b64encode(password.encode()).decode()
        return base64.b64encode(PKCS1_v1_5.new(key).encrypt(data.encode())).decode()


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if _auth["value"]:
        req.add_header("Authorization", _auth["value"])
    try:
        with _opener.open(req, timeout=120) as r:
            raw, code, headers = r.read().decode("utf-8", "replace"), r.status, r.headers
    except urllib.error.HTTPError as e:
        raw, code, headers = e.read().decode("utf-8", "replace"), e.code, e.headers
    except urllib.error.URLError as e:
        return 0, {"message": str(e.reason)}, {}
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {"message": raw[:200]}
    return code, payload, headers


def ok(code, d):
    return code == 200 and isinstance(d, dict) and d.get("code") == 0


def err(code, d):
    msg = d.get("message") if isinstance(d, dict) else d
    return "HTTP %s code=%s %s" % (code, d.get("code") if isinstance(d, dict) else "?", str(msg)[:200])


def items(d):
    """Extract a list from {"data": [...]} or {"data": {"<something>": [...]}}."""
    data = d.get("data") if isinstance(d, dict) else None
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                return v
    return []


def admin_email():
    """The superuser RAGFlow created on first start (compose/.env)."""
    return (os.environ.get("RAGFLOW_ADMIN_EMAIL") or os.environ.get("DEFAULT_SUPERUSER_EMAIL")
            or "admin@ragflow.io")


def login(email, password):
    code, d, headers = call("POST", "/api/v1/auth/login",
                            {"email": email, "password": encrypt_password(password)})
    token = headers.get("Authorization", "") if headers else ""
    if not ok(code, d) or not token:
        sys.exit("[FAIL] login as %s: %s" % (email, err(code, d)))
    _auth["value"] = token
    print("[OK]   login %s" % email)


def instance_detail(inst):
    """The instance list omits base_url; read it from the instance record."""
    if inst.get("base_url"):
        return inst
    iid = inst.get("id") or inst.get("instance_id")
    if iid:
        code, d, _ = call("GET", "/api/v1/providers/%s/instances/%s" % (PROVIDER, iid))
        if ok(code, d) and isinstance(d.get("data"), dict):
            return dict(inst, **d["data"])
    return inst


def list_instances():
    code, d, _ = call("GET", "/api/v1/providers/%s/instances" % PROVIDER)
    return items(d) if ok(code, d) else None


# Instances whose model follows the LLM file. The embedding model is fixed:
# the search index of every dataset was built with it.
SWITCHABLE = ("chat", "vision")


def q(name):
    return urllib.parse.quote(name, safe="/@")


def instance_models(instance):
    """{model name: record} of an instance (status "active" or "inactive"), or None."""
    code, d, _ = call("GET", "/api/v1/providers/%s/instances/%s/models" % (PROVIDER, q(instance)))
    if not ok(code, d):
        return None
    return {m["name"]: m for m in items(d) if isinstance(m, dict) and m.get("name")}


def served(s):
    """(True/False, detected types) - does the server at s["base_url"] list s["model"]?
    (None, error) when RAGFlow cannot list the server's models."""
    qs = urllib.parse.urlencode({"api_key": "x", "base_url": s["base_url"]})
    code, d, _ = call("GET", "/api/v1/providers/%s/models?%s" % (PROVIDER, qs))
    if not ok(code, d):
        return None, err(code, d)
    found = [m for m in items(d) if m.get("name") == s["model"]]
    return bool(found), (found[0].get("model_types") or []) if found else []


def model_info(s):
    return {"model_name": s["model"], "model_type": [s["type"]], "max_tokens": s["max_tokens"],
            "extra": {"is_tools": bool(s.get("is_tools"))}}


def composite(model, instance):
    return "%s@%s@%s" % (model, instance, PROVIDER)


def pages(path, size=100):
    """Every item of a paged RAGFlow list, or None when the first page fails."""
    out, page = [], 1
    sep = "&" if "?" in path else "?"
    while page <= 1000:
        code, d, _ = call("GET", "%s%spage=%d&page_size=%d" % (path, sep, page, size))
        if not ok(code, d):
            return out if page > 1 else None
        batch = items(d)
        out += batch
        if len(batch) < size:
            break
        page += 1
    return out


def repoint_chats(old_refs, s, new_id, check):
    """Chats on an old model of the instance move to the current one."""
    chats = pages("/api/v1/chats")
    if chats is None:
        print("[FAIL] chats - cannot list them")
        return 1
    failed = 0
    for c in chats:
        if c.get("tenant_llm_id") not in old_refs and c.get("llm_id") not in old_refs:
            continue
        name = c.get("name") or c.get("id")
        if check:
            print("[TODO] chat %r would move to %s" % (name, s["model"]))
            continue
        code, d, _ = call("PATCH", "/api/v1/chats/%s" % c["id"], {"llm_id": new_id})
        if ok(code, d):
            print("[SET]  chat %r -> %s" % (name, s["model"]))
        else:
            print("[FAIL] chat %r -> %s: %s" % (name, s["model"], err(code, d)))
            failed += 1
    return failed


_services = {}


def full_parser_config(doc_id):
    """A document's whole parser_config, or None. The API's document records leave out
    raptor (and graphrag), and a parser_config update without raptor deletes the
    document's own (DocumentService.update_parser_config in RAGFlow 0.27.2), so the
    settings are read through RAGFlow's services in this container. Read only: every
    change still goes through the API."""
    if "doc" not in _services:
        try:
            from common import settings
            settings.init_settings()
            from api.db.services.document_service import DocumentService
            _services["doc"] = DocumentService
        except Exception as e:  # noqa: BLE001 - any failure: documents stay as they are
            print("[WARN] RAGFlow's document service is not available here: %s" % str(e)[:200])
            _services["doc"] = None
    if _services["doc"] is None:
        return None
    try:
        found, doc = _services["doc"].get_by_id(doc_id)
    except Exception:  # noqa: BLE001
        return None
    return dict(doc.parser_config or {}) if found and doc is not None else None


def repoint_layouts(old_refs, s, check):
    """Datasets and documents that parse scans with an old vision model move to the current one.
    Only the parser setting changes: nothing is parsed again."""
    new_ref = composite(s["model"], s["instance"])
    datasets = pages("/api/v1/datasets")
    if datasets is None:
        print("[FAIL] datasets - cannot list them")
        return 1
    failed = 0
    for kb in datasets:
        name = kb.get("name") or kb.get("id")
        if (kb.get("parser_config") or {}).get("layout_recognize") in old_refs:
            if check:
                print("[TODO] dataset %r would parse scans with %s" % (name, new_ref))
            else:
                code, d, _ = call("PUT", "/api/v1/datasets/%s" % kb["id"],
                                  {"parser_config": {"layout_recognize": new_ref}})
                if ok(code, d):
                    print("[SET]  dataset %r parses scans with %s" % (name, new_ref))
                else:
                    print("[FAIL] dataset %r: %s" % (name, err(code, d)))
                    failed += 1
        docs = pages("/api/v1/datasets/%s/documents" % kb["id"])
        if docs is None:
            print("[FAIL] dataset %r - cannot list its documents" % name)
            failed += 1
            continue
        for doc in docs:
            if (doc.get("parser_config") or {}).get("layout_recognize") not in old_refs:
                continue
            label = "%r in %r" % (doc.get("name") or doc.get("id"), name)
            if check:
                print("[TODO] document %s would be parsed again with %s" % (label, new_ref))
                continue
            full = full_parser_config(doc["id"])
            if full is None:
                print("[FAIL] document %s - cannot read its RAPTOR settings, so it keeps %s; "
                      "change its PDF parser in RAGFlow" % (label, (doc.get("parser_config") or {}).get("layout_recognize")))
                failed += 1
                continue
            body = {"layout_recognize": new_ref}
            if full.get("raptor"):
                body["raptor"] = full["raptor"]         # sent back unchanged: without it RAGFlow deletes them
            code, d, _ = call("PATCH", "/api/v1/datasets/%s/documents/%s" % (kb["id"], doc["id"]),
                              {"parser_config": body})
            if ok(code, d):
                print("[SET]  document %s: a new parse uses %s" % (label, new_ref))
            else:
                print("[FAIL] document %s: %s" % (label, err(code, d)))
                failed += 1
    return failed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="read-only")
    ap.add_argument("--spec", help="JSON file with model specs (default: RAGSpark defaults)")
    a = ap.parse_args()
    spec = json.load(open(a.spec)) if a.spec else DEFAULT_SPEC
    print("== RAGFlow model setup: %s ==" % ("CHECK (read-only)" if a.check else "APPLY"))

    password = os.environ.get("ADMIN_DEFAULT_PASSWORD") or os.environ.get("DEFAULT_SUPERUSER_PASSWORD")
    if not password:
        sys.exit("[FAIL] ADMIN_DEFAULT_PASSWORD is not set in this environment")
    login(admin_email(), password)

    existing = list_instances()
    if not existing:
        if a.check:
            print("[TODO] provider %s has no instances yet" % PROVIDER)
        else:
            code, d, _ = call("PUT", "/api/v1/providers", {"provider_name": PROVIDER})
            if ok(code, d):
                print("[OK]   provider %s added" % PROVIDER)
            else:
                print("[WARN] add provider %s: %s (continuing)" % (PROVIDER, err(code, d)))
            existing = list_instances()
    existing = existing or []
    by_name = {i.get("instance_name"): i for i in existing if isinstance(i, dict)}

    failed, ready, recorded = 0, set(), {}
    for s in spec:
        label = "%-9s %s" % (s["type"], s["model"])
        inst = by_name.get(s["instance"])
        if inst:
            inst = instance_detail(inst)
        url = (inst.get("base_url") if inst else None) or s["base_url"]
        if inst and not inst.get("base_url"):
            print("[WARN] %s - cannot read the address of instance %s, checking %s"
                  % (label, s["instance"], s["base_url"]))
        code, d, _ = call("POST", "/api/v1/providers/%s/connection" % PROVIDER,
                          {"provider_name": PROVIDER, "api_key": "", "base_url": url})
        reachable = ok(code, d)
        if inst:
            if url != s["base_url"]:
                print("[FAIL] %s - instance %s points to %s, expected %s; fix it in RAGFlow "
                      "or delete the instance and rerun" % (label, s["instance"], url, s["base_url"]))
                failed += 1
                continue
            if not reachable:
                print("[FAIL] %s - instance %s exists but %s is not reachable: %s"
                      % (label, s["instance"], url, err(code, d)))
                failed += 1
            else:
                print("[OK]   %s - instance %s exists (reachable)" % (label, s["instance"]))
            models = instance_models(s["instance"])
            if models is None:
                print("[WARN] %s - cannot list the models of instance %s" % (label, s["instance"]))
                ready.add((s["instance"], s["model"]))
                continue
            recorded[s["instance"]] = models
            if s["model"] not in models:
                if s["type"] not in SWITCHABLE:
                    print("[FAIL] %s - instance %s holds %s; RAGSpark does not change this model in place"
                          % (label, s["instance"], ", ".join(sorted(models)) or "no model"))
                    failed += 1
                    continue
                if not reachable:
                    continue
                found, detail = served(s)
                if found is False:
                    print("[FAIL] %s - model not served at %s" % (label, s["base_url"]))
                    failed += 1
                    continue
                if a.check:
                    print("[TODO] %s - would add model %s to instance %s" % (label, s["model"], s["instance"]))
                    ready.add((s["instance"], s["model"]))
                    continue
                code, d, _ = call("POST", "/api/v1/providers/%s/instances/%s/models" % (PROVIDER, q(s["instance"])),
                                  model_info(s))
                if not ok(code, d):
                    print("[FAIL] %s - add to instance %s: %s" % (label, s["instance"], err(code, d)))
                    failed += 1
                    continue
                print("[OK]   %s - added to instance %s" % (label, s["instance"]))
            elif models[s["model"]].get("status") != "active":
                if a.check:
                    print("[TODO] %s - would enable it again in instance %s" % (label, s["instance"]))
                else:
                    code, d, _ = call("PATCH", "/api/v1/providers/%s/instances/%s/models/%s"
                                      % (PROVIDER, q(s["instance"]), q(s["model"])), {"status": "active"})
                    if not ok(code, d):
                        print("[FAIL] %s - enable in instance %s: %s" % (label, s["instance"], err(code, d)))
                        failed += 1
                        continue
                    print("[OK]   %s - enabled again in instance %s" % (label, s["instance"]))
            ready.add((s["instance"], s["model"]))
            continue
        if not reachable:
            print("[FAIL] %s - %s not reachable: %s" % (label, s["base_url"], err(code, d)))
            failed += 1
            continue
        found, detected = served(s)
        if found is None:
            print("[WARN] %s - cannot list models (%s), using spec" % (label, detected))
        elif not found:
            print("[FAIL] %s - model not served at %s" % (label, s["base_url"]))
            failed += 1
            continue
        elif s["type"] == "vision" and s["type"] not in detected:
            # RAGFlow reports a vLLM model's types from its name and never lists
            # vision for it; the LLM file (RAGSPARK_LLM_VISION) is the authority here.
            print("[OK]   %s - vision comes from the LLM file (RAGFlow lists %s)" % (label, detected))
        elif s["type"] not in detected:
            print("[WARN] %s - RAGFlow detects %s, using %s" % (label, detected, s["type"]))
        if a.check:
            print("[TODO] %s - would create instance %s" % (label, s["instance"]))
            ready.add((s["instance"], s["model"]))
            continue
        body = {"instance_name": s["instance"], "api_key": "", "base_url": s["base_url"],
                "model_info": [model_info(s)]}
        code, d, _ = call("POST", "/api/v1/providers/%s/instances" % PROVIDER, body)
        if not ok(code, d):
            print("[FAIL] %s - create instance: %s" % (label, err(code, d)))
            failed += 1
            continue
        print("[OK]   %s - instance %s created" % (label, s["instance"]))
        ready.add((s["instance"], s["model"]))

    # model ids, then defaults
    code, d, _ = call("GET", "/api/v1/models")
    ids = {(m.get("instance_name"), m.get("name")): m.get("model_id") for m in items(d)} if ok(code, d) else {}
    # Other models of a chat or vision instance: what an earlier LLM file served.
    old = {s["instance"]: {name: mid for (inst, name), mid in ids.items()
                           if inst == s["instance"] and name != s["model"] and mid}
           for s in spec if s["type"] in SWITCHABLE}
    code, d, _ = call("GET", "/api/v1/models/default")
    current = {m.get("model_type"): m for m in items(d)} if ok(code, d) else {}
    for s in spec:
        cur = current.get(s["type"]) or {}
        want = ids.get((s["instance"], s["model"]))
        moved = cur.get("model_id") in old.get(s["instance"], {}).values()
        if s.get("default") is False and not moved:
            print("[OK]   default %-9s - %s is registered only, by design" % (s["type"], s["model"]))
            continue
        names_match = cur.get("model_instance") == s["instance"] and cur.get("model_name") == s["model"]
        # RAGFlow stores the default by model id; after an instance is re-created the
        # names still resolve but the stored id is stale, so compare ids, not names.
        if names_match and want and cur.get("model_id") == want:
            print("[OK]   default %-9s = %s (%s)" % (s["type"], s["model"], s["instance"]))
            continue
        if (s["instance"], s["model"]) not in ready:
            print("[SKIP] default %-9s - %s is not configured" % (s["type"], s["model"]))
            continue
        if names_match:
            was = "stale model id %s" % cur.get("model_id")
        else:
            was = cur.get("model_name") or "(none)"
        if moved and s.get("default") is False:
            was += ", set by hand"                      # keep that choice on the current model
        if a.check:
            print("[TODO] default %-9s: %s -> %s" % (s["type"], was, s["model"]))
            continue
        mid = ids.get((s["instance"], s["model"]))
        if not mid:
            print("[FAIL] default %-9s - model id for %s not found" % (s["type"], s["model"]))
            failed += 1
            continue
        code, d, _ = call("PATCH", "/api/v1/models/default", {"model_id": mid, "model_type": s["type"]})
        if ok(code, d):
            print("[SET]  default %-9s: %s -> %s" % (s["type"], was, s["model"]))
        else:
            print("[FAIL] default %-9s: %s" % (s["type"], err(code, d)))
            failed += 1

    # What referred to an old model now refers to the current one; then the old model is disabled.
    for s in spec:
        stale = old.get(s["instance"]) if s["type"] in SWITCHABLE else None
        if not stale or (s["instance"], s["model"]) not in ready:
            continue
        refs = set(stale.values()) | {composite(name, s["instance"]) for name in stale}
        new_id = ids.get((s["instance"], s["model"]))
        if not new_id and not a.check:
            print("[FAIL] %s - model id of %s not found, references stay" % (s["type"], s["model"]))
            failed += 1
            continue
        if s["type"] == "chat":
            failed += repoint_chats(refs, s, new_id, a.check)
        else:
            failed += repoint_layouts(refs, s, a.check)
        for name in sorted(stale):
            if (recorded.get(s["instance"], {}).get(name) or {}).get("status") == "inactive":
                continue
            if a.check:
                print("[TODO] %-9s %s - would be disabled in instance %s (no longer served)"
                      % (s["type"], name, s["instance"]))
                continue
            code, d, _ = call("PATCH", "/api/v1/providers/%s/instances/%s/models/%s"
                              % (PROVIDER, q(s["instance"]), q(name)), {"status": "inactive"})
            if ok(code, d):
                print("[OK]   %-9s %s - disabled in instance %s (no longer served)" % (s["type"], name, s["instance"]))
            else:
                print("[FAIL] %-9s %s - disable in instance %s: %s" % (s["type"], name, s["instance"], err(code, d)))
                failed += 1

    if "ragspark-vision" in by_name and not any(s["instance"] == "ragspark-vision" for s in spec):
        print("[WARN] instance ragspark-vision - the current LLM file has no vision: datasets that parse "
              "scans with it fail until they use another PDF parser")

    print("== %s ==" % ("done, %d problem(s)" % failed if failed else "done"))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
