"""Documents and pictures the assistant builds on request — the renderers behind `export.py`:

* a lightweight Markdown reader (headings, paragraphs, bullet / numbered lists, tables, code, rules, **bold**);
* documents → PDF (fpdf2, the renderer's Unicode fonts), Word (python-docx), Markdown, plain text;
* tables → PDF / Word (the spreadsheet formats live in `export.py`);
* charts (bar / line / pie) → PNG (Pillow, supersampled) or SVG.

Pure functions over plain data — no database, no request context."""

from __future__ import annotations

import io
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fpdf import FPDF
from fpdf.enums import XPos, YPos
from fpdf.fonts import FontFace
from PIL import Image, ImageDraw, ImageFont

from app.core.exceptions import ValidationError

FONT_DIR = Path(__file__).resolve().parents[1] / "templates" / "renderer" / "fonts"
FONT_REGULAR = FONT_DIR / "DejaVuSans.ttf"
FONT_BOLD = FONT_DIR / "DejaVuSans-Bold.ttf"
#: the brand palette the charts use
PALETTE = ["#0F766E", "#2563EB", "#D97706", "#DC2626", "#7C3AED", "#059669", "#DB2777", "#4B5563", "#0891B2", "#CA8A04"]
INK, INK_SOFT, GRID, PAPER = "#1F2937", "#6B7280", "#E5E7EB", "#FFFFFF"
CHART_W, CHART_H = 1600, 900


# ============================================================================= markdown-lite


@dataclass
class Block:
    type: str  # h | p | ul | ol | table | code | hr
    text: str = ""
    level: int = 0
    items: list[str] = field(default_factory=list)
    header: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)


