"""RenderContext builder — port of Clinic-Web `src/features/documents/buildContext.ts` (spec §1).

Works on plain dicts so it can be used both from ORM rows (templates.service) and from tests.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
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
    entered_at: datetime | str | None = None,
    submitted_at: datetime | str | None = None,
    technician: str | None = None,
    doctor: str | None = None,
    price: int | float | None = None,
    final_price: int | float | None = None,
    category: str | None = None,
    status_label: str | None = None,
    schema_version: int | str | None = None,
) -> dict[str, Any]:
    """RenderItem dict for order-scoped documents (dates pre-formatted 'dd.MM.yyyy HH:mm');
    receipts also carry the formatted prices and the category name."""
    item: dict[str, Any] = {
        "code": code,
        "serviceTypeId": service_type_id,
        "serviceName": service_name,
        "status": status,
        "values": values or {},
        "schema": schema,
    }
    if status_label:
        item["statusLabel"] = status_label
    if entered_at:
        item["enteredAt"] = _fmt_datetime(entered_at)
    if submitted_at:
        item["submittedAt"] = _fmt_datetime(submitted_at)
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
    if schema_version is not None:
        item["schemaVersion"] = str(schema_version)
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

ORDER_STATUS_LABELS = {
    "uz": {"draft": "Qoralama", "open": "Ochiq", "in_progress": "Jarayonda", "completed": "Yakunlangan", "cancelled": "Bekor qilingan"},
    "ru": {"draft": "Черновик", "open": "Открыт", "in_progress": "В процессе", "completed": "Завершён", "cancelled": "Отменён"},
    "en": {"draft": "Draft", "open": "Open", "in_progress": "In progress", "completed": "Completed", "cancelled": "Cancelled"},
}

PAYMENT_STATUS_LABELS = {
    "uz": {"unpaid": "To‘lanmagan", "partial": "Qisman to‘langan", "paid": "To‘langan", "refunded": "Qaytarilgan"},
    "ru": {"unpaid": "Не оплачен", "partial": "Частично оплачен", "paid": "Оплачен", "refunded": "Возвращён"},
    "en": {"unpaid": "Unpaid", "partial": "Partially paid", "paid": "Paid", "refunded": "Refunded"},
}

ITEM_STATUS_LABELS = {
    "uz": {"pending": "Navbatda", "entered": "Kiritilgan", "submitted": "Yuborilgan", "approved": "Tasdiqlangan", "rejected": "Qaytarilgan", "cancelled": "Bekor qilingan"},
    "ru": {"pending": "В очереди", "entered": "Введён", "submitted": "Отправлен", "approved": "Подтверждён", "rejected": "Возвращён", "cancelled": "Отменён"},
    "en": {"pending": "Pending", "entered": "Entered", "submitted": "Submitted", "approved": "Approved", "rejected": "Rejected", "cancelled": "Cancelled"},
}


def _label(labels: dict[str, dict[str, str]], value: str | None, language: str = "uz") -> str:
    table = labels.get(language) or labels["uz"]
    return table.get(value or "", value or "")


def payment_method_label(method: str | None, language: str = "uz") -> str:
    return _label(PAYMENT_METHOD_LABELS, method, language)


def item_status_label(status: str | None, language: str = "uz") -> str:
    return _label(ITEM_STATUS_LABELS, status, language)


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
    region_name: str | None = None,
    items: list[dict[str, Any]] | None = None,
    language: str = "uz",
    today: date | None = None,
    payments: list[dict[str, Any]] | None = None,
    cashier: str | None = None,
    doc_link: str | None = None,
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
    tags = p.get("tags") or []
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
            "pinfl": p.get("pinfl") or "",
            "workplace": p.get("workplace") or "",
            "discountPercent": str(p.get("discountPercent") or 0),
            "contractNumber": p.get("contractNumber") or "",
            "note": p.get("note") or "",
            "tags": ", ".join(str(x) for x in tags),
            "region": region_name or "",
            "district": district_name or "",
            "street": p.get("street") or "",
        },
        "order": _order_block(order or {}, bool(payments), language),
        "item": {
            "serviceName": it.get("serviceName") or "",
            "serviceCode": it.get("serviceCode") or "",
            "categoryName": it.get("categoryName") or "",
            "status": it.get("status") or "",
            "statusLabel": _label(ITEM_STATUS_LABELS, it.get("status"), language),
            "price": fmt_money(it.get("price")) if it.get("price") is not None else "",
            "finalPrice": fmt_money(it.get("finalPrice")) if it.get("finalPrice") is not None else "",
            "enteredAt": _fmt_datetime(it.get("enteredAt")) if it.get("enteredAt") else "",
            "submittedAt": _fmt_datetime(it.get("submittedAt")) if it.get("submittedAt") else "",
            "approvedAt": _fmt_datetime(it.get("approvedAt")) if it.get("approvedAt") else "",
            "technician": it.get("technicianName") or "",
            "doctor": it.get("doctorName") or "",
            "labNote": it.get("labNote") or "",
            "rejectReason": it.get("rejectReason") or "",
            "schemaVersion": str(it.get("schemaVersion")) if it.get("schemaVersion") is not None else "",
        },
        "company": {
            "name": (company or {}).get("name") or "",
            "legalName": (company or {}).get("legalName") or "",
            "slug": (company or {}).get("slug") or "",
            "phone": (company or {}).get("phone"),
            "email": (company or {}).get("email"),
            "address": (company or {}).get("address"),
        },
        "branch": {
            "name": (branch or {}).get("name") or "",
            "code": (branch or {}).get("code") or "",
            "address": (branch or {}).get("address"),
            "phone": fmt_phone((branch or {}).get("phone")) if (branch or {}).get("phone") else None,
            "timezone": (branch or {}).get("timezone") or "",
        },
        "category": {"name": (category or {}).get("name") or "", "code": (category or {}).get("code") or "", "phone": (category or {}).get("phone")},
        "today": fmt_date(ref_day),
        "now": fmt_datetime(datetime.now(UTC)),
        "values": it.get("values") or {},
        "schema": schema,
    }
    if items is not None:
        ctx["items"] = items
    if cashier is not None:
        ctx["cashier"] = {"name": cashier}
    if doc_link is not None:
        # the QR element binds to {doc.link} — the public verification link of the issued document
        ctx["doc"] = {"link": doc_link}
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


def _fmt_order_date(v: datetime | date | str | None) -> str:
    """`{order.date}`: the cheque's date AND time (dd.MM.yyyy HH:mm, clinic timezone); a bare date stays a date; '—' when none."""
    d = _dt(v)
    return fmt_datetime(d) if isinstance(d, datetime) else fmt_date(d)


