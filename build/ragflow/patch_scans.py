#!/usr/bin/env python3
"""Fixes that let RAGFlow read scanned and Russian PDFs.

Found on RAGFlow 0.27.2 with Qwen 3.8 27B NVFP4 and Russian documents; the
measurements are in RAGSpark's docs/VALIDATION.md.

1. DeepDOC discarded the text layer of Russian pages. `_is_garbled_by_font_encoding`
   in deepdoc/parser/pdf_parser.py declares a page garbled when the subset-font
   share is at least 0.3 and

       cjk_ratio < 0.05 and punct_ratio > 0.4

   Cyrillic is counted in neither bucket, so on a Russian page `cjk_ratio` is
   always 0 and the verdict rests on punctuation alone - which a table of
   contents with dot leaders ("Содержание.........5") exceeds easily. The page's
   characters are then cleared and the page is recognised as an image by a
   model whose dictionary (rag/res/deepdoc/ocr.res, 6623 entries, 6277 CJK)
   holds one of the 33 lowercase Russian letters: "Содержание" came back as
   "Conep>KaHne". On the MSI manual pages 1-2 (69% and 85% dots) were cleared.
   The letters of the page's own script - Cyrillic and Greek - now count as
   text: at a share of 0.05 or more, the CJK threshold the function already
   uses, the text layer is kept. A page that really is garbled has no letters
   of any script and is unaffected.

2. Vision parsing was a coin toss. A dataset whose PDF parser is a vision model
   (a scan has no text layer, and DeepDOC cannot read Russian at all) sends
   every page through `describe_with_prompt` of rag/llm/cv_model.py, which for
   vLLM is `OpenAI_APICV`. It passes no sampling parameters and `extra_body` is
   None for this class, so vLLM applies the server default - for RAGSpark's LLM
   the chat sampling, temperature 0.7. The same scanned page gave five different
   transcriptions in five runs, and in one of three tries it replaced the LAN
   and audio tables with image placeholders, losing "Оранжевый - 1 Гбит/с".
   At temperature 0 the transcription was byte-identical every run, complete,
   without placeholders and at the same speed (~22 s a page). The server default
   cannot be lowered instead: it is shared with chat.
   `OpenAI_APICV` now sends, unless the variable is empty:
     temperature   RAGSPARK_VISION_TEMPERATURE, default 0
   Scope: every call of this class - page transcription, figure description,
   and, unused by RAGSpark, chat with images through a vision model. Chat with
   the LLM goes through rag/llm/chat_model.py and is not affected.

3. Tables were cut in half. `naive_merge` in rag/nlp/__init__.py splits every
   section on the delimiter, whose default includes the newline, so each row of
   a markdown table becomes a paragraph of its own; paragraphs are then packed
   greedily up to chunk_token_num. A table that sits on a budget boundary is
   torn, and its rows land in a chunk without the header row and the title. On
   the scanned MSI manual that lost the LAN indicator table: the chunk holding
   "| Мигает | ... | Оранжевый | 1 Гбит/c подключение |" carried neither "LAN"
   nor "скорость", and the reranker scored it 0.014 against "Что означает
   оранжевый индикатор скорости на порту LAN?" - at 512 and at 1024 tokens alike,
   as a larger budget only moves the cut. The same page with the table whole
   scores 0.544; the threshold needs about 0.11.
   Consecutive table rows (lines starting with "|") are now glued into one
   paragraph together with a markdown heading right before them, within one
   section; a glued table larger than chunk_token_num becomes its own chunk, as
   any oversize paragraph does. Text without table rows is chunked exactly as
   before. `naive_merge_with_images` (sections that carry images) is unchanged.

Usage: patch_scans.py <ragflow source root>     (idempotent; checks every
anchor before it writes anything)
"""
import os
import sys

PDF = os.path.join("deepdoc", "parser", "pdf_parser.py")
CV = os.path.join("rag", "llm", "cv_model.py")
NLP = os.path.join("rag", "nlp", "__init__.py")

