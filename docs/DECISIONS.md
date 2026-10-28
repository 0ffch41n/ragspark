# RAGSpark — Decision record

[Русская версия](DECISIONS.ru.md)

Stage 1 output. Every decision states **what**, **why**, **how** and the
**risks**. Facts were verified against upstream sources on **2026-10-01**;
re-check them before any version bump.

---

## D1. Name — RAGSpark

- **What:** project, repository, CLI, install directory and container prefix:
  `ragspark`, `/opt/ragspark`, `ragspark-<service>`.
- **Why:** descriptive and free of third-party marks. "DGX" is an NVIDIA
  trademark and "DGX Station" is an NVIDIA product, so neither is used in the
  name. NVIDIA products are referenced descriptively only ("for NVIDIA DGX Spark").
- **How:** the name is defined once and derived everywhere else.

## D2. License — Apache-2.0, original code only

- **What:** `LICENSE` (Apache-2.0) at the root.
- **Why:** permissive, includes a patent grant, compatible with RAGFlow
  (Apache-2.0).
- **How:** all code is written from scratch. No files or fragments are copied
  from other projects, so no third-party notices are inherited. Third-party
  software (RAGFlow, Elasticsearch, MySQL, Valkey, MinIO fork, vLLM) runs as
  separate containers under its own licenses. RAGFlow's image is built from
  its upstream source with RAGSpark's patches, each listed in D3 and
  [BUILD.md](BUILD.md); changes to RAGFlow's code are marked with `ragspark:`
  comments that name the patch script.
- **Risk:** copying any third-party file later would require keeping its
  copyright notices — avoid, or record it here.

## D3. Core engine — RAGFlow 0.27.2, self-built for arm64

- **What:** RAGFlow server: parsing (DeepDoc), chunking, retrieval, chat with
  citations, web UI.
- **Why this version:** 0.27.x is the last Python-based RAGFlow line; the next
  major version (1.0) is a Go rewrite. 1.0.0-rc1 shipped on 2026-09-29, and its
  Go image is not a supported native build target on Linux arm64 (it depends on
  native libraries published for x86-64 only). The 0.27.x code is maintained on
  a separate `0.27.x` branch for fixes.
- **Why self-built:** RAGFlow publishes x86 images only; arm64 users are
  expected to build their own. The upstream build guide asks for xgboost 1.6.0
  and unixODBC on arm64.
- **How:** build from the `v0.27.2` tag on a DGX Spark (native arm64, no
  emulation), slim edition (no bundled embedding models — vLLM provides them),
  publish to GHCR as `ghcr.io/0ffch41n/ragspark-ragflow`, reference by digest.
  The images the upstream Dockerfile builds from by floating tags
  (`ubuntu:24.04`, `infiniflow/ragflow_deps:latest`) are pinned by digest, so a
  rebuild takes the same base and data files. Recipe: [BUILD.md](BUILD.md).
- **Published:** `0.27.2-arm64-r6` (2026-10-21),
  `sha256:16ba7dd9d03ae8d6a8d9f819e6c7e62d78874dfeb27367bbec536da02ea40fe9`,
  the first image with all patches below. It replaces `0.27.2-arm64-r5`
  (2026-10-20,
  `sha256:a34a9f1f1009442497f50da91691edbc1dc19dea4cea7a73c9673a0000f61375`,
  patches 1–6), `0.27.2-arm64-r4` (2026-10-19,
  `sha256:1ed7725f5932a4799591104b5efd1d1ed7f50984e070be24ed4251d27341e0f8`,
  patches 1–5), `0.27.2-arm64-r3` (2026-10-14,
  `sha256:8d08d824a9b248fceba8a0c5885aaf3cef7f24c7137ec87154aef53031c22e5a`,
  built before the last three edits of patch 4 and missing two of its fixes —
  see [BUILD.md](BUILD.md)), `0.27.2-arm64-r2` (2026-10-08,
  `sha256:4232ddbb513cff48c51864f081f6b82d37371927dea70bb01a200c6994f70dcc`,
  patches 1–2) and `0.27.2-arm64-r1` (2026-10-01,
  `sha256:1bf5fc0031bff2d775dc8bc1626b2820f9b658b43696a9bb4bdc02a8304f0660`,
  patch 1).
