# Building the RAGFlow arm64 image

[Русская версия](BUILD.ru.md)

RAGFlow publishes x86 images only, so RAGSpark builds its own arm64 image
from the upstream source. This page documents how, and what differs from
upstream.

## Published images

| Tag | Digest | Patches | Status |
|---|---|---|---|
| `0.27.2-arm64-r2` | `sha256:1c8e60a0841b333c700488cb029d3664807249da0c071e862191b00fe34b228c` | Chrome + reranker input | current |
| `0.27.2-arm64-r1` | `sha256:1bf5fc0031bff2d775dc8bc1626b2820f9b658b43696a9bb4bdc02a8304f0660` | Chrome | superseded |

Current image:

```bash
docker pull ghcr.io/0ffch41n/ragspark-ragflow:0.27.2-arm64-r2@sha256:1c8e60a0841b333c700488cb029d3664807249da0c071e862191b00fe34b228c
```

`r2` was verified on 2026-10-08 on a DGX Spark: the patch marker is present in
`/ragflow/rag/nlp/search.py`, the model check passed on the test stack, and the
retrieval matrix matched the hot-patched results
([VALIDATION.md](VALIDATION.md), §4). Build inputs:

- `ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55`
- `infiniflow/ragflow_deps:latest@sha256:e69762c256ea2338a786a9b2f003da8e91e5e69b26bb6797c2206276ce32e5ff` — upstream refers to this image by a
  floating tag, so a later rebuild may take different data files (see the open
  questions in [DECISIONS.md](DECISIONS.md)).

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
3. builds `ghcr.io/0ffch41n/ragspark-ragflow:0.27.2-arm64-r2` with OCI labels
   (source, version, upstream commit, license);
4. with `--push`, pushes the image to GHCR.

## Differences from upstream

Two, each applied by a script that stops the build if the code it expects is
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
