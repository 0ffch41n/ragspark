# Tools

[Русская версия](TOOLS.ru.md)

Standalone Python scripts for setting up and checking a RAGSpark stack. They
need nothing beyond the Python standard library. Three of them run *inside*
the RAGFlow container: it can reach the vLLM services by name, has RAGFlow's
own password encryption, and already holds the superuser credentials and the
LLM settings in its environment (`compose/.env` and the LLM file).

Run the commands below from the `compose/` directory.

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