- **Deviations from upstream** — seven, each applied by a script that refuses
  to run if the code it expects is not there:
  1. The Chrome/ChromeDriver steps run on x86_64 only: the archives in
     `ragflow_deps` are x86 builds that cannot run on arm64. Browser-driven
     features (e.g. web crawling in agents) are unavailable on arm64.
  2. The reranker receives the original chunk text (`r2`). Upstream builds the
     cross-encoder input with `remove_redundant_spaces()`, which deletes every
     space after a non-Latin letter, so Russian chunks reach the reranker as one
     long word and rerank scores drop 2–3x. See D15 and
     [VALIDATION.md](VALIDATION.md); to be reported upstream.
  3. Vector search no longer requires a word match (`r3`). Upstream passes
     the full-text query to kNN as its filter, so only chunks sharing words
     with the question are ranked; RAGFlow does not reduce Russian words to a
     common form, and a question worded differently from the document finds
     nothing. RAGFlow's own function that drops that filter is commented out;
     the patch calls it. See D19.
  4. Twelve fixes to the agentic chat pipeline (`r3`, `r4`), D19.
  5. The chat input starts at the "Naive" reasoning level instead of "Low"
     (`r3`), D19.
  6. Three fixes that let scanned and Russian PDFs be read (`r5`), D14: DeepDOC
     keeps a Russian page's text layer, vision calls run at temperature 0, and
     the chunker never splits a markdown table.
  7. The health check returns its database connection to the pool (`r6`).
     Upstream's probe kept a pooled connection until RAGFlow's own
     `close_stale(age=30)` closed it under the worker thread, so every third
     check under Docker's 15 s health check logged a reconnect. See
     [VALIDATION.md](VALIDATION.md), section 11.
- **Verified (2026-10-01):** the unmodified upstream build completed on a DGX
  Spark; with RAGFlow's own compose, the server reported `v0.27.2`, served the
  web UI, and Elasticsearch, MySQL, Valkey and the object store were healthy.
- **Risks:**
  - 0.27.x is a dead-end line long-term. The RAGFlow version is therefore a
    swappable component; moving to 1.0 is a separate decision once arm64 is
    supported.
  - The 0.27.2 → 1.0 data migration is irreversible: a verified backup is
    mandatory before it.
  - 1.0 changes the stack (Valkey → Apache Kvrocks, ClickHouse added).

## D4. Document engine — Elasticsearch 8.11.3

- **What:** storage for chunks, BM25 and dense vectors.
- **Why:** the version RAGFlow 0.27.x pins; Infinity is not officially
  supported on Linux arm64.
- **How:** official arm64 image; explicit memory limit (memory is shared with
  the GPU on GB10); backups through the snapshot API, not a tar of a live
  data directory.

## D5. Object storage — pgsty/silo

- **What:** storage for the original uploaded files.
- **Why:** MinIO stopped publishing images; the Docker Hub repositories were
  removed in September 2026 and anonymous Quay pulls were denied when tested
  from the deployment network (2026-09-28). RAGFlow 0.27.2 itself pins
  `pgsty/silo:RELEASE.2026-08-06T00-00-00Z`, pgsty's maintained MinIO fork.
- **How:** use the version RAGFlow pins; arm64 availability confirmed.
- **Plan B:** any other S3-compatible storage RAGFlow supports.

## D6. Inference — vLLM, official images, pinned per model

- **What:** OpenAI-compatible servers for the LLM, the embedding model and the
  reranker.
- **Why:** NGC (`nvcr.io`) answered HTTP 403 to every request from the
  deployment network, including the token endpoint. Official `vllm/vllm-openai`
  images from Docker Hub are the upstream path for DGX Spark, with the
  Spark-specific part in the model recipe, image tag and flags.
- **How:**
  - each catalog entry pins its own image by digest — newest models sometimes
    need model-specific images, and some FP4 kernels have gaps on sm_121;
  - the weights are pinned too: every `vllm serve` carries `--revision` with
    the commit of the Hugging Face repository that was validated, and the
    installer downloads and checks exactly that snapshot, so a repository that
    changes under the same name does not change an install;
  - pooling models (embeddings, reranker) run with a neutral entrypoint
    (`/usr/bin/env`), because the image entrypoint is `vllm serve`;
  - with Docker's `systemd` cgroup driver, `systemctl daemon-reload` can revoke
    GPU access from running containers (a known NVIDIA Container Toolkit
    issue). The installer does not change Docker's settings:
    `ragspark check --gpu-reload` tests the machine, and
    [INSTALL.md](INSTALL.md) describes the switch to `cgroupfs` (D18). On
    DGX OS 7.5 with Docker 29.6.2 the test passed with the `systemd` driver
    (2026-10-09), so it stays.

