"""Files the assistant builds on request: a list of flat rows → Excel / CSV / JSON, after the filters, sort and
column choice the model derived from the employee's words ("faqat to‘langan cheklar, summa bo‘yicha kamayish").

Pure functions over plain dicts — no database here. `tools.py` loads the dataset (with the employee's own rights)
and stores the result as an `assistant_files` row the chat offers for download."""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass
from typing import Any

import xlsxwriter

from app.core.exceptions import ValidationError
from app.modules.assistant import render as rd

MAX_ROWS = 10_000
#: table formats (export_data / export_table)
FORMATS: dict[str, tuple[str, str]] = {
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "csv": ("text/csv; charset=utf-8", ".csv"),
    "json": ("application/json; charset=utf-8", ".json"),
    "pdf": ("application/pdf", ".pdf"),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    "md": ("text/markdown; charset=utf-8", ".md"),
    "txt": ("text/plain; charset=utf-8", ".txt"),
}
#: document formats (export_document) and picture formats (export_chart)
DOC_FORMATS: dict[str, tuple[str, str]] = {k: FORMATS[k] for k in ("docx", "pdf", "md", "txt")}
CHART_FORMATS: dict[str, tuple[str, str]] = {"png": ("image/png", ".png"), "svg": ("image/svg+xml", ".svg")}
ALL_FORMATS: dict[str, tuple[str, str]] = {**FORMATS, **CHART_FORMATS}
OPS = ("eq", "ne", "contains", "startsWith", "endsWith", "gt", "gte", "lt", "lte", "in", "notIn", "between", "empty", "notEmpty")


@dataclass(frozen=True)
class Column:
    key: str
    title: str


# ----------------------------------------------------------------------------- rows


def flatten(row: Any, prefix: str = "") -> dict[str, Any]:
    """`{"patient": {"name": "A"}, "tags": ["x", "y"]}` → `{"patient.name": "A", "tags": "x, y"}`."""
    out: dict[str, Any] = {}
    if not isinstance(row, dict):
        return {"value": row}
    for k, v in row.items():
        key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            out.update(flatten(v, key))
        elif isinstance(v, list):
            out[key] = ", ".join(str(x) for x in v) if all(not isinstance(x, (dict, list)) for x in v) else json.dumps(v, ensure_ascii=False, default=str)
        else:
            out[key] = v
    return out


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace(" ", "").replace(",", ".")
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _text(v: Any) -> str:
    return "" if v is None else str(v).strip().lower()


def _eq(cell: Any, value: Any) -> bool:
    if cell is None or cell == "":
        return value is None or value == ""
    a, b = _num(cell), _num(value)
    if a is not None and b is not None:
        return a == b
    c, v = _text(cell), _text(value)
    # a date against a timestamp: "2026-11-12" matches "2026-11-12T10:00:00"
    return c == v or (len(v) == 10 and c.startswith(v))


def _cmp(cell: Any, value: Any) -> int | None:
    if cell is None or cell == "":
        return None
    a, b = _num(cell), _num(value)
    if a is not None and b is not None:
        return (a > b) - (a < b)
    c, v = _text(cell), _text(value)
    return (c > v) - (c < v)


def _match(cell: Any, op: str, value: Any) -> bool:
    if op == "empty":
        return cell is None or cell == ""
    if op == "notEmpty":
        return not (cell is None or cell == "")
    if op in ("in", "notIn"):
        vals = value if isinstance(value, list) else [value]
        hit = any(_eq(cell, v) for v in vals)
        return hit if op == "in" else not hit
    if op == "between":
        if not isinstance(value, list) or len(value) != 2:
            raise ValidationError("between uchun [dan, gacha] juftligi kerak", code="export_filter")
        lo, hi = _cmp(cell, value[0]), _cmp(cell, value[1])
        return lo is not None and hi is not None and lo >= 0 and hi <= 0
    if op == "eq":
        return _eq(cell, value)
    if op == "ne":
        return not _eq(cell, value)
    if op == "contains":
        return _text(value) in _text(cell)
    if op == "startsWith":
        return _text(cell).startswith(_text(value))
    if op == "endsWith":
        return _text(cell).endswith(_text(value))
    c = _cmp(cell, value)
    if c is None:
        return False
    return {"gt": c > 0, "gte": c >= 0, "lt": c < 0, "lte": c <= 0}[op]


def available_columns(rows: list[dict[str, Any]]) -> list[str]:
    seen: dict[str, None] = {}
    for r in rows:
        for k in r:
            seen.setdefault(k, None)
    return list(seen)


def _check_columns(rows: list[dict[str, Any]], keys: list[str]) -> None:
    have = set(available_columns(rows))
    unknown = [k for k in keys if k not in have]
    if unknown and rows:
        raise ValidationError(f"Noma’lum ustun: {', '.join(unknown)}", code="export_column", details={"availableColumns": sorted(have)})


