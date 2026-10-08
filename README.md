# RAGSpark

**A private RAG appliance for NVIDIA DGX Spark, built on RAGFlow.**

[Русская версия](README.ru.md)

> **Status: pre-alpha.** The project is being built in public. Nothing here is
> installable yet — see the roadmap below.

## What it is

RAGSpark turns a DGX Spark into a self-contained document question-answering
system: you upload your documents, ask questions, and get answers with
citations — with every model running locally and no data leaving the machine.

- **RAGFlow** for document parsing, chunking, retrieval and the web interface
- **vLLM** serving the LLM, the embedding model and the reranker on the GB10 GPU
- **One command** to install, back up, restore and remove
- **A model catalog**: pick a model that fits your hardware; each entry is
  marked *validated* or *experimental*
- **Two modes**: `single` (one DGX Spark) and `stack` (two DGX Spark units
  linked over QSFP, for larger models — planned)

## Requirements

| | |
|---|---|
| Hardware | NVIDIA DGX Spark (GB10, arm64, 128 GB unified memory) |
| OS | DGX OS 7.x (Ubuntu 24.04 arm64) |
| Driver | NVIDIA 580.x (pinned — newer branches regress on GB10) |
| Disk | ~150 GB free for images and model weights |

## Components

| Component | Version | Notes |
|---|---|---|
| RAGFlow | 0.27.2 | built for arm64 by this project, with two documented fixes — no official arm64 images exist ([how](docs/BUILD.md)) |
| Elasticsearch | 8.11.3 | the version RAGFlow pins |
| MySQL | 8.0 | RAGFlow metadata |
| Valkey | 8 | RAGFlow task queue |
| Object storage (pgsty/silo) | pinned by RAGFlow | maintained MinIO fork; official MinIO images were withdrawn |
| vLLM | pinned per model | official images from Docker Hub, pinned by digest |

Default models: Qwen 3.8 27B NVFP4 with multi-token prediction (LLM),
deepvk/USER-bge-m3 (embeddings), BAAI/bge-reranker-v2-m3 (reranker); all three
on the official vLLM 0.27.1 image. Gemma 4 26B-A4B is the faster alternative.
See [catalog/models.yaml](catalog/models.yaml).

**Russian documents:** RAGFlow's defaults do not work well for Russian, and
RAGSpark changes them — PDFs are read from their text layer, and retrieval
relies on the reranker. The measurements are in
[docs/VALIDATION.md](docs/VALIDATION.md). Scanned PDFs are not supported yet.

## Roadmap

- [x] **Stage 1** — decisions and compatibility review ([docs/DECISIONS.md](docs/DECISIONS.md))
- [x] **Stage 2** — RAGFlow 0.27.2 arm64 image ([docs/BUILD.md](docs/BUILD.md))
- [ ] **Stage 3** — `single` mode: install, automatic model setup, built-in acceptance test
  - [x] 3.1 — end-to-end validation on a test stack ([docs/VALIDATION.md](docs/VALIDATION.md), [tools](docs/TOOLS.md))
  - [ ] 3.2 — RAGSpark compose; 3.3 — installer; 3.4 — clean install
- [ ] **Stage 4** — model catalog beyond the defaults
- [ ] **Stage 5** — `stack` mode on two DGX Spark units
- [ ] **Stage 6** — backups, optional monitoring, offline bundle

## Design principles

1. **The installer does not modify the host** beyond what it declares — no DNS
   rewiring, no system packages it cannot cleanly remove.
2. **Uninstall mirrors install**: everything created is recorded in a manifest
   and removed from it.
3. **Only the web entry point faces the network**; every other port binds to
   `127.0.0.1`.
4. **An install is not finished until RAG answers**: the last step uploads a
   document, asks a question and checks for a cited answer.
5. **Registries are unreliable**: an offline bundle and an image mirror are
   first-class features.

## License

[Apache License 2.0](LICENSE). Third-party components run as separate
containers under their own licenses; the RAGFlow image is built from unmodified
upstream source with one documented build-recipe change.

RAGSpark is an independent project, not affiliated with or endorsed by NVIDIA
or InfiniFlow. NVIDIA, DGX and DGX Spark are trademarks of NVIDIA Corporation;
RAGFlow is a project of InfiniFlow.

## Acknowledgements

The design draws on operational lessons from running the AGmind stack on
DGX Spark. RAGSpark contains no AGmind code.