## D7. Embeddings and reranker

- **What:** `deepvk/USER-bge-m3` (Russian fine-tune of bge-m3, 8192 context)
  and `BAAI/bge-reranker-v2-m3`.
- **Why:** validated on DGX Spark; about 3 GB of memory together.
- **Reranker role:** it is the only component that separates "the answer is
  in the documents" from "it is not". Embedding similarity scores an unrelated
  question as high as a real answer (0.58 vs 0.56–0.62), so without the
  reranker the "not in the knowledge base" response would never trigger (D15).
- **Note:** the embedding model is fixed per knowledge base; changing it means
  re-indexing. The documentation must say so.

## D8. Model configuration — through the RAGFlow API

- **What:** after install, RAGFlow already has the LLM, embedding and rerank
  models configured and selected as defaults. No manual UI steps.
- **Why API:** the `user_default_llm` mechanism in `service_conf.yaml` is
  deprecated and no longer supported in open-source RAGFlow since the 0.26
  model-catalog refactor; each tenant configures its own model instances.
- **How:** the installer creates the administrator, logs in through the API and
  registers the models. The 0.27 model-provider interface was redesigned, so
  request formats are captured from a live 0.27.2 install before automation is
  written.
- **Administrator:** on first start the admin server creates the superuser
  with the address from `DEFAULT_SUPERUSER_EMAIL` (upstream default
  `admin@ragflow.io`; RAGSpark uses `admin@ragspark.local`) and the password
  from `ADMIN_DEFAULT_PASSWORD`. The installer sets a random value, so there is
  no race for the first sign-up. Verified: the superuser signs in to the
  regular web UI (2026-10-01), and the configured address works (2026-10-08).
- **Users:** model instances belong to a tenant, so models are configured once
  in the administrator's tenant and other people are invited through **Team**.
  Open sign-up is disabled with `REGISTER_ENABLED=0` and `ENABLE_REGISTER=0`
  (verified: the sign-up button disappears).
- **Verified (2026-10-06):** the request flow was captured from the web UI and
  automated in [tools/ragflow_models.py](../tools/ragflow_models.py); tested on a
  live system including re-creation of a deleted instance. The flow is listed
  in [VALIDATION.md](VALIDATION.md).
- **Defaults are stored by model id.** Deleting and re-creating an instance
  leaves the default pointing to a deleted model; the UI shows a bare id with a
  warning sign. The script compares ids, not names, and re-assigns.
- **The default reranker is not applied everywhere:** retrieval testing and new
  chats start without one. The installer sets it explicitly on the chats it
  creates (D15).

## D9. Model catalog

- **What:** [catalog/models.yaml](../catalog/models.yaml) — one entry per model:
  repository, memory share, context presets, tool calling, supported modes,
  measurements, license, `validated` flag. A runnable entry points to its
  compose file, which holds the image, its digest and the vLLM flags (D17).
- **How:** the wizard offers only models that fit the detected hardware and mode
  and shows the expected context and speed. Model weights live in a host
  directory outside Docker volumes, so reinstalls do not re-download them.
- **Rule:** `validated: true` only after the acceptance test passes on real
  hardware. Each model's license is shown before download.

## D10. Stack mode — stage 5

- **What:** two DGX Spark units linked by a direct QSFP cable.
- **Tier 1:** the second unit runs only the LLM, without tensor parallelism;
  RAGFlow, embeddings and reranker stay on the first.
- **Tier 2:** tensor parallelism across both units (Ray + NCCL) for MoE models in
  the 200–300B range; NVIDIA documents this topology in its vLLM multi-node
  playbook.
- **Why later:** networking, NCCL, Ray and multi-node serving on Docker Hub
  images (instead of NGC) each need separate validation.

## D11. Languages — English and Russian

- Wizard, installer messages, README and documentation exist in both languages;
  a test enforces that every message has both translations.

## D12. Engineering principles

