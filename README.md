# RAGSpark

**A private RAG appliance for NVIDIA DGX Spark, built on RAGFlow.**

[Русская версия](README.ru.md)

> **Status: pre-alpha.** The project is being built in public. The installer
> works on DGX Spark (stages 3–5), but expect changes, and do not use it on a
> machine whose data you need. See the roadmap below.

## What it is

RAGSpark turns a DGX Spark into a self-contained document question-answering
system: you upload your documents, ask questions, and get answers with
citations — with every model running locally and no data leaving the machine.

- **RAGFlow** for document parsing, chunking, retrieval and the web interface
- **vLLM** serving the LLM, the embedding model and the reranker on the GB10 GPU
- **One command** to install, change the LLM and remove (backups: stage 6)
- **A model catalog**: pick a model that fits your hardware; each entry is
  marked *validated* or *experimental*
- **Two modes**: `single` (one DGX Spark) and `stack` (two DGX Spark units
  linked over QSFP, for a model neither holds alone)

## Requirements

| | |
|---|---|
| Hardware | NVIDIA DGX Spark (GB10, arm64, 128 GB unified memory) |
| OS | DGX OS 7.x (Ubuntu 24.04 arm64) |
| Driver | NVIDIA 580.x (pinned — newer branches regress on GB10) |
| Disk | ~150 GB free for images and model weights |
| Stack mode | a second DGX Spark, a QSFP cable, ~190 GB more on each unit |

## Install

```bash
git clone https://github.com/0ffch41n/ragspark.git
cd ragspark
sudo ./install.sh
```

The installer checks the machine, downloads the images and model weights,
starts the stack and finishes with an acceptance test. Then
`sudo ragspark status | start | stop | logs | uninstall`. Guide:
[docs/INSTALL.md](docs/INSTALL.md); two units: its section *Stack mode*.

## Components

| Component | Version | Notes |
|---|---|---|
| RAGFlow | 0.27.2 | built for arm64 by this project, with documented fixes — no official arm64 images exist ([how](docs/BUILD.md)) |
| Elasticsearch | 8.11.3 | the version RAGFlow pins |
| MySQL | 8.0 | RAGFlow metadata |
| Valkey | 8 | RAGFlow task queue |
| Object storage (pgsty/silo) | pinned by RAGFlow | maintained MinIO fork; official MinIO images were withdrawn |
| vLLM | pinned per model | official images from Docker Hub, pinned by digest; DeepSeek V4 in stack mode on a community build ([D20](docs/DECISIONS.md)) |

Default models: Qwen 3.8 27B NVFP4 with multi-token prediction (LLM),
deepvk/USER-bge-m3 (embeddings), BAAI/bge-reranker-v2-m3 (reranker); all three
on the official vLLM 0.27.1 image. Qwen 3.6 35B-A3B is the fast alternative:
about 4 times the speed, the same answers at the chat's first levels (Naive,
Low), weaker on two-part answers above them. Choose it when installing or later
with `sudo ragspark llm`. See [catalog/models.yaml](catalog/models.yaml) and
[docs/VALIDATION.md](docs/VALIDATION.md), section 14.

On two DGX Spark units, `stack` mode runs DeepSeek V4 Flash (284B, MoE)
across both: about twice the default's speed, the same answers on the
question sets, scans read as well; RAGSpark's own pair runs it in production
([docs/VALIDATION.md](docs/VALIDATION.md), sections 17–18).

**Russian documents:** RAGFlow's defaults do not work well for Russian, and
RAGSpark changes them — PDFs are read from their text layer, and retrieval
relies on the reranker. RAGFlow's agentic chat modes (Low … Ultra) failed on
Russian documents and are fixed in RAGSpark's image; the chat starts in the
fast classic mode. The measurements are in
[docs/VALIDATION.md](docs/VALIDATION.md). Scanned PDFs are read through the
LLM's own vision: about 20 s a page with the default LLM, about 7 with Qwen 3.6.

## Roadmap

- [x] **Stage 1** — decisions and compatibility review ([docs/DECISIONS.md](docs/DECISIONS.md))
- [x] **Stage 2** — RAGFlow 0.27.2 arm64 image ([docs/BUILD.md](docs/BUILD.md))
- [x] **Stage 3** — `single` mode: install, automatic model setup, built-in acceptance test
  - [x] 3.1 — end-to-end validation on a test stack ([docs/VALIDATION.md](docs/VALIDATION.md), [tools](docs/TOOLS.md))
  - [x] 3.2 — RAGSpark compose ([docs/COMPOSE.md](docs/COMPOSE.md))
  - [x] 3.3 — installer ([docs/INSTALL.md](docs/INSTALL.md))
  - [x] 3.4 — clean install ([docs/VALIDATION.md](docs/VALIDATION.md), §9)
- [x] **Stage 4** — model catalog beyond the defaults
  - [x] 4.1 — a second LLM, Qwen 3.6 35B-A3B ([docs/VALIDATION.md](docs/VALIDATION.md), §14)
  - [x] 4.2 — the installer's model menu ([docs/INSTALL.md](docs/INSTALL.md))
  - [x] 4.3 — changing the LLM of a running installation ([docs/COMPOSE.md](docs/COMPOSE.md), *Changing the LLM*)
  - [x] 4.4 — clean install with the second LLM ([docs/VALIDATION.md](docs/VALIDATION.md), §15)
- [x] **Stage 5** — `stack` mode on two DGX Spark units
  - [x] 5.1 — the model and a trial window ([docs/VALIDATION.md](docs/VALIDATION.md), §17)
  - [x] 5.2 — compose files, relays and the pair watch ([docs/COMPOSE.md](docs/COMPOSE.md), *Stack mode*)
  - [x] 5.3 — the installer's node role and switching modes ([docs/INSTALL.md](docs/INSTALL.md), *Stack mode*)
  - [x] 5.4 — production on two units, a crash and a reboot ([docs/VALIDATION.md](docs/VALIDATION.md), §18)
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
containers under their own licenses; the RAGFlow image is built from the
upstream source with RAGSpark's documented patches
([docs/BUILD.md](docs/BUILD.md)).

RAGSpark is an independent project, not affiliated with or endorsed by NVIDIA
or InfiniFlow. NVIDIA, DGX and DGX Spark are trademarks of NVIDIA Corporation;
RAGFlow is a project of InfiniFlow.

## Acknowledgements

The design draws on operational lessons from running the AGmind stack on
DGX Spark. RAGSpark contains no AGmind code.
