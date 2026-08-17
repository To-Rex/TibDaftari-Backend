"""TemplateDoc + RenderContext → PDF bytes (fpdf2). Port of Clinic-Web `DocumentRenderer.tsx`.

Units: 1 user unit = 1 CSS px (fpdf `unit=0.75` → 1 px = 0.75 pt). Fonts: sans → Arial (DejaVu Sans
fallback glyphs), serif → Times New Roman, mono → Mono; weight ≥ 600 → Bold, italic → Italic.
Everything is clipped to the page and to each element box (single page, no pagination).
"""

from __future__ import annotations

import io
import logging
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fpdf import FPDF
from fpdf.enums import XPos, YPos

from app.modules.files.service import decode_data_url, image_size
from app.modules.templates.renderer import expressions as ex

log = logging.getLogger("app.templates.renderer")

AssetLoader = Callable[[str], tuple[bytes, str] | None]
"""assetId → (bytes, mime) or None when the asset is unknown."""

FONT_DIR = Path(__file__).parent / "fonts"
PX_TO_PT = 0.75
PAPER_PX: dict[str, tuple[float, float]] = {"A4": (794, 1123), "A5": (559, 794), "Letter": (816, 1056)}
ABN = "#c2413f"
ROW_NUMBER_W = 28.0
DEFAULT_STYLE: dict[str, Any] = {
    "fontFamily": "sans",
    "fontSize": 13,
    "fontWeight": 400,
    "color": "#14201d",
    "align": "left",
    "vAlign": "top",
    "lineHeight": 1.35,
}
FONT_FILES: dict[str, dict[str, str]] = {
    "sans": {"": "Arial.ttf", "B": "Arial-Bold.ttf", "I": "Arial-Italic.ttf", "BI": "Arial-BoldItalic.ttf"},
    "serif": {"": "TimesNewRoman.ttf", "B": "TimesNewRoman-Bold.ttf", "I": "TimesNewRoman-Italic.ttf", "BI": "TimesNewRoman-BoldItalic.ttf"},
    "mono": {"": "Mono.ttf", "B": "Mono-Bold.ttf", "I": "Mono-Italic.ttf", "BI": "Mono-BoldItalic.ttf"},
    "fallbacksans": {"": "DejaVuSans.ttf", "B": "DejaVuSans-Bold.ttf"},
    "fallbackserif": {"": "DejaVuSerif.ttf", "B": "DejaVuSerif-Bold.ttf"},
}
NAMED_COLORS: dict[str, tuple[int, int, int]] = {
    "black": (0, 0, 0),
    "white": (255, 255, 255),
    "red": (255, 0, 0),
    "green": (0, 128, 0),
    "blue": (0, 0, 255),
    "gray": (128, 128, 128),
    "grey": (128, 128, 128),
    "yellow": (255, 255, 0),
}
_RGB_RE = re.compile(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)")
_TOKEN_RE = re.compile(r"\S+|\s+")

RGB = tuple[int, int, int]


def paper_size(doc: dict[str, Any]) -> tuple[float, float]:
    """Page size in px for paper + orientation (unknown paper → A4)."""
    w, h = PAPER_PX.get(str(doc.get("paper") or "A4"), PAPER_PX["A4"])
    return (h, w) if doc.get("orientation") == "landscape" else (w, h)


def parse_color(value: Any) -> RGB | None:
    """CSS colour → (r, g, b); None = transparent / unparsable."""
    if not value or not isinstance(value, str):
        return None
    s = value.strip().lower()
    if s in ("transparent", "none"):
        return None
    if s.startswith("#"):
        hx = s[1:]
        if len(hx) in (3, 4):
            hx = "".join(ch * 2 for ch in hx[:3])
        if len(hx) in (6, 8):
            try:
                return int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16)
            except ValueError:
                return None
        return None
    m = _RGB_RE.match(s)
    if m:
        return tuple(min(255, int(m.group(i))) for i in (1, 2, 3))  # type: ignore[return-value]
    return NAMED_COLORS.get(s)