1. The installer does not modify the host beyond what it declares.
2. Uninstall mirrors install, driven by a manifest of created resources.
3. Only the web entry point faces the network; other ports bind to `127.0.0.1`.
4. An install is complete only when a RAG query returns a cited answer.
5. Registries are unreliable: offline bundle and image mirror are first-class.

## D13. Registry access

- **What:** how images are fetched.
- **Why:** on 2026-10-01 Docker Hub completed the TLS handshake in 15–20 s from
  the deployment network, while Docker gives up after 10 s. GitHub, PyPI, GHCR
  and `mirror.gcr.io` answered in under 0.2 s at the same time. IPv6 to Docker
  Hub failed instantly and was not the cause.
- **How:**
  - the preflight check measures registry latency, separately over IPv4 and
    IPv6, not just reachability;
  - a registry mirror (`mirror.gcr.io`) is an optional, declared change to
    `/etc/docker/daemon.json`, recorded in the install manifest and reverted on
    uninstall;
  - RAGSpark's own images live on GHCR.

## D14. Document parsing — the PDF text layer, vision for scans

- **What:** how PDFs become text before chunking.
- **Decision:** PDF parser **Naive** (the PDF's own text layer, labelled
  "Plain Text" in the code) for PDFs that have one. A **scanned** PDF has no
  text layer; its parser is the LLM's own vision — the running Qwen 3.8 is
  registered a second time as the vision model `ragspark-vision` (`r5`).
  DeepDOC is not used for Russian documents.
- **Why:** DeepDOC's recogniser cannot output Russian: its dictionary
  (`rag/res/deepdoc/ocr.res`) has 6623 entries, 6277 of them CJK and one of the
  33 lowercase Russian letters, so Cyrillic comes out as Latin look-alikes
  (`Pa3beM` for «Разъем»). DeepDOC also discards the text layer of a whole page
  when more than 40% of its characters are ASCII punctuation from embedded
  subset fonts — table-of-contents dot leaders trigger it — and then
  recognises the page. On a 39-page Russian manual:

  | Parser | Cyrillic share | Garbled words | Questions found |
  |---|---|---|---|
  | DeepDOC | 0.665 | 194 | 4 of 8 |
  | Naive | **0.835** (= text layer) | **0** | 5 of 8 → 8 of 8 with D15 |

- **Scans:** the same manual turned into a scan (every page an image, no text
  layer), measured with [tools/rag_eval.py](../tools/rag_eval.py):

  | Parser | Cyrillic share | Garbled words | Questions found | No-answer rejected |
  |---|---|---|---|---|
  | Naive | 0.000 (no text layer) | 0 | 0 of 8 | — |
  | DeepDOC | 0.000 | 1066 | 0 of 8 | — |
  | vision, `r5` | **0.818** | **2** | **8 of 8** | **2 of 2** |

  Vision needed three fixes in RAGFlow, all in `patch_scans.py` (D3, deviation
  6): DeepDOC counts Cyrillic as text when it judges a page garbled; vision
  calls run at temperature 0 — at the chat's 0.7 the same page gave a different
  transcription every time and sometimes replaced its tables with image
  placeholders; and the chunker keeps a markdown table and its title in one
  chunk — it used to cut tables at the token budget, and rows without their
  header could not be found. Details: [VALIDATION.md](VALIDATION.md), section 10.
- **Trade-offs:** Naive flattens tables into lines (still readable, retrieval
  works) and cannot read scans. Vision reads about 17 s a page (the manual took
  11 min against 6 s for its text layer) and shares the LLM with chat while it
  indexes; cells made of symbols only (● matrices) carry small errors, and
  screenshots inside a page may become image placeholders.
- **Open:** measured on a scan made from a digital PDF (150 dpi, greyscale,
  JPEG); a real scan with skew and paper noise is still to be measured.

## D15. Retrieval defaults — reranker, vector weight 0.7, threshold 0.1

- **Decision:** reranker `bge-reranker-v2-m3` on, vector similarity weight
  **0.7**, similarity threshold **0.1**, rerank candidates 64, top N 8, with
  patch 2 of D3.
