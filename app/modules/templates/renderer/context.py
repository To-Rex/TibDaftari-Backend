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
) -> dict[str, Any]:
    """RenderItem dict for order-scoped documents (approvedAt pre-formatted 'dd.MM.yyyy HH:mm')."""
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
    return item


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
) -> dict[str, Any]:
    """Build a RenderContext dict (spec §1) from plain dicts.

    Input shapes (all optional):
      patient  {fullName, phone, birthDate, gender, street, passportNumber}
      order    {number, createdAt}
      item     {serviceName, approvedAt, technicianName, doctorName, labNote, values}
      company  {name, phone, address}   branch {name, address}   category {name, phone}
      items    list of RenderItem dicts (see `to_render_item`)
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
        "order": {"number": (order or {}).get("number") or "", "date": _fmt_date((order or {}).get("createdAt"))},
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
        "branch": {"name": (branch or {}).get("name") or "", "address": (branch or {}).get("address")},
        "category": {"name": (category or {}).get("name") or "", "phone": (category or {}).get("phone")},
        "today": fmt_date(ref_day),
        "values": it.get("values") or {},
        "schema": schema,
    }
    if items is not None:
        ctx["items"] = items
    return ctx
