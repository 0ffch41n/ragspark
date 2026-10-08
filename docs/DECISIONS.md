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
  separate containers under its own licenses. RAGFlow's source is used
  unmodified; its image is built with one RAGSpark change to the build recipe
  (see D3), marked in the patched Dockerfile.
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
  Recipe: [BUILD.md](BUILD.md).
- **Published:** `0.27.2-arm64-r2` (2026-10-08),
  `sha256:4232ddbb513cff48c51864f081f6b82d37371927dea70bb01a200c6994f70dcc`, with both patches below.
  It replaces `0.27.2-arm64-r1` (2026-10-01,
  `sha256:1bf5fc0031bff2d775dc8bc1626b2820f9b658b43696a9bb4bdc02a8304f0660`),
  which had patch 1 only.
- **Deviations from upstream** — two, each applied by a script that refuses to
  run if the code it expects is not there:
  1. The Chrome/ChromeDriver steps run on x86_64 only: the archives in
     `ragflow_deps` are x86 builds that cannot run on arm64. Browser-driven
     features (e.g. web crawling in agents) are unavailable on arm64.
  2. The reranker receives the original chunk text (`r2`). Upstream builds the
     cross-encoder input with `remove_redundant_spaces()`, which deletes every
     space after a non-Latin letter, so Russian chunks reach the reranker as one
     long word and rerank scores drop 2–3x. See D15 and
     [VALIDATION.md](VALIDATION.md); to be reported upstream.
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
  - pooling models (embeddings, reranker) run with a neutral entrypoint
    (`/usr/bin/env`), because the image entrypoint is `vllm serve`;
  - Docker uses the `cgroupfs` driver so that `systemctl daemon-reload` does not
    revoke GPU access from running containers.

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
  `admin@ragflow.io` with the password from `ADMIN_DEFAULT_PASSWORD`. The
  installer sets a random value, so there is no race for the first sign-up.
  Verified: the superuser signs in to the regular web UI.
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
  repository, image and digest, flags, memory, context presets, tool-calling
  parser, supported modes, license, `validated` flag.
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

## D14. Document parsing — plain text layer for PDF

- **What:** how PDFs become text before chunking.
- **Decision:** PDF parser **Naive** (the PDF's own text layer, labelled
  "Plain Text" in the code). DeepDOC is not used for Russian documents.
- **Why:** DeepDOC's OCR model recognises CJK and Latin only, so Cyrillic comes
  out as Latin look-alikes (`Pa3beM` for «Разъем»). DeepDOC also discards the
  text layer of a whole page when more than 40% of its characters are ASCII
  punctuation from embedded subset fonts — table-of-contents dot leaders
  trigger it — and then OCRs the page. On a 39-page Russian manual:

  | Parser | Cyrillic share | Garbled words | Questions found |
  |---|---|---|---|
  | DeepDOC | 0.665 | 194 | 4 of 8 |
  | Naive | **0.835** (= text layer) | **0** | 5 of 8 → 8 of 8 with D15 |

- **Trade-offs:** Naive flattens tables into lines (still readable, retrieval
  works) and cannot read scans.
- **Open:** scanned Russian documents need another parser — Qwen 3.8 as a
  vision model, or Docling — measured with
  [tools/rag_eval.py](../tools/rag_eval.py) before a choice is made.

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

## D16. Default LLM — Qwen 3.8 27B NVFP4 with MTP

- **Decision:** `Inferact/Qwen3.8-27B-NVFP4` on the official vLLM 0.27.1
  image, multi-token prediction with 3 speculative tokens, thinking disabled
  and sampling (temperature 0.7, top-p 0.8, top-k 20) set on the server. Flags
  are in the [catalog](../catalog/models.yaml).
- **Why:** strong open model in its size class, Apache-2.0, native tool
  calling, decode speed nearly flat on long contexts. In Russian probes
  (30 answers) it never mixed in other scripts and cited sources correctly.
- **Measured:** 10.2 tok/s per user without MTP, **17.1** with MTP (19–21 on
  answers drawn from context); 8 concurrent requests served, ~90 tok/s
  aggregate. Thinking disabled on the server because RAGFlow does not pass the
  switch: with thinking, the same answer took 1,116 tokens and 108 s instead of
  101 tokens and 10 s.
- **Trade-off:** slower per user than Gemma 4 26B-A4B (4B active parameters,
  ~23 tok/s), which stays in the catalog as an alternative.

## Open questions

- **Scanned documents.** See D14.
- **Glued lines.** When RAGFlow merges lines of a DOCX into a chunk it adds no
  separator (`GPUКритерий`, `скоростьsudo` — 11 places in a short document),
  which hurts keyword search.
- **Time zone.** RAGFlow logs in UTC+8 by default; the installer should pass the
  host time zone to the containers.
- **Auto-question and keyword analysis.** Both use the LLM to improve matching
  (questions generated per chunk at indexing; keywords extracted per query).
  Measure the gain against the extra time.
- **Upstream reports.** Two RAGFlow defects found in stage 3 (D3 patch 2, D14)
  should be reported to the `0.27.x` branch.
- **Superuser e-mail.** Whether `admin@ragflow.io` can be changed through
  configuration is unverified.
- **Sign-up on the server side.** The UI hides sign-up; whether the API rejects
  registration requests is unverified.
- **ODBC driver licence.** The upstream Dockerfile installs Microsoft's ODBC
  driver and accepts its EULA. Review its redistribution terms for the
  published image, or drop the driver if SQL Server connectors are not needed.
- **Dependency image.** The upstream Dockerfile copies data files (OCR models,
  NLTK data, Tika, uv) from `infiniflow/ragflow_deps:latest`, a floating tag.
  Pin it by digest in the build so that a rebuild reproduces the published
  image; the digest used for `r2` is in [BUILD.md](BUILD.md).

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