# ── 1. Cyrillic counts as text ──────────────────────────────────────────────
COUNTERS_OLD = """        subset_font_count = 0
        total_non_space = 0
        ascii_punct_sym = 0
        cjk_like = 0
"""
COUNTERS_NEW = """        subset_font_count = 0
        total_non_space = 0
        ascii_punct_sym = 0
        cjk_like = 0
        script_like = 0  # ragspark: letters of the page's own script
"""

BUCKET_OLD = """            elif 0x21 <= cp <= 0x2F or 0x3A <= cp <= 0x40 or 0x5B <= cp <= 0x60 or 0x7B <= cp <= 0x7E:
                ascii_punct_sym += 1
"""
BUCKET_NEW = """            elif 0x0400 <= cp <= 0x052F or 0x0370 <= cp <= 0x03FF:
                script_like += 1  # ragspark: Cyrillic and Greek are text, not noise
            elif 0x21 <= cp <= 0x2F or 0x3A <= cp <= 0x40 or 0x5B <= cp <= 0x60 or 0x7B <= cp <= 0x7E:
                ascii_punct_sym += 1
"""

VERDICT_OLD = """        cjk_ratio = cjk_like / total_non_space
        punct_ratio = ascii_punct_sym / total_non_space
        if cjk_ratio < 0.05 and punct_ratio > 0.4:
            return True
"""
VERDICT_NEW = """        cjk_ratio = cjk_like / total_non_space
        punct_ratio = ascii_punct_sym / total_non_space
        if script_like / total_non_space >= 0.05:
            # ragspark: the page is written in a real script, so its text layer
            # is not font-encoding garbage; keep it and let the layout analysis
            # work on it instead of recognising the page as an image.
            return False
        if cjk_ratio < 0.05 and punct_ratio > 0.4:
            return True
"""

# ── 2. vision calls at a fixed temperature ──────────────────────────────────
VISION_OLD = """class OpenAI_APICV(GptV4):
    _FACTORY_NAME = ["VLLM", "OpenAI-API-Compatible"]

    def __init__(self, key, model_name, lang="Chinese", base_url="", **kwargs):
        if not base_url:
            raise ValueError("url cannot be None")
        self.base_url = ensure_v1(base_url)
        self.client = OpenAI(api_key=key, base_url=self.base_url)
        self.async_client = AsyncOpenAI(api_key=key, base_url=self.base_url)
        self.model_name = model_name.split("___")[0]
        self.lang = lang
        Base.__init__(self, **kwargs)
"""
VISION_NEW = """class OpenAI_APICV(GptV4):
    _FACTORY_NAME = ["VLLM", "OpenAI-API-Compatible"]

    def __init__(self, key, model_name, lang="Chinese", base_url="", **kwargs):
        if not base_url:
            raise ValueError("url cannot be None")
        self.base_url = ensure_v1(base_url)
        self.client = OpenAI(api_key=key, base_url=self.base_url)
        self.async_client = AsyncOpenAI(api_key=key, base_url=self.base_url)
        self.model_name = model_name.split("___")[0]
        self.lang = lang
        Base.__init__(self, **kwargs)
        # ragspark: vision calls at a fixed temperature - see build/ragflow/patch_scans.py
        # in RAGSpark. Without it vLLM samples transcriptions at the chat temperature.
        temperature = os.environ.get("RAGSPARK_VISION_TEMPERATURE", "0").strip()
        if temperature:
            self.extra_body = {"temperature": float(temperature)}
"""

