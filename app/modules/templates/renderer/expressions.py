"""Pure placeholder / value helpers — 1:1 port of Clinic-Web `src/domain/template-render.ts` and
`evaluateNumber` / `referenceText` from `src/domain/catalog.ts` (see docs/RENDERER_SPEC.md §1, §2, §4).

All functions work on plain dicts (the RenderContext / AttributeSchema JSON shapes) so they can run
both on live ORM data and on frozen document snapshots.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

Context = dict[str, Any]
Row = dict[str, Any]

PLACEHOLDER = re.compile(r"\{([a-zA-Z0-9_.\-]+)\}")
ITEMS_DATASET = "items"

Flag = str  # 'normal' | 'abnormal' | 'critical' | 'unknown'


# ----------------------------------------------------------------------------- primitives


def fmt(v: Any) -> str:
    """JS `fmt`: None → '', list → joined ', ', bool → ✓/—, else str()."""
    if v is None:
        return ""
    if isinstance(v, list):
        return ", ".join(fmt(x) for x in v)
    if isinstance(v, bool):
        return "✓" if v else "—"
    if isinstance(v, float):
        return js_number(v)
    return str(v)


def js_number(v: float | int) -> str:
    """`String(number)` in JS: 12.0 → '12', 12.5 → '12.5'."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if v != v or v in (float("inf"), float("-inf")):
        return "NaN" if v != v else ("Infinity" if v > 0 else "-Infinity")
    if float(v).is_integer():
        return str(int(v))
    return repr(float(v))


def is_numeric(v: Any) -> bool:
    """`typeof raw === 'number'` (bool excluded)."""
    return isinstance(v, int | float) and not isinstance(v, bool)


def to_fixed(v: float, decimals: int) -> str:
    """JS `Number.prototype.toFixed` (round half up on the decimal representation)."""
    q = Decimal(1).scaleb(-max(0, int(decimals)))
    return str(Decimal(str(v)).quantize(q, rounding=ROUND_HALF_UP))


def get_path(obj: Any, path: str) -> Any:
    """Dotted traversal through dicts; anything missing → None."""
    cur = obj
    for key in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(key)
        else:
            return None
    return cur


# ----------------------------------------------------------------------------- schema access


def field_def(schema: dict[str, Any] | None, key: str) -> dict[str, Any] | None:
    """Field definition by key from an AttributeSchema dict (or None)."""
    if not schema:
        return None
    for f in schema.get("fields") or []:
        if isinstance(f, dict) and f.get("key") == key:
            return f
    return None


def option_label(options: list[dict[str, Any]] | None, value: Any) -> str | None:
    for o in options or []:
        if o.get("value") == value:
            return str(o.get("label") if o.get("label") is not None else value)
    return None


def option_of(options: list[dict[str, Any]] | None, value: Any) -> dict[str, Any] | None:
    for o in options or []:
        if o.get("value") == value:
            return o
    return None


# ----------------------------------------------------------------------------- values


def format_value(ctx: Context, key: str) -> str:
    """Human-readable value of `ctx.values[key]` according to the primary schema (spec §1)."""
    return format_raw(get_values(ctx).get(key), field_def(ctx.get("schema"), key))


def format_raw(raw: Any, fdef: dict[str, Any] | None) -> str:
    """`formatValue` core on a raw value + field def (shared with items / svc lookups)."""
    if raw is None or raw == "":
        return ""
    if not fdef:
        return fmt(raw)
    ftype = fdef.get("type")
    if ftype == "select":
        return option_label(fdef.get("options"), raw) or fmt(raw)
    if ftype == "multiselect":
        if not isinstance(raw, list):
            return fmt(raw)
        return ", ".join(option_label(fdef.get("options"), v) or fmt(v) for v in raw)
    if ftype == "boolean":
        return (fdef.get("trueLabel") or "Ha") if raw else (fdef.get("falseLabel") or "Yo‘q")
    if ftype == "number":
        if is_numeric(raw):
            return re.sub(r"\.0+$", "", to_fixed(float(raw), int(fdef.get("decimals") or 0)))
        return fmt(raw)
    if ftype == "table":
        return ""  # documented divergence: JS prints "[object Object]"
    return fmt(raw)


def get_values(ctx: Context) -> dict[str, Any]:
    values = ctx.get("values")
    return values if isinstance(values, dict) else {}


def evaluate_number(fdef: dict[str, Any], value: float, gender: str | None, age_months: int | None) -> str:
    """`evaluateNumber`: normal | low | high | unknown by the first applicable reference range."""
    ref = None
    for r in fdef.get("references") or []:
        if r.get("gender") and gender and r["gender"] != gender:
            continue
        if r.get("ageFromMonths") is not None and age_months is not None and age_months < r["ageFromMonths"]:
            continue
        if r.get("ageToMonths") is not None and age_months is not None and age_months > r["ageToMonths"]:
            continue
        if r.get("min") is not None or r.get("max") is not None:
            ref = r
            break
    if ref is None:
        return "unknown"
    if ref.get("min") is not None and value < ref["min"]:
        return "low"
    if ref.get("max") is not None and value > ref["max"]:
        return "high"
    return "normal"


