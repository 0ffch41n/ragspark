#!/usr/bin/env python3
"""mode_eval.py - compare RAGFlow's chat modes: Naive, Low, Medium, High, Ultra.

The selector under RAGFlow's chat input sends a reasoning level with every
question: 0 (Naive) is the classic retrieve-then-answer path, 1-4 (Low ... Ultra)
the agentic pipeline. This tool asks one chat the same questions at each level
through the API and reports, per answer: time, whether the expected facts are
there, sources, CJK characters, and for a question the documents do not
answer, whether the chat gave its "no answer" text.

Runs INSIDE the RAGFlow container, like the other tools; from compose/:

  sudo docker compose exec -T ragflow \\
    sh -c 'cd /ragflow && PYTHONPATH=/ragflow .venv/bin/python - --modes 0,1,2' < ../tools/mode_eval.py

Which chat: --chat NAME (default the acceptance example, "RAGSpark — пример"),
or --dataset NAME, which creates a temporary chat over that knowledge base with
RAGSpark's settings (as the acceptance test does) and deletes it afterwards.

Which questions: --set sample (the acceptance sample, default) or --set msi
(the MSI MPG B550 Gaming Plus manual used in stage 3.1, not distributed), or
--questions FILE with a JSON list of {"q": ..., "expect": [...]}. An expect
item may be a list of alternatives; "expect": null marks a question with no
answer in the documents. Sessions created by the run are deleted at the end
unless --keep-sessions is given. --trace prints, under each answer, the
progress log the agentic levels show as "thinking" in the web interface
([Hybrid search] ..., keyword narrowing, [Composing the answer] ...).

  ... .venv/bin/python - --dataset test --set msi --modes 0,1,2
"""
import argparse, base64, http.cookiejar, json, os, re, statistics, sys, time, urllib.error, urllib.parse, urllib.request

API = os.environ.get("RAGFLOW_API", "http://127.0.0.1:9380").rstrip("/")
MODES = {0: "Naive", 1: "Low", 2: "Medium", 3: "High", 4: "Ultra"}
CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯]")
THINK = re.compile(r"<think>.*?</think>", re.S)

QUESTIONS = [
    {"q": "В какой день недели и во сколько выполняется резервное копирование базы знаний?", "expect": ["сред", "2:30"]},
    {"q": "Какая максимальная температура допустима в серверной?", "expect": ["27"]},
    {"q": "Когда проходят плановые работы?", "expect": ["суббот", "9:00", "13:00"]},
    {"q": "По какому телефону вызвать дежурного администратора?", "expect": ["4417"]},
    {"q": "Сколько резервных копий хранится и где?", "expect": [["14", "четырнадцат"], "архив-2"]},
    {"q": "Как часто меняют пароль администратора и где хранится новый пароль?", "expect": ["90", "сейф"]},
    {"q": "Что должен сделать дежурный, если в серверной жарче 27 °C дольше десяти минут?",
     "expect": [["получас", "30 мин"]]},
    {"q": "Какая столица Австралии?", "expect": None},
]

# Stage 3.1 question set for the MSI manual, with the facts an answer must state.
MSI = [
    {"q": "Какой максимальный объём оперативной памяти поддерживает плата?", "expect": ["128"]},
    {"q": "В какой слот устанавливать первый модуль памяти?", "expect": ["DIMMA2"]},
    {"q": "Что означает оранжевый индикатор скорости на порту LAN?", "expect": [["1 Гбит", "1 Gbps", "1000"]]},
    {"q": "Как сбросить BIOS с помощью джампера JBAT1?", "expect": ["JBAT1"]},
    {"q": "Какой клавишей войти в настройки BIOS?", "expect": [["Delete", "Del"]]},
    {"q": "Почему может быть недоступен слот PCI_E3?", "expect": ["PCI_E3", "недоступ"]},
    {"q": "Какие размеры накопителей поддерживает разъём M2_2?", "expect": ["2280"]},
    {"q": "При какой температуре нельзя хранить материнскую плату?", "expect": ["60"]},
    {"q": "Сколько стоит эта материнская плата в рублях?", "expect": None},
    {"q": "Есть ли у платы встроенный Wi-Fi и Bluetooth?", "expect": None},
]
SETS = {"sample": QUESTIONS, "msi": MSI}

