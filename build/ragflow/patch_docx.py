#!/usr/bin/env python3
"""DOCX chunks keep a separator between the pieces they are built from.

Found on RAGFlow 0.27.2 with RAGSpark's datasets (naive chunking, delimiter
"\\n"); the measurements are in RAGSpark's docs/VALIDATION.md.

For a DOCX, `naive_merge_docx` in rag/nlp/__init__.py first lets `_build_cks`
split every paragraph on the dataset's delimiter; each piece is stripped and
the delimiter it was split on is dropped. `_merge_cks` then packs the pieces
into chunks up to `chunk_token_num`, joining them with `text + text`. Every
paragraph boundary inside a chunk therefore lost its separator: the last word
of one paragraph ran into the first word of the next. On a 77-paragraph guide
74 of 76 boundaries were glued, 25 of them letter to letter ("lm-sensorsgit",
"make" + "git", "H100" + "nvidia"), and in 10 the keyword-search tokens changed,
so those words could not be found. PDFs take a different path (`naive_merge`)
and are not affected.
The pieces are now joined with a newline. Chunk borders do not move: the
budget is counted from the pieces' token counts, which are unchanged, so the
chunks are the same chunks with their separators back. Documents indexed before
this change keep the glued text until they are parsed again.

Usage: patch_docx.py <ragflow source root>     (idempotent; checks the
anchor before it writes anything)
"""
import os
import sys

NLP = os.path.join("rag", "nlp", "__init__.py")

MERGE_OLD = """        merged[prev_text_ck]["text"] = (merged[prev_text_ck].get("text") or "") + (cks[i].get("text") or "")
"""
MERGE_NEW = """        # ragspark: pieces are joined with a newline - see build/ragflow/patch_docx.py in RAGSpark.
        # _build_cks strips each piece and drops the delimiter it split on, so joining them
        # directly glued the last word of one paragraph to the first word of the next.
        left = merged[prev_text_ck].get("text") or ""
        right = cks[i].get("text") or ""
        merged[prev_text_ck]["text"] = left + "\\n" + right if left and right else left + right
"""

EDITS = [
    ("DOCX chunks keep a separator between pieces", NLP,
     "# ragspark: pieces are joined with a newline", [
         (MERGE_OLD, MERGE_NEW),
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
            print("patch_docx: %s - already applied" % name)
            continue
        for old, _ in subs:
            if texts[path].count(old) != 1:
                sys.exit("patch_docx: %s - expected code not found exactly once in %s; "
                         "RAGFlow version changed? Nothing was written." % (name, path))
        todo.append((name, path, subs))
    for name, path, subs in todo:
        for old, new in subs:
            texts[path] = texts[path].replace(old, new)
        print("patch_docx: %s" % name)
    for path in {p for _, p, _ in todo}:
        with open(path, "w", encoding="utf-8") as f:
            f.write(texts[path])


if __name__ == "__main__":
    main()
