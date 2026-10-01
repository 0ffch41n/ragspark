#!/usr/bin/env python3
"""Make RAGFlow's x86-only Chrome/ChromeDriver steps conditional on x86_64.

The Chrome for Testing archives shipped in infiniflow/ragflow_deps are
linux64 (x86_64) builds. On arm64 they install but cannot run, so RAGSpark
skips them there. On x86_64 the original commands run unchanged.

Usage: patch_dockerfile.py <path/to/Dockerfile>
Idempotent: a second run leaves the file as is.
"""
import sys

MARK = "# ragspark: x86-only step, skipped on other architectures"
TOKENS = ("source=/chrome-linux64-", "source=/chromedriver-linux64-")


def find_block(lines, token):
    """Return (start, end) of the RUN instruction whose first line has token."""
    hits = [i for i, l in enumerate(lines) if l.lstrip().startswith("RUN ") and token in l]
    if len(hits) != 1:
        sys.exit("patch: expected exactly one RUN with %r, found %d" % (token, len(hits)))
    start = end = hits[0]
    while lines[end].rstrip().endswith("\\"):
        end += 1
        if end >= len(lines):
            sys.exit("patch: RUN block for %r never ends" % token)
    return start, end


def wrap(block):
    """Wrap the commands of a RUN block in an x86_64 guard."""
    head, body = block[0], block[1:]
    if not head.rstrip().endswith("\\") or not body:
        sys.exit("patch: unexpected RUN layout: %r" % head)
    out = [MARK, head, '    if [ "$(uname -m)" = "x86_64" ]; then \\']
    for i, line in enumerate(body):
        text = line.rstrip()
        if i == len(body) - 1:
            text = text + "; \\"
        out.append("    " + text)
    out += ["    else \\",
            '        echo "ragspark: skipping x86-only step on $(uname -m)"; \\',
            "    fi"]
    return out


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    path = sys.argv[1]
    text = open(path, encoding="utf-8").read()
    if MARK in text:
        print("patch: already applied")
        return
    lines = text.split("\n")
    for token in TOKENS:
        start, end = find_block(lines, token)
        lines[start:end + 1] = wrap(lines[start:end + 1])
    open(path, "w", encoding="utf-8").write("\n".join(lines))
    print("patch: Chrome and ChromeDriver steps now run on x86_64 only")


if __name__ == "__main__":
    main()
