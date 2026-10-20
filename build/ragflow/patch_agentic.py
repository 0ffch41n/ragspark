#!/usr/bin/env python3
"""Fixes for RAGFlow's agentic chat modes (reasoning levels Low ... Ultra).

Found on RAGFlow 0.27.2 with Qwen 3.8 27B NVFP4 and Russian documents; the
measurements are in RAGSpark's docs/VALIDATION.md.

1. Helper calls that never end. The pipeline's helper calls (keywords,
   question rewriting, verdicts) run at temperature 0.1 without an output
   limit - RAGFlow strips max_tokens from every request. The keyword request
   looped in 9 of 30 runs ("серверная", "серверная", ...) until the 64k
   context ran out. All of them go through CountingChatModel.async_chat;
   they now get, unless the caller sets its own:
     frequency_penalty      RAGSPARK_HELPER_FREQUENCY_PENALTY, default 0.5
                            (0 loops in 30 runs, 30/30 valid JSON)
     max_completion_tokens  RAGSPARK_HELPER_MAX_TOKENS, default 2048
   An empty variable disables that default.

2. Medium, High and Ultra always gave the "no answer" text. Their graph
   starts with empty_result=True and nothing ever resets it, so the answer
   step treated the gathered passages as no evidence whenever the chat has an
   empty response configured. empty_result now counts only without chunks.

3. Agentic retrieval ignored the chat's reranker. The hybrid search of the
   agentic pipeline (RAGTools.retrieve and the harness's hybrid_search) passes
   no rerank model, so passages were ranked and cut by raw scores; for Russian
   text the reranker decides relevance (RAGSpark D15). The chat's reranker is
   now handed to the pipeline and used by both. The specialised vector-only and
   keyword-only tools keep their own scoring.

4. A note for the outer model leaked into answers. When research ends
   insufficient, the rag tool appends "[Research status] ... call rag again"
   for the outer model; but rag is a terminal tool, so that text became part
   of the answer shown to the user. It is removed from the answer.

5. The reranker scored the expanded query. Low mode's hybrid search queries
   with the question plus its weighted keywords (entity terms three times,
   English aliases, fact types); with a reranker that whole string became the
   rerank query, scores fell under the threshold and Low lost answers it had
   found without one. With a reranker the search now uses the question itself;
   the expansion only ever helped word matching, which the kNN filter fix and
   the reranker cover.

6. The outer model answered from its own knowledge. Its router prompt lets it
   skip the rag tool for questions it thinks need no evidence: in a document
   chat it answered "capital of Australia" itself - once "Sydney". When the
   chat has knowledge bases, the prompt now says to answer only through rag,
   as the plain (Naive) path does. Chats without knowledge bases keep the
   upstream prompt and answer freely.

7. Keyword narrowing dropped the right passage. After each search the
   pipeline keeps only the passages that mention one of the question's
   keywords - stem-tolerant for ASCII words, but verbatim for anything else.
   Russian word forms differ: with the keywords "день недели, время, резервное
   копирование базы знаний" the passage "Резервная копия базы знаний
   создаётся ... каждую среду в 02:30 по местному времени" matched nothing,
   another passage matched "время" ("В это время чат может быть недоступен"),
   and Low answered that the documents had no answer. With Cyrillic keywords
   the passages are now kept as the search (with the reranker) ranked them.

8. The outer model did not always call rag. Every agentic level starts with
   an outer model that has the rag tool and decides whether to call it
   (tool_choice "auto"). On a fresh install it skipped the tool in 3 of 9 Low
   questions: once it answered "the knowledge base has no information" from
   the chat's system prompt, whose knowledge section is empty at that point,
   and twice it wrote the self-contained question as plain text, which came
   back as the answer. In a chat with knowledge bases the first round now
   requires the rag call (tool_choice naming the function; vLLM then
   constrains the output to its arguments); later rounds and chats without
   knowledge bases stay on "auto".

9. Research ran out of time before it was finished. The whole graph had a 180s
   wall-clock ceiling, chosen upstream so that one question stays under a
   benchmark client's 300s read timeout. The Spark streams this model at about
   17 tok/s, so High and Ultra spent the budget inside the first research round
   and the routing guards sent the graph to synthesis with partial evidence.
   The ceiling is now RAGSPARK_AGENTIC_BUDGET_S, default 300: the answer
   streams over SSE, so no client read timeout caps the pipeline. The per-call
   bounds (research pass 120s, prefetch 90s, draft 60s, review 60s) and the 50s
   headroom a new round requires are unchanged, so the larger budget buys more
   research rounds, not longer single calls. Set the variable lower to restore
   the upstream bound.

10. The planner split Russian questions into English sub-questions. The
    fan-out prompt states no language, so "Когда выполняется резервное
    копирование?" came back as "knowledge base backup schedule" - and the
    fan-outs are used verbatim as corpus queries and as the slot table's
    aspect hints. Against Russian documents they match nothing. The prompt now
    requires every fan-out in the question's language, the rule RAGFlow's own
    keyword and multi-query prompts already carry.

11. The research queries of the upper levels were English for the same reason.
    Neither the slot-table decomposition (action_initialize_state.md, which
    emits "first_queries" plus the per-slot clues) nor the gap rewriter
    (sca_query_rewrite.md, whose every example is English) says which language
    to write in. Both prompts now carry the rule. This is the one failure the
    reranker and the kNN filter fix cannot repair: a query in the wrong
    language has no overlap left to rank.

12. Ultra died when the reviewer named its claims instead of describing them.
    The Sufficient Context Agent reads the review as a list of claim objects;
    the model sometimes answers "claims": ["c0", "s1"], or keys the objects by
    claim id, and item.get() on a string raises AttributeError. Nothing caught
    it - the graph awaits the review through _bounded, which handles only
    TimeoutError - so the question ended in a traceback instead of an answer.
    Claim items that are not objects are now logged and skipped, and a claims
    object keyed by id is read as well.

Usage: patch_agentic.py <ragflow source root>     (idempotent; checks every
anchor before it writes anything)
"""
import os
import sys