_INLINE = [
    (re.compile(r"!\[([^\]]*)\]\([^)]*\)"), r"\1"),
    (re.compile(r"\[([^\]]+)\]\(([^)]+)\)"), r"\1 (\2)"),
    (re.compile(r"`([^`]*)`"), r"\1"),
    (re.compile(r"\*\*(.+?)\*\*"), r"\1"),
    (re.compile(r"__(.+?)__"), r"\1"),
    (re.compile(r"(?<!\w)\*(?!\s)(.+?)(?<!\s)\*(?!\w)"), r"\1"),
    (re.compile(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)"), r"\1"),
    (re.compile(r"~~(.+?)~~"), r"\1"),
]


def _inline(text: str) -> str:
    for rx, rep in _INLINE:
        text = rx.sub(rep, text)
    return text


def plain(text: str) -> str:
    """Inline Markdown stripped: what a cell or a plain-text file shows."""
    return _inline(text).strip()


def fpdf_markdown(text: str) -> str:
    """Only **bold** survives for fpdf2's markdown mode; the other inline marks are stripped."""
    return "".join(f"**{t}**" if b else t for t, b in bold_runs(text))


def body_blocks(title: str, markdown: str) -> list[Block]:
    """The document's blocks without a leading heading that merely repeats the title."""
    blocks = parse_markdown(markdown)
    if title and blocks and blocks[0].type == "h" and blocks[0].text.strip().lower() == title.strip().lower():
        return blocks[1:]
    return blocks


def bold_runs(text: str) -> list[tuple[str, bool]]:
    """`a **b** c` → [("a ", False), ("b", True), (" c", False)] with the other inline marks stripped."""
    out: list[tuple[str, bool]] = []
    for i, part in enumerate(re.split(r"\*\*(.+?)\*\*", text)):
        if part:
            out.append((_inline(part), i % 2 == 1))
    return out or [("", False)]


def _split_row(line: str) -> list[str]:
    cells = line.strip().strip("|").split("|")
    return [c.strip() for c in cells]


def parse_markdown(text: str) -> list[Block]:
    lines = (text or "").replace("\r\n", "\n").split("\n")
    blocks: list[Block] = []
    para: list[str] = []
    i = 0

    def flush() -> None:
        if para:
            blocks.append(Block("p", " ".join(s.strip() for s in para)))
            para.clear()

    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if not s:
            flush()
            i += 1
            continue
        if s.startswith("```"):
            flush()
            j = i + 1
            code: list[str] = []
            while j < len(lines) and not lines[j].strip().startswith("```"):
                code.append(lines[j])
                j += 1
            blocks.append(Block("code", "\n".join(code)))
            i = j + 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            flush()
            blocks.append(Block("h", plain(m.group(2)), level=len(m.group(1))))
            i += 1
            continue
        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", s):
            flush()
            blocks.append(Block("hr"))
            i += 1
            continue
        if s.startswith("|") and i + 1 < len(lines) and re.match(r"^\|?\s*:?-{2,}", lines[i + 1].strip()):
            flush()
            header = _split_row(s)
            j = i + 2
            rows: list[list[str]] = []
            while j < len(lines) and lines[j].strip().startswith("|"):
                r = _split_row(lines[j])
                rows.append((r + [""] * len(header))[: len(header)])
                j += 1
            blocks.append(Block("table", header=[plain(h) for h in header], rows=[[plain(c) for c in r] for r in rows]))
            i = j
            continue
        m = re.match(r"^([-*+]|\d+[.)])\s+(.*)$", s)
        if m:
            flush()
            ordered = m.group(1)[0].isdigit()
            items: list[str] = []
            j = i
            while j < len(lines):
                mm = re.match(r"^\s*([-*+]|\d+[.)])\s+(.*)$", lines[j])
                if not mm or (mm.group(1)[0].isdigit() != ordered):
                    break
                items.append(mm.group(2).strip())
                j += 1
            blocks.append(Block("ol" if ordered else "ul", items=items))
            i = j
            continue
        if s.startswith(">"):
            para.append(s.lstrip("> "))
            i += 1
            continue
        para.append(s)
        i += 1
    flush()
    return blocks


def to_text(title: str, blocks: list[Block]) -> str:
    out: list[str] = [title.upper(), "=" * min(len(title), 60), ""] if title else []
    if title and blocks and blocks[0].type == "h" and blocks[0].text.strip().lower() == title.strip().lower():
        blocks = blocks[1:]
    for b in blocks:
        if b.type == "h":
            out += ["", b.text.upper() if b.level <= 2 else b.text, ""]
        elif b.type == "p":
            out += [plain(b.text), ""]
        elif b.type in ("ul", "ol"):
            out += [f"{(str(n + 1) + '.') if b.type == 'ol' else '•'} {plain(it)}" for n, it in enumerate(b.items)] + [""]
        elif b.type == "table":
            out += ["\t".join(b.header)] + ["\t".join(r) for r in b.rows] + [""]
        elif b.type == "code":
            out += [b.text, ""]
        elif b.type == "hr":
            out += ["-" * 40, ""]
    return "\n".join(out).strip() + "\n"


def to_markdown(title: str, markdown: str) -> str:
    body = (markdown or "").strip()
    return (f"# {title}\n\n{body}\n" if title and not body.startswith("#") else body + "\n")


# ============================================================================= PDF


def _pdf(landscape: bool = False) -> FPDF:
    pdf = FPDF(orientation="L" if landscape else "P", unit="mm", format="A4")
    pdf.add_font("DejaVu", "", str(FONT_REGULAR))
    pdf.add_font("DejaVu", "B", str(FONT_BOLD))
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_margins(15, 15, 15)
    pdf.add_page()
    pdf.set_font("DejaVu", size=10)
    return pdf


def _pdf_title(pdf: FPDF, title: str) -> None:
    if title:
        pdf.set_font("DejaVu", "B", 16)
        pdf.multi_cell(0, 8, title, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(2)
        pdf.set_font("DejaVu", size=10)


def _pdf_table(pdf: FPDF, header: list[str], rows: list[list[Any]], *, numeric: list[bool] | None = None) -> None:
    if not header:
        return
    pdf.set_font("DejaVu", size=8 if len(header) > 6 else 9)
    heading = FontFace(emphasis="BOLD", fill_color=(227, 239, 234))
    # widths from content length, bounded
    lens = [max(len(str(h)), *(len(str(r[i])) for r in rows[:300] if i < len(r))) if rows else len(str(h)) for i, h in enumerate(header)]
    lens = [min(max(n, 4), 40) for n in lens]
    total = sum(lens) or 1
    widths = [max(8.0, pdf.epw * n / total) for n in lens]
    align = ["RIGHT" if numeric and numeric[i] else "LEFT" for i in range(len(header))]
    with pdf.table(headings_style=heading, col_widths=widths, text_align=align, line_height=pdf.font_size * 1.6, padding=1.2) as table:  # type: ignore[arg-type]
        table.row([str(h) for h in header])
        for r in rows:
            table.row([("" if v is None else _fmt_num(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v)) for v in r])
    pdf.set_font("DejaVu", size=10)


def _fmt_num(v: int | float) -> str:
    if isinstance(v, float) and not v.is_integer():
        return f"{v:,.2f}".replace(",", " ")
    return f"{int(v):,}".replace(",", " ")


def table_pdf(title: str, header: list[str], rows: list[list[Any]], *, numeric: list[bool] | None = None, note: str | None = None) -> bytes:
    pdf = _pdf(landscape=len(header) > 5)
    _pdf_title(pdf, title)
    if note:
        pdf.set_text_color(107, 114, 128)
        pdf.multi_cell(0, 5, note, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_text_color(0, 0, 0)
        pdf.ln(2)
    _pdf_table(pdf, header, rows, numeric=numeric)
    return bytes(pdf.output())


def document_pdf(title: str, markdown: str) -> bytes:
    pdf = _pdf()
    _pdf_title(pdf, title)
    for b in body_blocks(title, markdown):
        if b.type == "h":
            size = {1: 15, 2: 13, 3: 12}.get(b.level, 11)
            pdf.ln(2)
            pdf.set_font("DejaVu", "B", size)
            pdf.multi_cell(0, size * 0.55, b.text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_font("DejaVu", size=10)
            pdf.ln(1)
        elif b.type == "p":
            pdf.multi_cell(0, 5.5, fpdf_markdown(b.text), markdown=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(2)
        elif b.type in ("ul", "ol"):
            for n, it in enumerate(b.items):
                marker = f"{n + 1}." if b.type == "ol" else "•"
                x = pdf.get_x()
                pdf.cell(7, 5.5, marker)
                pdf.multi_cell(0, 5.5, fpdf_markdown(it), markdown=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                pdf.set_x(x)
            pdf.ln(2)
        elif b.type == "table":
            _pdf_table(pdf, b.header, [list(r) for r in b.rows])
            pdf.ln(3)
        elif b.type == "code":
            pdf.set_fill_color(243, 244, 246)
            pdf.multi_cell(0, 5, b.text, fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(2)
        elif b.type == "hr":
            y = pdf.get_y()
            pdf.set_draw_color(209, 213, 219)
            pdf.line(pdf.l_margin, y, pdf.l_margin + pdf.epw, y)
            pdf.ln(3)
    return bytes(pdf.output())


# ============================================================================= Word


def _docx():
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Arial"
    style.font.size = Pt(10.5)
    return doc


def _docx_runs(par, text: str) -> None:
    for chunk, bold in bold_runs(text):
        run = par.add_run(chunk)
        run.bold = bold


def _docx_table(doc, header: list[str], rows: list[list[Any]]) -> None:
    if not header:
        return
    table = doc.add_table(rows=1, cols=len(header))
    table.style = "Light Grid Accent 1"
    for i, h in enumerate(header):
        cell = table.rows[0].cells[i]
        cell.text = ""
        cell.paragraphs[0].add_run(str(h)).bold = True
    for r in rows:
        cells = table.add_row().cells
        for i in range(len(header)):
            v = r[i] if i < len(r) else ""
            cells[i].text = "" if v is None else _fmt_num(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v)


def table_docx(title: str, header: list[str], rows: list[list[Any]], *, note: str | None = None) -> bytes:
    doc = _docx()
    if title:
        doc.add_heading(title, level=1)
    if note:
        doc.add_paragraph(note)
    _docx_table(doc, header, rows)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def document_docx(title: str, markdown: str) -> bytes:
    doc = _docx()
    if title:
        doc.add_heading(title, level=0)
    for b in body_blocks(title, markdown):
        if b.type == "h":
            doc.add_heading(b.text, level=min(max(b.level, 1), 4))
        elif b.type == "p":
            _docx_runs(doc.add_paragraph(), b.text)
        elif b.type in ("ul", "ol"):
            for it in b.items:
                _docx_runs(doc.add_paragraph(style="List Number" if b.type == "ol" else "List Bullet"), it)
        elif b.type == "table":
            _docx_table(doc, b.header, [list(r) for r in b.rows])
            doc.add_paragraph()
        elif b.type == "code":
            p = doc.add_paragraph()
            run = p.add_run(b.text)
            run.font.name = "Courier New"
        elif b.type == "hr":
            doc.add_paragraph("―" * 30)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ============================================================================= charts


@dataclass
class Series:
    name: str
    values: list[float | None]


@dataclass
class ChartSpec:
    kind: str  # bar | line | pie
    title: str
    labels: list[str]
    series: list[Series]
    x_title: str = ""
    y_title: str = ""


def chart_spec(args: dict[str, Any]) -> ChartSpec:
    kind = str(args.get("type") or "bar").lower()
    if kind not in ("bar", "line", "pie"):
        raise ValidationError(f"Noma’lum diagramma turi: {kind}", code="chart_type", details={"types": ["bar", "line", "pie"]})
    labels = [str(x) for x in (args.get("labels") or [])]
    series_in = args.get("series") or []
    series: list[Series] = []
    for s in series_in:
        if not isinstance(s, dict):
            continue
        vals = [(_to_num(v)) for v in (s.get("values") or [])]
        series.append(Series(name=str(s.get("name") or f"Qator {len(series) + 1}"), values=(vals + [None] * len(labels))[: len(labels)]))
    if not labels or not series or not any(v is not None for s in series for v in s.values):
        raise ValidationError("Diagramma uchun labels va kamida bitta series (values) kerak", code="chart_data")
    if kind == "pie":
        series = series[:1]
    return ChartSpec(kind=kind, title=str(args.get("title") or ""), labels=labels, series=series[:8], x_title=str(args.get("xTitle") or ""), y_title=str(args.get("yTitle") or ""))


def _to_num(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(str(v).replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def _nice_max(v: float) -> float:
    if v <= 0:
        return 1.0
    exp = math.floor(math.log10(v))
    f = v / (10**exp)
    nice = 1 if f <= 1 else 2 if f <= 2 else 5 if f <= 5 else 10
    return nice * (10**exp)


def _ticks(vmax: float, n: int = 5) -> list[float]:
    top = _nice_max(vmax)
    step = top / n
    return [round(step * i, 6) for i in range(n + 1)]


def _short(v: float) -> str:
    a = abs(v)
    if a >= 1_000_000_000:
        return f"{v / 1_000_000_000:.1f} mlrd".replace(".0 ", " ")
    if a >= 1_000_000:
        return f"{v / 1_000_000:.1f} mln".replace(".0 ", " ")
    if a >= 10_000:
        return f"{v / 1000:.0f} ming"
    return _fmt_num(v) if float(v).is_integer() else f"{v:.1f}"


def _wrap(text: str, width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        if len(cur) + len(w) + 1 > width and cur:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines[:2] if lines else [""]


def chart_png(spec: ChartSpec) -> bytes:
    """A 1600×900 PNG drawn at 2× and downsampled — crisp lines and text without a plotting library."""
    k = 2
    cw, ch = CHART_W * k, CHART_H * k
    img = Image.new("RGB", (cw, ch), PAPER)
    d = ImageDraw.Draw(img)
    f_title = ImageFont.truetype(str(FONT_BOLD), 30 * k)
    f_lab = ImageFont.truetype(str(FONT_REGULAR), 18 * k)
    f_small = ImageFont.truetype(str(FONT_REGULAR), 15 * k)
    f_val = ImageFont.truetype(str(FONT_BOLD), 15 * k)
    if spec.title:
        d.text((60 * k, 36 * k), spec.title, fill=INK, font=f_title)
    legend_h = (34 * k) if (len(spec.series) > 1 or spec.kind == "pie") else 0
    left, right, top, bottom = 110 * k, 50 * k, 100 * k, 110 * k + legend_h
    if spec.kind == "pie":
        _pie(d, spec, cw, ch, top, f_lab, f_small)
    else:
        _axes_chart(d, spec, left, right, top, bottom, cw, ch, f_lab, f_small, f_val)
    out = img.resize((CHART_W, CHART_H), Image.LANCZOS)
    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _axes_chart(d: ImageDraw.ImageDraw, spec: ChartSpec, left: int, right: int, top: int, bottom: int, cw: int, ch: int, f_lab, f_small, f_val) -> None:
    k = 2
    x0, x1, y0, y1 = left, cw - right, top, ch - bottom
    vals = [v for s in spec.series for v in s.values if v is not None]
    vmax = max([*vals, 0.0])
    vmin = min([*vals, 0.0])
    ticks = _ticks(max(vmax, abs(vmin)))
    top_v = ticks[-1] if ticks else 1.0
    base_v = -top_v if vmin < 0 else 0.0

    def ypos(v: float) -> float:
        return y1 - (v - base_v) / (top_v - base_v) * (y1 - y0)

    for tv in ticks:
        for sign in ((1, -1) if vmin < 0 else (1,)):
            y = ypos(tv * sign)
            d.line([(x0, y), (x1, y)], fill=GRID, width=k)
            d.text((x0 - 12 * k, y), _short(tv * sign), fill=INK_SOFT, font=f_small, anchor="rm")
    d.line([(x0, ypos(0)), (x1, ypos(0))], fill=INK_SOFT, width=k)
    n = len(spec.labels)
    slot = (x1 - x0) / max(n, 1)
    if spec.kind == "bar":
        m = len(spec.series)
        bw = slot * 0.7 / m
        for si, s in enumerate(spec.series):
            col = _hex(PALETTE[si % len(PALETTE)])
            for i, v in enumerate(s.values):
                if v is None:
                    continue
                cx = x0 + slot * i + slot * 0.15 + bw * si
                y_a, y_b = ypos(v), ypos(0)
                d.rounded_rectangle([cx, min(y_a, y_b), cx + bw - 2 * k, max(y_a, y_b)], radius=4 * k, fill=col)
                if n * m <= 24:
                    d.text((cx + bw / 2, min(y_a, y_b) - 6 * k), _short(v), fill=INK, font=f_val, anchor="mb")
    else:
        for si, s in enumerate(spec.series):
            col = _hex(PALETTE[si % len(PALETTE)])
            pts = [(x0 + slot * i + slot / 2, ypos(v)) for i, v in enumerate(s.values) if v is not None]
            if len(pts) > 1:
                d.line(pts, fill=col, width=4 * k, joint="curve")
            for px, py in pts:
                d.ellipse([px - 6 * k, py - 6 * k, px + 6 * k, py + 6 * k], fill=PAPER, outline=col, width=3 * k)
            if n <= 16:
                for (px, py), v in zip(pts, [v for v in s.values if v is not None], strict=False):
                    d.text((px, py - 12 * k), _short(v), fill=INK, font=f_val, anchor="mb")
    width_chars = max(6, int(slot / (9 * k)))
    for i, lab in enumerate(spec.labels):
        lines = _wrap(lab, width_chars)
        for li, line in enumerate(lines):
            d.text((x0 + slot * i + slot / 2, y1 + 12 * k + li * 20 * k), line, fill=INK, font=f_small if n > 8 else f_lab, anchor="ma")
    if spec.y_title:
        d.text((x0 - 12 * k, y0 - 24 * k), spec.y_title, fill=INK_SOFT, font=f_small, anchor="rb")
    if spec.x_title:
        d.text(((x0 + x1) / 2, y1 + 60 * k), spec.x_title, fill=INK_SOFT, font=f_small, anchor="ma")
    if len(spec.series) > 1:
        _legend(d, [(s.name, PALETTE[i % len(PALETTE)]) for i, s in enumerate(spec.series)], x0, ch - 34 * k, f_small)


def _legend(d: ImageDraw.ImageDraw, items: list[tuple[str, str]], x: int, y: int, font) -> None:
    k = 2
    for name, col in items:
        d.rounded_rectangle([x, y - 8 * k, x + 16 * k, y + 8 * k], radius=3 * k, fill=_hex(col))
        d.text((x + 24 * k, y), name, fill=INK, font=font, anchor="lm")
        x += int(d.textlength(name, font=font)) + 56 * k


def _pie(d: ImageDraw.ImageDraw, spec: ChartSpec, cw: int, ch: int, top: int, f_lab, f_small) -> None:
    k = 2
    s = spec.series[0]
    pairs = [(lab, v) for lab, v in zip(spec.labels, s.values, strict=False) if v is not None and v > 0]
    total = sum(v for _, v in pairs) or 1.0
    r = min((ch - top - 80 * k) / 2, cw * 0.28)
    cx, cy = cw * 0.33, top + (ch - top) / 2 - 20 * k
    start = -90.0
    legend: list[tuple[str, str]] = []
    for i, (lab, v) in enumerate(pairs):
        ang = v / total * 360
        col = PALETTE[i % len(PALETTE)]
        d.pieslice([cx - r, cy - r, cx + r, cy + r], start, start + ang, fill=_hex(col), outline=PAPER, width=3 * k)
        mid = math.radians(start + ang / 2)
        if ang >= 14:
            d.text((cx + math.cos(mid) * r * 0.62, cy + math.sin(mid) * r * 0.62), f"{v / total * 100:.0f}%", fill=PAPER, font=f_lab, anchor="mm")
        legend.append((f"{lab} — {_short(v)} ({v / total * 100:.1f}%)", col))
        start += ang
    y = top + 20 * k
    x = int(cx + r + 70 * k)
    for name, col in legend[:14]:
        d.rounded_rectangle([x, y - 9 * k, x + 18 * k, y + 9 * k], radius=3 * k, fill=_hex(col))
        d.text((x + 28 * k, y), name, fill=INK, font=f_small, anchor="lm")
        y += 30 * k


def chart_svg(spec: ChartSpec) -> bytes:
    """The same chart as scalable SVG text — crisp at any size, no raster library needed."""
    cw, ch = CHART_W, CHART_H
    out: list[str] = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{cw}" height="{ch}" viewBox="0 0 {cw} {ch}" font-family="DejaVu Sans, Arial, sans-serif">', f'<rect width="{cw}" height="{ch}" fill="{PAPER}"/>']
    esc = lambda t: str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")  # noqa: E731
    if spec.title:
        out.append(f'<text x="60" y="58" font-size="30" font-weight="bold" fill="{INK}">{esc(spec.title)}</text>')
    if spec.kind == "pie":
        s = spec.series[0]
        pairs = [(lab, v) for lab, v in zip(spec.labels, s.values, strict=False) if v is not None and v > 0]
        total = sum(v for _, v in pairs) or 1.0
        cx, cy, r = cw * 0.33, ch * 0.55, min(ch * 0.36, cw * 0.28)
        start = -90.0
        ly = 130
        for i, (lab, v) in enumerate(pairs):
            ang = v / total * 360
            a0, a1 = math.radians(start), math.radians(start + ang)
            large = 1 if ang > 180 else 0
            x0, y0 = cx + r * math.cos(a0), cy + r * math.sin(a0)
            x1, y1 = cx + r * math.cos(a1), cy + r * math.sin(a1)
            col = PALETTE[i % len(PALETTE)]
            if ang >= 359.99:
                out.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{col}"/>')
            else:
                out.append(f'<path d="M{cx},{cy} L{x0:.1f},{y0:.1f} A{r},{r} 0 {large} 1 {x1:.1f},{y1:.1f} Z" fill="{col}" stroke="{PAPER}" stroke-width="3"/>')
            out.append(f'<rect x="{cx + r + 70}" y="{ly - 9}" width="18" height="18" rx="3" fill="{col}"/><text x="{cx + r + 98}" y="{ly + 5}" font-size="15" fill="{INK}">{esc(lab)} — {esc(_short(v))} ({v / total * 100:.1f}%)</text>')
            ly += 30
            start += ang
    else:
        left, right, top, bottom = 110, 50, 100, 110 + (34 if len(spec.series) > 1 else 0)
        x0, x1, y0, y1 = left, cw - right, top, ch - bottom
        vals = [v for s in spec.series for v in s.values if v is not None]
        vmax, vmin = max([*vals, 0.0]), min([*vals, 0.0])
        ticks = _ticks(max(vmax, abs(vmin)))
        top_v = ticks[-1] if ticks else 1.0
        base_v = -top_v if vmin < 0 else 0.0
        ypos = lambda v: y1 - (v - base_v) / (top_v - base_v) * (y1 - y0)  # noqa: E731
        for tv in ticks:
            for sign in ((1, -1) if vmin < 0 else (1,)):
                y = ypos(tv * sign)
                out.append(f'<line x1="{x0}" y1="{y:.1f}" x2="{x1}" y2="{y:.1f}" stroke="{GRID}"/><text x="{x0 - 12}" y="{y + 5:.1f}" font-size="15" text-anchor="end" fill="{INK_SOFT}">{esc(_short(tv * sign))}</text>')
        out.append(f'<line x1="{x0}" y1="{ypos(0):.1f}" x2="{x1}" y2="{ypos(0):.1f}" stroke="{INK_SOFT}"/>')
        n = len(spec.labels)
        slot = (x1 - x0) / max(n, 1)
        if spec.kind == "bar":
            m = len(spec.series)
            bw = slot * 0.7 / m
            for si, s in enumerate(spec.series):
                col = PALETTE[si % len(PALETTE)]
                for i, v in enumerate(s.values):
                    if v is None:
                        continue
                    cx = x0 + slot * i + slot * 0.15 + bw * si
                    ya, yb = ypos(v), ypos(0)
                    out.append(f'<rect x="{cx:.1f}" y="{min(ya, yb):.1f}" width="{max(bw - 2, 1):.1f}" height="{abs(yb - ya):.1f}" rx="4" fill="{col}"/>')
                    if n * m <= 24:
                        out.append(f'<text x="{cx + bw / 2:.1f}" y="{min(ya, yb) - 6:.1f}" font-size="15" font-weight="bold" text-anchor="middle" fill="{INK}">{esc(_short(v))}</text>')
        else:
            for si, s in enumerate(spec.series):
                col = PALETTE[si % len(PALETTE)]
                pts = [(x0 + slot * i + slot / 2, ypos(v)) for i, v in enumerate(s.values) if v is not None]
                if len(pts) > 1:
                    out.append(f'<polyline points="{" ".join(f"{px:.1f},{py:.1f}" for px, py in pts)}" fill="none" stroke="{col}" stroke-width="4" stroke-linejoin="round"/>')
                for px, py in pts:
                    out.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="6" fill="{PAPER}" stroke="{col}" stroke-width="3"/>')
        width_chars = max(6, int(slot / 9))
        for i, lab in enumerate(spec.labels):
            for li, line in enumerate(_wrap(lab, width_chars)):
                out.append(f'<text x="{x0 + slot * i + slot / 2:.1f}" y="{y1 + 26 + li * 20}" font-size="{15 if n > 8 else 18}" text-anchor="middle" fill="{INK}">{esc(line)}</text>')
        if len(spec.series) > 1:
            lx = x0
            for i, s in enumerate(spec.series):
                col = PALETTE[i % len(PALETTE)]
                out.append(f'<rect x="{lx}" y="{ch - 42}" width="16" height="16" rx="3" fill="{col}"/><text x="{lx + 24}" y="{ch - 29}" font-size="15" fill="{INK}">{esc(s.name)}</text>')
                lx += 24 + len(s.name) * 9 + 32
    out.append("</svg>")
    return "\n".join(out).encode("utf-8")
