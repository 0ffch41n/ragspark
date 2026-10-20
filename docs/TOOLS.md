# Tools

[Русская версия](TOOLS.ru.md)

Standalone Python scripts for setting up and checking a RAGSpark stack. They
need nothing beyond the Python standard library. Five of them run *inside*
the RAGFlow container: it can reach the vLLM services by name, has RAGFlow's
own password encryption, and already holds the superuser credentials and the
LLM settings in its environment (`compose/.env` and the LLM file).

The installer runs `ragflow_models.py` and `acceptance.py` itself. Run the
commands below from the `compose/` directory — `/opt/ragspark/compose` on an
installed machine, where the tools are in `/opt/ragspark/tools`.

## ragflow_models.py — register models in RAGFlow

Registers the LLM, embedding and rerank models served by vLLM in the
administrator's RAGFlow account and makes them the defaults. Idempotent: a
second run changes nothing.

```bash
# read-only check
sudo docker compose exec -T ragflow \
  sh -c 'cd /ragflow && PYTHONPATH=/ragflow .venv/bin/python - --check' < ../tools/ragflow_models.py
# apply
sudo docker compose exec -T ragflow \
  sh -c 'cd /ragflow && PYTHONPATH=/ragflow .venv/bin/python -' < ../tools/ragflow_models.py
```

The LLM entry comes from `RAGSPARK_LLM_NAME`, `RAGSPARK_LLM_CONTEXT` and
`RAGSPARK_LLM_TOOLS`, which the selected LLM file sets for the RAGFlow
service; `--spec FILE` replaces the whole list.

| Status | Meaning |
|---|---|
| `[OK]` | already as expected |
| `[TODO]` | would be changed (`--check` only) |
| `[SET]` | a default model was (re)assigned |
| `[FAIL]` | needs attention; exit code 1 |

Existing instances are never modified. If an instance points to a different
address, the script reports it and leaves it alone. A default that refers to a
deleted model (it happens after an instance is deleted and re-created) is
detected by model id and re-assigned.

## acceptance.py — acceptance test

