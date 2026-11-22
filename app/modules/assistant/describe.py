"""What a proposed write will touch, in words the employee can check before saying yes: every id among the
arguments is resolved to the name of the thing — through the same service getters the HTTP routes use, so a name
the employee may not see is never shown (the id stays, marked as unknown)."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.exceptions import AppError
from app.modules.assistant.tools import MASK, SECRET_KEYS, Tool, ToolContext, redact
from app.modules.catalog import service as catalog_svc
from app.modules.orders import service as orders_svc
from app.modules.patients import service as patients_svc
from app.modules.staff import service as staff_svc
from app.modules.templates import service as tpl_svc
from app.modules.units import repository as units_repo
from app.modules.units import service as units_svc

UNKNOWN = "noma’lum yoki ruxsat yo‘q"
MAX_ROWS = 24

#: argument → what the employee reads on the card
LABELS: dict[str, str] = {
    "unitId": "Birlik", "parentId": "Yuqori birlik", "facilityId": "Muassasa", "sampleFacilityId": "Namuna muassasa", "unitIds": "Birliklar", "disabledUnitIds": "O‘chirilgan birliklar",
    "registeredUnitId": "Ro‘yxatga olingan muassasa", "employeeId": "Xodim", "roleId": "Rol", "roleKey": "Rol", "categoryId": "Kategoriya", "categoryIds": "Kategoriyalar",
    "serviceTypeId": "Xizmat", "serviceTypeIds": "Xizmatlar", "templateId": "Andoza", "defaultTemplateId": "Standart andoza", "schemaId": "Natija sxemasi", "patientId": "Bemor",
    "relatedPatientId": "Qarindoshi", "orderId": "Chek", "itemId": "Xizmat (chekda)", "itemIds": "Xizmatlar (chekda)", "paymentId": "To‘lov", "documentId": "Hujjat",
    "countryId": "Davlat", "regionId": "Viloyat", "districtId": "Tuman / shahar",
    "name": "Nomi", "shortName": "Qisqa nomi", "legalName": "Yuridik nomi", "fullName": "F.I.Sh.", "login": "Login", "phone": "Telefon", "phoneExtra": "Qo‘shimcha telefon", "email": "E-pochta",
    "address": "Manzil", "code": "Kod", "kind": "Turi", "isActive": "Faol", "status": "Holat", "price": "Narx", "unitPrices": "Birliklar bo‘yicha narx", "turnaroundDays": "Tayyor bo‘lish (kun)",
    "amount": "Summa", "method": "To‘lov usuli", "parts": "Qismlar", "reason": "Sabab", "note": "Izoh", "labNote": "Laboratoriya izohi", "values": "Natija qiymatlari", "text": "Matn", "to": "Kimga",
    "permissions": "Ruxsatlar", "allow": "Qo‘shimcha ruxsatlar", "deny": "Olib tashlangan ruxsatlar", "owner": "Egasi", "templates": "SMS matnlari", "applyToAll": "Ostidagilarga ham",
    "birthDate": "Tug‘ilgan sana", "gender": "Jinsi", "passportNumber": "Pasport", "pinfl": "JSHSHIR", "discountPercent": "Chegirma (%)", "tags": "Belgilar", "relation": "Qarindoshlik",
    "language": "Til", "scope": "Qamrov", "description": "Tavsif", "fields": "Maydonlar", "facility": "Muassasa", "staff": "Xodimlar", "sendSms": "SMS yuborish", "dryRun": "Faqat matnni ko‘rish",
    "documentFreeViewDays": "Kodsiz ko‘rish (kun)", "locale": "Til", "timezone": "Vaqt mintaqasi", "workflow": "Ish tartibi", "paper": "Qog‘oz", "order": "Tartib", "workplace": "Ish joyi",
}
BOOL = {True: "ha", False: "yo‘q"}

Resolver = Callable[[ToolContext, str], Awaitable[str]]


async def _unit(ctx: ToolContext, value: str) -> str:
    uid = uuid.UUID(value)
    unit = await units_svc.get_unit_or_404(ctx.session, uid)
    await units_svc.require_visible(ctx.session, ctx.staff, unit)
    return await units_repo.unit_path(ctx.session, uid) or unit.name


async def _employee(ctx: ToolContext, value: str) -> str:
    e = await staff_svc.get_employee_dto(ctx.session, uuid.UUID(value), ctx.staff)
    return f"{e.full_name} ({e.login})"


async def _role(ctx: ToolContext, value: str) -> str:
    for r in await staff_svc.list_roles(ctx.session, ctx.scope):
        if str(r.id) == value:
            return r.name
    raise LookupError(value)


async def _category(ctx: ToolContext, value: str) -> str:
    for c in await catalog_svc.list_categories(ctx.session, ctx.scope):
        if str(c.id) == value:
            return c.name
    raise LookupError(value)


async def _service(ctx: ToolContext, value: str) -> str:
    return (await catalog_svc.get_service_type_dto(ctx.session, uuid.UUID(value), ctx.staff)).name


async def _template(ctx: ToolContext, value: str) -> str:
    return (await tpl_svc.get_template_dto(ctx.session, uuid.UUID(value), ctx.staff)).name


async def _schema(ctx: ToolContext, value: str) -> str:
    return (await catalog_svc.get_schema_dto(ctx.session, uuid.UUID(value), ctx.staff)).name


async def _patient(ctx: ToolContext, value: str) -> str:
    p = await patients_svc.get_patient_dto(ctx.session, uuid.UUID(value), ctx.staff)
    return f"{p.full_name} · {p.phone}" if p.phone else p.full_name


async def _order(ctx: ToolContext, value: str) -> str:
    b = await orders_svc.get_order_bundle(ctx.session, uuid.UUID(value), ctx.staff)
    o = b.order
    return " · ".join(str(x) for x in (f"№ {o.number}", getattr(o, "patient_name", None), getattr(o, "unit_name", None)) if x)


async def _item(ctx: ToolContext, value: str) -> str:
    i = await orders_svc.get_item_dto(ctx.session, uuid.UUID(value), ctx.staff)
    return " · ".join(str(x) for x in (getattr(i, "service_name", None), getattr(i, "patient_name", None)) if x) or value


async def _document(ctx: ToolContext, value: str) -> str:
    d = await orders_svc.get_document_dto(ctx.session, uuid.UUID(value), ctx.staff)
    return " · ".join(str(x) for x in (getattr(d, "number", None), getattr(d, "patient_name", None)) if x) or value


def _geo(kind: str) -> Resolver:
    async def find(ctx: ToolContext, value: str) -> str:
        if kind == "countries":
            rows = await patients_svc.list_countries(ctx.session)
        elif kind == "regions":
            rows = await patients_svc.list_regions(ctx.session, None) + (await patients_svc.list_regions(ctx.session, ctx.scope.country_id) if ctx.scope.country_id else [])
        else:
            rows = await patients_svc.list_districts(ctx.session, None)
        for r in rows:
            if str(r.get("id")) == value:
                return str(r.get("name"))
        raise LookupError(value)

    return find


RESOLVERS: dict[str, Resolver] = {
    "unitId": _unit, "parentId": _unit, "facilityId": _unit, "sampleFacilityId": _unit, "unitIds": _unit, "disabledUnitIds": _unit, "registeredUnitId": _unit,
    "employeeId": _employee, "roleId": _role, "categoryId": _category, "categoryIds": _category, "serviceTypeId": _service, "serviceTypeIds": _service,
    "templateId": _template, "defaultTemplateId": _template, "schemaId": _schema, "patientId": _patient, "relatedPatientId": _patient, "orderId": _order,
    "itemId": _item, "itemIds": _item, "documentId": _document, "countryId": _geo("countries"), "regionId": _geo("regions"), "districtId": _geo("districts"),
}


async def _name(ctx: ToolContext, resolve: Resolver, value: Any) -> str:
    try:
        return await resolve(ctx, str(value))
    except (AppError, LookupError, ValueError, AttributeError, TypeError):
        return f"{str(value)[:36]} ({UNKNOWN})"


def _plain(value: Any) -> str:
    if isinstance(value, bool):
        return BOOL[value]
    if isinstance(value, (int, float)):
        return f"{value:,}".replace(",", " ")
    if isinstance(value, list):
        return ", ".join(_plain(v) for v in value[:12]) + (f" … (+{len(value) - 12})" if len(value) > 12 else "")
    if isinstance(value, dict):
        return "; ".join(f"{LABELS.get(k, k)}: {_plain(v)}" for k, v in list(value.items())[:12] if k not in SECRET_KEYS)
    return redact(str(value))


async def describe(ctx: ToolContext, t: Tool, args: dict[str, Any]) -> list[dict[str, str]]:
    """[{label, value}] for the confirmation card: the unit the write lands in first (the working unit when the
    arguments name none), then every argument with ids turned into names."""
    rows: list[dict[str, str]] = []
    props = t.params.get("properties") or {}
    targets = [k for k in ("unitId", "facilityId", "parentId") if k in props]
    if targets and not any(args.get(k) for k in targets):
        rows.append({"label": LABELS[targets[0]], "value": await _name(ctx, _unit, ctx.unit_id)})
    for key, value in args.items():
        if value is None or value == "" or len(rows) >= MAX_ROWS:
            continue
        if key in SECRET_KEYS:
            rows.append({"label": LABELS.get(key, key), "value": MASK})
            continue
        resolve = RESOLVERS.get(key)
        if resolve is not None and isinstance(value, list):
            names = [await _name(ctx, resolve, v) for v in value[:12]]
            text = ", ".join(names) + (f" … (+{len(value) - 12})" if len(value) > 12 else "")
        elif resolve is not None:
            text = await _name(ctx, resolve, value)
        else:
            text = _plain(value)
        rows.append({"label": LABELS.get(key, key), "value": text[:600]})
    return rows