STATS = os.path.join("rag", "advanced_rag", "harness", "stats.py")
GRAPH = os.path.join("rag", "advanced_rag", "agentic_rag_graph.py")
TOOLS = os.path.join("rag", "advanced_rag", "agentic_rag.py")
SEARCH = os.path.join("rag", "advanced_rag", "harness", "tools", "search.py")
TEXT = os.path.join("rag", "advanced_rag", "harness", "tools", "text_processing.py")
SCA = os.path.join("rag", "advanced_rag", "harness", "orchestrator", "sufficient_context.py")
DIALOG = os.path.join("api", "db", "services", "dialog_service.py")
CHAT = os.path.join("rag", "llm", "chat_model.py")
SLOT_PROMPT = os.path.join("rag", "prompts", "action_initialize_state.md")
REWRITE_PROMPT = os.path.join("rag", "prompts", "sca_query_rewrite.md")

# ── 9. research budget ──────────────────────────────────────────────────────
BUDGET_OLD = "_TOTAL_BUDGET_S = 180.0  # whole-graph wall-clock ceiling per question\n"
BUDGET_NEW = '''# ragspark: research budget from the environment - RAGSPARK_AGENTIC_BUDGET_S, default
# 300. The answer streams over SSE, so no client read timeout caps the pipeline; the
# per-call bounds below are unchanged, so this buys more rounds, not longer calls.
_TOTAL_BUDGET_S = float((__import__("os").environ.get("RAGSPARK_AGENTIC_BUDGET_S") or "300").strip() or 300)
'''

# ── 10. planner fan-out language ────────────────────────────────────────────
FANOUT_OLD = '''    'Respond with a JSON object: {"fanouts": ["...", "..."]}. No prose, JSON only.'
)
'''
FANOUT_NEW = '''    # ragspark: fan-outs in the question's language
    "4. Write every fan-out in the SAME language as the user's question. The corpus is in "
    "that language; a sub-question translated into another language matches nothing.\\n"
    'Respond with a JSON object: {"fanouts": ["...", "..."]}. No prose, JSON only.'
)
'''

# ── 11. search-query language of the upper levels ───────────────────────────
SLOT_MARK = "- LANGUAGE: write every clue and every first_query in the SAME language"
SLOT_OLD = """- 1-4 first_queries: direct keyword-style searches against the listed sources.
- No prose outside JSON.
"""
SLOT_NEW = """- 1-4 first_queries: direct keyword-style searches against the listed sources.
- LANGUAGE: write every clue and every first_query in the SAME language as the question. The corpus is written in that language; a query translated into another language matches nothing in it.
- No prose outside JSON.
"""

REWRITE_MARK = "9. LANGUAGE: write every query in the SAME language"
REWRITE_OLD = """
Output format (JSON):
"""
REWRITE_NEW = """
9. LANGUAGE: write every query in the SAME language as the original user question above. The corpus is written in that language; a query translated into another language matches nothing in it. The examples in rules 1-4 are English only because their example questions are.

Output format (JSON):
"""

