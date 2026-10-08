#!/usr/bin/env python3
"""rag_eval.py - measure RAGFlow parsing and retrieval quality (read-only).

Runs INSIDE the RAGFlow container (same way as ragflow_models.py):

  docker exec -i -e ADMIN_DEFAULT_PASSWORD="$PW" <ragflow> \
    sh -c 'cd /ragflow && PYTHONPATH=/ragflow .venv/bin/python - --dataset test' < rag_eval.py

Reports, per document: chunk count, share of Cyrillic letters, number of
garbled-looking words (Latin look-alikes such as "Pa3beM" produced by OCR
that cannot read Cyrillic). Then runs a fixed question set through the
retrieval API with several setups and checks whether a chunk containing
the expected facts is found in the top N.

Changes nothing in RAGFlow.
"""
import argparse, base64, http.cookiejar, json, os, re, sys, urllib.error, urllib.parse, urllib.request

API = os.environ.get("RAGFLOW_API", "http://127.0.0.1:9380").rstrip("/")
RERANK = "BAAI/bge-reranker-v2-m3@ragspark-rerank@VLLM"
RERANK_URL = os.environ.get("RERANK_URL", "http://ragspark-vllm-rerank:8000/v1/rerank")
RERANK_MODEL = "BAAI/bge-reranker-v2-m3"

# Questions for the MSI MPG B550 Gaming Plus manual (Russian, 39 pages).
# "expect": every term must appear (case-insensitive) in one retrieved chunk.
QUESTIONS = [
    {"q": "Какой максимальный объём оперативной памяти поддерживает плата?", "expect": ["128"], "page": 4},
    {"q": "В какой слот устанавливать первый модуль памяти?", "expect": ["DIMMA2", "сначала"], "page": 15},
    {"q": "Что означает оранжевый индикатор скорости на порту LAN?", "expect": ["оранжев", "1 Гбит"], "page": 10},
    {"q": "Как сбросить BIOS с помощью джампера JBAT1?", "expect": ["JBAT1", "секунд"], "page": 25},
    {"q": "Какой клавишей войти в настройки BIOS?", "expect": ["Delete", "Setup"], "page": 31},
    {"q": "Почему может быть недоступен слот PCI_E3?", "expect": ["PCI_E3", "недоступ"], "page": 4},
    {"q": "Какие размеры накопителей поддерживает разъём M2_2?", "expect": ["M2_2", "2280"], "page": 5},
    {"q": "При какой температуре нельзя хранить материнскую плату?", "expect": ["60", "хран"], "page": 3},
    # No answer in the document: a good setup returns nothing above the threshold,
    # so RAGFlow answers with "Empty response" instead of letting the LLM improvise.
    {"q": "Сколько стоит эта материнская плата в рублях?", "expect": None},
    {"q": "Есть ли у платы встроенный Wi-Fi и Bluetooth?", "expect": None},
]

# Retrieval setups to compare: (label, vector weight, threshold, reranker on)
CONFIGS = [
    ("rr .3/.2", 0.3, 0.2, True),    # RAGFlow defaults + reranker
    ("rr .7/.2", 0.7, 0.2, True),
    ("rr .7/.1", 0.7, 0.1, True),
    ("vec .7/.2", 0.7, 0.2, False),
    ("vec .7/.5", 0.7, 0.5, False),
]

CYR = re.compile(r"[А-Яа-яЁё]")
LAT = re.compile(r"[A-Za-z]")
# Latin look-alikes of Cyrillic words: letters with 3/6/4/9 inside ("Pa3beM"),
# or one word mixing Latin and Cyrillic letters ("KoHфигурация").
GARBLED = [re.compile(r"[A-Za-z]+[3469][A-Za-z]+"),
           re.compile(r"\b(?=\w*[A-Za-z])(?=\w*[А-Яа-яЁё])\w{4,}\b"),
           re.compile(r"\b[A-Za-z]*[a-z][A-Z][A-Za-z]*[a-z][A-Z][A-Za-z]*\b")]  # "YcTaHOBKa"

_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_jar))
_auth = {"value": ""}


def encrypt_password(password):
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
        with _opener.open(req, timeout=300) as r:
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
    return "HTTP %s code=%s %s" % (code, d.get("code") if isinstance(d, dict) else "?",
                                   str(d.get("message") if isinstance(d, dict) else d)[:200])


def login(email, password):
    code, d, headers = call("POST", "/api/v1/auth/login", {"email": email, "password": encrypt_password(password)})
    token = headers.get("Authorization", "") if headers else ""
    if not ok(code, d) or not token:
        sys.exit("[FAIL] login as %s: %s" % (email, err(code, d)))
    _auth["value"] = token


