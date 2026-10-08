#!/usr/bin/env python3
"""Probe an OpenAI-compatible LLM for Russian answer quality and speed.

Runs the same Russian prompts in several modes and reports, per mode:
speed (completion tokens / wall time), and how many answers contain
CJK characters (language mixing).

Runs inside the RAGFlow container, which can reach the vLLM service; from compose/:

  sudo docker compose exec -T ragflow /ragflow/.venv/bin/python - < ../tools/model_probe.py

Elsewhere: VLLM_URL=http://host:8000 MODEL=<served name> python3 model_probe.py
"""
import json, os, re, sys, time, urllib.request

MODEL = os.environ.get("MODEL") or os.environ.get("RAGSPARK_LLM_NAME", "qwen3.8-27b")
CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")

CONTEXT = (
    "[1] Векторная база данных хранит эмбеддинги — числовые представления "
    "текстов — и ищет ближайшие по смыслу фрагменты.\n"
    "[2] BM25 — классический алгоритм полнотекстового поиска, который "
    "оценивает документы по совпадению слов запроса с учётом их частоты.\n"
    "[3] Реранкер заново сортирует найденные фрагменты, оценивая пару "
    "«вопрос — фрагмент» целиком, и поэтому точнее определяет релевантность."
)
PROMPTS = [
    "Объясни простыми словами, что такое векторная база данных. 3–4 предложения.",
    "Чем поиск BM25 отличается от поиска по эмбеддингам? Ответь кратко.",
    "Перечисли три преимущества локального запуска языковых моделей.",
    "Используя только контекст ниже, ответь, зачем нужен реранкер, и укажи "
    "номер источника в квадратных скобках.\n\nКонтекст:\n" + CONTEXT,
    "Кратко перескажи в двух предложениях: " + CONTEXT,
]
MODES = {
    "thinking (default)": {"max_tokens": 1536},
    "no-thinking":        {"max_tokens": 600,
                           "chat_template_kwargs": {"enable_thinking": False}},
    "no-thinking t=0.7":  {"max_tokens": 600, "temperature": 0.7, "top_p": 0.8, "top_k": 20,
                           "chat_template_kwargs": {"enable_thinking": False}},
}


def base_url():
    return os.environ.get("VLLM_URL", "http://vllm-llm:8000").rstrip("/")


def ask(url, prompt, params):
    body = dict(model=MODEL, messages=[{"role": "user", "content": prompt}], **params)
    req = urllib.request.Request(url + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=600) as r:
        d = json.load(r)
    dt = time.time() - t0
    msg = d["choices"][0]["message"]
    return {"time": dt,
            "tokens": d["usage"]["completion_tokens"],
            "content": (msg.get("content") or "").strip(),
            "reasoning": msg.get("reasoning") or msg.get("reasoning_content") or "",
            "finish": d["choices"][0].get("finish_reason")}


def main():
    url = base_url()
    print("model %s at %s\n" % (MODEL, url))
    summary = {}
    for mode, params in MODES.items():
        print("=" * 72 + "\n" + mode + "\n" + "=" * 72)
        rates, mixed = [], 0
        for i, p in enumerate(PROMPTS, 1):
            try:
                r = ask(url, p, params)
            except Exception as e:
                print("  #%d ERROR %s" % (i, e))
                continue
            cjk = CJK.findall(r["content"])
            mixed += 1 if cjk else 0
            rate = r["tokens"] / r["time"] if r["time"] else 0
            rates.append(rate)
            flag = "MIXED " + "".join(cjk)[:12] if cjk else "ok"
            print("  #%d %5.1fs %4d tok %5.1f tok/s  finish=%-6s %s" %
                  (i, r["time"], r["tokens"], rate, r["finish"], flag))
            print("      " + r["content"].replace("\n", " ")[:220])
        avg = sum(rates) / len(rates) if rates else 0
        summary[mode] = (avg, mixed, len(rates))
        print()
    print("=" * 72 + "\nSUMMARY")
    for mode, (avg, mixed, n) in summary.items():
        print("  %-20s avg %5.1f tok/s   answers with CJK: %d of %d" % (mode, avg, mixed, n))


if __name__ == "__main__":
    main()