Creates the knowledge base and the chat "RAGSpark — пример" ("RAGSpark
sample" with `--lang en`) with RAGSpark's settings, uploads the sample PDF,
waits for parsing and asks the chat the sample questions. Answers must contain
the expected facts, cite the document and contain no CJK characters; a
question the document does not answer must get the chat's "no answer" text.
The first question is asked again at the level Low (the agentic pipeline).
A knowledge base and chat with the same name are replaced; the new ones stay
as an example. The chat "Ассистент" ("Assistant"), without a knowledge base,
is created if it does not exist and checked with a simple question; an
existing one is left as it is. The conversations the test opened are deleted.
Details: [INSTALL.md](INSTALL.md), *Acceptance test*.

```bash
sudo ragspark acceptance              # on an installed machine
# by hand:
sudo docker compose cp ../acceptance/. ragflow:/tmp/ragspark-acceptance
sudo docker compose exec -T ragflow \
  sh -c 'cd /ragflow && PYTHONPATH=/ragflow .venv/bin/python - --lang ru' < ../tools/acceptance.py
```

| Option | Effect |
|---|---|
| `--lang ru\|en` | names, prompts and "no answer" text of the example |
| `--dir DIR` | directory with the sample files inside the container (default `/tmp/ragspark-acceptance`) |
| `--cleanup` | delete the example knowledge base and chat after the test (the assistant chat stays) |
| `--no-agentic` | skip the question at the level Low |
| `--assistant-only` | only create and check the assistant chat (the installer with `--skip-acceptance`) |

## rag_eval.py — parsing and retrieval quality

Read-only. For every document in a dataset it reports the chunk count, the
share of Cyrillic letters and the number of garbled-looking words (Latin
look-alikes such as `Pa3beM` produced by OCR that cannot read Cyrillic). Then
it runs a question set through the retrieval API with five setups and checks
whether a chunk containing the expected facts is found.

```bash
sudo docker compose exec -T ragflow \
  sh -c 'cd /ragflow && PYTHONPATH=/ragflow .venv/bin/python - --dataset test' < ../tools/rag_eval.py
```

| Option | Effect |
|---|---|
| `--dataset NAME` | dataset to examine (default `test`) |
| `--questions FILE` | JSON list of `{"q": ..., "expect": [...]}`; `"expect": null` marks a question with no answer in the documents |
| `-v` | show examples of garbled words |
| `--why` | for failed questions, re-run with threshold 0 and show where the answer ranks |
| `--probe` | for failed questions, score the answer chunk directly with the reranker in several forms |

Result cells: `#N` — rank of the first chunk with the expected facts;
`miss` — not in the top N; `none` — nothing above the threshold; `ok` — a
no-answer question correctly returned nothing; `LEAK` — a no-answer question
returned something (top score shown).

The built-in question set targets the Russian manual of the MSI MPG B550
Gaming Plus motherboard (39-page excerpt). The manual is not distributed with
RAGSpark; for other documents pass `--questions`.

## mode_eval.py — chat modes compared

Asks one chat the same questions at each reasoning level of the selector
under the chat input — 0 Naive, 1 Low, 2 Medium, 3 High, 4 Ultra (D19) —
through the API, and reports for every answer the time, the verdict and the
number of sources, then a summary per level. The conversations it opens are
deleted at the end.

```bash
# the example chat, the acceptance sample's questions, Naive, Low and Medium:
sudo docker compose exec -T ragflow \
  sh -c 'cd /ragflow && PYTHONPATH=/ragflow .venv/bin/python - --modes 0,1,2' < ../tools/mode_eval.py
# a temporary chat over the knowledge base "test", the MSI question set:
sudo docker compose exec -T ragflow \
  sh -c 'cd /ragflow && PYTHONPATH=/ragflow .venv/bin/python - --dataset test --set msi' < ../tools/mode_eval.py
```

| Option | Effect |
|---|---|
| `--chat NAME` | chat to ask (default "RAGSpark — пример") |
| `--dataset NAME` | instead, a temporary chat over this knowledge base with RAGSpark's settings, deleted afterwards |
| `--modes 0,1,2,3,4` | levels to run (default all five) |
| `--set sample\|msi` | built-in questions: 7 + 1 about the acceptance sample (default), or the stage 3.1 MSI set |
| `--questions FILE` | JSON list of `{"q": ..., "expect": [...]}` instead; an `expect` item can be a list of alternatives; `"expect": null` marks a question with no answer in the documents |
| `--timeout S` | seconds per answer (default 900) |
| `--keep-sessions` | keep the conversations |
| `--trace` | under each answer, the progress log the agentic levels show as "thinking" in the web interface (searches, passages kept, answer composition) |

Verdicts: `ok`; `miss` — expected facts missing; `no-src` — no sources;
`cjk` — CJK characters in the answer; for a question with no answer in the
documents `ok` is the chat's "no answer" text, `LEAK` — another answer with
sources, `LEAK-own` — another answer without sources; `TIMEOUT`, `ERROR`. The
summary counts `ok` among the questions with answers and among those without.
At Medium and above, expect 1–2 minutes per answer.

## model_probe.py — LLM language and speed check

Runs Russian prompts against the LLM in three modes (thinking on, thinking
off, thinking off with temperature 0.7) and reports speed and how many
answers contain CJK characters (language mixing).

```bash
sudo docker compose exec -T ragflow /ragflow/.venv/bin/python - < ../tools/model_probe.py
# any OpenAI-compatible server:
VLLM_URL=http://host:8000 MODEL=<served name> python3 tools/model_probe.py
```

## pin_images.py — pin images by digest

Runs on the host. For every `image:` line with a tag only, it writes the
registry digest of the local image (`name:tag@sha256:…`); pinned lines are
compared with the local image. Use it after changing a tag and pulling the
new image.

```bash
sudo python3 tools/pin_images.py compose/compose.yaml compose/llm/*.yaml          # pin
sudo python3 tools/pin_images.py --check compose/compose.yaml compose/llm/*.yaml  # report only
```