def find_dataset(name):
    code, d, _ = call("GET", "/api/v1/datasets?" + urllib.parse.urlencode({"name": name, "page_size": 100}))
    if not ok(code, d):
        sys.exit("[FAIL] list datasets: %s" % err(code, d))
    data = d.get("data") or []
    match = [x for x in data if x.get("name") == name]
    if not match:
        sys.exit("[FAIL] dataset %r not found" % name)
    return match[0]["id"]


def list_docs(ds):
    code, d, _ = call("GET", "/api/v1/datasets/%s/documents?page_size=100" % ds)
    if not ok(code, d):
        sys.exit("[FAIL] list documents: %s" % err(code, d))
    data = d.get("data") or {}
    return data.get("docs", []) if isinstance(data, dict) else data


def doc_chunks(ds, doc):
    out, page = [], 1
    while True:
        code, d, _ = call("GET", "/api/v1/datasets/%s/documents/%s/chunks?page=%d&page_size=100" % (ds, doc, page))
        if not ok(code, d):
            print("[WARN] chunks of %s: %s" % (doc, err(code, d)))
            return out
        data = d.get("data") or {}
        batch = data.get("chunks", [])
        out.extend(c.get("content", "") or "" for c in batch)
        if not batch or len(out) >= int(data.get("total", 0) or 0):
            return out
        page += 1


def text_stats(chunks):
    text = "\n".join(chunks)
    c, l = len(CYR.findall(text)), len(LAT.findall(text))
    garbled = set()
    for rx in GARBLED:
        garbled.update(rx.findall(text))
    share = c / (c + l) if c + l else 0.0
    return share, sorted(garbled)


def retrieve(ds, q, weight, threshold, topn, rerank=True):
    body = {"dataset_ids": [ds], "question": q, "similarity_threshold": threshold,
            "vector_similarity_weight": weight, "page_size": topn,
            "rerank_candidates_count": 64}
    if rerank:
        body["rerank_id"] = RERANK
    code, d, _ = call("POST", "/api/v1/retrieval", body)
    if not ok(code, d):
        return None, err(code, d)
    return (d.get("data") or {}).get("chunks", []), ""


def norm(s):
    return re.sub(r"\s+", " ", (s or "").lower().replace("ё", "е"))


def first_hit(chunks, expect):
    terms = [norm(t) for t in expect]
    for i, c in enumerate(chunks, 1):
        body = norm(c.get("content"))
        if all(t in body for t in terms):
            return i
    return 0


def scores(c):
    return "total=%.3f words=%.3f vector/rerank=%.3f" % (
        float(c.get("similarity", 0) or 0), float(c.get("term_similarity", 0) or 0),
        float(c.get("vector_similarity", 0) or 0))


def explain(ds, item, weight):
    """Re-run a failed question with threshold 0 to see where the answer chunk ranks."""
    for rerank in (True, False):
        chunks, e = retrieve(ds, item["q"], weight, 0.0, 20, rerank=rerank)
        tag = "w=%s %s" % (weight, "reranker" if rerank else "no reranker")
        if chunks is None:
            print("        %s: ERROR %s" % (tag, e))
            continue
        if not chunks:
            print("        %s: no candidates at all" % tag)
            continue
        rank = first_hit(chunks, item["expect"])
        top = "top-1 %s" % scores(chunks[0])
        if rank:
            print("        %s: answer chunk at #%d (%s); %s" % (tag, rank, scores(chunks[rank - 1]), top))
        else:
            print("        %s: answer chunk not in top %d; %s" % (tag, len(chunks), top))