- **Why:** final score = term weight × keyword match + vector weight × rerank
  score. Keyword matching is weak for Russian (inflections, and 60% of it comes
  from exact pairs of adjacent words), so the reranker must dominate. On 8
  questions with answers and 2 without, after patch 2:

  | Setup | Answers found | No-answer rejected |
  |---|---|---|
  | RAGFlow defaults (0.3 / 0.2), reranker | 6 of 8 | 1 of 2 |
  | reranker, 0.7 / 0.2 | 7 of 8 | 2 of 2 |
  | **reranker, 0.7 / 0.1** | **8 of 8** | **2 of 2** |
  | no reranker, 0.7 / 0.2 or 0.5 | 8 of 8 | 0 of 2 |

- **How:** the installer applies these to the chats it creates. Settings in
  the UI's retrieval testing page are not saved, so users creating their own
  chats are told to set them.
- **Risk:** measured on one document and 10 questions; the margin below 0.1 is
  not measured yet. The acceptance test re-checks every install.
- **Since `r3`** vector search also finds chunks that share no word with the
  question (D3 patch 3, D19). The same matrix on the same manual was
  unchanged cell for cell in its totals: the reranker and the 0.1 threshold
  still reject both no-answer questions.

## D16. Default LLM — Qwen 3.8 27B NVFP4 with MTP

- **Decision:** `unsloth/Qwen3.8-27B-NVFP4` on the official vLLM 0.27.1
  image, multi-token prediction with 3 speculative tokens, thinking disabled
  and sampling (temperature 0.7, top-p 0.8, top-k 20) and an 8192-token
  answer limit set on the server (D19). Flags are in [compose/llm/qwen-3.8-27b-nvfp4.yaml](../compose/llm/qwen-3.8-27b-nvfp4.yaml).
- **Why:** strong open model in its size class, Apache-2.0, native tool
  calling, decode speed nearly flat on long contexts. In Russian probes
  (30 answers) it never mixed in other scripts and cited sources correctly.
- **Why this checkpoint (since 2026-10-28):** until then the default was
  `Inferact/Qwen3.8-27B-NVFP4`, the same model quantised differently. A
  profile showed 62% of its decode step spent on weights it keeps in bf16: the
  largest linear-attention projections of all 48 linear layers and the 2.5 GB
  `lm_head`, which the MTP drafts read three times per step. unsloth's
  checkpoint stores those in FP8: 23.4 GB instead of 26.4, **29% more tokens
  per second** for one user on the production Spark (27.2 instead of 21.1),
  the same MTP acceptance (74%), and equal or better answers in every RAGFlow
  check ([VALIDATION.md](VALIDATION.md), section 12). The earlier note that
  Inferact's checkpoint had the higher MTP acceptance (0.897 vs 0.788) did not
  reproduce.
- **Measured:** 27.2 tok/s per user at a 1k-token context, 26.0 at 8k;
  8 concurrent requests ~137 tok/s aggregate. The first measurements, on
  Inferact's checkpoint, are in section 1 of VALIDATION (10.2 tok/s without
  MTP, 17.1 with it). Thinking disabled on the server because RAGFlow does not
  pass the switch: with thinking, the same answer took 1,116 tokens and 108 s
  instead of 101 tokens and 10 s.
- **Trade-off:** long prompts are read more slowly: the first token of an
  8k-token prompt comes after 6.4 s instead of 4.9 s. For answers of a few
  hundred tokens the faster decode still wins. Gemma 4 26B-A4B stays in the
  catalog as an alternative.

## D17. Runtime — RAGSpark's own compose

- **What:** [compose/](../compose/) replaces RAGFlow's compose: one project,
  `ragspark`, with RAGFlow (image `r3`), Elasticsearch, MySQL, object storage,
  Valkey and three vLLM services. The LLM lives in its own file under
  `compose/llm/`, selected with `COMPOSE_FILE` in `.env`. Guide:
  [COMPOSE.md](COMPOSE.md).
- **Why not RAGFlow's compose:** it publishes a port for every service
  (Elasticsearch, MySQL, object storage, Valkey, the API, admin and MCP
  servers), ships shared default passwords, fixes the time zone to
  Asia/Shanghai and knows nothing about the models.