# ── 3. markdown tables are never split ──────────────────────────────────────
TABLES_HELPER_OLD = r'''def naive_merge(sections: str | list, chunk_token_num=128, delimiter=DEFAULT_DELIMITER, overlapped_percent=0, strategy=MergeStrategy.OVER_CAP):
'''
TABLES_HELPER_NEW = r'''# ragspark: markdown tables are never split - see build/ragflow/patch_scans.py in RAGSpark
_RAGSPARK_TABLE_ROW = re.compile(r"\s*\|")
_RAGSPARK_HEADING = re.compile(r"\s*#{1,6}\s")


def _ragspark_atomic_tables(paragraphs):
    """Glue every run of markdown table rows, and a heading right before it, into one paragraph.

    The default delimiter includes the newline, so each table row is a paragraph of
    its own and the token budget may cut a table anywhere; its rows then land in a
    chunk without the header row and the title, and retrieval cannot match them. A
    glued run stays whole - one larger than chunk_token_num becomes its own chunk,
    like any oversize paragraph. Runs never cross a section (page) boundary, and
    paragraphs without table rows are returned unchanged.
    """
    out = []  # [text, pos, is_table]
    for text, pos in paragraphs:
        row = _RAGSPARK_TABLE_ROW.match(text.lstrip("\n")) is not None
        if row and out and out[-1][1] == pos and (out[-1][2] or _RAGSPARK_HEADING.match(out[-1][0].lstrip("\n"))):
            out[-1][0] += text
            out[-1][2] = True
            continue
        out.append([text, pos, row])
    return [(t, p) for t, p, _ in out]


def naive_merge(sections: str | list, chunk_token_num=128, delimiter=DEFAULT_DELIMITER, overlapped_percent=0, strategy=MergeStrategy.OVER_CAP):
'''

TABLES_CALL_OLD = r'''            paragraphs.append(("\n" + sub_sec, pos))

    groups = _merge_paragraph_groups([p[0] for p in paragraphs], chunk_token_num, strategy, num_tokens_from_string, overlapped_percent)
    cks = [_reconstruct_text_chunk(paragraphs, g) for g in groups]
'''
TABLES_CALL_NEW = r'''            paragraphs.append(("\n" + sub_sec, pos))

    paragraphs = _ragspark_atomic_tables(paragraphs)  # ragspark: markdown tables are never split
    groups = _merge_paragraph_groups([p[0] for p in paragraphs], chunk_token_num, strategy, num_tokens_from_string, overlapped_percent)
    cks = [_reconstruct_text_chunk(paragraphs, g) for g in groups]
'''

EDITS = [
    ("Cyrillic counts as text", PDF, "# ragspark: letters of the page's own script", [
        (COUNTERS_OLD, COUNTERS_NEW),
        (BUCKET_OLD, BUCKET_NEW),
        (VERDICT_OLD, VERDICT_NEW),
    ]),
    ("vision calls at a fixed temperature", CV, "# ragspark: vision calls at a fixed temperature", [
        (VISION_OLD, VISION_NEW),
    ]),
    ("markdown tables are never split", NLP, "# ragspark: markdown tables are never split", [
        (TABLES_HELPER_OLD, TABLES_HELPER_NEW),
        (TABLES_CALL_OLD, TABLES_CALL_NEW),
    ]),
]


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    root = sys.argv[1]
    texts, todo = {}, []
    for name, rel, mark, subs in EDITS:
        path = os.path.join(root, rel)
        if path not in texts:
            texts[path] = open(path, encoding="utf-8").read()
        if mark in texts[path]:
            print("patch_scans: %s - already applied" % name)
            continue
        for old, _ in subs:
            if texts[path].count(old) != 1:
                sys.exit("patch_scans: %s - expected code not found exactly once in %s; "
                         "RAGFlow version changed? Nothing was written." % (name, path))
        todo.append((name, path, subs))
    for name, path, subs in todo:
        for old, new in subs:
            texts[path] = texts[path].replace(old, new)
        print("patch_scans: %s" % name)
    for path in {p for _, p, _ in todo}:
        with open(path, "w", encoding="utf-8") as f:
            f.write(texts[path])


if __name__ == "__main__":
    main()