def blend(color: RGB, alpha: float, base: RGB = (255, 255, 255)) -> RGB:
    """`opacity: alpha` over white — used for the muted unit/reference spans."""
    return tuple(round(c * alpha + b * (1 - alpha)) for c, b in zip(color, base, strict=True))  # type: ignore[return-value]


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _style(raw: Any) -> dict[str, Any]:
    st = dict(DEFAULT_STYLE)
    if isinstance(raw, dict):
        st.update({k: v for k, v in raw.items() if v is not None})
    return st


class _Renderer:
    """One PDF page for one document (stateful: fonts registered lazily per family/style)."""

    def __init__(self, doc: dict[str, Any], ctx: dict[str, Any], loader: AssetLoader | None) -> None:
        self.doc = doc
        self.ctx = ctx
        self.loader = loader
        self.page_w, self.page_h = paper_size(doc)
        pdf = FPDF(unit=PX_TO_PT, format=(self.page_w, self.page_h))
        pdf.set_auto_page_break(False)
        pdf.set_margins(0, 0, 0)
        pdf.c_margin = 0
        pdf.set_compression(True)
        pdf.set_title("Natija")
        pdf.add_page()
        self.pdf = pdf
        self._fonts: set[str] = set()
        self._font_add("fallbacksans", "")
        self._font_add("fallbackserif", "")
        pdf.set_fallback_fonts(["fallbacksans", "fallbackserif"], exact_match=False)

    @contextmanager
    def clip(self, x: float, y: float, w: float, h: float) -> Iterator[None]:
        """Rectangular clip. Wrapped in `local_context` so fpdf's tracked graphics state (font, colours,
        line width…) is restored together with the PDF `Q` — a bare `rect_clip` would desynchronise them."""
        with self.pdf.local_context(), self.pdf.rect_clip(x, y, max(0.0, w), max(0.0, h)):
            yield

    # ------------------------------------------------------------------ fonts & text metrics

    def _font_add(self, family: str, style: str) -> None:
        key = family + style
        if key in self._fonts:
            return
        self.pdf.add_font(family, style, FONT_DIR / FONT_FILES[family][style])
        self._fonts.add(key)

    def set_font(self, st: dict[str, Any], *, weight: int | None = None, color: RGB | None = None) -> None:
        """Select font family/style/size/colour/letter-spacing for a TextStyle dict."""
        family = st.get("fontFamily") if st.get("fontFamily") in FONT_FILES else "sans"
        w = weight if weight is not None else int(_num(st.get("fontWeight"), 400))
        style = ("B" if w >= 600 else "") + ("I" if st.get("italic") else "")
        self._font_add(family, style)
        self.pdf.set_font(family, style + ("U" if st.get("underline") else ""), _num(st.get("fontSize"), 13) * PX_TO_PT)
        self.pdf.set_char_spacing(_num(st.get("letterSpacing")) * PX_TO_PT)
        rgb = color if color is not None else (parse_color(st.get("color")) or (0, 0, 0))
        self.pdf.set_text_color(*rgb)

    def sw(self, s: str) -> float:
        return self.pdf.get_string_width(s) if s else 0.0

    def metrics(self, st: dict[str, Any]) -> tuple[float, float, float]:
        """(fontSize px, line height px, first-baseline offset from line top) — CSS half-leading model."""
        fs = _num(st.get("fontSize"), 13)
        lh = fs * _num(st.get("lineHeight"), 1.35)
        font = self.pdf.current_font
        asc = _num(getattr(getattr(font, "desc", None), "ascent", None), 905) / 1000
        desc = abs(_num(getattr(getattr(font, "desc", None), "descent", None), -212)) / 1000
        baseline = (lh - (asc + desc) * fs) / 2 + asc * fs
        return fs, lh, baseline

    def put(self, x: float, baseline: float, s: str) -> None:
        """Draw a text run with its origin at (x, baseline) — via `cell` so glyph fallback applies."""
        if not s:
            return
        pdf = self.pdf
        pdf.set_xy(x, baseline - 0.3 * pdf.font_size)
        pdf.cell(w=max(0.01, self.sw(s)), h=0, text=s, new_x=XPos.LEFT, new_y=YPos.TOP)

    def wrap(self, text: str, width: float) -> list[tuple[str, bool]]:
        """CSS pre-wrap + break-word: → [(line, ends_paragraph)]."""
        lines: list[tuple[str, bool]] = []
        eps = 0.01
        for para in (text or "").split("\n"):
            cur, cur_w = "", 0.0
            for tok in _TOKEN_RE.findall(para):
                tw = self.sw(tok)
                if tok.isspace() or cur_w + tw <= width + eps:
                    cur, cur_w = cur + tok, cur_w + tw
                    continue
                if cur.strip():
                    lines.append((cur, False))
                    cur, cur_w = "", 0.0
                if tw <= width + eps:
                    cur, cur_w = tok, tw
                    continue
                for ch in tok:  # word longer than the box: break anywhere
                    cw = self.sw(ch)
                    if cur and cur_w + cw > width + eps:
                        lines.append((cur, False))
                        cur, cur_w = "", 0.0
                    cur, cur_w = cur + ch, cur_w + cw
            lines.append((cur, True))
        return lines

    def draw_line(self, x: float, width: float, baseline: float, line: str, align: str, justify: bool) -> None:
        """One laid-out line inside [x, x+width] honouring align (justify only when `justify`)."""
        vis = line.rstrip()
        if not vis:
            return
        if align == "justify" and justify:
            words = vis.split(" ")
            gaps = len(words) - 1
            if gaps > 0:
                text_w = sum(self.sw(w) for w in words)
                space = self.sw(" ")
                extra = max(0.0, (width - text_w - gaps * space) / gaps)
                cx = x
                for w in words:
                    self.put(cx, baseline, w)
                    cx += self.sw(w) + space + extra
                return
        tw = self.sw(vis)
        dx = (width - tw) / 2 if align == "center" else (width - tw) if align == "right" else 0.0
        # CSS: an overflowing LTR line box spills to the end (right) side, never before the box start.
        self.put(x + max(0.0, dx), baseline, vis)

    def text_block(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        text: str,
        st: dict[str, Any],
        *,
        single_line: bool = False,
        v_align: str | None = None,
        weight: int | None = None,
        color: RGB | None = None,
    ) -> float:
        """Lay out `text` in the content box (x, y, w, h) per TextStyle; returns the block height."""
        self.set_font(st, weight=weight, color=color)
        _fs, lh, base = self.metrics(st)
        if single_line:
            lines = [(text.replace("\n", " "), True)]
        else:
            lines = self.wrap(text, w)
        block_h = len(lines) * lh
        va = v_align if v_align is not None else st.get("vAlign") or "top"
        off = (h - block_h) / 2 if va == "middle" else (h - block_h) if va == "bottom" else 0.0
        align = str(st.get("align") or "left")
        for i, (line, ends_para) in enumerate(lines):
            self.draw_line(x, w, y + off + i * lh + base, line, align, justify=not ends_para)
        return block_h

    # ------------------------------------------------------------------ shapes

    def fill_rect(self, x: float, y: float, w: float, h: float, color: RGB | None, radius: float = 0.0) -> None:
        if color is None or w <= 0 or h <= 0:
            return
        self.pdf.set_fill_color(*color)
        if radius > 0:
            self.pdf.rect(x, y, w, h, style="F", round_corners=True, corner_radius=min(radius, w / 2, h / 2))
        else:
            self.pdf.rect(x, y, w, h, style="F")

    def stroke_rect(self, x: float, y: float, w: float, h: float, color: RGB, sw: float, radius: float = 0.0, ellipse: bool = False) -> None:
        """Border drawn inside the box (inset by sw/2)."""
        if sw <= 0:
            return
        pdf = self.pdf
        pdf.set_draw_color(*color)
        pdf.set_line_width(sw)
        ix, iy, iw, ih = x + sw / 2, y + sw / 2, max(0.0, w - sw), max(0.0, h - sw)
        if ellipse:
            pdf.ellipse(ix, iy, iw, ih, style="D")
        elif radius > 0:
            pdf.rect(ix, iy, iw, ih, style="D", round_corners=True, corner_radius=max(0.0, min(radius - sw / 2, iw / 2, ih / 2)))
        else:
            pdf.rect(ix, iy, iw, ih, style="D")

    def hline(self, x1: float, x2: float, y: float, sw: float, color: RGB, dashed: bool = False) -> None:
        pdf = self.pdf
        pdf.set_draw_color(*color)
        pdf.set_line_width(sw)
        if dashed:
            pdf.set_dash_pattern(dash=2 * sw, gap=sw)
        pdf.line(x1, y, x2, y)
        if dashed:
            pdf.set_dash_pattern()

    def vline(self, x: float, y1: float, y2: float, sw: float, color: RGB, dashed: bool = False) -> None:
        pdf = self.pdf
        pdf.set_draw_color(*color)
        pdf.set_line_width(sw)
        if dashed:
            pdf.set_dash_pattern(dash=2 * sw, gap=sw)
        pdf.line(x, y1, x, y2)
        if dashed:
            pdf.set_dash_pattern()

    # ------------------------------------------------------------------ elements

    def render(self) -> bytes:
        bg = parse_color(self.doc.get("background")) or (255, 255, 255)
        self.fill_rect(0, 0, self.page_w, self.page_h, bg)
        for el in self.doc.get("elements") or []:
            if not isinstance(el, dict) or el.get("hidden"):
                continue
            clones: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
            rep = el.get("repeat")
            if isinstance(rep, dict) and rep.get("fieldKey"):
                step = _num(rep.get("step"))
                for i, row in enumerate(ex.table_rows(self.ctx, str(rep["fieldKey"]))):
                    clones.append(({**el, "y": _num(el.get("y")) + i * step}, {**row, "__i": i + 1}))
            else:
                clones.append((el, None))
            for clone, row in clones:
                if not ex.show_if(clone.get("showIf"), self.ctx, row):
                    continue
                try:
                    self.element(clone, row)
                except Exception:  # one broken element must not kill the document
                    log.exception("template element %s (%s) failed to render", clone.get("id"), clone.get("type"))
        return bytes(self.pdf.output())

    def element(self, el: dict[str, Any], row: dict[str, Any] | None) -> None:
        pdf = self.pdf
        x, y, w, h = _num(el.get("x")), _num(el.get("y")), _num(el.get("w")), _num(el.get("h"))
        opacity = min(1.0, max(0.0, _num(el.get("opacity"), 1.0)))
        rotation = _num(el.get("rotation"))
        with pdf.local_context(fill_opacity=opacity, stroke_opacity=opacity), self.clip(0, 0, self.page_w, self.page_h):
            if rotation:
                with pdf.rotation(-rotation, x + w / 2, y + h / 2):
                    self._dispatch(el, row, x, y, w, h)
            else:
                self._dispatch(el, row, x, y, w, h)

    def _dispatch(self, el: dict[str, Any], row: dict[str, Any] | None, x: float, y: float, w: float, h: float) -> None:
        kind = el.get("type")
        if kind == "text":
            self.el_text(el, row, x, y, w, h)
        elif kind == "field":
            self.el_field(el, x, y, w, h)
        elif kind in ("rect", "ellipse"):
            self.el_rect(el, x, y, w, h, ellipse=kind == "ellipse")
        elif kind == "line":
            self.el_line(el, x, y, w, h)
        elif kind == "image":
            self.el_image(el, x, y, w, h)
        elif kind == "table":
            self.el_table(el, x, y, w, h)

    def el_text(self, el: dict[str, Any], row: dict[str, Any] | None, x: float, y: float, w: float, h: float) -> None:
        st = _style(el.get("style"))
        raw = str(el.get("text") or "")
        pad = _num(el.get("padding"))
        fs = _num(st.get("fontSize"), 13)
        lh = fs * _num(st.get("lineHeight"), 1.35)
        single = h < lh * 2 and "\n" not in raw
        text = ex.interpolate(raw, self.ctx, row)
        with self.clip(x, y, w, h):
            self.fill_rect(x, y, w, h, parse_color(st.get("background")))
            self.text_block(x + pad, y + pad, max(0.0, w - 2 * pad), max(0.0, h - 2 * pad), text, st, single_line=single)

    def el_field(self, el: dict[str, Any], x: float, y: float, w: float, h: float) -> None:
        st = _style(el.get("style"))
        key = str(el.get("fieldKey") or "")
        fdef = ex.field_def(self.ctx.get("schema"), key)
        label = str((fdef or {}).get("label") or key)
        value = ex.format_value(self.ctx, key)
        unit = ex.field_unit(self.ctx, key) if el.get("showUnit") else ""
        ref = ex.field_reference(self.ctx, key) if el.get("showReference") else ""
        flag = ex.field_flag(self.ctx, key) if el.get("highlightAbnormal") else "unknown"
        abnormal = flag in ("abnormal", "critical")
        color = parse_color(st.get("color")) or (0, 0, 0)
        muted = blend(color, 0.7)
        with self.clip(x, y, w, h):
            self.fill_rect(x, y, w, h, parse_color(st.get("background")))
            self.set_font(st)
            _fs, _lh, base = self.metrics(st)
            baseline = y + base
            if fdef and fdef.get("type") == "table":
                n = len(ex.table_rows(self.ctx, key))
                cx = x
                if el.get("showLabel"):
                    self.set_font(st, weight=600)
                    self.put(cx, baseline, label + ": ")
                    cx += self.sw(label + ": ")
                    self.set_font(st)
                self.put(cx, baseline, f"{n} qator")
                return
            gap = 8.0
            value_text = (value or "—") + (" ‼" if flag == "critical" else " *" if abnormal else "")
            self.set_font(st, weight=600)
            value_w = self.sw(value_text)
            self.set_font(st)
            unit_w = max(60.0, self.sw(unit)) if unit else 0.0
            ref_w = max(90.0, self.sw(ref)) if el.get("showReference") else 0.0
            spans = 1 + (1 if unit else 0) + (1 if el.get("showReference") else 0) + (1 if el.get("showLabel") else 0)
            gaps = gap * max(0, spans - 1)
            cx = x
            if el.get("showLabel"):
                label_w = max(0.0, w - value_w - unit_w - ref_w - gaps)
                self.text_block(cx, y, label_w, h, label, {**st, "align": "left"}, v_align="top")
                cx += label_w + gap
            self.set_font(st, weight=600, color=parse_color(ABN) if abnormal else None)
            self.put(cx, baseline, value_text)
            cx += value_w + gap
            if unit:
                self.set_font(st, color=muted)
                self.put(cx, baseline, unit)
                cx += unit_w + gap
            if el.get("showReference"):
                self.set_font(st, color=muted)
                self.put(cx + ref_w - self.sw(ref), baseline, ref)

    def el_rect(self, el: dict[str, Any], x: float, y: float, w: float, h: float, *, ellipse: bool) -> None:
        fill = parse_color(el.get("fill"))
        stroke = parse_color(el.get("stroke"))
        sw = _num(el.get("strokeWidth"), 1.0) if el.get("stroke") else 0.0
        radius = 0.0 if ellipse else _num(el.get("radius"))
        if fill is not None:
            if ellipse:
                self.pdf.set_fill_color(*fill)
                self.pdf.ellipse(x, y, w, h, style="F")
            else:
                self.fill_rect(x, y, w, h, fill, radius)
        if stroke is not None and sw > 0:
            self.stroke_rect(x, y, w, h, stroke, sw, radius, ellipse=ellipse)

    def el_line(self, el: dict[str, Any], x: float, y: float, w: float, h: float) -> None:
        color = parse_color(el.get("stroke")) or (0, 0, 0)
        sw = _num(el.get("strokeWidth"), 1.0)
        dashed = bool(el.get("dashed"))
        if sw <= 0:
            return
        if el.get("orientation") == "vertical":
            self.vline(x + sw / 2, y, y + h, sw, color, dashed)
        else:
            self.hline(x, x + w, y + sw / 2, sw, color, dashed)

    def _image_bytes(self, el: dict[str, Any]) -> tuple[bytes, str] | None:
        src = el.get("src")
        if isinstance(src, str) and src.strip():
            if src.strip().startswith("data:"):
                try:
                    return decode_data_url(src)
                except Exception:
                    return None
            if self.loader:
                return self.loader(src)
            return None
        asset_id = el.get("assetId")
        if asset_id and self.loader:
            return self.loader(str(asset_id))
        return None

    def el_image(self, el: dict[str, Any], x: float, y: float, w: float, h: float) -> None:
        loaded = self._image_bytes(el)
        if loaded and w > 0 and h > 0:
            data, mime = loaded
            size = image_size(data, mime)
            fit = el.get("fit") or "contain"
            dx, dy, dw, dh = x, y, w, h
            if size and size[0] > 0 and size[1] > 0 and fit in ("contain", "cover"):
                iw, ih = size
                scale = min(w / iw, h / ih) if fit == "contain" else max(w / iw, h / ih)
                dw, dh = iw * scale, ih * scale
                dx, dy = x + (w - dw) / 2, y + (h - dh) / 2
            try:
                with self.clip(x, y, w, h):
                    self.pdf.image(io.BytesIO(data), dx, dy, dw, dh)
                return
            except Exception:
                log.warning("image %s could not be embedded (%s); drawing placeholder", el.get("id"), mime)
        self.image_placeholder(x, y, w, h)

    def image_placeholder(self, x: float, y: float, w: float, h: float) -> None:
        grey = (153, 153, 153)
        pdf = self.pdf
        pdf.set_draw_color(*grey)
        pdf.set_line_width(1)
        pdf.set_dash_pattern(dash=3, gap=3)
        pdf.rect(x + 0.5, y + 0.5, max(0.0, w - 1), max(0.0, h - 1), style="D")
        pdf.set_dash_pattern()
        st = {**DEFAULT_STYLE, "fontSize": 10, "color": "#999999", "align": "center"}
        self.text_block(x, y, w, h, "image", st, single_line=True, v_align="middle")

    # ------------------------------------------------------------------ table

    def el_table(self, el: dict[str, Any], x: float, y: float, w: float, h: float) -> None:
        cols_def = [c for c in (el.get("columns") or []) if isinstance(c, dict)]
        fkey = str(el.get("fieldKey") or "")
        fdef = ex.field_def(self.ctx.get("schema"), fkey) if fkey else None
        schema_cols = {c.get("key"): c for c in ((fdef or {}).get("columns") or []) if isinstance(c, dict)} if fdef and fdef.get("type") == "table" else {}
        if fkey:
            rows = ex.table_rows(self.ctx, fkey)
        else:
            rows = [{(c.get("bind") or str(i)): (r[i] if i < len(r) else "") for i, c in enumerate(cols_def)} for r in (el.get("staticRows") or []) if isinstance(r, list)]
        head_st = _style(el.get("headerStyle"))
        cell_st = _style(el.get("cellStyle"))
        row_h_min = _num(el.get("rowHeight"), 22)
        bw = _num(el.get("borderWidth"), 1)
        bcolor = parse_color(el.get("borderColor")) or (195, 206, 201)
        zebra = parse_color(el.get("zebra"))
        show_num = bool(el.get("showRowNumber"))
        highlight = bool(el.get("highlightAbnormal"))
        total_w = sum(_num(c.get("width")) for c in cols_def) or 1.0
        num_w = ROW_NUMBER_W if show_num else 0.0
        avail = max(0.0, w - num_w)
        widths = [_num(c.get("width")) / total_w * avail for c in cols_def]

        def fmt_cell(r: dict[str, Any], bind: str) -> tuple[str, bool]:
            v = r.get(bind)
            col = schema_cols.get(bind)
            if v is None or v == "":
                return "", False
            ctype = (col or {}).get("type")
            if ctype == "select":
                o = ex.option_of(col.get("options"), v)
                return (str(o.get("label")) if o and o.get("label") is not None else ex.fmt(v)), bool(o and o.get("flag") in ("abnormal", "critical"))
            if ctype == "multiselect" and isinstance(v, list):
                return ", ".join(ex.option_label(col.get("options"), item) or ex.fmt(item) for item in v), False
            if ctype == "boolean":
                return (str(col.get("trueLabel") or "✓") if v else str(col.get("falseLabel") or "—")), False
            if ctype == "number" and ex.is_numeric(v):
                refs = col.get("references") or []
                ref = refs[0] if refs else None
                ab = bool(ref and ((ref.get("min") is not None and v < ref["min"]) or (ref.get("max") is not None and v > ref["max"])))
                return ex.js_number(v), ab
            return (", ".join(ex.fmt(i) for i in v) if isinstance(v, list) else ex.fmt(v)), False

        # --- layout: compute row heights first (text wraps grow rows), then draw within the clip
        def row_height(cells: list[tuple[str, str]], st: dict[str, Any], pad_v: float, weights: list[int | None]) -> tuple[float, list[list[tuple[str, bool]]]]:
            self.set_font(st)
            _, lh, _ = self.metrics(st)
            wrapped: list[list[tuple[str, bool]]] = []
            best = row_h_min
            for (text, _align), cw, wt in zip(cells, widths, weights, strict=False):
                self.set_font(st, weight=wt)
                lines = self.wrap(text, max(0.0, cw - 12)) if text else [("", True)]
                wrapped.append(lines)
                best = max(best, len(lines) * lh + 2 * pad_v)
            return best, wrapped

        with self.clip(x, y, w, h):
            cy = y
            boundaries: list[float] = [y]
            if el.get("showHeader"):
                cells = [(str(c.get("header") or ""), str(c.get("align") or "left")) for c in cols_def]
                rh, wrapped = row_height(cells, head_st, 4.0, [None] * len(cells))
                self.draw_row(x, cy, rh, num_w, widths, "№" if show_num else None, cells, wrapped, head_st, 4.0, [False] * len(cells), highlight=False)
                cy += rh
                boundaries.append(cy)
            for i, r in enumerate(rows):
                if cy >= y + h:
                    break
                formatted = [fmt_cell(r, str(c.get("bind") or "")) for c in cols_def]
                cells = [(t, str(c.get("align") or "left")) for (t, _), c in zip(formatted, cols_def, strict=True)]
                abn = [a and highlight for _, a in formatted]
                rh, wrapped = row_height(cells, cell_st, 3.0, [600 if a else None for a in abn])
                if zebra is not None and i % 2 == 1:
                    self.fill_rect(x, cy, w, rh, zebra)
                self.draw_row(x, cy, rh, num_w, widths, str(i + 1) if show_num else None, cells, wrapped, cell_st, 3.0, abn, highlight=highlight)
                cy += rh
                boundaries.append(cy)
            if bw > 0 and len(boundaries) > 1:
                bottom = boundaries[-1]
                for by in boundaries[1:-1]:  # inner horizontal rules (collapsed borders)
                    self.hline(x, x + w, by, bw, bcolor)
                xs: list[float] = []
                cx = x + num_w if show_num else x
                if show_num:
                    xs.append(cx)
                for cw in widths[:-1]:
                    cx += cw
                    xs.append(cx)
                for bx in xs:  # inner vertical rules
                    self.vline(bx, y, bottom, bw, bcolor)
                # outer rectangle inset so the full stroke stays inside the element box
                self.stroke_rect(x, y, w, bottom - y, bcolor, bw)

    def draw_row(
        self,
        x: float,
        cy: float,
        rh: float,
        num_w: float,
        widths: list[float],
        number: str | None,
        cells: list[tuple[str, str]],
        wrapped: list[list[tuple[str, bool]]],
        st: dict[str, Any],
        pad_v: float,
        abnormal: list[bool],
        *,
        highlight: bool,
    ) -> None:
        """One table row: optional centred row number + cells (vertically middle, 6px side padding)."""
        pad_h = 6.0
        cx = x
        if number is not None:
            self.set_font(st)
            _, lh, base = self.metrics(st)
            self.draw_line(cx + pad_h, max(0.0, num_w - 2 * pad_h), cy + (rh - lh) / 2 + base, number, "center", justify=False)
            cx += num_w
        abn_color = parse_color(ABN)
        for (_text, align), lines, cw, ab in zip(cells, wrapped, widths, abnormal, strict=False):
            self.set_font(st, weight=600 if ab and highlight else None, color=abn_color if ab and highlight else None)
            _, lh, base = self.metrics(st)
            block_h = len(lines) * lh
            top = cy + (rh - block_h) / 2
            with self.clip(cx, cy, cw, rh):
                for k, (line, _ends) in enumerate(lines):
                    self.draw_line(cx + pad_h, max(0.0, cw - 2 * pad_h), top + k * lh + base, line, align, justify=False)
            cx += cw


def render(doc: dict[str, Any], context: dict[str, Any], asset_loader: AssetLoader | None = None) -> bytes:
    """Render a TemplateDoc dict with a RenderContext dict to PDF bytes (single page)."""
    return _Renderer(doc, context, asset_loader).render()
