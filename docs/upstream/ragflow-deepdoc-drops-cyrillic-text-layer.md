# [Bug] DeepDOC drops a valid Cyrillic text layer and OCRs it into Latin look-alikes

*Draft for an issue in infiniflow/ragflow, branch `0.27.x`. Found by RAGSpark.*

**Version:** v0.27.2 (`a024bea0cd93f39e6652a42bf84dd20c55bc560b`), PDF parser
DeepDOC, Linux arm64 (CPU OCR).

## Summary

On Russian PDFs with a correct text layer, DeepDOC replaces whole pages of
text with OCR output, and the OCR model (CJK + Latin) renders Cyrillic as
Latin look-alikes: «Содержание» → `Conep>KaHne`, «Разъем» → `Pa3beM`,
«Установка» → `YcTaHOBKa`.

## Cause

`RAGFlowPdfParser.__images__()` runs two page-level checks and clears the
page's characters when either fires (`deepdoc/parser/pdf_parser.py`, around
lines 1634–1660). Strategy 2, `_is_garbled_by_font_encoding()`, fires when at
least 30% of characters come from subset fonts, less than 5% are CJK and more
than 40% are ASCII punctuation or symbols.

A table-of-contents page full of dot leaders (`Технические характеристики.....4`)
easily exceeds 40% ASCII punctuation. Its Cyrillic letters are not counted as
anything, so the page is declared garbled, its text layer is dropped and the
page is OCRed.

The box-level path already guards against this — the comment reads
"ocr.res is CJK+Latin, so re-OCRing e.g. a Cyrillic page only produces
garbage" (`_ocr_can_represent`) — but the page-level path has no such guard.

## Log

```
Page 1: detected font-encoding garbled text (subset fonts with no CJK output, 2840 chars), clearing to use OCR fallback.
Page 2: detected font-encoding garbled text (subset fonts with no CJK output, 1395 chars), clearing to use OCR fallback.
```

## Impact

39-page Russian motherboard manual (text layer verified with `pdftotext`):

| Parser | Share of Cyrillic letters | Garbled words |
|---|---|---|
| text layer (reference) | 0.835 | 0 |
| DeepDOC | 0.665 | 194 |
| Plain text ("Naive") | 0.835 | 0 |

Garbling also appears on content pages, in boxes that received no
characters from the text layer.

## Suggested fix

- Apply the `_ocr_can_represent` guard at page level: if the page's letters
  are mostly in scripts the OCR model cannot produce (e.g. Cyrillic), keep the
  text layer.
- Do not count runs of `.` (dot leaders) as punctuation in the ratio, or count
  letters of any script, not only CJK, as evidence of a valid text layer.
- Longer term: an OCR model with Cyrillic support, or documentation that
  DeepDOC OCR covers CJK and Latin only.
