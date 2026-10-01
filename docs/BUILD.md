# Building the RAGFlow arm64 image

[Русская версия](BUILD.ru.md)

RAGFlow publishes x86 images only, so RAGSpark builds its own arm64 image
from the upstream source. This page documents how, and what differs from
upstream.

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
2. resets the Dockerfile to upstream and applies the RAGSpark patch;
3. builds `ghcr.io/0ffch41n/ragspark-ragflow:0.27.2-arm64-r1` with OCI labels
   (source, version, upstream commit, license);
4. with `--push`, pushes the image to GHCR.

## Differences from upstream

There is exactly one, applied by
[patch_dockerfile.py](../build/ragflow/patch_dockerfile.py): the Chrome and
ChromeDriver steps run on x86_64 only. The archives in `ragflow_deps` are
x86 builds that cannot run on arm64. On x86_64 the original commands run
unchanged.

**Consequence:** RAGFlow features that drive a browser (for example web
crawling in agents) are not available on arm64. Document parsing, retrieval
and chat are not affected.

## Expected warnings

| Message | Meaning |
|---|---|
| `InvalidBaseImagePlatform: ... ragflow_deps ... linux/amd64` | `ragflow_deps` is an x86-only image, but the build only copies data files from it. Harmless. |
| `SecretsUsedInArgOrEnv ... GITEE_TOKEN` | Lint note on the upstream Dockerfile; the variable is unused. |
| `can't import package 'torch'` (at runtime) | The slim edition has no bundled models; vLLM serves them. |

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