def rerank_direct(query, docs):
    body = json.dumps({"model": RERANK_MODEL, "query": query, "documents": docs}).encode()
    req = urllib.request.Request(RERANK_URL, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            res = json.load(r).get("results", [])
    except Exception as e:
        return None, str(e)
    out = [0.0] * len(docs)
    for x in res:
        out[x["index"]] = float(x.get("relevance_score", 0))
    return out, ""


def focus(content, expect):
    """The line holding the first expected term, plus one line on each side."""
    lines = [l for l in (content or "").splitlines() if l.strip()]
    t = norm(expect[0])
    for i, l in enumerate(lines):
        if t in norm(l):
            return "\n".join(lines[max(0, i - 1): i + 2])
    return ""


def probe(ds, item):
    """Score one answer chunk three ways, directly against the reranker."""
    chunks, e = retrieve(ds, item["q"], 0.7, 0.0, 20, rerank=False)
    rank = first_hit(chunks or [], item["expect"])
    if not rank:
        print("        probe: answer chunk not found (%s)" % (e or "not in top 20"))
        return
    c = chunks[rank - 1]
    title = re.sub(r"\.[a-zA-Z]+$", "", c.get("document_keyword") or c.get("docnm_kwd") or "")
    try:
        from rag.nlp import rag_tokenizer
        title_tks = rag_tokenizer.tokenize(title)
    except Exception:
        title_tks = re.sub(r"[^\w]+", " ", title.lower())
    variants = [("as RAGFlow sends it (+title)", ((c.get("content_ltks") or "") + " " + title_tks).strip()),
                ("tokenized, no title", c.get("content_ltks") or ""),
                ("original chunk text", c.get("content") or ""),
                ("only the answer lines", focus(c.get("content"), item["expect"]))]
    sc, e = rerank_direct(item["q"], [v[1] or " " for v in variants])
    if sc is None:
        print("        probe: reranker call failed: %s" % e)
        return
    for (name, text), score in zip(variants, sc):
        print("        probe %-32s %.3f   (%d chars)" % (name, score, len(text)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="test")
    ap.add_argument("--topn", type=int, default=5)
    ap.add_argument("--questions", help="JSON file with [{q, expect, page}] (default: built-in MSI set)")
    ap.add_argument("-v", "--verbose", action="store_true", help="show garbled words and top chunk on misses")
    ap.add_argument("--why", action="store_true", help="for failed questions, re-run with threshold 0 and show scores")
    ap.add_argument("--probe", action="store_true", help="for failed questions, score the answer chunk directly with the reranker")
    a = ap.parse_args()
    questions = json.load(open(a.questions)) if a.questions else QUESTIONS

    password = os.environ.get("ADMIN_DEFAULT_PASSWORD")
    if not password:
        sys.exit("[FAIL] ADMIN_DEFAULT_PASSWORD is not set")
    login(os.environ.get("RAGFLOW_ADMIN_EMAIL", "admin@ragflow.io"), password)
    ds = find_dataset(a.dataset)

    print("== documents in %r" % a.dataset)
    for doc in list_docs(ds):
        chunks = doc_chunks(ds, doc["id"])
        share, garbled = text_stats(chunks)
        cfg = doc.get("parser_config") or {}
        print("   %-50.50s %4d chunks  parser=%-9s cyrillic=%.3f  garbled words=%d"
              % (doc.get("name", "?"), len(chunks), cfg.get("layout_recognize", "?"), share, len(garbled)))
        if a.verbose and garbled:
            print("      e.g. " + ", ".join(garbled[:15]))

    print("\n== retrieval: top %d; columns = reranker on/off (rr/vec), vector weight / threshold" % a.topn)
    print("   %-52s" % "question" + "".join("%-11s" % c[0] for c in CONFIGS))
    found = [0] * len(CONFIGS)
    rejected = [0] * len(CONFIGS)
    npos = sum(1 for q in questions if q.get("expect"))
    nneg = len(questions) - npos
    for n, item in enumerate(questions, 1):
        neg = not item.get("expect")
        row = "   %2d. %-46.46s  " % (n, item["q"] if not neg else "[no answer] " + item["q"])
        failed, notes = False, []
        for i, (label, w, t, rr) in enumerate(CONFIGS):
            chunks, e = retrieve(ds, item["q"], w, t, a.topn, rerank=rr)
            if chunks is None:
                row += "%-11s" % "ERROR"
                notes.append("%s: %s" % (label, e))
                continue
            if neg:
                if chunks:
                    row += "%-11s" % ("LEAK %.2f" % float(chunks[0].get("similarity", 0) or 0))
                else:
                    rejected[i] += 1
                    row += "%-11s" % "ok"
                continue
            rank = first_hit(chunks, item["expect"])
            if rank:
                found[i] += 1
                row += "%-11s" % ("#%d" % rank)
            else:
                failed = failed or i == 1
                row += "%-11s" % ("miss" if chunks else "none")
        print(row)
        for m in notes:
            print("        " + m)
        if not neg and failed:
            if a.why:
                explain(ds, item, CONFIGS[1][1])
            if a.probe:
                probe(ds, item)
    print("   %-52s" % ("found (of %d)" % npos) + "".join("%-11s" % ("%d/%d" % (f, npos)) for f in found))
    if nneg:
        print("   %-52s" % ("no-answer rejected (of %d)" % nneg) + "".join("%-11s" % ("%d/%d" % (r, nneg)) for r in rejected))
    print("\n   #N = rank of the first chunk with the expected facts; miss = not in top %d; none = nothing above"
          " the threshold; ok = correctly nothing found; LEAK = something passed the threshold (top score)" % a.topn)


if __name__ == "__main__":
    main()