def reference_text(fdef: dict[str, Any], gender: str | None) -> str:
    """`referenceText`: text | 'min - max' (en dash) | '≥ min' | '≤ max' | '' (no age filter)."""
    ref = None
    for r in fdef.get("references") or []:
        if not r.get("gender") or not gender or r["gender"] == gender:
            ref = r
            break
    if ref is None:
        return ""
    if ref.get("text"):
        return str(ref["text"])
    mn, mx = ref.get("min"), ref.get("max")
    if mn is not None and mx is not None:
        return f"{js_number(mn)} – {js_number(mx)}"
    if mn is not None:
        return f"≥ {js_number(mn)}"
    if mx is not None:
        return f"≤ {js_number(mx)}"
    return ""


def field_flag(ctx: Context, key: str) -> Flag:
    """`fieldFlag`: normal | abnormal | critical | unknown for the primary item value."""
    fdef = field_def(ctx.get("schema"), key)
    raw = get_values(ctx).get(key)
    if not fdef or raw is None or raw == "":
        return "unknown"
    patient = ctx.get("patient") or {}
    if fdef.get("type") == "number" and is_numeric(raw):
        r = evaluate_number(fdef, float(raw), patient.get("genderRaw"), patient.get("ageMonths"))
        return "normal" if r == "normal" else "unknown" if r == "unknown" else "abnormal"
    if fdef.get("type") == "select":
        opt = option_of(fdef.get("options"), raw)
        return (opt or {}).get("flag") or "unknown"
    return "unknown"


def field_reference(ctx: Context, key: str) -> str:
    """Reference range text of a number field ('' for other types)."""
    fdef = field_def(ctx.get("schema"), key)
    if not fdef or fdef.get("type") != "number":
        return ""
    return reference_text(fdef, (ctx.get("patient") or {}).get("genderRaw"))


def field_unit(ctx: Context, key: str) -> str:
    """`fieldUnit`: def.unit or ''."""
    fdef = field_def(ctx.get("schema"), key)
    return str((fdef or {}).get("unit") or "")


# ----------------------------------------------------------------------------- items dataset


def _sub_context(ctx: Context, item: dict[str, Any]) -> Context:
    return {**ctx, "values": item.get("values") or {}, "schema": item.get("schema")}


def find_item(ctx: Context, code: str) -> dict[str, Any] | None:
    """RenderItem by service code (trimmed, case-insensitive)."""
    c = code.strip().lower()
    for it in ctx.get("items") or []:
        if str(it.get("code") or "").strip().lower() == c:
            return it
    return None


def service_value(ctx: Context, path: str) -> str:
    """`svc.<code>.<key>` → item meta (name/status/approvedAt/technician/doctor) or formatted value."""
    dot = path.rfind(".")
    if dot < 0:
        return ""
    code, key = path[:dot], path[dot + 1 :]
    it = find_item(ctx, code)
    if not it:
        return ""
    if key == "name":
        return str(it.get("serviceName") or "")
    if key == "status":
        return str(it.get("status") or "")
    if key in ("approvedAt", "technician", "doctor"):
        return str(it.get(key) or "")
    return format_value(_sub_context(ctx, it), key)


def item_rows(ctx: Context) -> list[Row]:
    """Rows of the reserved `items` dataset: i/code/name/status + every non-table field formatted."""
    rows: list[Row] = []
    for i, it in enumerate(ctx.get("items") or []):
        sub = _sub_context(ctx, it)
        row: Row = {"code": it.get("code"), "name": it.get("serviceName"), "status": it.get("status"), "i": i + 1}
        for f in (it.get("schema") or {}).get("fields") or []:
            if isinstance(f, dict) and f.get("type") != "table":
                row[f["key"]] = format_value(sub, f["key"])
        rows.append(row)
    return rows


def table_rows(ctx: Context, field_key: str) -> list[Row]:
    """Dataset rows for a table / repeat binding: `items` → itemRows, else raw list value or []."""
    if field_key == ITEMS_DATASET:
        return item_rows(ctx)
    v = get_values(ctx).get(field_key)
    return [r for r in v if isinstance(r, dict)] if isinstance(v, list) else []


# ----------------------------------------------------------------------------- interpolation


def interpolate(text: str, ctx: Context, row: Row | None = None) -> str:
    """Replace `{path}` placeholders (spec §2). Non-matching braces stay verbatim."""

    def repl(m: re.Match[str]) -> str:
        path = m.group(1)
        if row is not None and path == "i":
            return fmt(row.get("__i"))
        if row is not None and path.startswith("row."):
            return fmt(row.get(path[4:]))
        if path.startswith("values."):
            return fmt(format_value(ctx, path[7:]))
        if path.startswith("svc."):
            return fmt(service_value(ctx, path[4:]))
        return fmt(get_path(ctx, path))

    return PLACEHOLDER.sub(repl, text or "")


def show_if(expr: str | None, ctx: Context, row: Row | None = None) -> bool:
    """`showIf` presence test: keep iff the interpolated expression is non-blank."""
    if not expr:
        return True
    return bool(interpolate(expr, ctx, row).strip())
