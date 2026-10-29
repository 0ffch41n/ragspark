# Building the RAGFlow arm64 image

[Русская версия](BUILD.ru.md)

RAGFlow publishes x86 images only, so RAGSpark builds its own arm64 image
from the upstream source. This page documents how, and what differs from
upstream.

## Published images

| Tag | Digest | Patches | Status |
|---|---|---|---|
| `0.27.2-arm64-r6` | `sha256:16ba7dd9d03ae8d6a8d9f819e6c7e62d78874dfeb27367bbec536da02ea40fe9` | + health check returns its connection; base images pinned | current |
| `0.27.2-arm64-r5` | `sha256:a34a9f1f1009442497f50da91691edbc1dc19dea4cea7a73c9673a0000f61375` | + scans: Cyrillic text layer, vision at temperature 0, whole tables | superseded |
| `0.27.2-arm64-r4` | `sha256:1ed7725f5932a4799591104b5efd1d1ed7f50984e070be24ed4251d27341e0f8` | + research budget, query language, claim shapes | superseded |
| `0.27.2-arm64-r3` | `sha256:8d08d824a9b248fceba8a0c5885aaf3cef7f24c7137ec87154aef53031c22e5a` | + kNN filter, agentic pipeline, Naive by default — two of them absent, see below | superseded |
| `0.27.2-arm64-r2` | `sha256:4232ddbb513cff48c51864f081f6b82d37371927dea70bb01a200c6994f70dcc` | Chrome + reranker input | superseded |
| `0.27.2-arm64-r1` | `sha256:1bf5fc0031bff2d775dc8bc1626b2820f9b658b43696a9bb4bdc02a8304f0660` | Chrome | superseded |

Current image:

```bash
docker pull ghcr.io/0ffch41n/ragspark-ragflow:0.27.2-arm64-r6@sha256:16ba7dd9d03ae8d6a8d9f819e6c7e62d78874dfeb27367bbec536da02ea40fe9
```

`r6` was built and pushed on 2026-10-21 on a DGX Spark, the first image built
from pinned base images. Verified by comparing it with `r5` file by file: of the
1947 files under `/ragflow` outside `.venv`, exactly one differs —
`api/utils/health_utils.py`, the file of `build/ragflow/patch_health.py` — and
`.venv` is the same. Every file the build patches is byte for byte the one in
the container the health check was measured on, so
[VALIDATION.md](VALIDATION.md), §11 — no database warnings, every health check
`ok` — describes this image. When this was written the stack was still running
`r5` with that fix applied hot; recreating it on `r6` replaces the hot patch
with the same code baked into the image. Build inputs, pinned in `build.sh` and
confirmed by the build log: `ubuntu:24.04@sha256:68ee2200…` and
`infiniflow/ragflow_deps:latest@sha256:e69762c2…`, the same as `r4` and `r5`.

`r5` was built and pushed on 2026-10-20 on a DGX Spark. Verified by comparing
it with `r4` file by file: of the 1947 files under `/ragflow` outside `.venv`,
exactly three differ — `deepdoc/parser/pdf_parser.py`, `rag/llm/cv_model.py`
and `rag/nlp/__init__.py`, the files of `build/ragflow/patch_scans.py` — and
`.venv` is the same. So `r5` carries everything `r4` does unchanged (the fifteen
markers, the two prompt rules without one, the research budget) plus the three
markers of `patch_scans.py`. Those three files are byte for byte the ones in the
container the scans were measured on, so the measurements in
[VALIDATION.md](VALIDATION.md), §10 — the scan 8/8 through vision, the digital
manual unchanged — describe this image. When this was written the stack was
still running the hot-patched `r4` container; recreating it on `r5` replaces
those hot patches with the same code baked into the image. Build inputs, from
the build log: the same `ubuntu:24.04` and `infiniflow/ragflow_deps:latest`
digests as `r4`.

