# Validation log

[Русская версия](VALIDATION.ru.md)

Sections 1–5 are stage 3.1: evidence behind decisions D8 and D14–D16 in
[DECISIONS.md](DECISIONS.md), collected 2026-10-02 … 2026-10-07 on one DGX
Spark (GB10, DGX OS 7.5.0, driver 580.178.04, CUDA 13.0) with RAGFlow's own
compose, the RAGSpark image `0.27.2-arm64-r1` and three vLLM containers on the
same Docker network. Section 6 is stage 3.2, RAGSpark's own compose (D17).
Numbers can be reproduced with the scripts in [tools/](../tools/).

## 1. LLM serving — Qwen 3.8 27B NVFP4

- Image `vllm/vllm-openai:v0.27.1-aarch64`: vLLM 0.27.1, CUDA 13.0, compute
  capability (12, 1). The NVFP4 checkpoint is detected and
  `FlashInferCutlassNvFp4LinearKernel` is selected — no community build needed.
- Weights load in ~150 s from cache; KV cache ~814k tokens at
  `gpu_memory_utilization 0.50` (without MTP). Start-up with MTP and CUDA graph
  capture: ~7 min.
- [model_probe.py](../tools/model_probe.py), 5 Russian prompts per mode:

  | Mode | Without MTP | MTP, 3 tokens | Answers with CJK |
  |---|---|---|---|
  | thinking on | 10.3 tok/s | — | 0 of 5 |
  | thinking off | 10.2 tok/s | — | 0 of 5 |
  | thinking off, t=0.7 | 10.2 tok/s | — | 0 of 5 |
  | all modes, MTP | — | **17.1 tok/s** (19–21 on context answers) | 0 of 15 |

- Thinking: the same answer took 1,116 tokens / 108 s with thinking and
  101 tokens / 10 s without. Disabled on the server with
  `--default-chat-template-kwargs '{"enable_thinking": false}'`; verified that a
  request without parameters returns no `reasoning` field.
- 8 concurrent requests with MTP: all HTTP 200, 25–27 s each, ~90 tok/s
  aggregate; the server stayed up.
- One earlier ad-hoc answer contained a Chinese fragment («相关信息»); the
  acceptance test should keep checking for CJK characters.

## 2. Model registration through the RAGFlow API

Captured from the web UI of RAGFlow 0.27.2 and automated in
[ragflow_models.py](../tools/ragflow_models.py):

| Step | Request | Body / notes |
|---|---|---|
| log in | `POST /api/v1/auth/login` | `{email, password}`; the password is RSA-encrypted with `conf/public.pem` (`api.utils.crypt.crypt`). The token comes back in the `Authorization` **response header**, without `Bearer` |
| add provider | `PUT /api/v1/providers` | `{"provider_name": "VLLM"}` |
| check address | `POST /api/v1/providers/VLLM/connection` | `{provider_name, api_key: "", base_url}` |
| list served models | `GET /api/v1/providers/VLLM/models?api_key=x&base_url=…` | returns `name`, `model_types` (detected correctly: chat / embedding / rerank), `max_tokens` from vLLM |
| create instance | `POST /api/v1/providers/VLLM/instances` | `{instance_name, api_key: "", base_url, model_info: [{model_name, model_type: [type], max_tokens, extra: {is_tools}}]}`; an empty key is stored as `"x"` |
| list instances | `GET /api/v1/providers/VLLM/instances` | **no `base_url`** in the list; read it from `GET …/instances/<id>` |
| update instance | `PUT /api/v1/providers/VLLM/instances/<id>` | full record |
| model ids | `GET /api/v1/models` | `model_id`, `name`, `instance_name`, `model_type` per model; a model id differs from its instance id |
| set default | `PATCH /api/v1/models/default` | `{model_id, model_type}`; the UI saves each field immediately |
| read defaults | `GET /api/v1/models/default` | stored by `model_id`; names are resolved, so a stale id can still show matching names |
| search | `POST /api/v1/retrieval` | `{dataset_ids, question, similarity_threshold, vector_similarity_weight, rerank_id: "BAAI/bge-reranker-v2-m3@ragspark-rerank@VLLM", page_size, rerank_candidates_count}` |

All responses carry `{"code": 0}` on success. Models live in the
administrator's tenant (`tenant_name: "admin’s Kingdom"`).

## 3. Ingestion

- **DOCX** (18 KB, Russian with shell commands): 3 chunks, about 1.5 s from
  start to indexed; three `POST /v1/embeddings` reached vLLM. The task executor
  works without NATS — queues are in Valkey. Lines are merged without a
  separator (`GPUКритерий`): 11 such places.
- **PDF** (39-page Russian motherboard manual, text layer present):

  | Parser | Time | Chunks | Cyrillic share | Garbled words |
  |---|---|---|---|---|
  | DeepDOC | ~2 min (OCR 7–9 s, layout ~8 s, tables 4–39 s per 12 pages) | 100 | 0.665 | 194 |
  | Naive | seconds | 41 | **0.835** | **0** |

  Reference: 0.835 from the PDF's own text layer (`pdftotext`).
- DeepDOC root causes, from the 0.27.2 source:
  - `deepdoc/parser/pdf_parser.py`, page-level "font-encoding garbled" check:
    a page whose characters come ≥30% from subset fonts, <5% CJK and >40% ASCII
    punctuation is cleared and OCRed. Table-of-contents dot leaders trigger it.
  - The OCR model is CJK + Latin. A box-level guard against re-OCRing Cyrillic
    exists (`_ocr_can_represent`), but the page-level check has none.

## 4. Retrieval