- **How:**
  - one published port — RAGFlow's nginx, 80 by default; everything else only
    on the internal network (D12);
  - start order through health checks: the vLLM services one at a time
    (embeddings, reranker, LLM), the databases in parallel with them, RAGFlow
    last. vLLM sizes its cache from the memory it measures while starting; on
    unified memory, memory taken by another model starting in the same window
    is counted against it, so concurrent starts can leave a model without room
    for its cache;
  - restart policy `on-failure:5`: a crashed container restarts. The start at
    boot is a systemd unit that runs compose (D18). Docker itself does start
    some containers at boot — see D18 — and the unit puts them back in order;
  - random passwords per install (`init-env.sh`, `.env` with mode 600). The
    session-signing key is set explicitly (`RAGFLOW_SECRET_KEY`); otherwise
    RAGFlow generates one and keeps it in Valkey;
  - the superuser's address is configurable: RAGFlow's admin server reads
    `DEFAULT_SUPERUSER_EMAIL` when it creates the account
    (`admin/server/auth.py`); RAGSpark uses `admin@ragspark.local`;
  - host time zone for every container (`RAGSPARK_TZ`; variables that a shell
    may also define carry the `RAGSPARK_` prefix, because compose prefers the
    shell's value over `.env`);
  - vLLM starts offline (`HF_HUB_OFFLINE=1`): weights come from the host cache
    only, so a start does not depend on Hugging Face. Usage statistics are off
    (`VLLM_NO_USAGE_STATS`, `DO_NOT_TRACK`);
  - logs capped at 5 × 20 MB per container; every image pinned by digest
    ([tools/pin_images.py](../tools/pin_images.py)).
- **Differences from RAGFlow's compose** besides the above: the MySQL database
  is created with `MYSQL_DATABASE` instead of a mounted `init.sql`;
  `hostname: ragflow` keeps the task executor's id stable when the container is
  re-created; health checks are stricter (Elasticsearch cluster at least
  yellow; RAGFlow's `/api/v1/system/healthz` through nginx, which checks MySQL,
  Valkey, Elasticsearch and storage); no profiles, Go server, NATS or TEI.
- **Verified (2026-10-08):** from empty volumes the stack started in the
  intended order in 9 min 10 s (LLM 7 min 11 s; 332 s on restart with the
  compile cache); only port 80 listens; containers use the host time zone;
  the configured superuser address works; models registered by the script on
  the empty database; retrieval results identical to stage 3.1. The models
  started without freeing the file cache. Details:
  [VALIDATION.md](VALIDATION.md), section 6.

## D18. Installer — one Python tool, a manifest and a systemd unit

- **What:** `install.sh` runs [installer/](../installer/), a Python 3.10+
  program that uses the standard library only. The same tool, installed as
  `/usr/local/bin/ragspark`, has the commands `status`, `start`, `stop`,
  `restart`, `logs`, `acceptance`, `check` and `uninstall`. Guide:
  [INSTALL.md](INSTALL.md).
- **Why Python, not shell:** checks with structured results, a JSON manifest,
  messages in two languages and tests that run the whole installation with
  fake `docker` and `systemctl`. DGX OS ships Python 3.12; nothing is
  installed on the host.
- **How:**
  - checks run before any change. Problems that would damage existing data —
    a stack started by hand, volumes of an earlier stack, an `/opt/ragspark`
    or a unit file the installer did not create — stop the installation even
    with `--force`;
  - files go to `/opt/ragspark`; settings come from `init-env.sh` (D17);
  - everything created — directories, files, the unit, pulled images (by
    exact reference), downloaded model repositories — is recorded in
    `manifest.json`, and uninstall removes exactly that (D12). Images, weights
    and directories that existed before stay. If `docker compose down` fails,
    uninstall stops and keeps `.env`, the only copy of the passwords;
  - every step is idempotent; an interrupted installation resumes with the
    settings it was started with;
  - weights are checked offline and downloaded by a temporary container from
    the vLLM image (`huggingface_hub`), only the formats vLLM loads;
    `HF_TOKEN`, `HF_ENDPOINT` and proxy variables are passed on;
  - the unit is `Type=exec` with `RemainAfterExit`: it runs
    `docker compose up -d --wait` and does not hold up the boot. Before that
    it stops project containers that run without being healthy. Docker
    re-evaluates restart policies when it starts, and `on-failure` restarts
    every container whose last exit code was not 0 (moby
    `daemon/daemon.go`, `restartmanager`): one killed after the stop timeout
    (137), Elasticsearch (143), or all of them after a power loss (255).
    Started together, the models leave each other without memory;
  - systemd is reloaded before the models start; on a resumed installation
    the stack is stopped first. `/etc/docker/daemon.json` is not changed:
    `ragspark check --gpu-reload` runs a container with the GPU, reloads
    systemd and checks the GPU again, and the switch to `cgroupfs` is a
    documented manual step (D6);
  - the installation ends with an acceptance test (D12): a one-page Russian
    PDF (a fictional memo, generated reproducibly by
    `acceptance/make_sample.py`) is indexed with RAGSpark's settings (D14,
    D15), and the chat must answer two questions with the expected facts, a
    citation and no CJK characters, and give its "no answer" text to a
    question the document does not answer; the first question is asked
    again at reasoning level Low (D19). The knowledge base and the chat stay
    as an example. The test also creates, once, the chat "Ассистент"
    ("Assistant") without a knowledge base for general questions, and checks
    that it answers (with `--skip-acceptance` only this part runs);
  - messages in English and Russian (D11); the log has mode 600 and no
    passwords; health checks read passwords from the container environment,
    and Valkey's password is kept off its command line.