`r4` was built and pushed on 2026-10-19 on a DGX Spark. Verified by inspecting
the image itself: it carries all fifteen patch markers of
`build/ragflow/patch_*.py` plus the two prompt rules that carry no marker
(`rag/prompts/action_initialize_state.md`, `rag/prompts/sca_query_rewrite.md`),
and `_TOTAL_BUDGET_S` reads `RAGSPARK_AGENTIC_BUDGET_S`. That marker set is
identical to the one in the container the chat modes were measured on, so the
measurements in [VALIDATION.md](VALIDATION.md), §8 — every level 7/7 with the
question without an answer still rejected — describe this image. When this was
written the stack was still running the hot-patched `r3` container; recreating
it on `r4` replaces those hot patches with the same code baked into the image.
Build inputs, as the registry reported them right after the build:

- `ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac`
- `infiniflow/ragflow_deps:latest@sha256:e69762c256ea2338a786a9b2f003da8e91e5e69b26bb6797c2206276ce32e5ff`
  — the same digest as `r2` and `r3`, so the floating tag had not moved and the
  data files are unchanged.

`r3` was verified on 2026-10-14 on a DGX Spark, and the check found a gap: the
image was built at 12:12, before the last three edits of
`build/ragflow/patch_agentic.py` were written at 13:59, so it carries nine of
the fifteen markers and lacks the Cyrillic keyword narrowing and the forced
first `rag` call. Those two only ever ran as a hot patch on the container, and
the chat-mode measurements of 2026-10-14 were taken with them applied. `r4` is
the first image to carry them.

`r2` was verified on 2026-10-08 on a DGX Spark: the patch marker is present in
`/ragflow/rag/nlp/search.py`, the model check passed on the test stack, and the
retrieval matrix matched the hot-patched results
([VALIDATION.md](VALIDATION.md), §4). Build inputs:

- `ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55`
- `infiniflow/ragflow_deps:latest@sha256:e69762c256ea2338a786a9b2f003da8e91e5e69b26bb6797c2206276ce32e5ff` — upstream refers to this image by a
  floating tag, so a later rebuild could take different data files; the build
  now pins it, and `ubuntu:24.04`, by digest (see *Build*).

`r1` was verified on 2026-10-01 on a DGX Spark: `/ragflow/VERSION` reports `v0.27.2`,
`/opt/chrome` is absent, and with RAGFlow's own compose the web UI answers
HTTP 200 and the superuser can sign in.

## Requirements

- A DGX Spark (native arm64 — no emulation)
- Docker ≥ 24 with BuildKit
- ~50 GB free disk, 16 GB RAM; the build takes 30–60 minutes

## Build

```bash
cd ragspark
sudo nohup build/ragflow/build.sh > ~/ragspark-build.log 2>&1 &
tail -f ~/ragspark-build.log
```

Run it in the background: a foreground build is cancelled if the SSH session
drops. Leaving `tail -f` with Ctrl+C does not stop the build.

The script:

1. clones RAGFlow at tag `v0.27.2` and **verifies the commit**
   (`a024bea0…`) — if the tag ever moves, it stops;
2. resets the patched files to upstream and applies the RAGSpark patches;
   [patch_base_images.py](../build/ragflow/patch_base_images.py) also pins the
   two images the Dockerfile builds from — `ubuntu:24.04` and
   `infiniflow/ragflow_deps:latest`, floating tags upstream — to the digests set
   in `build.sh` (those of `r4` and `r5`). This changes no code in the image;
   it makes a rebuild take the same base and data files;
3. builds `ghcr.io/0ffch41n/ragspark-ragflow:0.27.2-arm64-r7` with OCI labels
   (source, version, upstream commit, license);
4. with `--push`, pushes the image to GHCR.

## Differences from upstream

Eight, each applied by a script that stops the build if the code it expects is
not there (for example after a RAGFlow version change):

1. [patch_dockerfile.py](../build/ragflow/patch_dockerfile.py) — the Chrome and
   ChromeDriver steps run on x86_64 only. The archives in `ragflow_deps` are
   x86 builds that cannot run on arm64. On x86_64 the original commands run
   unchanged. **Consequence:** RAGFlow features that drive a browser (for
   example web crawling in agents) are not available on arm64.