# The temporary chat for --dataset: RAGSpark's settings, as tools/acceptance.py sets them.
RERANK_INSTANCE, RERANK_MODEL = "ragspark-rerank", "BAAI/bge-reranker-v2-m3"
RETRIEVAL = {"similarity_threshold": 0.1, "vector_similarity_weight": 0.7, "top_n": 8, "rerank_candidates_count": 64}
EMPTY_TEXT = "В базе знаний нет ответа на этот вопрос."
SYSTEM = ("Ты — помощник, который отвечает на вопросы по документам из базы знаний. "
          "Отвечай на языке вопроса, кратко и по существу. Не используй китайские, "
          "японские и корейские иероглифы. Опирайся только на сведения из базы знаний "
          "ниже; если ответа в ней нет, так и скажи. Учитывай историю диалога.\n"
          "База знаний:\n{knowledge}\nКонец базы знаний.")

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


def call(method, path, body=None, timeout=120):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode() if body is not None else None, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if _auth["value"]:
        req.add_header("Authorization", _auth["value"])
    try:
        with _opener.open(req, timeout=timeout) as r:
            raw, code, headers = r.read().decode("utf-8", "replace"), r.status, r.headers
    except urllib.error.HTTPError as e:
        raw, code, headers = e.read().decode("utf-8", "replace"), e.code, e.headers
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, {"message": "timeout" if "timed out" in str(e) else str(getattr(e, "reason", e))}, {}
    try:
        return code, (json.loads(raw) if raw.strip() else {}), headers
    except ValueError:
        return code, {"message": raw[:200]}, headers


def ok(code, d):
    return code == 200 and isinstance(d, dict) and d.get("code") == 0


def items(d):
    data = d.get("data") if isinstance(d, dict) else None
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                return v
    return []


def strip_think(text):
    """The agentic path puts its progress and reasoning before "</think>"."""
    text = text or ""
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    return THINK.sub("", text).strip()


def progress_lines(text):
    """The pipeline's progress log ("[Hybrid search] ...") from the part before "</think>"."""
    text = text or ""
    if "</think>" not in text:
        return []
    head = re.sub(r"</?think>", "", text.rsplit("</think>", 1)[0])
    lines = re.split(r"<br\s*/?>|\n", head)
    return [" ".join(l.split()) for l in lines if l.strip().startswith("[")]


def norm(s):
    return re.sub(r"\s+", " ", (s or "").lower().replace("ё", "е"))


def login():
    password = os.environ.get("ADMIN_DEFAULT_PASSWORD") or os.environ.get("DEFAULT_SUPERUSER_PASSWORD")
    email = os.environ.get("RAGFLOW_ADMIN_EMAIL") or os.environ.get("DEFAULT_SUPERUSER_EMAIL") or "admin@ragflow.io"
    if not password:
        sys.exit("ADMIN_DEFAULT_PASSWORD is not set in this environment")
    code, d, headers = call("POST", "/api/v1/auth/login", {"email": email, "password": encrypt_password(password)})
    token = headers.get("Authorization", "") if headers else ""
    if not ok(code, d) or not token:
        sys.exit("login as %s failed: HTTP %s %s" % (email, code, d.get("message")))
    _auth["value"] = token


def find_chat(name):
    code, d, _ = call("GET", "/api/v1/chats?" + urllib.parse.urlencode({"name": name}))
    for c in items(d) if ok(code, d) else []:
        if c.get("name") == name:
            prompt = c.get("prompt_config") or c.get("prompt") or {}
            return c["id"], prompt.get("empty_response") or ""
    sys.exit("chat %r not found; run the acceptance test first or pass --chat" % name)


def temp_chat(dataset):
    code, d, _ = call("GET", "/api/v1/datasets?" + urllib.parse.urlencode({"name": dataset}))
    ds = [x["id"] for x in items(d) if x.get("name") == dataset] if ok(code, d) else []
    if not ds:
        sys.exit("knowledge base %r not found" % dataset)
    code, d, _ = call("GET", "/api/v1/models")
    rerank = [m.get("model_id") for m in items(d) if ok(code, d)
              and m.get("instance_name") == RERANK_INSTANCE and m.get("name") == RERANK_MODEL]
    if not rerank:
        sys.exit("reranker %s is not registered; run tools/ragflow_models.py first" % RERANK_MODEL)
    body = dict(RETRIEVAL, name="mode_eval: %s %d" % (dataset, int(time.time())), dataset_ids=ds,
                tenant_rerank_id=rerank[0],
                prompt_config={"system": SYSTEM, "prologue": "", "empty_response": EMPTY_TEXT, "quote": True,
                               "parameters": [{"key": "knowledge", "optional": False}]})
    code, d, _ = call("POST", "/api/v1/chats", body)
    if not ok(code, d):
        sys.exit("cannot create a chat over %r: HTTP %s %s" % (dataset, code, d.get("message")))
    return d["data"]["id"], body["name"]


