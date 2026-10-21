#!/usr/bin/env python3
"""Pin the images RAGFlow's Dockerfile builds from to digests.

Upstream names them by floating tags: `FROM ubuntu:24.04` for the base and
`infiniflow/ragflow_deps:latest` for the data files the build copies with
`RUN --mount=type=bind,from=...` (OCR models, NLTK data, Tika, uv, the Chrome
archives). A tag can move, so a later rebuild of the same RAGSpark revision
could take other files - ubuntu:24.04 did move between r2 (2026-10-08) and r4
(2026-10-19). The references are replaced by the ones given on the command
line, which build.sh pins by digest; the image content does not change as
long as the digests are the ones the tags pointed to when they were recorded.

Usage: patch_base_images.py <path/to/Dockerfile> <base image ref> <deps image ref>
Idempotent: a second run with the same references leaves the file as is.
"""
import sys

BASE_TAG = "ubuntu:24.04"
DEPS_TAG = "infiniflow/ragflow_deps:latest"
BASE_LINE = "FROM %s AS base"
DEPS_MOUNT = "from=%s,"


def main():
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    path, base, deps = sys.argv[1:]
    for ref, tag in ((base, BASE_TAG), (deps, DEPS_TAG)):
        if not ref.startswith(tag + "@sha256:"):
            sys.exit("patch_base_images: %r must be %s@sha256:<digest>" % (ref, tag))
    text = open(path, encoding="utf-8").read()
    if (BASE_LINE % base) in text and (DEPS_MOUNT % DEPS_TAG) not in text:
        print("patch_base_images: already applied")
        return
    if text.count(BASE_LINE % BASE_TAG) != 1:
        sys.exit("patch_base_images: expected one %r in %s; Dockerfile changed? Nothing was written."
                 % (BASE_LINE % BASE_TAG, path))
    mounts = text.count(DEPS_MOUNT % DEPS_TAG)
    if mounts == 0:
        sys.exit("patch_base_images: no %r in %s; Dockerfile changed? Nothing was written."
                 % (DEPS_MOUNT % DEPS_TAG, path))
    text = text.replace(BASE_LINE % BASE_TAG, BASE_LINE % base)
    text = text.replace(DEPS_MOUNT % DEPS_TAG, DEPS_MOUNT % deps)
    open(path, "w", encoding="utf-8").write(text)
    print("patch_base_images: base image and %d ragflow_deps mount(s) pinned by digest" % mounts)


if __name__ == "__main__":
    main()