# ── 12. claim items that are not objects ────────────────────────────────────
CLAIMS_OLD = '''    claims_out: dict[str, dict] = {}
    for item in result.get("claims") or []:
        cid = str(item.get("claim_id") or "")
'''
CLAIMS_NEW = '''    claims_out: dict[str, dict] = {}
    raw_claims = result.get("claims") or []  # ragspark: tolerate claim items that are not objects
    if isinstance(raw_claims, dict):
        # The review keyed its claims by id instead of listing them.
        raw_claims = [dict(v, claim_id=v.get("claim_id") or k) if isinstance(v, dict) else {"claim_id": k} for k, v in raw_claims.items()]
    elif not isinstance(raw_claims, list):
        raw_claims = []
    for item in raw_claims:
        if not isinstance(item, dict):
            # A bare claim id instead of the claim object: there is no verdict to
            # read, and item.get() would raise AttributeError out of the graph
            # (_bounded handles only TimeoutError, so Ultra failed with a
            # traceback instead of an answer).
            _LOG.info("[SCA] ignoring %s claim item %r", type(item).__name__, str(item)[:120])
            continue
        cid = str(item.get("claim_id") or "")
'''

EDITS = [
    ("helper-call defaults", STATS, "# ragspark: helper-call defaults", [
        ("class CountingChatModel:\n",
         '''# ragspark: helper-call defaults - see build/ragflow/patch_agentic.py in RAGSpark
def _ragspark_helper_conf(gen_conf):
    import os

    conf = dict(gen_conf or {})
    penalty = os.environ.get("RAGSPARK_HELPER_FREQUENCY_PENALTY", "0.5").strip()
    limit = os.environ.get("RAGSPARK_HELPER_MAX_TOKENS", "2048").strip()
    if penalty and "frequency_penalty" not in conf:
        conf["frequency_penalty"] = float(penalty)
    if limit and "max_completion_tokens" not in conf and "max_tokens" not in conf:
        conf["max_completion_tokens"] = int(limit)
    return conf


class CountingChatModel:
'''),
        ("            txt = await self._chat_mdl.async_chat(system, history, gen_conf or {}, **kwargs)\n",
         "            txt = await self._chat_mdl.async_chat(system, history, _ragspark_helper_conf(gen_conf), **kwargs)\n"),
    ]),
    ("stale empty_result", GRAPH, "# ragspark: empty_result only without chunks", [
        ('    empty_result = state.get("empty_result", False)\n',
         '    empty_result = state.get("empty_result", False) and not kbinfos["chunks"]  '
         '# ragspark: empty_result only without chunks\n'),
    ]),
    ("reranker handed to the pipeline", DIALOG, "# ragspark: reranker for the agentic pipeline", [
        ("        top_k=dialog.top_k,\n    )\n\n    async def decorate_answer(answer):\n",
         "        top_k=dialog.top_k,\n    )\n"
         "    rag_tools.rerank_mdl = rerank_mdl  # ragspark: reranker for the agentic pipeline\n"
         "\n    async def decorate_answer(answer):\n"),
    ]),
    ("reranker in RAGTools.retrieve", TOOLS, "# ragspark: agentic retrieval uses the reranker", [
        ("            rank_feature=label_question(question, self.kbs),\n"
         "            rerank_candidates_count=rerank_candidates_count,\n        )\n",
         "            rank_feature=label_question(question, self.kbs),\n"
         "            rerank_candidates_count=rerank_candidates_count,\n"
         '            rerank_mdl=getattr(self, "rerank_mdl", None),  # ragspark: agentic retrieval uses the reranker\n'
         "        )\n"),
    ]),
    ("reranker in hybrid_search", SEARCH, "# ragspark: agentic retrieval uses the reranker", [
        ('        must_not={"exists": "compile_kwd"},  # plain retrieval = document chunks only; '
         'compiled products have their own tools\n'
         "        rerank_candidates_count=rerank_candidates_count,\n    )\n",
         '        must_not={"exists": "compile_kwd"},  # plain retrieval = document chunks only; '
         'compiled products have their own tools\n'
         "        rerank_candidates_count=rerank_candidates_count,\n"
         '        rerank_mdl=getattr(tools, "rerank_mdl", None),  # ragspark: agentic retrieval uses the reranker\n'
         "    )\n"),
    ]),
    ("rerank the question, not the expansion", SEARCH, "# ragspark: rerank the question, not the expansion", [
        ("    if retrieval_query:\n"
         '        effective_query = f"{query} {retrieval_query}".strip()[:400]\n',
         '    if getattr(tools, "rerank_mdl", None) is not None:  # ragspark: rerank the question, not the expansion\n'
         "        effective_query = query\n"
         "    elif retrieval_query:\n"
         '        effective_query = f"{query} {retrieval_query}".strip()[:400]\n'),
    ]),
    ("router answers only through rag", TOOLS, "# ragspark: document chats answer only through rag", [
        ('            "You are a smart agent. For any question that needs "\n'
         '            "evidence from the knowledge bases or the web, call the `rag` tool "\n',
         "            (  # ragspark: document chats answer only through rag\n"
         '                "You are a smart agent. Answer every user question only from the "\n'
         '                "knowledge bases, never from your own knowledge: always call the `rag` tool "\n'
         "                if self.kb_ids else\n"
         '                "You are a smart agent. For any question that needs "\n'
         '                "evidence from the knowledge bases or the web, call the `rag` tool "\n'
         "            ) +\n"),
    ]),
    ("no keyword narrowing for Cyrillic", TEXT, "# ragspark: no keyword narrowing for Cyrillic", [
        ('    kwds = [k.strip().lower() for k in (keywords or "").split(",") if k.strip()]\n'
         "    if not kwds or not chunks:\n        return chunks\n",
         '    kwds = [k.strip().lower() for k in (keywords or "").split(",") if k.strip()]\n'
         "    if not kwds or not chunks:\n        return chunks\n"
         '    if re.search("[а-яё]", (keywords or "").lower()):  # ragspark: no keyword narrowing for Cyrillic\n'
         '        _LOG.info("[ragspark] Keyword narrowing skipped for Cyrillic keywords; keeping all %d passage(s).", len(chunks))\n'
         "        return chunks\n"),
    ]),
    ("first round calls rag", CHAT, "# ragspark: first round must call this tool", [
        ("        tools = self.tools if tools is None else tools\n"
         "        if not tools:\n            return {}\n"
         '        return {"tools": tools, "tool_choice": "auto"}\n',
         "        tools = self.tools if tools is None else tools\n"
         "        if not tools:\n            return {}\n"
         '        forced = getattr(self, "ragspark_first_tool", None)  # ragspark: first round must call this tool\n'
         '        if forced and any((t.get("function") or {}).get("name") == forced for t in tools):\n'
         "            self.ragspark_first_tool = None\n"
         '            return {"tools": tools, "tool_choice": {"type": "function", "function": {"name": forced}}}\n'
         '        return {"tools": tools, "tool_choice": "auto"}\n'),
    ]),
    ("document chats start with rag", DIALOG, "# ragspark: document chats start with rag", [
        ('    if getattr(chat_mdl, "mdl", None) is not None:\n'
         '        chat_mdl.mdl.terminal_tools = {"rag"}\n',
         '    if getattr(chat_mdl, "mdl", None) is not None:\n'
         '        chat_mdl.mdl.terminal_tools = {"rag"}\n'
         "        if dialog.kb_ids:  # ragspark: document chats start with rag\n"
         '            chat_mdl.mdl.ragspark_first_tool = "rag"\n'),
    ]),
    ("research-status note", DIALOG, "# ragspark: drop the research-status note", [
        ('        nonlocal rag_tools, messages\n\n        refs = []\n        ans = answer.split("</think>")\n',
         "        nonlocal rag_tools, messages\n\n        refs = []\n"
         '        answer = re.sub(r"\\s*\\[Research status\\].*\\Z", "", answer or "", flags=re.S)  '
         "# ragspark: drop the research-status note\n"
         '        ans = answer.split("</think>")\n'),
    ]),
    ("research budget from the environment", GRAPH, "# ragspark: research budget from the environment", [
        (BUDGET_OLD, BUDGET_NEW),
    ]),
    ("fan-outs in the question's language", GRAPH, "# ragspark: fan-outs in the question's language", [
        (FANOUT_OLD, FANOUT_NEW),
    ]),
    ("slot-table queries in the question's language", SLOT_PROMPT, SLOT_MARK, [
        (SLOT_OLD, SLOT_NEW),
    ]),
    ("gap-rewrite queries in the question's language", REWRITE_PROMPT, REWRITE_MARK, [
        (REWRITE_OLD, REWRITE_NEW),
    ]),
    ("claim items that are not objects", SCA, "# ragspark: tolerate claim items that are not objects", [
        (CLAIMS_OLD, CLAIMS_NEW),
    ]),
]


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    root = sys.argv[1]
    texts, todo = {}, []
    for name, rel, mark, subs in EDITS:
        path = os.path.join(root, rel)
        if path not in texts:
            texts[path] = open(path, encoding="utf-8").read()
        if mark in texts[path]:
            print("patch_agentic: %s - already applied" % name)
            continue
        for old, _ in subs:
            if texts[path].count(old) != 1:
                sys.exit("patch_agentic: %s - expected code not found exactly once in %s; "
                         "RAGFlow version changed? Nothing was written." % (name, path))
        todo.append((name, path, subs))
    for name, path, subs in todo:
        for old, new in subs:
            texts[path] = texts[path].replace(old, new)
        print("patch_agentic: %s" % name)
    for path in {p for _, p, _ in todo}:
        with open(path, "w", encoding="utf-8") as f:
            f.write(texts[path])


if __name__ == "__main__":
    main()
