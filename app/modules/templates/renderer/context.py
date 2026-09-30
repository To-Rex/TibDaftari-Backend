"""RenderContext builder — port of Clinic-Web `src/features/documents/buildContext.ts` (spec §1).

Works on plain dicts so it can be used both from ORM rows (templates.service) and from tests.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.core.textutil import fmt_phone
from app.core.timeutil import age_months, age_years, fmt_date, fmt_datetime, parse_iso, today_local

GENDER_LABELS: dict[str, dict[str, str]] = {
    "uz": {"male": "Erkak", "female": "Ayol"},
    "ru": {"male": "Мужской", "female": "Женский"},
    "en": {"male": "Male", "female": "Female"},
}


def gender_label(gender: str | None, language: str = "uz") -> str:
    """Gender label in the template language ('' when unknown)."""
    if not gender:
        return ""
    return GENDER_LABELS.get(language, GENDER_LABELS["uz"]).get(gender, "")


def _dt(v: datetime | date | str | None) -> datetime | date | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime | date):
        return v
    text = str(v)
    if len(text) == 10:
        return date.fromisoformat(text)
    return parse_iso(text)


def _date(v: datetime | date | str | None) -> date | None:
    d = _dt(v)
    if isinstance(d, datetime):
        return d.date()
    return d


def _fmt_date(v: datetime | date | str | None) -> str:
    return fmt_date(_dt(v))


def _fmt_datetime(v: datetime | date | str | None) -> str:
    d = _dt(v)
    if d is None:
        return ""
    if isinstance(d, date) and not isinstance(d, datetime):
        d = datetime(d.year, d.month, d.day)
    return fmt_datetime(d)


def to_render_item(
    *,
    code: str,
    service_type_id: str,
    service_name: str,
    status: str,
    values: dict[str, Any] | None,
    schema: dict[str, Any] | None,
    approved_at: datetime | str | None = None,
    technician: str | None = None,
    doctor: str | None = None,
    price: int | float | None = None,
    final_price: int | float | None = None,
    category: str | None = None,
) -> dict[str, Any]:
    """RenderItem dict for order-scoped documents (approvedAt pre-formatted 'dd.MM.yyyy HH:mm');
    receipts also carry the formatted prices and the category name."""
    item: dict[str, Any] = {
        "code": code,
        "serviceTypeId": service_type_id,
        "serviceName": service_name,
        "status": status,
        "values": values or {},
        "schema": schema,
    }
    if approved_at:
        item["approvedAt"] = _fmt_datetime(approved_at)
    if technician:
        item["technician"] = technician
    if doctor:
        item["doctor"] = doctor
    if price is not None:
        item["price"] = fmt_money(price)
    if final_price is not None:
        item["finalPrice"] = fmt_money(final_price)
    if category:
        item["category"] = category
    return item


def fmt_money(v: int | float | None) -> str:
    """Frontend `fmtMoney(v, false)`: thousands separated by commas, no decimals ("305,000")."""
    if v is None:
        return ""
    return f"{round(float(v)):,}"


PAYMENT_METHOD_LABELS = {
    "uz": {"cash": "Naqd", "card": "Karta", "transfer": "O‘tkazma", "insurance": "Sug‘urta"},
    "ru": {"cash": "Наличные", "card": "Карта", "transfer": "Перевод", "insurance": "Страховка"},
    "en": {"cash": "Cash", "card": "Card", "transfer": "Transfer", "insurance": "Insurance"},
}


def payment_method_label(method: str | None, language: str = "uz") -> str:
    labels = PAYMENT_METHOD_LABELS.get(language) or PAYMENT_METHOD_LABELS["uz"]
    return labels.get(method or "", method or "")


def build_render_context(
    *,
    patient: dict[str, Any] | None = None,
    order: dict[str, Any] | None = None,
    item: dict[str, Any] | None = None,
    company: dict[str, Any] | None = None,
    branch: dict[str, Any] | None = None,
    category: dict[str, Any] | None = None,
    schema: dict[str, Any] | None = None,
    district_name: str | None = None,
    items: list[dict[str, Any]] | None = None,
    language: str = "uz",
    today: date | None = None,
    payments: list[dict[str, Any]] | None = None,
    cashier: str | None = None,
) -> dict[str, Any]:
    """Build a RenderContext dict (spec §1) from plain dicts.

    Input shapes (all optional):
      patient  {fullName, phone, birthDate, gender, street, passportNumber}
      order    {number, createdAt, subtotal?, discountPercent?, discountAmount?, total?, paidAmount?, itemCount?, note?, status?}
      item     {serviceName, approvedAt, technicianName, doctorName, labNote, values}
      company  {name, phone, address}   branch {name, address, phone}   category {name, phone}
      items    list of RenderItem dicts (see `to_render_item`)
      payments receipts: [{createdAt, method, amount, note}] → `payments` dataset (i/date/method/amount/note)
      cashier  receipts: name of the employee who opened the cheque
    """
    p = patient or {}
    birth = _date(p.get("birthDate"))
    ref_day = today or today_local()
    address = ", ".join(x for x in (district_name, p.get("street")) if x)
    it = item or {}
    ctx: dict[str, Any] = {
        "patient": {
            "fullName": p.get("fullName") or "",
            "phone": fmt_phone(p.get("phone")),
            "birthDate": fmt_date(birth),
            "age": str(age_years(birth, ref_day)) if birth else "",
            "gender": gender_label(p.get("gender"), language),
            "genderRaw": p.get("gender"),
            "ageMonths": age_months(birth, ref_day),
            "address": address,
            "passportNumber": p.get("passportNumber") or "",
        },
        "order": _order_block(order or {}, bool(payments)),
        "item": {
            "serviceName": it.get("serviceName") or "",
            "approvedAt": _fmt_datetime(it.get("approvedAt")) if it.get("approvedAt") else "",
            "technician": it.get("technicianName") or "",
            "doctor": it.get("doctorName") or "",
            "labNote": it.get("labNote") or "",
        },
        "company": {
            "name": (company or {}).get("name") or "",
            "phone": (company or {}).get("phone"),
            "address": (company or {}).get("address"),
        },
        "branch": {"name": (branch or {}).get("name") or "", "address": (branch or {}).get("address"), "phone": fmt_phone((branch or {}).get("phone")) if (branch or {}).get("phone") else None},
        "category": {"name": (category or {}).get("name") or "", "phone": (category or {}).get("phone")},
        "today": fmt_date(ref_day),
        "values": it.get("values") or {},
        "schema": schema,
    }
    if items is not None:
        ctx["items"] = items
    if cashier is not None:
        ctx["cashier"] = {"name": cashier}
    if payments is not None:
        ctx["payments"] = [
            {
                "i": i + 1,
                "date": _fmt_datetime(p.get("createdAt")) if p.get("createdAt") else "",
                "method": payment_method_label(p.get("method"), language),
                "amount": fmt_money(p.get("amount")),
                "note": p.get("note") or "",
            }
            for i, p in enumerate(payments)
        ]
    return ctx


def _order_block(order: dict[str, Any], has_payments: bool = False) -> dict[str, Any]:
    """`order.*` placeholders: number/date for every document, money fields when the order carries them (receipts)."""
    block: dict[str, Any] = {"number": order.get("number") or "", "date": _fmt_date(order.get("createdAt"))}
    if order.get("createdAt"):
        block["dateTime"] = _fmt_datetime(order.get("createdAt"))
    if "total" in order:
        total = float(order.get("total") or 0)
        paid = float(order.get("paidAmount") or 0)
        block.update(
            {
                "subtotal": fmt_money(order.get("subtotal")),
                "discountPercent": str(order.get("discountPercent") or 0),
                "discountAmount": fmt_money(order.get("discountAmount")),
                "total": fmt_money(total),
                "paidAmount": fmt_money(paid),
                "remaining": fmt_money(max(0.0, total - paid)),
                "itemCount": str(order.get("itemCount") or 0),
                "note": order.get("note") or "",
                "status": order.get("status") or "",
                # presence flags for showIf: the discount line, the balance line, the payments table
                "hasDiscount": "1" if float(order.get("discountPercent") or 0) > 0 or float(order.get("discountAmount") or 0) > 0 else "",
                "hasRemaining": "1" if total - paid > 0 else "",
                "hasPayments": "1" if has_payments else "",
            }
        )
    return block
