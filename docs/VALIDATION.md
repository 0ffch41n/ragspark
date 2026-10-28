# Validation log

[Русская версия](VALIDATION.ru.md)

Sections 1–5 are stage 3.1: evidence behind decisions D8 and D14–D16 in
[DECISIONS.md](DECISIONS.md), collected 2026-10-02 … 2026-10-07 on one DGX
Spark (GB10, DGX OS 7.5.0, driver 580.178.04, CUDA 13.0) with RAGFlow's own
compose, the RAGSpark image `0.27.2-arm64-r1` and three vLLM containers on the
same Docker network. Section 6 is stage 3.2, RAGSpark's own compose (D17);
section 7 is stage 3.3, the installer (D18); section 8 is the chat modes and
the images `r3` and `r4` (D19); section 9 is stage 3.4, a clean install from a
fresh clone (D18); section 10 is scanned documents (D14); section 11 is three
checks of 2026-10-21: database warnings, sign-up, pinned revisions (D3, D6);
section 12 is the change of the LLM checkpoint on 2026-10-28 (D16).
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
- These numbers are for the checkpoint `Inferact/Qwen3.8-27B-NVFP4`. Since
  2026-10-28 the default is `unsloth/Qwen3.8-27B-NVFP4` — see section 12.

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

## 7. Installer (stage 3.3)

Evidence for D18, collected 2026-10-09 on the same DGX Spark (DGX OS 7.5,
Docker 29.6.2, Compose v5.2.0, Python 3.12.3). The stack of stage 3.2 was
removed with its volumes first; images and weights stayed on the machine.

- **Offline tests on the Spark:** 19 of 19 passed with the machine's Python
  and Compose.
- **GPU after `systemctl daemon-reload`** (`ragspark check --gpu-reload`): the
  test container kept the GPU with Docker's `systemd` cgroup driver. The
  driver stays as it is (D6).
- **Installation** (`sudo ./install.sh`, Russian, defaults): every check
  passed; 6 of 6 images present; GPU visible in the vLLM image; the stack
  started from empty volumes in **9.4 min**; models registered; acceptance
  test passed in **16 s** — 2 chunks with Cyrillic share 0.992, both answers
  correct with a citation (3 s and 2 s), the question with no answer got the
  "no answer" text without calling the LLM. Listening on the network: ports
  22 and 80 only.
- **Weights check:** the Qwen cache was complete, but the offline check
  through `huggingface_hub` failed with `IncompleteSnapshotError`: it demands
  three auxiliary files (`crc32.txt`, `hf_quant_config.json`, an empty
  `safetensors-md5sum.txt`) that vLLM's own download had skipped. The
  installer fetched them (17.5 kB). The check now reads the cache itself —
  the snapshot that `refs/main` names, `config.json` and every weight file of
  the index — so a complete cache needs no network.
- **Reboot:** Docker itself started `es01` and `ragflow`, the two containers
  that had exited with a non-zero code at shutdown. The unit stopped them
  (`ExecStartPre`) and started the stack in order: databases and embeddings
  at once, reranker after 27 s, LLM after another 25 s, RAGFlow when the LLM
  was healthy 5.5 min later (compile cache). About 7 min from the start of
  the unit to a ready stack, no container restarted. The file cache was empty
  after the cold boot; the models needed no cache flush; 36 GiB available
  with the stack idle. `ragspark start` during the boot only followed the
  unit.
- **Stop and start** (`ragspark stop`, `ragspark start`): all 8 containers
  stopped; started again in order in 6.2 min.
