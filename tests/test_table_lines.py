"""Table rows: single-line cells, unlimited wrapping, and min / max text lines per row (pure — no database)."""

from __future__ import annotations

from typing import Any

from app.modules.templates.renderer import render
from app.modules.templates.renderer.pdf import _Renderer

LONG = "Escherichia coli ajratildi, antibiotiklarga sezgirligi aniqlandi va qo'shimcha izoh yozildi " * 2
STYLE = {"fontFamily": "sans", "fontSize": 10, "fontWeight": 400, "color": "#000", "align": "left", "lineHeight": 1.2}
LH = 10 * 1.2  # line height of the cell style
PAD = 3.0  # vertical cell padding (top / bottom)


def _table(**extra: Any) -> dict[str, Any]:
    return {
        "id": "tb", "type": "table", "x": 20, "y": 20, "w": 300, "h": 40, "grow": True, "fieldKey": "",
        "staticRows": [["Nomi", LONG], ["Qisqa", "ok"]], "showHeader": False, "showRowNumber": False, "highlightAbnormal": False,
        "rowHeight": 22, "borderColor": "#000", "borderWidth": 1,
        "columns": [{"id": "c1", "header": "", "bind": "k", "width": 1, "align": "left"}, {"id": "c2", "header": "", "bind": "v", "width": 2, "align": "left"}],
        "headerStyle": STYLE, "cellStyle": STYLE, **extra,
    }


def _height(**extra: Any) -> float:
    """Measured height of the grow table (dry layout pass — nothing is drawn)."""
    table = _table(**extra)
    r = _Renderer({"paper": "A4", "orientation": "portrait", "margin": 20, "elements": [table]}, {}, lambda _aid: None)
    r._grow_shifts([table])
    return r._grow_h[id(table)]


def test_single_line_rows_keep_the_row_height() -> None:
    assert _height(nowrap=True) == 2 * 22
    assert _height(nowrap=True, minLines=4, maxLines=1) == 2 * 22  # single-line cells ignore line limits


def test_unlimited_wrapping_shows_the_whole_text() -> None:
    full = _height()
    assert full > 2 * 22 + 2 * LH  # the long cell wraps over several lines
    assert _height(maxLines=0) == full  # 0 = as many lines as needed


def test_max_lines_caps_a_row() -> None:
    capped = _height(maxLines=2)
    assert capped == (2 * LH + 2 * PAD) + 22  # long row: 2 lines; short row: the row height
    assert capped < _height()


def test_min_lines_reserves_room_even_for_short_text() -> None:
    assert _height(minLines=3, maxLines=3) == 2 * (3 * LH + 2 * PAD)
    assert _height(minLines=4, maxLines=2) == 2 * (4 * LH + 2 * PAD)  # max below min is lifted to min


def test_clamped_table_renders() -> None:
    pdf = render({"paper": "A4", "orientation": "portrait", "margin": 20, "elements": [_table(maxLines=2, minLines=1)]}, {}, lambda _aid: None)
    assert pdf[:5] == b"%PDF-"
