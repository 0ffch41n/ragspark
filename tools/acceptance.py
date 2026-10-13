#!/usr/bin/env python3
"""acceptance.py - RAGSpark acceptance test: document -> question -> cited answer.

Runs INSIDE the RAGFlow container, like the other tools. The sample document
is copied in first; from the compose/ directory:

  sudo docker compose cp ../acceptance/. ragflow:/tmp/ragspark-acceptance
  sudo docker compose exec -T ragflow \\
    sh -c 'cd /ragflow && PYTHONPATH=/ragflow .venv/bin/python - --lang ru' < ../tools/acceptance.py

It (re)creates the dataset and the chat "RAGSpark — пример" with RAGSpark's
settings (PDF text layer, reranker, vector weight 0.7, threshold 0.1), uploads
the sample PDF, waits for parsing and asks the chat the sample questions:
  - an answer must contain the expected facts, cite the document and contain
    no CJK characters;
  - a question with no answer in the document must get the configured
    "no answer" text, which RAGFlow returns without calling the LLM.
The dataset and the chat stay as a working example; --cleanup removes them.
Exit code 0 means passed.
"""
import argparse, base64, http.cookiejar, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request, uuid

API = os.environ.get("RAGFLOW_API", "http://127.0.0.1:9380").rstrip("/")
RERANK_INSTANCE = os.environ.get("RAGSPARK_RERANK_INSTANCE", "ragspark-rerank")
RERANK_MODEL = "BAAI/bge-reranker-v2-m3"

# RAGSpark defaults: docs/DECISIONS.md D14 (parsing) and D15 (retrieval).
PARSER_CONFIG = {"layout_recognize": "Plain Text", "chunk_token_num": 512}
RETRIEVAL = {"similarity_threshold": 0.1, "vector_similarity_weight": 0.7,
             "top_n": 8, "rerank_candidates_count": 64}

# RAGFlow HTML-escapes the "no answer" text before returning it; keep ' " & < > out of
# "empty", or the exact comparison in check_answers() fails.
TEXT = {
    "ru": {
        "name": "RAGSpark — пример",
        "prologue": "Здравствуйте! Задайте вопрос по документам из базы знаний.",
        "empty": "В базе знаний нет ответа на этот вопрос.",
        "system": ("Ты — помощник, который отвечает на вопросы по документам из базы знаний. "
                   "Отвечай на языке вопроса, кратко и по существу. Не используй китайские, "
                   "японские и корейские иероглифы. Опирайся только на сведения из базы знаний "
                   "ниже; если ответа в ней нет, так и скажи. Учитывай историю диалога.\n"
                   "База знаний:\n{knowledge}\nКонец базы знаний."),
    },
    "en": {
        "name": "RAGSpark sample",
        "prologue": "Hello! Ask a question about the documents in the knowledge base.",
        "empty": "The knowledge base has no answer to this question.",
        "system": ("You are an assistant that answers questions about the documents in the "
                   "knowledge base. Answer in the language of the question, briefly and to the "
                   "point. Do not use Chinese, Japanese or Korean characters. Use only the "
                   "knowledge base below; if it has no answer, say so. Consider the chat history.\n"
                   "Knowledge base:\n{knowledge}\nEnd of the knowledge base."),
    },
}

CYR = re.compile(r"[А-Яа-яЁё]")
LAT = re.compile(r"[A-Za-z]")
CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]")

_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_jar))
_auth = {"value": ""}
problems = []


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