Final score with a reranker (`rag/nlp/search.py`, `rerank_by_model`):
`(1 − w) × keyword similarity + w × rerank score`, compared with the
similarity threshold; `w` is the vector similarity weight.

- With RAGFlow defaults (`w` = 0.3, threshold 0.2) the correct DOCX chunk
  scored 0.7 × 0.154 + 0.3 × 0.293 = **0.196** and was dropped.
- [rag_eval.py](../tools/rag_eval.py) on the PDF (Naive), 8 questions with
  answers and 2 without:

  | Setup | Before patch: found / rejected | After patch: found / rejected |
  |---|---|---|
  | reranker, 0.3 / 0.2 | 5/8 · 1/2 | 6/8 · 1/2 |
  | reranker, 0.7 / 0.2 | 5/8 · 2/2 | 7/8 · 2/2 |
  | reranker, 0.7 / 0.1 | 6/8 · 2/2 | **8/8 · 2/2** |
  | no reranker, 0.7 / 0.2 | 8/8 · 0/2 | 8/8 · 0/2 |
  | no reranker, 0.7 / 0.5 | 8/8 · 0/2 | 8/8 · 0/2 |

  Without the reranker the no-answer questions scored 0.58 — as high as real
  answers (0.56–0.62).
- Root cause of low rerank scores: RAGFlow sends the reranker
  `remove_redundant_spaces(" ".join(tokens))`. Its first regex,
  `([^a-z0-9.,\)>]) +([^ ])` → `\1\2`, deletes every space after a non-Latin
  character:

  ```
  "оранжевый 1 гбит с подключение таблица состояний индикатора порта lan"
  → "оранжевый1гбитсподключениетаблицасостоянийиндикаторапортаlan"
  ```

  English is unaffected. Scoring the same chunk directly (`--probe`):

  | Question | Glued tokens (RAGFlow) | Original text | Answer lines only |
  |---|---|---|---|
  | memory size | 0.152 | 0.116 | 0.047 |
  | orange LAN LED | 0.373 | 0.457 | 0.285 |
  | M2_2 sizes | 0.320 | 0.640 | 0.960 |

  The file-name tokens RAGFlow appends changed scores by less than 0.05. The
  LLM is not affected: it receives the original chunk text.
- Fix: [patch_search.py](../build/ragflow/patch_search.py) (image `r2`) — the
  reranker gets the original chunk text plus the chunk's keywords and
  generated questions. Tested hot-patched in the running container, then
  again with the image `r2` on 2026-10-08: the same matrix.

## 5. End to end

- Chat with Qwen 3.8, dataset with the DOCX, reranker, `w` = 0.7, threshold
  0.2: «Какая максимальная температура GPU допустима при стресс-тесте?» →
  «≤80 °C, пик ≤90 °C» with a citation, in Russian.
- A question with no answer in the documents returned the configured "Empty
  response" text. RAGFlow returns it without calling the LLM when nothing
  passes the threshold, so the model has no chance to invent an answer.
- New datasets and chats picked up the default LLM and embedding model; the
  reranker had to be selected manually (D8).

## 6. RAGSpark compose (stage 3.2)

Evidence for D17, collected 2026-10-08 on the same DGX Spark with Docker
29.6.2 and Compose v5.2.0: [compose/](../compose/) started from empty volumes,
with the weights already in `/srv/ragspark/hf-cache`.

- **Start order and time** (`docker compose up -d --wait`, start times from
  `docker inspect`):

  | Step | Time |
  |---|---|
  | `vllm-embed` until healthy | 36 s |
  | `vllm-rerank` until healthy | 36 s |
  | `vllm-llm` (Qwen 3.8, MTP) until healthy | 7 min 11 s |
  | `ragflow` until healthy | ~46 s |
  | **whole stack** | **9 min 10 s** |

  The databases became healthy in parallel with the models. Health checks run
  every 20 s, so each step is accurate to about 20 s.
- **LLM restart** (`docker compose restart vllm-llm`): 332 s instead of 431 s.
  The compile cache kept in the `vllm-compile-cache` volume saves about a
  quarter; loading the weights and capturing CUDA graphs are repeated.
- **Exposure:** the only listening `docker-proxy` is `0.0.0.0:80`.
- **Time zone:** the RAGFlow container shows the host's time (MSK).
- **Health:** `/api/v1/system/healthz` returned `ok` for MySQL,
  Elasticsearch, Valkey and storage.
- **Memory with the stack idle:** 86 GiB used, 35 GiB file cache, 34 GiB
  available. The models started without freeing the file cache first.
- **Superuser:** RAGFlow created `admin@ragspark.local` from
  `DEFAULT_SUPERUSER_EMAIL`; the API and the web interface accept it.
- **Models:** on the empty database [ragflow_models.py](../tools/ragflow_models.py)
  added the VLLM provider, created the three instances and set the defaults; a
  second run reported everything `[OK]`. A new dataset picked up the default
  embedding and indexing models.
- **Retrieval:** the same two documents, re-uploaded (PDF with the Plain Text
  parser), gave exactly the stage 3.1 results — PDF 41 chunks, Cyrillic share
  0.835, no garbled words; the [rag_eval.py](../tools/rag_eval.py) matrix
  identical cell by cell, **8/8 found · 2/2 rejected** with the RAGSpark
  settings (reranker, 0.7 / 0.1).
- **LLM from inside the stack** ([model_probe.py](../tools/model_probe.py)
  run in the RAGFlow container): 16.5–17.0 tok/s, no CJK in 15 answers. The
  "thinking" mode is as fast as the others because the server turns thinking
  off unless a request asks for it.