- **Verified (2026-10-09):** installation from scratch on the stage 3.2
  machine with images and weights present — stack in 9.4 min, acceptance test
  passed in 16 s; the GPU stays after `systemctl daemon-reload`; after a
  reboot Docker started two containers by itself, the unit stopped them and
  started the stack in order in about 7 min; `stop` / `start` in 6.2 min. The
  offline weights check was rewritten after it asked the network for files
  vLLM does not need. Details: [VALIDATION.md](VALIDATION.md), section 7.
- **Verified again (2026-10-20), stage 3.4:** an installation from zero on a
  wiped machine, out of a fresh clone of `main`, with the image pulled from
  GHCR by digest — stack in 9.4 min, acceptance test passed in 29 s, and the
  chat modes reproduced section 8's results at every level. Details:
  [VALIDATION.md](VALIDATION.md), section 9.

## D19. Chat modes — Naive by default, agentic levels made to work

- **What:** the reasoning selector under RAGFlow's chat input. Level 0,
  "Naive", is the classic path: retrieval with the D15 settings, one LLM call,
  or the chat's "no answer" text without one. Levels 1–4 (Low, Medium, High,
  Ultra) answer through RAGFlow's agentic pipeline: an outer model with a
  `rag` tool, then keyword extraction, search, an intermediate draft and a
  sufficiency review, more of each per level.
- **Found (stage 3.3):** with Russian documents and Qwen 3.8 27B none of the
  agentic levels worked. Low hung for ten minutes; Medium answered "no answer"
  to everything; a paraphrased question found nothing at any level.
- **Decision:** images `r3` and `r4` fix the causes (all in RAGFlow, none in
  the model), and the selector starts at **Naive**: the same answers on RAGSpark's
  question sets in 3 s instead of 15 s for Low and up to 1–2 min for Medium and
  High. The other levels stay in the selector for questions that need several
  searches.
- **Fixes:**
  - kNN without the full-text filter (D3 patch 3): a question worded unlike
    the document now finds it;
  - the pipeline's helper calls get `frequency_penalty 0.5` and a
    2048-token limit (`RAGSPARK_HELPER_FREQUENCY_PENALTY`,
    `RAGSPARK_HELPER_MAX_TOKENS`): at temperature 0.1 and with no limit, the
    keyword request looped in 9 of 30 runs until the 64k context was full;
  - Medium, High, Ultra: a flag set to "nothing found" at the start was never
    cleared, so the answer step discarded the evidence;
  - the agentic search uses the chat's reranker, scoring the question rather
    than the question plus generated keywords;
  - in a chat with knowledge bases the outer model answers only through the
    `rag` tool — it had answered "capital of Australia" itself, once
    "Sydney"; chats without a knowledge base keep the upstream prompt;
  - a note meant for the outer model ("[Research status] …") is removed from
    the answer;
  - vLLM's default answer limit is 8192 tokens (`max_new_tokens`), since
    RAGFlow strips `max_tokens` from every request;
  - passages are not narrowed by Cyrillic keywords: after each search the
    pipeline kept only the passages mentioning one of the question's keywords,
    stem-tolerant for ASCII words but verbatim for anything else, so Russian
    word forms dropped the passage that held the answer;
  - in a chat with knowledge bases the first round must call `rag`: on
    `tool_choice "auto"` the outer model skipped the tool in 3 of 9 Low
    questions and its own plain text came back as the answer;
  - the research budget is `RAGSPARK_AGENTIC_BUDGET_S`, 300 s by default
    instead of a hard-coded 180 s: at this model's ~17 tok/s High and Ultra
    spent the ceiling inside the first research round, and the routing guards
    then synthesised an answer from partial evidence. The per-call bounds are
    unchanged, so the larger budget buys more research rounds, not longer
    calls;
  - the planner and the upper levels write their search queries in the
    question's language: none of the three prompts that produce queries stated
    a language, so a Russian question was split into English sub-questions
    ("knowledge base backup schedule"), which match nothing in a Russian
    corpus — the one failure neither the reranker nor the kNN fix can repair;
  - the sufficiency review tolerates claim items that are not objects: it
    reads the review as a list of claim objects, the model sometimes answers
    `"claims": ["c0", …]`, and `item.get()` on a string raised
    `AttributeError` out of the graph — `_bounded` handles only
    `TimeoutError`, so Ultra ended in a traceback instead of an answer.