def request(method, path, data=None, content_type="application/json", timeout=120):
    req = urllib.request.Request(API + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", content_type)
    if _auth["value"]:
        req.add_header("Authorization", _auth["value"])
    try:
        with _opener.open(req, timeout=timeout) as r:
            raw, code, headers = r.read().decode("utf-8", "replace"), r.status, r.headers
    except urllib.error.HTTPError as e:
        raw, code, headers = e.read().decode("utf-8", "replace"), e.code, e.headers
    except (urllib.error.URLError, TimeoutError) as e:
        return 0, {"message": str(getattr(e, "reason", e))}, {}
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {"message": raw[:200]}
    return code, payload, headers


def call(method, path, body=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    return request(method, path, data, timeout=timeout)


def ok(code, d):
    return code == 200 and isinstance(d, dict) and d.get("code") == 0


def err(code, d):
    msg = d.get("message") if isinstance(d, dict) else d
    return "HTTP %s code=%s %s" % (code, d.get("code") if isinstance(d, dict) else "?", str(msg)[:200])


def items(d):
    data = d.get("data") if isinstance(d, dict) else None
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                return v
    return []


def report(status, text):
    print("[%-4s] %s" % (status, text), flush=True)
    if status == "FAIL":
        problems.append(text)


def fatal(text):
    report("FAIL", text)
    print("== acceptance FAILED ==")
    sys.exit(1)


def admin_email():
    return (os.environ.get("RAGFLOW_ADMIN_EMAIL") or os.environ.get("DEFAULT_SUPERUSER_EMAIL")
            or "admin@ragflow.io")


def login():
    password = os.environ.get("ADMIN_DEFAULT_PASSWORD") or os.environ.get("DEFAULT_SUPERUSER_PASSWORD")
    if not password:
        fatal("ADMIN_DEFAULT_PASSWORD is not set in this environment")
    email = admin_email()
    code, d, headers = call("POST", "/api/v1/auth/login", {"email": email, "password": encrypt_password(password)})
    token = headers.get("Authorization", "") if headers else ""
    if not ok(code, d) or not token:
        fatal("login as %s: %s" % (email, err(code, d)))
    _auth["value"] = token
    report("OK", "login %s" % email)


def remove_previous(name):
    """Delete an earlier chat and dataset of the same name, so a re-run starts clean."""
    code, d, _ = call("GET", "/api/v1/chats?" + urllib.parse.urlencode({"name": name}))
    chat_ids = [c["id"] for c in items(d) if c.get("name") == name] if ok(code, d) else []
    if chat_ids:
        code, d, _ = call("DELETE", "/api/v1/chats", {"ids": chat_ids})
        report("OK" if ok(code, d) else "WARN", "previous chat removed" if ok(code, d) else "remove chat: " + err(code, d))
    code, d, _ = call("GET", "/api/v1/datasets?" + urllib.parse.urlencode({"name": name, "page_size": 100}))
    ds_ids = [x["id"] for x in (d.get("data") or []) if x.get("name") == name] if ok(code, d) else []
    if ds_ids:
        code, d, _ = call("DELETE", "/api/v1/datasets", {"ids": ds_ids})
        report("OK" if ok(code, d) else "WARN", "previous dataset removed" if ok(code, d) else "remove dataset: " + err(code, d))


def create_dataset(name, language):
    body = {"name": name, "language": language, "chunk_method": "naive", "parser_config": PARSER_CONFIG}
    code, d, _ = call("POST", "/api/v1/datasets", body)
    if not ok(code, d):
        fatal("create dataset: %s" % err(code, d))
    report("OK", "dataset %r created (%s, PDF parser %s)" % (name, language, PARSER_CONFIG["layout_recognize"]))
    return d["data"]["id"]


def upload(ds, path):
    boundary = "----ragspark" + uuid.uuid4().hex
    fname = os.path.basename(path)
    with open(path, "rb") as f:
        content = f.read()
    body = (("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
             "Content-Type: application/pdf\r\n\r\n" % (boundary, fname)).encode()
            + content + ("\r\n--%s--\r\n" % boundary).encode())
    code, d, _ = request("POST", "/api/v1/datasets/%s/documents" % ds, body,
                         "multipart/form-data; boundary=%s" % boundary)
    docs = items(d) if ok(code, d) else []
    if not docs:
        fatal("upload %s: %s" % (fname, err(code, d)))
    report("OK", "uploaded %s (%d KB)" % (fname, len(content) // 1024))
    return docs[0]["id"]


def parse_and_wait(ds, doc, timeout=600):
    code, d, _ = call("POST", "/api/v1/datasets/%s/documents/parse" % ds, {"document_ids": [doc]})
    if not ok(code, d):
        fatal("start parsing: %s" % err(code, d))
    t0, last = time.time(), ""
    while time.time() - t0 < timeout:
        code, d, _ = call("GET", "/api/v1/datasets/%s/documents?page_size=100" % ds)
        docs = [x for x in items(d) if x.get("id") == doc] if ok(code, d) else []
        if docs:
            run, progress = str(docs[0].get("run", "")), docs[0].get("progress") or 0
            if run == "DONE":
                report("OK", "parsed in %.0f s, %s chunks" % (time.time() - t0, docs[0].get("chunk_count")))
                return docs[0]
            if run in ("FAIL", "CANCEL"):
                fatal("parsing %s: %s" % (run, str(docs[0].get("progress_msg", ""))[-300:]))
            state = "%s %.0f%%" % (run, float(progress) * 100)
            if state != last:
                print("       parsing: %s" % state, flush=True)
                last = state
        time.sleep(3)
    fatal("parsing did not finish in %d s" % timeout)


def check_chunks(ds, doc, min_share):
    texts, page = [], 1
    while True:
        code, d, _ = call("GET", "/api/v1/datasets/%s/documents/%s/chunks?page=%d&page_size=100" % (ds, doc, page))
        if not ok(code, d):
            fatal("read chunks: %s" % err(code, d))
        data = d.get("data") or {}
        batch = data.get("chunks", [])
        texts.extend(c.get("content", "") or "" for c in batch)
        if not batch or len(texts) >= int(data.get("total", 0) or 0):
            break
        page += 1
    text = "\n".join(texts)
    c, l = len(CYR.findall(text)), len(LAT.findall(text))
    share = c / (c + l) if c + l else 0.0
    if not texts:
        fatal("no chunks were produced")
    report("OK" if share >= min_share else "FAIL",
           "%d chunks, Cyrillic share %.3f (minimum %.2f)" % (len(texts), share, min_share))


def rerank_model_id():
    code, d, _ = call("GET", "/api/v1/models")
    for m in items(d) if ok(code, d) else []:
        if m.get("instance_name") == RERANK_INSTANCE and m.get("name") == RERANK_MODEL:
            return m.get("model_id")
    fatal("reranker %s (instance %s) is not registered; run tools/ragflow_models.py first"
          % (RERANK_MODEL, RERANK_INSTANCE))


def create_chat(name, ds, text):
    body = dict(RETRIEVAL)
    body.update({
        "name": name,
        "dataset_ids": [ds],
        "tenant_rerank_id": rerank_model_id(),
        "prompt_config": {"system": text["system"], "prologue": text["prologue"],
                          "empty_response": text["empty"], "quote": True,
                          "parameters": [{"key": "knowledge", "optional": False}]},
    })
    code, d, _ = call("POST", "/api/v1/chats", body)
    if not ok(code, d):
        fatal("create chat: %s" % err(code, d))
    data = d.get("data") or {}
    report("OK", "chat %r created: reranker %s, vector weight %s, threshold %s, top %s"
           % (name, data.get("rerank_id") or RERANK_MODEL, data.get("vector_similarity_weight"),
              data.get("similarity_threshold"), data.get("top_n")))
    return data["id"]


def norm(s):
    return re.sub(r"\s+", " ", (s or "").lower().replace("ё", "е"))


def ask(chat, question):
    t0 = time.time()
    code, d, _ = call("POST", "/api/v1/chat/completions",
                      {"chat_id": chat, "question": question, "stream": False}, timeout=600)
    if not ok(code, d):
        return None, [], time.time() - t0, err(code, d)
    data = d.get("data") or {}
    refs = (data.get("reference") or {}).get("chunks") or []
    return data.get("answer") or "", refs, time.time() - t0, ""


def check_answers(chat, questions, empty_text):
    for item in questions:
        q, expect = item["q"], item.get("expect")
        answer, refs, dt, error = ask(chat, q)
        short = " ".join((answer or "").split())[:160]
        if answer is None:
            report("FAIL", "%s -> request failed: %s" % (q, error))
            continue
        if expect is None:
            if answer.strip() == empty_text.strip() and not refs:
                report("OK", "%s -> \"no answer\" text, no sources (%.0f s)" % (q, dt))
            else:
                report("FAIL", "%s -> expected the \"no answer\" text, got: %s (%d sources)" % (q, short, len(refs)))
            continue
        missing = [e for e in expect if norm(e) not in norm(answer)]
        cjk = CJK.findall(answer)
        if missing:
            report("FAIL", "%s -> missing %s in: %s" % (q, missing, short))
        elif not refs:
            report("FAIL", "%s -> answer has no sources: %s" % (q, short))
        elif cjk:
            report("FAIL", "%s -> CJK characters %r in: %s" % (q, "".join(cjk)[:10], short))
        else:
            report("OK", "%s -> %s (%d sources, %.0f s)" % (q, short, len(refs), dt))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="/tmp/ragspark-acceptance", help="copied acceptance/ directory")
    ap.add_argument("--spec", default="sample-ru.json")
    ap.add_argument("--lang", default=os.environ.get("RAGSPARK_LANG", "ru"), choices=sorted(TEXT))
    ap.add_argument("--cleanup", action="store_true", help="remove the dataset and chat afterwards")
    a = ap.parse_args()
    spec = json.load(open(os.path.join(a.dir, a.spec), encoding="utf-8"))
    text = TEXT[a.lang]
    name = text["name"]
    print("== RAGSpark acceptance test ==", flush=True)
    t0 = time.time()

    login()
    remove_previous(name)
    ds = create_dataset(name, spec.get("language", "Russian"))
    doc = upload(ds, os.path.join(a.dir, spec["document"]))
    parse_and_wait(ds, doc)
    check_chunks(ds, doc, float(spec.get("min_cyrillic_share", 0.8)))
    chat = create_chat(name, ds, text)
    check_answers(chat, spec["questions"], text["empty"])

    if a.cleanup:
        remove_previous(name)
    if problems:
        print("== acceptance FAILED: %d problem(s), %.0f s ==" % (len(problems), time.time() - t0))
        sys.exit(1)
    print("== acceptance passed in %.0f s ==" % (time.time() - t0))


if __name__ == "__main__":
    main()