def apply_filters(rows: list[dict[str, Any]], filters: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Every filter must hold (AND). `{"column", "op", "value"}`; `value` is a list for in / notIn / between."""
    if not filters:
        return rows
    specs: list[tuple[str, str, Any]] = []
    for f in filters:
        if not isinstance(f, dict) or not f.get("column"):
            raise ValidationError("Filtr: column kerak", code="export_filter")
        op = str(f.get("op") or "eq")
        if op not in OPS:
            raise ValidationError(f"Noma’lum filtr amali: {op}", code="export_filter", details={"ops": list(OPS)})
        specs.append((str(f["column"]), op, f.get("value")))
    _check_columns(rows, [c for c, _, _ in specs])
    return [r for r in rows if all(_match(r.get(c), op, v) for c, op, v in specs)]


def _sort_key(col: str, numeric: bool) -> Any:
    if numeric:
        return lambda r: _num(r.get(col)) or 0.0
    return lambda r: _text(r.get(col))


def apply_sort(rows: list[dict[str, Any]], sort: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """`[{"column", "dir": "asc" | "desc"}]` — first entry wins; empty cells always go last."""
    if not sort:
        return rows
    specs = [(str(s.get("column") or ""), str(s.get("dir") or "asc").lower() == "desc") for s in sort if isinstance(s, dict)]
    _check_columns(rows, [c for c, _ in specs if c])
    out = list(rows)
    for col, desc in reversed(specs):
        if not col:
            continue
        present = [r for r in out if r.get(col) not in (None, "")]
        missing = [r for r in out if r.get(col) in (None, "")]
        numeric = all(_num(r.get(col)) is not None for r in present)
        present.sort(key=_sort_key(col, numeric), reverse=desc)
        out = present + missing
    return out


def _title(key: str) -> str:
    parts = [re.sub(r"(?<!^)(?=[A-Z])", " ", p).replace("_", " ").strip() for p in key.split(".")]
    return " · ".join(p[:1].upper() + p[1:] for p in parts if p)


def pick_columns(rows: list[dict[str, Any]], columns: list[Any] | None) -> list[Column]:
    """The columns of the file, in order. Not given → every column the rows have, minus the ids."""
    if columns:
        out: list[Column] = []
        for c in columns:
            if isinstance(c, str):
                out.append(Column(c, _title(c)))
            elif isinstance(c, dict) and c.get("key"):
                out.append(Column(str(c["key"]), str(c.get("title") or _title(str(c["key"])))))
            else:
                raise ValidationError("Ustun: 'key' yoki {key, title} kutiladi", code="export_column")
        _check_columns(rows, [c.key for c in out])
        return out
    keys = [k for k in available_columns(rows) if not (k == "id" or k.endswith("Id") or k.endswith("Ids") or k.endswith(".id"))]
    return [Column(k, _title(k)) for k in keys]


# ----------------------------------------------------------------------------- files


def _cell(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "ha" if v else "yo‘q"
    if isinstance(v, (int, float)):
        return v
    return str(v)


def to_xlsx(title: str, columns: list[Column], rows: list[dict[str, Any]]) -> bytes:
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    ws = wb.add_worksheet(re.sub(r"[\[\]:*?/\\]", " ", title or "Ma’lumot")[:31] or "Sheet1")
    head = wb.add_format({"bold": True, "bg_color": "#E3EFEA", "border": 1, "text_wrap": True, "valign": "top"})
    num = wb.add_format({"num_format": "#,##0"})
    ws.write_row(0, 0, [c.title for c in columns], head)
    widths = [len(c.title) for c in columns]
    for i, r in enumerate(rows, start=1):
        for j, c in enumerate(columns):
            v = _cell(r.get(c.key))
            if isinstance(v, (int, float)):
                ws.write_number(i, j, v, num if isinstance(v, int) or float(v).is_integer() else None)
            else:
                ws.write_string(i, j, v)
            if i <= 500:
                widths[j] = max(widths[j], min(len(str(v)), 60))
    for j, w in enumerate(widths):
        ws.set_column(j, j, min(max(w + 2, 8), 62))
    if rows:
        ws.autofilter(0, 0, len(rows), max(len(columns) - 1, 0))
    ws.freeze_panes(1, 0)
    wb.close()
    return buf.getvalue()


def to_csv(columns: list[Column], rows: list[dict[str, Any]]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow([c.title for c in columns])
    for r in rows:
        w.writerow([_cell(r.get(c.key)) for c in columns])
    return ("﻿" + buf.getvalue()).encode("utf-8")


def to_json(columns: list[Column], rows: list[dict[str, Any]]) -> bytes:
    return json.dumps([{c.key: r.get(c.key) for c in columns} for r in rows], ensure_ascii=False, default=str, indent=1).encode("utf-8")


def norm_format(fmt: str | None, default: str = "xlsx") -> str:
    f = (fmt or default).lower().strip().lstrip(".")
    return {"excel": "xlsx", "xls": "xlsx", "word": "docx", "doc": "docx", "markdown": "md", "text": "txt", "image": "png", "picture": "png", "rasm": "png"}.get(f, f)


def _table_rows(columns: list[Column], rows: list[dict[str, Any]]) -> list[list[Any]]:
    return [[r.get(c.key) for c in columns] for r in rows]


def _numeric_columns(columns: list[Column], rows: list[dict[str, Any]]) -> list[bool]:
    return [bool(rows) and all(_num(r.get(c.key)) is not None for r in rows if r.get(c.key) not in (None, "")) for c in columns]


def to_md_table(title: str, columns: list[Column], rows: list[dict[str, Any]]) -> bytes:
    esc = lambda v: ("" if v is None else str(v)).replace("|", "\\|").replace("\n", " ")  # noqa: E731
    lines = [f"# {title}", ""] if title else []
    lines.append("| " + " | ".join(c.title for c in columns) + " |")
    lines.append("|" + "|".join("---" for _ in columns) + "|")
    lines += ["| " + " | ".join(esc(r.get(c.key)) for c in columns) + " |" for r in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


def to_txt_table(title: str, columns: list[Column], rows: list[dict[str, Any]]) -> bytes:
    lines = [title, ""] if title else []
    lines.append("\t".join(c.title for c in columns))
    lines += ["\t".join("" if r.get(c.key) is None else str(r.get(c.key)) for c in columns) for r in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


def build_file(fmt: str, title: str, columns: list[Column], rows: list[dict[str, Any]], *, note: str | None = None) -> tuple[bytes, str]:
    """(bytes, content type) of a TABLE in the asked format; `fmt` defaults to xlsx."""
    fmt = norm_format(fmt)
    if fmt not in FORMATS:
        raise ValidationError(f"Noma’lum jadval formati: {fmt}", code="export_format", details={"formats": list(FORMATS)})
    if fmt == "xlsx":
        data = to_xlsx(title, columns, rows)
    elif fmt == "csv":
        data = to_csv(columns, rows)
    elif fmt == "json":
        data = to_json(columns, rows)
    elif fmt == "pdf":
        data = rd.table_pdf(title, [c.title for c in columns], _table_rows(columns, rows), numeric=_numeric_columns(columns, rows), note=note)
    elif fmt == "docx":
        data = rd.table_docx(title, [c.title for c in columns], _table_rows(columns, rows), note=note)
    elif fmt == "md":
        data = to_md_table(title, columns, rows)
    else:
        data = to_txt_table(title, columns, rows)
    return data, FORMATS[fmt][0]


def build_document(fmt: str, title: str, markdown: str) -> tuple[bytes, str]:
    """(bytes, content type) of a DOCUMENT the model wrote in Markdown; `fmt` defaults to docx."""
    fmt = norm_format(fmt, "docx")
    if fmt not in DOC_FORMATS:
        raise ValidationError(f"Noma’lum hujjat formati: {fmt}", code="export_format", details={"formats": list(DOC_FORMATS)})
    if fmt == "docx":
        data = rd.document_docx(title, markdown)
    elif fmt == "pdf":
        data = rd.document_pdf(title, markdown)
    elif fmt == "md":
        data = rd.to_markdown(title, markdown).encode("utf-8")
    else:
        data = rd.to_text(title, rd.parse_markdown(markdown)).encode("utf-8")
    return data, DOC_FORMATS[fmt][0]


def build_chart(fmt: str, spec: rd.ChartSpec) -> tuple[bytes, str]:
    """(bytes, content type) of a chart picture; `fmt` defaults to png."""
    fmt = norm_format(fmt, "png")
    if fmt not in CHART_FORMATS:
        raise ValidationError(f"Noma’lum rasm formati: {fmt}", code="export_format", details={"formats": list(CHART_FORMATS)})
    return (rd.chart_png(spec) if fmt == "png" else rd.chart_svg(spec)), CHART_FORMATS[fmt][0]


def file_name(name: str | None, title: str | None, fmt: str, stamp: str) -> str:
    fmt = norm_format(fmt)
    ext = ALL_FORMATS.get(fmt, FORMATS["xlsx"])[1]
    base = (name or "").strip()
    if base.lower().endswith(ext):
        base = base[: -len(ext)]
    if not base:
        base = re.sub(r"[^\w\-]+", "-", (title or "malumot").strip().lower(), flags=re.UNICODE).strip("-") or "malumot"
        base = f"{base}-{stamp}"
    return re.sub(r"[\\/:*?\"<>|]+", "-", base)[:150] + ext


def prepare(rows: list[dict[str, Any]], *, filters: list[dict[str, Any]] | None, sort: list[dict[str, Any]] | None, columns: list[Any] | None, limit: int | None) -> tuple[list[Column], list[dict[str, Any]]]:
    """Flatten → filter → sort → cut → columns: everything the two export tools share."""
    flat = [flatten(r) for r in rows]
    flat = apply_sort(apply_filters(flat, filters), sort)
    cap = max(1, min(int(limit or MAX_ROWS), MAX_ROWS))
    flat = flat[:cap]
    return pick_columns(flat, columns), flat