2. [patch_search.py](../build/ragflow/patch_search.py) — the reranker receives
   the original chunk text instead of `remove_redundant_spaces(" ".join(tokens))`,
   which glues Cyrillic and other non-Latin text into one word and cuts rerank
   scores 2–3x. Details and measurements: [VALIDATION.md](VALIDATION.md), §4.
3. [patch_knn_filter.py](../build/ragflow/patch_knn_filter.py) — vector search
   no longer requires a word match: the full-text query stops being the kNN
   filter, through RAGFlow's own (commented-out) function for that. A question
   worded unlike the document found nothing before. §8.
4. [patch_agentic.py](../build/ragflow/patch_agentic.py) — twelve fixes to the
   agentic chat pipeline (reasoning levels Low … Ultra): finite helper calls,
   the stale "nothing found" flag, the reranker in agentic search and what it
   scores, answers only through `rag` in document chats, no internal notes in
   answers, no keyword narrowing for Cyrillic, a first `rag` call that is not
   skipped, a research budget that fits this hardware, search queries in the
   question's language, and a review that survives malformed claims. D19 in
   [DECISIONS.md](DECISIONS.md), §8.
5. [patch_web_default.py](../build/ragflow/patch_web_default.py) — the chat
   input starts at the "Naive" level instead of "Low".
6. [patch_scans.py](../build/ragflow/patch_scans.py) — scanned and Russian
   PDFs: DeepDOC counts Cyrillic as text before it discards a page's text
   layer; vision calls run at temperature 0 (`RAGSPARK_VISION_TEMPERATURE`)
   instead of the chat's sampling; and the chunker keeps every markdown table
   whole, with its title. D14 in [DECISIONS.md](DECISIONS.md), §10.
7. [patch_health.py](../build/ragflow/patch_health.py) — the health check
   returns its database connection to the pool. Upstream's probe kept it
   checked out until RAGFlow's own `close_stale(age=30)` closed it under the
   worker, so under Docker's 15 s health check every third check failed once
   and logged `Database connection issue (attempt 1/5): (0, '')`, about 1900
   times a day. §11.
8. [patch_docx.py](../build/ragflow/patch_docx.py) — DOCX chunks keep a
   newline between the pieces they join. `_merge_cks` joined the paragraphs
   with nothing after `_build_cks` had dropped the delimiter, so every
   paragraph boundary inside a chunk glued two words together. Chunk borders
   do not move. §13.

## Expected warnings

| Message | Meaning |
|---|---|
| `InvalidBaseImagePlatform: ... ragflow_deps ... linux/amd64` | `ragflow_deps` is an x86-only image, but the build only copies data files from it. Harmless. |
| `SecretsUsedInArgOrEnv ... GITEE_TOKEN` | Lint note on the upstream Dockerfile; the variable is unused. |
| `can't import package 'torch'` (at runtime) | The slim edition has no bundled models; vLLM serves them. |

## Third-party software inside the image

The upstream Dockerfile also installs, among other packages, the Microsoft
ODBC Driver for SQL Server (`msodbcsql18` on arm64, `msodbcsql17` on x86) and
accepts its EULA during the build; official RAGFlow images ship the x86
equivalent. Its redistribution terms are Microsoft's — see the open questions
in [DECISIONS.md](DECISIONS.md).

## Network notes

From some networks Docker Hub answers very slowly: on 2026-10-01 the TLS
handshake took 15–20 s, while Docker gives up after 10 s
(`TLS handshake timeout`). GitHub, PyPI, GHCR and `mirror.gcr.io` answered in
under 0.2 s at the same time. If you see this, configure a registry mirror in
`/etc/docker/daemon.json`:

```json
{ "registry-mirrors": ["https://mirror.gcr.io"] }
```

then `sudo systemctl restart docker`.

## Publishing to GHCR

1. Create a **classic** personal access token with the `write:packages` scope.
2. `sudo docker login ghcr.io -u <github-user>` and paste the token.
3. `sudo build/ragflow/build.sh --push`, or `sudo docker push <image>` for an
   existing build.
4. `sudo docker logout ghcr.io` — the credential is stored unencrypted.
5. On GitHub, open the package settings and set the visibility to public. The
   `org.opencontainers.image.source` label links the package to this
   repository.

Releases reference the image by digest, not by tag.
