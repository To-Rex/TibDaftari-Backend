"""The assistant's document and chart renderers — pure: Markdown-lite, PDF / Word / text, PNG / SVG charts."""

from __future__ import annotations

import pytest
from app.core.exceptions import ValidationError
from app.modules.assistant import export as xp
from app.modules.assistant import render as rd

MD = """# Hisobot

Bu **muhim** jumla va *oddiy* matn `kod` bilan, [havola](https://x.uz).

## Ro'yxat
- birinchi
- ikkinchi **qalin**

1. bir
2. ikki

| Nomi | Soni |
|---|---:|
| Bog‘ot SES | 12 |
| Shovot SES | 7 |

---
```
kod
```
"""


def test_markdown_lite_reads_the_structure() -> None:
    blocks = rd.parse_markdown(MD)
    assert [b.type for b in blocks] == ["h", "p", "h", "ul", "ol", "table", "hr", "code"]
    assert blocks[1].text.startswith("Bu **muhim**") and rd.plain(blocks[1].text) == "Bu muhim jumla va oddiy matn kod bilan, havola (https://x.uz)."
    assert blocks[3].items == ["birinchi", "ikkinchi **qalin**"] and blocks[4].items == ["bir", "ikki"]
    assert blocks[5].header == ["Nomi", "Soni"] and blocks[5].rows[0] == ["Bog‘ot SES", "12"]
    assert rd.bold_runs("a **b** c") == [("a ", False), ("b", True), (" c", False)]
    assert rd.fpdf_markdown("*x* **y** `z`") == "x **y** z"
    # a leading heading that repeats the title is not printed twice
    assert rd.body_blocks("Hisobot", MD)[0].type == "p"
    text = rd.to_text("Hisobot", rd.parse_markdown(MD))
    assert text.startswith("HISOBOT\n=======") and text.count("HISOBOT") == 1 and "• birinchi" in text and "Nomi\tSoni" in text


def test_documents_in_every_format() -> None:
    data, ct = xp.build_document("docx", "Hisobot", MD)
    assert data[:2] == b"PK" and "wordprocessingml" in ct
    data, ct = xp.build_document("pdf", "Hisobot", MD)
    assert data[:5] == b"%PDF-" and ct == "application/pdf"
    data, ct = xp.build_document("md", "Hisobot", MD)
    assert data.decode().startswith("# Hisobot") and ct.startswith("text/markdown")
    data, ct = xp.build_document("txt", "Hisobot", MD)
    assert data.decode().startswith("HISOBOT") and ct.startswith("text/plain")
    assert xp.norm_format("Word") == "docx" and xp.norm_format("Excel") == "xlsx" and xp.norm_format("rasm") == "png"
    with pytest.raises(ValidationError):
        xp.build_document("png", "x", MD)


def test_tables_as_pdf_word_markdown_text() -> None:
    cols, rows = xp.prepare([{"name": "Bog‘ot SES", "total": 1200000}, {"name": "Shovot", "total": 540000}], filters=None, sort=None, columns=[{"key": "name", "title": "Nomi"}, {"key": "total", "title": "Summa"}], limit=None)
    assert xp.build_file("pdf", "Cheklar", cols, rows, note="shu oy")[0][:5] == b"%PDF-"
    assert xp.build_file("docx", "Cheklar", cols, rows)[0][:2] == b"PK"
    md = xp.build_file("md", "Cheklar", cols, rows)[0].decode()
    assert "| Nomi | Summa |" in md and "| Bog‘ot SES | 1200000 |" in md
    txt = xp.build_file("txt", "Cheklar", cols, rows)[0].decode()
    assert "Nomi\tSumma" in txt
    assert xp.file_name(None, "Cheklar", "pdf", "2026-11-12") == "cheklar-2026-11-12.pdf"


def test_charts_png_and_svg() -> None:
    spec = rd.chart_spec({"type": "bar", "title": "Tushum", "labels": ["A", "B", "C"], "series": [{"name": "so‘m", "values": [1200000, "540 000", None]}]})
    assert spec.series[0].values == [1200000.0, 540000.0, None]
    png, ct = xp.build_chart("png", spec)
    assert png[:4] == b"\x89PNG" and ct == "image/png"
    svg, ct = xp.build_chart("svg", spec)
    assert svg.startswith(b"<svg ") and b"Tushum" in svg and ct == "image/svg+xml"
    pie = rd.chart_spec({"type": "pie", "title": "Ulush", "labels": ["A", "B"], "series": [{"name": "x", "values": [3, 1]}, {"name": "ignored", "values": [1, 1]}]})
    assert len(pie.series) == 1 and xp.build_chart("svg", pie)[0].count(b"<path") == 2
    line = rd.chart_spec({"type": "line", "title": "", "labels": [str(i) for i in range(20)], "series": [{"name": "y", "values": list(range(20))}, {"name": "z", "values": [i * 2 for i in range(20)]}]})
    assert xp.build_chart("png", line)[0][:4] == b"\x89PNG"
    with pytest.raises(ValidationError):
        rd.chart_spec({"type": "area", "labels": ["a"], "series": [{"values": [1]}]})
    with pytest.raises(ValidationError):
        rd.chart_spec({"type": "bar", "labels": ["a"], "series": [{"values": [None]}]})
    assert rd._short(1_250_000) == "1.2 mln" and rd._short(12_000) == "12 ming" and rd._short(950) == "950"