def _order_block(order: dict[str, Any], has_payments: bool = False, language: str = "uz") -> dict[str, Any]:
    """`order.*` placeholders: number/date for every document, money fields when the order carries them.

    `date` carries the time too; `dateOnly` is the bare date for blanks that want just the day."""
    block: dict[str, Any] = {"number": order.get("number") or "", "date": _fmt_order_date(order.get("createdAt")), "dateOnly": _fmt_date(order.get("createdAt"))}
    created = _dt(order.get("createdAt"))
    if created:
        block["dateTime"] = _fmt_datetime(created)
        block["time"] = _fmt_datetime(created).split(" ")[-1] if isinstance(created, datetime) else ""
    if order.get("status"):
        block["status"] = order.get("status")
        block["statusLabel"] = _label(ORDER_STATUS_LABELS, order.get("status"), language)
    if order.get("payment"):
        block["payment"] = order.get("payment")
        block["paymentLabel"] = _label(PAYMENT_STATUS_LABELS, order.get("payment"), language)
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
                # presence flags for showIf: the discount line, the balance line, the payments table
                "hasDiscount": "1" if float(order.get("discountPercent") or 0) > 0 or float(order.get("discountAmount") or 0) > 0 else "",
                "hasRemaining": "1" if total - paid > 0 else "",
                "hasPayments": "1" if has_payments else "",
            }
        )
    return block