def ask(chat, question, level, timeout):
    t0 = time.time()
    code, d, _ = call("POST", "/api/v1/chat/completions",
                      {"chat_id": chat, "question": question, "stream": False, "reasoning": level}, timeout=timeout)
    dt = time.time() - t0
    if not ok(code, d):
        return {"error": str(d.get("message") or "HTTP %s" % code)[:120], "time": dt}
    data = d.get("data") or {}
    return {"answer": strip_think(data.get("answer")), "trace": progress_lines(data.get("answer")),
            "refs": len((data.get("reference") or {}).get("chunks") or []),
            "session": data.get("session_id"), "time": dt}


def judge(res, expect, empty_text):
    if "error" in res:
        return "TIMEOUT" if "timeout" in res["error"] else "ERROR"
    answer = res["answer"]
    if expect is None:
        if empty_text and norm(answer) == norm(empty_text):
            return "ok"
        return "LEAK" if res["refs"] else "LEAK-own"
    missing = [e for e in expect
               if not any(norm(alt) in norm(answer) for alt in (e if isinstance(e, list) else [e]))]
    if missing:
        return "miss"
    if not res["refs"]:
        return "no-src"
    if CJK.search(answer):
        return "cjk"
    return "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat", default="RAGSpark — пример")
    ap.add_argument("--dataset", help="create a temporary chat over this knowledge base instead")
    ap.add_argument("--modes", default="0,1,2,3,4", help="reasoning levels, e.g. 0,1,2")
    ap.add_argument("--set", default="sample", choices=sorted(SETS), help="built-in question set")
    ap.add_argument("--questions", help="JSON file with questions (instead of --set)")
    ap.add_argument("--timeout", type=int, default=900, help="seconds per answer")
    ap.add_argument("--keep-sessions", action="store_true")
    ap.add_argument("--trace", action="store_true", help="print the agentic pipeline's progress log for each answer")
    a = ap.parse_args()
    levels = [int(x) for x in a.modes.split(",") if x.strip()]
    questions = json.load(open(a.questions, encoding="utf-8")) if a.questions else SETS[a.set]

    login()
    if a.dataset:
        chat, name = temp_chat(a.dataset)
        empty_text = EMPTY_TEXT
    else:
        (chat, empty_text), name = find_chat(a.chat), a.chat
    print("== chat %r, modes %s, %d questions ==" % (name, ", ".join(MODES[l] for l in levels), len(questions)),
          flush=True)
    try:
        run(chat, levels, questions, empty_text, a)
    finally:
        if a.dataset:
            call("DELETE", "/api/v1/chats", {"ids": [chat]})


def run(chat, levels, questions, empty_text, a):
    results, sessions = {}, []
    for level in levels:
        for i, item in enumerate(questions, 1):
            res = ask(chat, item["q"], level, a.timeout)
            if res.get("session"):
                sessions.append(res["session"])
            verdict = judge(res, item.get("expect"), empty_text)
            results.setdefault(level, []).append((verdict, res["time"]))
            shown = res.get("error") or " ".join(res["answer"].split())[:110]
            print("[%-6s] Q%d %-8s %5.0f s %2s src  %s" % (MODES[level], i, verdict, res["time"],
                                                           res.get("refs", "-"), shown), flush=True)
            if a.trace:
                for line in res.get("trace") or []:
                    print("           | " + line[:220], flush=True)

    print("\n%-7s %8s %9s %6s %6s %6s" % ("mode", "correct", "no-answer", "median", "max", "fails"))
    for level in levels:
        rows = results[level]
        facts = [(v, t) for (v, t), q in zip(rows, questions) if q.get("expect") is not None]
        rejects = [(v, t) for (v, t), q in zip(rows, questions) if q.get("expect") is None]
        times = [t for _, t in rows]
        fails = sum(v in ("TIMEOUT", "ERROR") for v, _ in rows)
        print("%-7s %4d/%-3d %5d/%-3d %5.0fs %5.0fs %6d" % (
            MODES[level], sum(v == "ok" for v, _ in facts), len(facts), sum(v == "ok" for v, _ in rejects),
            len(rejects), statistics.median(times), max(times), fails))

    if sessions and not a.keep_sessions:
        call("DELETE", "/api/v1/chats/%s/sessions" % chat, {"ids": sessions})


if __name__ == "__main__":
    main()
