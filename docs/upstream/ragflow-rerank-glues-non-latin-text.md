# [Bug] Reranker input glues non-Latin text into one word (`remove_redundant_spaces`)

*Draft for an issue in infiniflow/ragflow, branch `0.27.x`. Found by RAGSpark.*

**Version:** v0.27.2 (`a024bea0cd93f39e6652a42bf84dd20c55bc560b`), Python backend,
Elasticsearch document engine.

## Summary

`Dealer.rerank_by_model()` builds the cross-encoder input with
`remove_redundant_spaces(" ".join(tks))` (`rag/nlp/search.py`, around line 578).
The first regex in `remove_redundant_spaces()` (`common/string_utils.py`,
line 39) is

```python
re.sub(r"([^a-z0-9.,\)>]) +([^ ])", r"\1\2", txt, flags=re.IGNORECASE)
```

It removes every space that follows a character outside `[a-z0-9.,)>]`. For
CJK this is intended; for Cyrillic, Greek, Arabic, Hebrew, Armenian, Georgian
and other scripts that separate words with spaces, it deletes **all** spaces,
so the reranker receives a single long "word".

## Reproduction

```python
from common.string_utils import remove_redundant_spaces
remove_redundant_spaces("оранжевый 1 гбит с подключение таблица состояний индикатора порта lan")
# 'оранжевый1гбитсподключениетаблицасостоянийиндикаторапортаlan'
remove_redundant_spaces("the quick brown fox jumps over the lazy dog")
# 'the quick brown fox jumps over the lazy dog'
```

## Impact

Measured with `BAAI/bge-reranker-v2-m3` (served by vLLM) on a Russian
manual. Same chunk, scored directly against the reranker:

| Question | Input as RAGFlow builds it | Original chunk text |
|---|---|---|
| LAN LED colours | 0.33 | 0.46 |
| M.2 slot drive sizes | 0.37 | 0.64 |

Inside RAGFlow the scores were lower still (0.10 and 0.15), so with the
default similarity threshold 0.2 correct chunks returned nothing at all. With
the change below, 8 of 8 test questions were answered (vector weight 0.7,
threshold 0.1) while 2 of 2 questions with no answer in the documents
returned nothing.

The LLM is not affected (it receives `content_with_weight`). Two other callers
use the same helper: the chunk list highlight (`api/apps/restful_apis/chunk_api.py`)
and SQL result rows (`api/db/services/dialog_service.py`).

## Suggested fix

Give cross-encoders the original text — they are trained on natural text, not
on tokenizer output:

```python
docs = []
for i, tks in zip(sres.ids, ins_tw):
    fld = sres.field[i]
    parts = [str(fld.get("content_with_weight") or "")]
    for key in ("important_kwd", "question_kwd"):
        val = fld.get(key) or []
        parts.append("\n".join(map(str, val)) if isinstance(val, list) else str(val))
    text = "\n".join(p for p in parts if p.strip())
    docs.append(text or remove_redundant_spaces(" ".join(tks)))
```

Independently, `remove_redundant_spaces()` could restrict the first rule to
CJK characters instead of "anything that is not ASCII alphanumeric".
