#!/usr/bin/env python3
"""make_sample.py - render sample-ru.md to sample-ru.pdf (maintainers only).

  python3 acceptance/make_sample.py [--font-dir /usr/share/fonts/truetype/dejavu]

Needs reportlab and the DejaVu Sans fonts (Bitstream Vera licence, embedding
allowed). The output is reproducible: no timestamps, fixed document id.
The installer uses the committed PDF; run this only after editing the text.
"""
import argparse, os, re

from reportlab import rl_config
rl_config.invariant = 1                     # no creation date, fixed id: same input, same bytes

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--font-dir", default="/usr/share/fonts/truetype/dejavu")
    a = ap.parse_args()
    pdfmetrics.registerFont(TTFont("DejaVuSans", os.path.join(a.font_dir, "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", os.path.join(a.font_dir, "DejaVuSans-Bold.ttf")))

    body = ParagraphStyle("body", fontName="DejaVuSans", fontSize=11, leading=15, spaceAfter=6)
    h1 = ParagraphStyle("h1", fontName="DejaVuSans-Bold", fontSize=16, leading=20, spaceAfter=10)
    h2 = ParagraphStyle("h2", fontName="DejaVuSans-Bold", fontSize=13, leading=17, spaceBefore=8, spaceAfter=6)

    text = open(os.path.join(HERE, "sample-ru.md"), encoding="utf-8").read()
    story = []
    for block in re.split(r"\n\s*\n", text.strip()):
        line = " ".join(block.split())
        if line.startswith("## "):
            story.append(Paragraph(line[3:], h2))
        elif line.startswith("# "):
            story.append(Paragraph(line[2:], h1))
        else:
            story.append(Paragraph(line, body))
    story.append(Spacer(1, 4 * mm))

    out = os.path.join(HERE, "sample-ru.pdf")
    doc = SimpleDocTemplate(out, pagesize=A4, leftMargin=22 * mm, rightMargin=22 * mm,
                            topMargin=20 * mm, bottomMargin=20 * mm,
                            title="Памятка по эксплуатации сервера знаний «Северный»",
                            author="RAGSpark", subject="RAGSpark acceptance sample")
    doc.build(story)
    print("wrote", out)


if __name__ == "__main__":
    main()