- **Chat modes in the web interface:** RAGFlow's chat input defaults to the
  "Low" reasoning level, which answers through RAGFlow's agentic pipeline. A
  question there did not finish in 10 minutes ("Running the rag tool...");
  the tool timeout (600 s) cut it. The pipeline's first helper call — keyword
  extraction, temperature 0.1, no output limit (RAGFlow strips `max_tokens`
  from every request) — never returned: vLLM generated that one request for
  10 minutes with every speculative token accepted, a repetition loop. The
  same request sent 30 times with an 800-token limit looped 9 times
  (`"серверная", "серверная", …`). With `frequency_penalty 0.5` it looped in
  none of 30 runs and all 30 answers were valid JSON; `repetition_penalty 1.1`
  also gave 0 loops but penalises the prompt's tokens too, `presence_penalty
  1.5` gave 2. The "Naive" level, which the acceptance test uses, answers in
  seconds. Fixed in image `r3`: section 8.

## 8. Chat modes and the images `r3` and `r4`

Evidence for D19, collected 2026-10-12 … 13 and 2026-10-19 on the same DGX
Spark with the stack installed in stage 3.3. Each fix was first applied to the
running container (`build/ragflow/patch_*.py` run inside it, then a restart) —
`r2` for the eight fixes that became `r3`, `r3` for the four that became `r4`;
[tools/mode_eval.py](../tools/mode_eval.py) asks one chat the same questions
at each reasoning level through the API.

The published `r3` image predates two of D19's fixes: the Cyrillic keyword
narrowing and the forced first `rag` call were written after it was built
(13:59 against 12:12 on 2026-10-14), so in that image they are absent and have
only ever run as a hot patch on the container. `r4` is the first image to carry
all twelve. Checked by the patch markers of `build/ragflow/patch_*.py`: nine in
the `r3` image against fifteen in the container the measurements below were
taken on — and that container's fifteen are byte for byte the set the `r4`
image carries, so these measurements describe `r4`.

- **Helper-call loops.** The keyword request of the agentic pipeline as
  RAGFlow sends it (temperature 0.1, no output limit), here capped at 800
  tokens; 5 questions × 6, five at a time:

  | Variant | Loops | Valid JSON | Normal answers, tokens | Time |
  |---|---|---|---|---|
  | as sent by RAGFlow | **9/30** | — | 60–704 | 134 s |
  | `repetition_penalty 1.05` | 2/30 | — | 41–338 | 70 s |
  | `presence_penalty 1.5` | 2/30 | 28/30 | 67–121 | 62 s |
  | Qwen's advice: t 0.7, top_p 0.8, presence 1.5 | 1/30 | 29/30 | 66–187 | 56 s |
  | `repetition_penalty 1.1` | 0/30 | 30/30 | 38–215 | 57 s |
  | **`frequency_penalty 0.5`** | **0/30** | **30/30** | 59–126 | 41 s |

  A loop repeats one word or phrase (`"серверная", "серверная", …`). Chosen:
  `frequency_penalty`, which counts only the answer's own repetitions;
  `repetition_penalty` also penalises the prompt's tokens. No variant produced
  CJK characters.
- **kNN filter.** "Когда проходят плановые работы?" against a document that
  says "Плановое обслуживание проводится…": before the fix no candidates at
  all in any setup, even at threshold 0; after it rank 1 in four setups of
  five (all but reranker 0.3/0.2). On the stage 3.1 manual the totals of the
  §4 matrix were unchanged in every setup: reranker 0.7/0.1 8/8 found, 2/2
  rejected; without the reranker 8/8 and 0/2 as before.
- **Medium's "no answer".** Log of one question: one passage found, review
  verdict SUFFICIENT (confidence 0.95), then "No supporting evidence was
  found; returning the configured empty response": the graph's `empty_result`
  flag, set at the start, was never cleared.
- **Reranker query.** With the reranker added to the agentic search, Low
  dropped to 5/7: the search string was the question plus the generated
  keywords (entity three times, English aliases), and the reranker scored that.
  Searching with the question itself restored 7/7.
- **Router.** Before the fix the outer model answered "Какая столица
  Австралии?" itself in some runs, once "Канберра", once "Сидней"; after it
  every run called `rag` and gave the "no answer" text.
- **Acceptance sample**, 7 questions with answers and 1 without (sources and
  no CJK required):

  | Level | Image `r2` | All fixes | Median / max time |
  |---|---|---|---|
  | Naive | 6/7 · 1/1 | **7/7 · 1/1** | 3 s / 4 s |
  | Low | hung in about half the questions | **7/7 · 1/1** | 15 s / 17 s |
  | Medium | 0/7 · 1/1¹ | **7/7 · 1/1** | 52 s / 92 s |
  | High | — | **7/7 · 1/1** | 47 s / 96 s |

  ¹ measured with the helper-call fix only, so that it would not hang.
- **Stage 3.1 manual**, 8 questions with answers and 2 without:

  | Level | Facts | "No answer" | Median / max time |
  |---|---|---|---|
  | Naive | 8/8 | 2/2 | 3 s / 8 s |
  | Low | 8/8 | 2/2 | 16 s / 20 s |
  | Medium | 8/8 | 0/2² | 96 s / 132 s |

  ² Medium found passages for both and wrote its own answer: "the documents
  have no information on the price", and, from the specifications with
  sources, "the board has no built-in Wi-Fi or Bluetooth", which is right for
  this board.
- **The four fixes of `r4`** (2026-10-19), the acceptance sample at every
  level against the same run without them (2026-10-14). "Before" is the
  container as it stood then — the `r3` image plus the three later edits noted
  above:

  | Level | Before | With the four fixes | Median / max time |
  |---|---|---|---|
  | Naive | 7/7 · 1/1 | **7/7 · 1/1** | 3 s / 4 s |
  | Low | 7/7 · 1/1 | **7/7 · 1/1** | 16 s / 21 s |
  | Medium | 6/7 · 1/1 | **7/7 · 1/1** | 56 s / 229 s |
  | High | 5/7 · 1/1 | **7/7 · 1/1** | 57 s / 122 s |
  | Ultra | 6/7 · 1/1 | **7/7 · 1/1** | 55 s / 100 s |

  What each fix is worth, read off the logs of both runs:

  - **Query language — the measured gain.** The planner now splits a Russian
    question into Russian sub-questions: `['расписание резервного копирования
    базы знаний', …]`, `['телефон дежурного администратора', …]`. Before the
    fix they came out in English and matched nothing in a Russian corpus. This
    is what recovered the Medium and High misses.
  - **Claim shapes — defect proven, guard untriggered.** The server log holds
    exactly one crash, and it is the Ultra miss above:
    `AttributeError: 'str' object has no attribute 'get'` at
    `sufficient_context.py:290`, `cid = str(item.get("claim_id") or "")`,
    reached through `_bounded`, which handles only `TimeoutError`. On
    2026-10-19 the model returned well-formed claim objects, so the new branch
    never ran: the fix insures against a shape the model produces now and
    then, and this run does not demonstrate it.
  - **Research budget — headroom, not a fix.** The whole-graph ceiling was
    never reached on 2026-10-19; the only budget warnings in the log are
    per-call ones. Medium's slowest question took 229 s, which the former
    180 s ceiling would have cut short, but on 2026-10-14 that same question
    answered correctly in 92 s. The cost is the longer worst case in the table
    above; no question in either run needed the extra time to be answered.
- **Image `r3`:** nine of the fifteen patch markers — the Cyrillic keyword
  narrowing and the forced first `rag` call are absent, so the image on its own
  does not reproduce the results above ([BUILD.md](BUILD.md)).
- **Image `r4`:** all fifteen markers plus the two prompt rules that carry
  none, byte for byte the set measured above, and `_TOTAL_BUDGET_S` reading
  `RAGSPARK_AGENTIC_BUDGET_S` ([BUILD.md](BUILD.md)).

## 9. Clean install (stage 3.4)

Evidence that what GitHub ships installs from zero and reproduces the numbers
of section 8, collected 2026-10-20 on the same DGX Spark. That machine had been
patched by hand for two days while section 8 was measured, so it was wiped
first: `sudo ragspark uninstall --keep-models` removed eight containers, six
volumes, the network, the unit, the command and `/opt/ragspark`, and kept the
29 GB of model weights. The locally built image was deleted with its tag as
well, so the installation had to fetch one from the registry.

- **The clone is self-sufficient.** A `git clone` of `main` at `0da23d3`
  carries all five patch scripts that `build/ragflow/build.sh` calls, plus
  `install.sh`, `tools/mode_eval.py` and `tools/acceptance.py`, and its
  `compose/compose.yaml` pins `r4` by digest. Before that commit three of the
  five scripts were untracked, so a clone could not have built the image at
  all.
- **The installer's own tests pass from the clone**: 23 of 23 in 33 s
  (`python3 -m unittest discover -s tests` with `RAGSPARK_TEST_COMPOSE` set).
  They run the whole installation against fake `docker`, `systemctl` and
  `nvidia-smi` and a mock RAGFlow API.
- **The installation** — `sudo ./install.sh --yes --lang ru` from a fresh
  clone — brought all eight containers up healthy in 9.4 min, registered the
  three models and set them as the defaults, and ended with the acceptance
  test passing in 29 s: the sample PDF parsed in 7 s into 2 chunks with a
  **Cyrillic share of 0.992** against the 0.80 minimum, both fact questions
  answered with a citation, the question the document does not answer refused,
  one Low-level agentic answer in 16 s, and the chat without a knowledge base
  answering 17 × 3.
- **The image came from GHCR.** The running container's image carries no tag
  at all, only `ghcr.io/0ffch41n/ragspark-ragflow@sha256:1ed7725f…`, which is
  what a pull by digest leaves behind — the locally built copy had been
  deleted with its tag before the install. Its fifteen patch markers therefore
  come from the image, not from a hot patch.
- **The chat modes, measured again on this installation** with
  `tools/mode_eval.py` from the clone, against section 8's run on the
  hand-patched machine:

  | Level | Section 8 | Clean install | Median / max time |
  |---|---|---|---|
  | Naive | 7/7 · 1/1 | **7/7 · 1/1** | 3 s / 4 s |
  | Low | 7/7 · 1/1 | **7/7 · 1/1** | 15 s / 20 s |
  | Medium | 7/7 · 1/1 | **7/7 · 1/1** | 62 s / 127 s |
  | High | 7/7 · 1/1 | **7/7 · 1/1** | 51 s / 151 s |
  | Ultra | 7/7 · 1/1 | **7/7 · 1/1** | 47 s / 127 s |

  Correctness is identical at every level, with no failures. The times move
  within the usual spread, and section 8's one outlier did not recur: Medium's
  slowest question took 127 s here against 229 s there, so that 229 s was
  noise rather than a consequence of the larger research budget.
- **Behaviour worth knowing**, both scored correct. Ultra prefaced its answer
  about the number of backups with "the knowledge base does not have enough
  information for a complete answer" and then gave the right figure; High
  answered the same question by leading with where the backups are kept rather
  than how many. This is the D19 note about the agentic levels writing their
  own answers.

## 10. Scanned documents

Evidence for the scan part of D14, collected 2026-10-20 on the clean install of
section 9 (`r4`), with the three fixes of `build/ragflow/patch_scans.py` applied
to the running container one by one. The test document is the stage 3.1 manual
turned into a scan: every page rendered at 150 dpi in greyscale, slightly
blurred and saved as JPEG at quality 70, so the PDF carries no text layer at
all. Measured with [tools/rag_eval.py](../tools/rag_eval.py) and its MSI
question set; "vision" is the running Qwen 3.8 27B registered a second time as
a vision model — no other model is loaded.

- **Why DeepDOC cannot read Russian.** Two causes, both in code. The dictionary
  of its recogniser (`rag/res/deepdoc/ocr.res`) has 6623 entries: 6277 CJK and
  one of the 33 lowercase Russian letters. And `_is_garbled_by_font_encoding`
  in `deepdoc/parser/pdf_parser.py` clears a page's text layer when at least
  30% of its characters come from subset fonts and
  `cjk_ratio < 0.05 and punct_ratio > 0.4`; Cyrillic is counted in neither
  ratio, so on a Russian page only the punctuation decides. On the digital
  manual it cleared pages 1–2, the table of contents (69% and 85% dots), and
  recognised them as images. With Cyrillic counted as text (fix 1) those pages
  keep their layer: garbled words 194 → 164, Cyrillic share 0.665 → 0.679. The
  other 85% of the garbage comes from regions DeepDOC always recognises — BIOS
  screenshots — which no text layer can save.
- **The scan through each parser:**

  | Parser | Time | Chunks | Cyrillic | Garbled words | Found (rr .7/.1) | No-answer rejected |
  |---|---|---|---|---|---|---|
  | Plain Text | 3 s | 0 | 0.000 | 0 | 0/8 | —¹ |
  | DeepDOC | 145 s | 89 | 0.000 | 1066 | 0/8 | —¹ |
  | vision, as shipped | 675 s | 39 | 0.823 | 1 | 7/8 | 2/2 |
  | vision, temperature 0 (fix 2) | 675 s | 40 | 0.818 | 2 | 7/8 | 2/2 |
  | **vision, temperature 0, whole tables (fixes 2–3)** | 674 s | 43 | **0.818** | **2** | **8/8** | **2/2** |
  | *digital manual, Plain Text, all fixes, for reference* | 6 s | 41 | 0.835 | 0 | 8/8 | 2/2 |

  ¹ nothing passes the threshold for any question, so "rejected" means nothing.

  The garbled words left are two words with a Latin letter inside
  (`Видеокarta`, `брackete`); `rag_eval` counts only mixed-script words, and a
  few `Г6/c` for «Гб/с» and one `Khonka` for «Кнопка» also occur on this
  deliberately poor scan.
- **Temperature (fix 2).** RAGFlow sends vision calls without sampling
  parameters (`describe_with_prompt` in `rag/llm/cv_model.py`; `extra_body` is
  None for `OpenAI_APICV`), so vLLM applies the LLM's chat default, temperature
  0.7. Page 10 of the scan, five runs each, through RAGFlow's own code path:

  | Temperature | Distinct outputs | LAN table transcribed | Runaways | Time |
  |---|---|---|---|---|
  | 0.7 (as shipped) | 5 of 5 | 5 of 5 | 0 | 21.5 s |
  | 0 | **1 of 5** | 5 of 5 | 0 | 22.0 s |
  | 0.2 | 4 of 5 | 5 of 5 | 0 | 22.1 s |

  On a re-rendered copy of the same page the 0.7 default replaced the LAN and
  audio tables with image placeholders on one try of three, and the indexed
  chunk lost «Оранжевый — 1 Гбит/с»; at 0 the output was byte-identical every
  time and kept both tables. The three parses of the whole scan at 0 (two chunk
  sizes, then fix 3) gave the same Cyrillic share and the same garbled words.
- **Tables (fix 3).** `naive_merge` in `rag/nlp/__init__.py` splits text on
  the delimiter, which includes the newline, and packs the lines greedily up to
  `chunk_token_num`; it has no notion of a table. The one miss above —
  «Что означает оранжевый индикатор скорости на порту LAN?» — was a table cut
  at the budget: its rows landed in a chunk without the title and the header
  row. Reranker scores against that question: those rows 0.014, the bare answer
  lines 0.009, the same page with the table intact 0.544 — the threshold needs
  about 0.11. A 1024-token chunk gave the same 7/8: a larger budget only moved
  the cut two rows down. On the recognised text of the whole scan the fix took
  torn tables from 7 to 0, and text without tables was chunked byte for byte as
  before. After it the question ranks #1 in all five setups.
- **Digital PDFs unchanged.** The digital manual through Plain Text with all
  three fixes: 8/8, 2/2, Cyrillic 0.835, as in stage 3.1, and none of its 41
  chunks contains a `|`, so the table fix never runs on that path.
- **Image `r5`:** differs from `r4` in exactly the three files of
  `patch_scans.py`, and those are byte for byte the files of the container
  measured above ([BUILD.md](BUILD.md)).
- **Not covered yet:** a real scan with skew and paper noise; cells made of
  symbols only (● matrices) carry small errors at any temperature.

## 11. Database warnings, sign-up, pinned revisions

Three open questions of [DECISIONS.md](DECISIONS.md), checked on 2026-10-21 on
the clean install of section 9 running `r5`.

- **Database warnings (D3, patch 7).** `Database connection issue (attempt
  1/5): (0, '')` appeared 1961 times in `/ragflow/logs/ragflow_server.log`
  (not in `docker logs`) between 2026-10-20 10:51 and 2026-10-21 10:27, all
  from `api/ragflow_server.py`, 46 s apart in 250 of the last 300 intervals
  and 47 s in 39, each followed 1 s later by the Elasticsearch probe of the
  same health check. MySQL is not the cause: `wait_timeout` is 28800 s, and
  `Aborted_clients` stayed flat over a minute with two warnings. The cause is
  in RAGFlow. Docker's health check calls `/api/v1/system/healthz` every 15 s;
  `check_db` in `api/utils/health_utils.py` runs `SELECT 1` outside
  `connection_context`, so the worker thread keeps a pooled connection checked
  out; and `close_connection()`, run after every request, calls
  `DB.close_stale(age=30)`, which closes every connection checked out for
  more than 30 s, that one included. The third check after a reconnect closes
  it, the fourth fails and retries after 1 s: 3 × 15 + 1 = 46 s. With the probe
  inside `connection_context` (patch 7, applied hot, then `docker restart`):
  in the next 10 minutes 0 warnings, 0 `DB execution failure`, 42 health
  checks, all `ok`; new MySQL connections 6.2 a minute instead of about 9.
  `close_stale(age=30)` itself closes any connection held longer than 30 s, a
  long transaction's too; that was not seen under RAGSpark's load.
- **Sign-up on the server side.** Closed. `REGISTER_ENABLED=0` (RAGSpark's
  `env.example`; upstream's default is 1) reaches the server, which reports
  `registerEnabled: 0` at `/api/v1/system/config`. `POST /api/v1/users` from
  the LAN address with a deliberately invalid e-mail answered `User
  registration is disabled!`; the handler checks the flag first, and with the
  flag on the invalid address would be rejected before any write, so the
  probe could not create an account, and the account list was unchanged. The
  admin service, reachable through port 80 at `/api/v1/admin` (nginx → 9381),
  answered 401 without a login, both to listing and to creating users. Only
  port 80 is published. No OAuth channel is configured — RAGFlow's OAuth
  callback creates accounts without checking `REGISTER_ENABLED`
  ([INSTALL.md](INSTALL.md)). The only account is the installer's superuser,
  and its password is none of `admin`, `ragflow` or empty (a local hash check,
  no login). RAGFlow's fallback to the literal password `admin` in
  `api/db/init_data.py` runs only with `--init-superuser`, which RAGSpark does
  not pass; the first superuser is created by the admin service from
  `ADMIN_DEFAULT_PASSWORD`.
- **Pinned revisions (D6, D3).** `main` of each model repository on Hugging
  Face pointed at the cached snapshot (`6128240e…`, `0cc6cfe4…`,
  `953dc6f6…`), and `ubuntu:24.04` and `infiniflow/ragflow_deps:latest` at
  the digests `r4` and `r5` were built from, so the pins record what was
  validated. vLLM's own `download_weights_from_hf` resolved the pinned
  revisions offline, with `refs/main` and without it (a pinned download writes
  none), and loaded the config and tokenizer at them; the tokenizer follows
  `--revision` by default. Installer tests: 25 of 25, the end-to-end test
  requiring every model to be pinned to a commit. The patched upstream
  Dockerfile has no floating reference left: one `FROM` and seven
  `ragflow_deps` mounts.
- **Image `r6`:** differs from `r5` in exactly `api/utils/health_utils.py`, and
  every file the build patches is byte for byte the one in the container
  measured above; its build log shows both base images at the pinned digests
  ([BUILD.md](BUILD.md)).

## 12. LLM checkpoint — unsloth instead of Inferact

Evidence for the checkpoint in D16. Collected 2026-10-23 on a second DGX Spark
(same image, flags and driver), with the checkpoint served there and reached
from the production RAGFlow through a test registration, and 2026-10-28 on the
production Spark of section 9 after the switch (image `r6`, DGX OS 7.6.0,
driver 580.178.04, CUDA 13.0). Speed: a Russian DOCX guide as context (1,283
tokens), 512 output tokens forced, temperature 0, three runs per case, MTP
acceptance from vLLM's metrics; the benchmark script is not in `tools/`.

- **Why Inferact's checkpoint was slow.** A torch profile of one 256-token
  answer on one Spark: GPU busy 98%; bf16 matrix multiplications 31% (the
  linear-attention projections `in_proj_qkv`, `in_proj_z`, `in_proj_a`,
  `in_proj_b` of all 48 linear layers, which its quantisation left in bf16),
  bf16 matrix-vector products 31% (mostly the MTP drafts reading the 2.54 GB
  bf16 `lm_head` three times per step), FP4 34%, the rest 4%. Neither the
  FlashInfer autotuner nor the GB10 environment variables of community recipes
  changed the speed: 21.0 tok/s as shipped, 20.6 without autotuning, 20.9 with
  the variables.
- **The unsloth checkpoint** (revision `f0b7c9e7…`, Apache-2.0, compressed
  tensors): FP8 for linear attention `in_proj_qkv`, `in_proj_z`, `out_proj`,
  for attention q/k/v/o and for `lm_head`; NVFP4 for the MLP; 23.4 GB instead
  of 26.4. vLLM 0.27.1 selects `CutlassFP8ScaledMMLinearKernel` and
  `FlashInferCutlassNvFp4LinearKernel` and loads MTP from
  `model_mtp.safetensors` without extra flags. Tokenizer, vocabulary and
  `generation_config` are identical; RAGFlow-style prompts (retrieval, tools,
  a tool round trip, thinking off) render byte for byte the same. The chat
  template differs elsewhere: it merges leading system and developer messages
  and is stricter about malformed tool calls.
- **Speed:**

  | | Inferact, second Spark | unsloth, second Spark | Inferact, production | unsloth, production |
  |---|---|---|---|---|
  | 1k context, tok/s | 21.0 | 27.8 | 21.1 | **27.2** |
  | 8k context, tok/s | 21.4 | 25.8 | 21.3 | **26.0** |
  | 8k, first token | 4.60 s | 6.55 s | 4.94 s | 6.43 s |
  | 4 concurrent, aggregate tok/s | 77.4 | 86.7 | 77.0 | 84.7 |
  | 8 concurrent, aggregate tok/s | 128.6 | 133.9 | 126.8 | 137.3 |
  | MTP draft tokens accepted | 74% | 73% | 74% | 74% |

  Decode is 21–32% faster for one user; prefill is slower, so a long prompt
  waits about 1.5 s longer for its first token. Under 8 concurrent requests the
  gain shrinks to 4–8%.
- **Through RAGFlow, side by side (2026-10-23).**
  - Acceptance questions: 2 of 2 with citations, the refusal, Low in 11 s.
  - Acceptance sample, all five levels: 7/7 and 1/1 at every level; medians
    Naive 2 s, Low 11 s, Medium 41 s, High 36 s, Ultra 40 s.
  - The digital MSI manual (Plain Text, 41 chunks), levels Naive, Low,
    Medium: unsloth 8/8, 8/8, 8/8; Inferact 8/8, 8/8, 7/8 (at Medium one
    answer gave a wrong figure, «16 GB»). Both rejected 2 of 2
    no-answer questions at Naive and Low and 0 of 2 at Medium — the agent
    refuses in its own words or infers Wi-Fi from the specifications, the same
    with either checkpoint. Medium median 55 s against 117 s.
  - Four pages of the section 10 scan through vision: 84 s, Cyrillic 0.879,
    0 garbled words, 5/5 and 2/2 (D15 settings).
  - 30 Russian answers at temperature 0.7: no CJK characters with unsloth, one
    fragment with Inferact («拥estion» inside an answer about TCP).
- **After the switch on the production Spark (2026-10-28).** Only `vllm-llm`
  was recreated; the catalog id, the compose file and the served name stayed,
  so the RAGFlow registrations, chats and `.env` did not change. Healthy after
  443 s: weights 22.13 GiB loaded in 171 s from disk, engine start 217 s of
  which 76 s compilation; KV cache 32.4 GiB, 770,357 tokens at
  `gpu_memory_utilization 0.50`. A Russian answer, a tool call and an image:
  correct. Acceptance sample, all five levels: 7/7 and 1/1 at every level;
  medians Naive 2 s, Low 10 s, Medium 41 s, High 37 s, Ultra 36 s. The same
  four scanned pages: 85 s, 8 chunks, Cyrillic 0.880, 0 garbled words, 5/5 and
  2/2 (Inferact on `r6`: 118 s, 0.886, 0, 5/5 and 2/2).
- **Memory at the switch.** On GB10 CUDA does not count the file cache as
  free: before the switch `torch.cuda.mem_get_info` reported 1.4 GiB free while
  the kernel's `MemAvailable` was 39 GiB, 38.9 GiB of it cache (mostly the
  freshly copied weights). The old model's 61 GiB would have left about 62 GiB
  for the 60.9 GiB vLLM reserves at 0.50, so the cache was dropped between
  stopping the old container and starting the new one, as in
  [COMPOSE.md](COMPOSE.md), *Troubleshooting*.
- **The installer** takes the repository and revision from the compose file:
  its tests pass 25 of 25 with the new pin, and for the new compose file it
  resolves `unsloth/Qwen3.8-27B-NVFP4` at `f0b7c9e7…`. The copied weights
  matched the hub's sha256 in all 11 files.
