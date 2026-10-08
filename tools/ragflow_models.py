#!/usr/bin/env python3
"""ragflow_models.py - register RAGSpark's vLLM models in RAGFlow 0.27 (idempotent).

Runs INSIDE the RAGFlow container, so it uses RAGFlow's own password
encryption and needs nothing on the host. From the compose/ directory:

  sudo docker compose exec -T ragflow \
    sh -c 'cd /ragflow && PYTHONPATH=/ragflow .venv/bin/python - --check' < ../tools/ragflow_models.py

  --check        read-only: verify connectivity, report what would change
  (no flag)      create missing instances, set default models

Existing instances are never modified; a mismatch is reported instead.
Everything comes from the container environment (compose/.env and the LLM
file): the superuser DEFAULT_SUPERUSER_EMAIL / ADMIN_DEFAULT_PASSWORD, and
the LLM RAGSPARK_LLM_NAME, RAGSPARK_LLM_CONTEXT, RAGSPARK_LLM_TOOLS.
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

    failed, ready = 0, set()
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
            ready.add((s["instance"], s["model"]))
            continue
        if not reachable:
            print("[FAIL] %s - %s not reachable: %s" % (label, s["base_url"], err(code, d)))
            failed += 1
            continue
        qs = urllib.parse.urlencode({"api_key": "x", "base_url": s["base_url"]})
        code, d, _ = call("GET", "/api/v1/providers/%s/models?%s" % (PROVIDER, qs))
        if ok(code, d):
            found = [m for m in items(d) if m.get("name") == s["model"]]
            if not found:
                print("[FAIL] %s - model not served at %s" % (label, s["base_url"]))
                failed += 1
                continue
            detected = found[0].get("model_types") or []
            if s["type"] not in detected:
                print("[WARN] %s - RAGFlow detects %s, using %s" % (label, detected, s["type"]))
        else:
            print("[WARN] %s - cannot list models (%s), using spec" % (label, err(code, d)))
        if a.check:
            print("[TODO] %s - would create instance %s" % (label, s["instance"]))
            ready.add((s["instance"], s["model"]))
            continue
        body = {"instance_name": s["instance"], "api_key": "", "base_url": s["base_url"],
                "model_info": [{"model_name": s["model"], "model_type": [s["type"]],
                                "max_tokens": s["max_tokens"],
                                "extra": {"is_tools": bool(s.get("is_tools"))}}]}
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
    code, d, _ = call("GET", "/api/v1/models/default")
    current = {m.get("model_type"): m for m in items(d)} if ok(code, d) else {}
    for s in spec:
        cur = current.get(s["type"]) or {}
        want = ids.get((s["instance"], s["model"]))
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

    print("== %s ==" % ("done, %d problem(s)" % failed if failed else "done"))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
