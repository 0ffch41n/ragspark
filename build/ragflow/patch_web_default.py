#!/usr/bin/env python3
"""Start the chat input at the "Naive" reasoning level instead of "Low".

The level selector under RAGFlow's chat input remembers the user's choice in
the browser; until a choice is made it sends level 1 (Low), which answers
through the agentic pipeline. On a DGX Spark with the default model that takes
about 15 s per answer against 3 s for level 0 (Naive), the plain
retrieve-then-answer path, with the same answers on RAGSpark's question sets
(docs/VALIDATION.md). The other levels stay available in the selector.

Usage: patch_web_default.py <ragflow source root>     (idempotent)
"""
import os
import sys

FILE = os.path.join("web", "src", "utils", "authorization-util.ts")
MARK = "// ragspark: Naive by default"
OLD = "    return localStorage.getItem(ThinkingLevel) || '1';\n"
NEW = "    return localStorage.getItem(ThinkingLevel) || '0'; %s\n" % MARK


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    path = os.path.join(sys.argv[1], FILE)
    src = open(path, encoding="utf-8").read()
    if MARK in src:
        print("patch_web_default: already applied")
        return
    if src.count(OLD) != 1:
        sys.exit("patch_web_default: expected line not found exactly once in %s - RAGFlow version changed?" % path)
    open(path, "w", encoding="utf-8").write(src.replace(OLD, NEW))
    print("patch_web_default: the chat input starts at Naive")


if __name__ == "__main__":
    main()
