#!/usr/bin/env python3
"""Give RAGFlow's reranker the original chunk text instead of glued tokens.

RAGFlow 0.27.2 builds the cross-encoder input with
remove_redundant_spaces(" ".join(tokens)). That helper deletes every space
that follows a character outside [a-z0-9.,)>], so Cyrillic (and Greek,
Arabic, ...) text reaches the reranker as one long word:
"оранжевый1гбитсподключение...". Rerank scores drop 2-3x and good answers
fall below the similarity threshold.

This patch sends the chunk's original text (plus its keywords and generated
questions, which RAGFlow also appends) and keeps the old input only as a
fallback for empty chunks. Token similarity is not touched.

Usage: patch_search.py <path/to/rag/nlp/search.py>     (idempotent)
"""
import sys

MARK = "# ragspark: rerank on original text"
OLD = ('        docs = [remove_redundant_spaces(" ".join(tks)) or str(sres.field[i].get("content_with_weight") or "") '
       'for i, tks in zip(sres.ids, ins_tw)]\n')
NEW = '''        %s - remove_redundant_spaces() glues non-Latin text into one word
        docs = []
        for i, tks in zip(sres.ids, ins_tw):
            fld = sres.field[i]
            parts = [str(fld.get("content_with_weight") or "")]
            for key in ("important_kwd", "question_kwd"):
                val = fld.get(key) or []
                parts.append("\\n".join(map(str, val)) if isinstance(val, list) else str(val))
            text = "\\n".join(p for p in parts if p.strip())
            docs.append(text or remove_redundant_spaces(" ".join(tks)))
''' % MARK


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    path = sys.argv[1]
    src = open(path, encoding="utf-8").read()
    if MARK in src:
        print("patch_search: already applied")
        return
    if src.count(OLD) != 1:
        sys.exit("patch_search: expected line not found exactly once in %s - RAGFlow version changed?" % path)
    open(path, "w", encoding="utf-8").write(src.replace(OLD, NEW))
    print("patch_search: reranker now receives original chunk text")


if __name__ == "__main__":
    main()