- **Verified (2026-10-12/13 and 2026-10-19):** each set of fixes was first
  hot-patched into the running container — `r2` for the first eight, `r3` for
  the last four — and measured with
  [tools/mode_eval.py](../tools/mode_eval.py) on the acceptance sample and the
  stage 3.1 manual. With all of them every level answers 7/7 and still rejects
  the question the documents do not answer; with the first eight alone Medium
  gave 6/7, High 5/7 and Ultra 6/7. The image that carries all twelve is
  `r4`. Details: [VALIDATION.md](VALIDATION.md), section 8.
- **Behaviour to know:** the agentic levels write their own answer whenever
  they find passages, so for a question the documents do not answer, Medium
  may reply "the documents have no information on …" in its own words instead
  of the chat's "no answer" text, or infer an answer from what it found.

## Open questions

- **Scanned documents.** Read through vision since `r5` (D14), but measured on a
  scan made from a digital PDF only; a real scan is still to be measured.
- **Glued lines.** When RAGFlow merges lines of a DOCX into a chunk it adds no
  separator (`GPUКритерий`, `скоростьsudo` — 11 places in a short document),
  which hurts keyword search.
- **Auto-question and keyword analysis.** Both use the LLM to improve matching
  (questions generated per chunk at indexing; keywords extracted per query).
  Measure the gain against the extra time.
- **Upstream reports.** RAGFlow defects found in stage 3 should be reported to
  the `0.27.x` branch: the reranker input (D3 patch 2), DeepDOC on Cyrillic
  (D14), the kNN filter (D3 patch 3), the agentic-pipeline defects of D19, and
  the health check's held connection (D3 patch 7) together with
  `close_stale(age=30)` itself, which closes any connection held longer than
  30 s, a long transaction's included.
- **Ultra.** The fourth level answers 7/7 on the acceptance sample (`r4`), but
  its own distinguishing feature is still untested: the knowledge-graph tool
  needs a graph built at indexing, and none of RAGSpark's question sets has
  one, so the level was measured without it.
- **ODBC driver licence.** The upstream Dockerfile installs Microsoft's ODBC
  driver and accepts its EULA. Review its redistribution terms for the
  published image, or drop the driver if SQL Server connectors are not needed.

---

## Sources (checked 2026-10-01)

- RAGFlow 0.27 announcement — last Python version, move to Go:
  https://ragflow.io/blog/ragflow-0.27-knowledge-compilation-and-agentic-retrieval
- RAGFlow release notes (1.0.0-rc1, 0.27.0): https://ragflow.io/docs/release_notes
- RAGFlow Go quickstart — arm64 not a supported native target: https://ragflow.io/docs/
- 0.27.x maintenance branch: https://github.com/infiniflow/ragflow/issues/20287
- Building RAGFlow images, arm64 notes: https://ragflow.io/docs/build_docker_image
- RAGFlow configuration (Elasticsearch 8.11.3): https://ragflow.io/docs/v0.27.2/configurations
- `user_default_llm` deprecation discussion: https://github.com/infiniflow/ragflow/pull/18185
- RAGFlow and frozen MinIO image: https://github.com/infiniflow/ragflow/issues/13840
- vLLM on DGX Spark (official image path): https://vllm.ai/blog/2026-06-01-vllm-dgx-spark
- NVIDIA vLLM multi-node playbook: https://build.nvidia.com/spark/vllm/multi-node.md
