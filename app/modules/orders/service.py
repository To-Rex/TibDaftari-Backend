"""Orders workflow — cheques, items, payments/refunds, lab worklist, result values, submit/approve/reject,
order-scope approval and result documents (DOMAIN_RULES section 7).

Tenancy (docs/V2_PORT.md): a cheque, its items, payments and documents belong to ONE facility (`unit_id`). Lists
take a `UnitScope` (the URL's unit and everything under it, optionally narrowed by `unitId`); row entry points load
the row and call `staff.require_unit(row.unit_id)`. Services, prices, templates, SMS accounts and texts are
resolved for the facility by the catalog / templates / messaging modules (owner on the facility's path, nearest wins).

Every mutation ends with `recompute()` (totals, progress, payment + order status) and keeps the
patient's denormalised stats in sync. Approval freezes a render snapshot (templates.service),
renders + stores the PDF, bumps template usage and queues SMS/Telegram notifications.
Other modules (portal, telegram) consume `order_out`, `item_out`, `document_out`, `ensure_document_pdf`,
`mark_document_viewed`, `pdf_filename` and `allowed_category_ids`.
"""

from __future__ import annotations

import copy
import hmac
import logging
import math
import re
import secrets
import uuid
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text as sql_text
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.api.deps import RequestMeta, StaffPrincipal
from app.core.audit import audit
from app.core.config import settings
from app.core.exceptions import (
    AuthError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    RateLimitedError,
    StateError,
    ValidationError,
)
from app.core.pagination import page_of
from app.core.schemas import Page, iso_z
from app.core.scope import UnitScope, ids_of
from app.core.security import random_digits, sha256_hex
from app.core.textutil import is_valid_uz_phone, norm_phone, slugify
from app.core.timeutil import utcnow
from app.infrastructure.db.models import (
    Order,
    OrderItem,
    OrgUnit,
    OtpChallenge,
    Patient,
    Payment,
    ResultDocument,
    ResultTemplate,
    empty_progress,
)
from app.infrastructure.redis import cache, rate_limit
from app.infrastructure.redis.client import get_redis
from app.modules.catalog import service as catalog_svc
from app.modules.files import service as files_svc
from app.modules.messaging import service as messaging
from app.modules.orders import export as order_export
from app.modules.orders import repository as repo
from app.modules.orders.schemas import (
    AddItemsIn,
    ApproveItemIn,
    ApproveItemOut,
    ApproveOrderIn,
    ApproveOrderOut,
    CreateOrderIn,
    DocumentDeliveryOut,
    ItemEventOut,
    MethodSumOut,
    OrderBundleOut,
    OrderExportQuery,
    OrderItemOut,
    OrderItemsOut,
    OrderListQuery,
    OrderOut,
    OrderPaymentsOut,
    OrderSummaryOut,
    PayIn,
    PaymentCorrectionOut,
    PaymentMethodIn,
    PaymentOut,
    ProgressOut,
    PublicDocumentInfoOut,
    PublicDocumentOtpOut,
    PublicDocumentVerifyOut,
    ResultDocumentOut,
    ResultSmsIn,
    ResultSmsOut,
    RevokeOut,
    SaveValuesIn,
    WorklistCountsOut,
    WorklistItemOut,
    WorklistQuery,
)
from app.modules.patients import service as patients_svc
from app.modules.templates import service as templates_svc
from app.modules.templates.renderer.context import build_render_context, item_status_label, to_render_item
from app.modules.units import repository as units_repo
from app.modules.units import service as units_svc
from app.modules.units.schemas import document_free_view_days

log = logging.getLogger("app.orders")

ORDER_NOT_FOUND = "Chek topilmadi"
ITEM_NOT_FOUND = "Tahlil topilmadi"
DOC_NOT_FOUND = "Hujjat topilmadi"
UNIT_NOT_FOUND = "Muassasa topilmadi"
PATIENT_NOT_FOUND = "Bemor topilmadi"


def _round_half_up(x: float) -> int:
    """JS `Math.round` for non-negative money values."""
    return math.floor(x + 0.5)


def _touch(row: Any, now: datetime) -> None:
    """Set `updated_at` explicitly (forced into the UPDATE so the server-side onupdate never expires it)."""
    row.updated_at = now
    flag_modified(row, "updated_at")


def _s(v: uuid.UUID | None) -> str | None:
    return str(v) if v is not None else None


def _unit_filter(scope: UnitScope, unit_id: str | None) -> list[uuid.UUID]:
    """The facilities a list covers: everything under the URL's unit, narrowed to `unitId` when given (a unit
    outside the scope — or a malformed id — matches nothing)."""
    if not unit_id:
        return scope.descendant_ids
    return scope.narrow(ids_of([unit_id]) or [uuid.UUID(int=0)])


async def _unit_names(session: AsyncSession, ids: Iterable[uuid.UUID | None]) -> dict[uuid.UUID, str]:
    """facility id → name, for the `unitName` of list rows (one IN query per list; closed facilities included)."""
    return {u.id: u.name for u in (await units_repo.units_by_ids(session, ids)).values()}


# ----------------------------------------------------------------------------- projections


def order_out(o: Order, unit_name: str | None = None) -> OrderOut:
    """ORM row -> frontend `Order`."""
    return OrderOut(
        id=str(o.id),
        unit_id=str(o.unit_id),
        unit_name=unit_name,
        number=o.number,
        patient_id=str(o.patient_id),
        patient_name=o.patient_name,
        patient_phone=o.patient_phone,
        created_by_employee_id=str(o.created_by_employee_id),
        status=o.status,  # type: ignore[arg-type]
        payment=o.payment,  # type: ignore[arg-type]
        subtotal=o.subtotal,
        discount_percent=o.discount_percent,
        discount_amount=o.discount_amount,
        total=o.total,
        paid_amount=o.paid_amount,
        item_count=o.item_count,
        progress=ProgressOut(**{**empty_progress(), **(o.progress or {})}),
        note=o.note,
        cancel_reason=o.cancel_reason,
        cancelled_at=o.cancelled_at,
        created_at=o.created_at,
        updated_at=o.updated_at,
        created_by=_s(o.created_by),
        first_paid_at=o.first_paid_at,
    )


def item_out(i: OrderItem) -> OrderItemOut:
    """ORM row -> frontend `OrderItem`."""
    return OrderItemOut(**_item_fields(i))


def _item_fields(i: OrderItem) -> dict[str, Any]:
    return {
        "id": str(i.id),
        "order_id": str(i.order_id),
        "unit_id": str(i.unit_id),
        "service_type_id": str(i.service_type_id),
        "service_name": i.service_name,
        "category_id": str(i.category_id),
        "category_name": i.category_name,
        "price": i.price,
        "final_price": i.final_price,
        "status": i.status,
        "schema_id": _s(i.schema_id),
        "schema_version": i.schema_version,
        "values": dict(i.values or {}),
        "technician_id": _s(i.technician_id),
        "technician_name": i.technician_name,
        "entered_at": i.entered_at,
        "submitted_at": i.submitted_at,
        "doctor_id": _s(i.doctor_id),
        "doctor_name": i.doctor_name,
        "approved_at": i.approved_at,
        "reject_reason": i.reject_reason,
        "document_id": _s(i.document_id),
        "lab_note": i.lab_note,
        "created_at": i.created_at,
        "updated_at": i.updated_at,
        "created_by": _s(i.created_by),
        "history": [ItemEventOut.model_validate(e) for e in item_history(i) if isinstance(e, dict)],
    }


def _legacy_history(i: OrderItem) -> list[dict[str, Any]]:
    """Items from before the trail existed: what their timestamps tell (shown until their next event)."""
    events: list[dict[str, Any]] = []
    if i.submitted_at:
        events.append({"type": "submitted", "at": iso_z(i.submitted_at), "byId": _s(i.technician_id), "byName": i.technician_name})
    if i.status == "approved" and i.approved_at:
        events.append({"type": "approved", "at": iso_z(i.approved_at), "byId": _s(i.doctor_id), "byName": i.doctor_name, "documentId": _s(i.document_id)})
    if i.status == "rejected" and i.reject_reason and i.updated_at:
        events.append({"type": "returned", "at": iso_z(i.updated_at), "reason": i.reject_reason})
    return events


def item_history(i: OrderItem) -> list[dict[str, Any]]:
    """The item's result trail, oldest first. A rebuilt step stored right before the identical new one (same type and
    time — the first release of the trail did that) is shown once."""
    events = list(i.history) if i.history else _legacy_history(i)
    out: list[dict[str, Any]] = []
    for e in events:
        if out and isinstance(e, dict) and e.get("type") == out[-1].get("type") and e.get("at") == out[-1].get("at"):
            continue
        out.append(e)
    return out


def _seed_history(i: OrderItem) -> None:
    """Call BEFORE an item's state changes: an item from before the trail keeps what its timestamps tell."""
    if not i.history:
        legacy = _legacy_history(i)
        if legacy:
            i.history = legacy


def _push_history(i: OrderItem, kind: str, staff: StaffPrincipal, now: datetime, *, reason: str | None = None, document_id: uuid.UUID | str | None = None) -> None:
    event: dict[str, Any] = {"type": kind, "at": iso_z(now), "byId": str(staff.id), "byName": staff.employee.full_name}
    if reason:
        event["reason"] = reason
    if document_id:
        event["documentId"] = str(document_id)
    i.history = [*(i.history or []), event]  # reassign → the JSONB change is tracked


def payment_out(p: Payment) -> PaymentOut:
    """ORM row -> frontend `Payment`."""
    return PaymentOut(
        id=str(p.id),
        order_id=str(p.order_id),
        unit_id=str(p.unit_id),
        amount=p.amount,
        method=p.method,  # type: ignore[arg-type]
        employee_id=str(p.employee_id),
        note=p.note,
        refunded_at=p.refunded_at,
        created_at=p.created_at,
        updated_at=p.updated_at,
        created_by=_s(p.created_by),
        corrections=[PaymentCorrectionOut.model_validate(c) for c in p.corrections or []],
    )


def document_pdf_url(document_id: uuid.UUID | str) -> str:
    """Staff PDF endpoint of a document."""
    return f"/api/v1/documents/{document_id}/pdf"


def _public_base(meta: RequestMeta | None) -> str:
    """The web app public links point at: the app the staff used (its Origin, checked against the CORS list),
    else FRONTEND_URL."""
    return (meta.origin if meta and meta.origin else settings.frontend_url).rstrip("/")


def public_result_link(doc: ResultDocument, meta: RequestMeta | None) -> str:
    """Short public link to a result for SMS / Telegram: `<web app>/d/<token>`."""
    if not doc.public_token:
        return ""
    return f"{_public_base(meta)}/d/{doc.public_token}"


def public_verify_link_for_token(token: str, meta: RequestMeta | None) -> str:
    """The QR authenticity link `<web app>/v/<verify token>` — frozen into the render snapshot as `{doc.link}`.
    It only answers whether the document is genuine; the result itself stays behind the SMS link."""
    return f"{_public_base(meta)}/v/{token}"


def document_qr_link(d: ResultDocument) -> str | None:
    """The exact value the document's QR encodes (`{doc.link}` frozen in its snapshot): `/v/…` since 2026-10-29,
    the SMS link `/d/…` for documents issued before; None when the snapshot carries no link."""
    ctx = (d.snapshot or {}).get("context") or {}
    return (ctx.get("doc") or {}).get("link") or None


def document_out(d: ResultDocument, unit_name: str | None = None) -> ResultDocumentOut:
    """ORM row -> frontend `ResultDocument` (`pdfUrl` always points at the staff PDF endpoint)."""
    return ResultDocumentOut(
        id=str(d.id),
        unit_id=str(d.unit_id),
        unit_name=unit_name,
        order_id=str(d.order_id),
        order_item_id=_s(d.order_item_id),
        order_item_ids=[str(x) for x in d.order_item_ids] if d.order_item_ids else None,
        template_id=str(d.template_id),
        template_version=d.template_version,
        title=d.title,
        status=d.status,  # type: ignore[arg-type]
        pdf_url=document_pdf_url(d.id),
        deliveries=[DocumentDeliveryOut(**x) for x in (d.deliveries or [])],
        created_at=d.created_at,
        updated_at=d.updated_at,
        created_by=_s(d.created_by),
        public_token=d.public_token,
        verify_link=document_qr_link(d),
    )


# ----------------------------------------------------------------------------- look-ups


async def get_order_or_404(session: AsyncSession, order_id: uuid.UUID, staff: StaffPrincipal, *, for_update: bool = False) -> Order:
    """Order or 404 'Chek topilmadi'; 403 when its facility is outside the employee's units.
    `for_update` locks the row (all order mutations)."""
    o = await repo.get_order(session, order_id, for_update=for_update)
    if not o:
        raise NotFoundError(ORDER_NOT_FOUND)
    staff.require_unit(o.unit_id)
    return o


async def _lock_order_of_item(session: AsyncSession, item: OrderItem, staff: StaffPrincipal) -> Order:
    """Lock the parent order (serialises item mutations per order) and re-read the item's committed state."""
    order = await get_order_or_404(session, item.order_id, staff, for_update=True)
    await session.refresh(item)
    return order


async def get_item_or_404(session: AsyncSession, item_id: uuid.UUID, staff: StaffPrincipal) -> OrderItem:
    """Item or 404 'Tahlil topilmadi'; 403 outside the employee's units."""
    it = await repo.get_item(session, item_id)
    if not it:
        raise NotFoundError(ITEM_NOT_FOUND)
    staff.require_unit(it.unit_id)
    return it


async def get_document_or_404(session: AsyncSession, document_id: uuid.UUID, staff: StaffPrincipal) -> ResultDocument:
    """Document or 404 'Hujjat topilmadi'; 403 outside the employee's units."""
    d = await repo.get_document(session, document_id)
    if not d:
        raise NotFoundError(DOC_NOT_FOUND)
    staff.require_unit(d.unit_id)
    return d


async def get_document_dto(session: AsyncSession, document_id: uuid.UUID, staff: StaffPrincipal) -> ResultDocumentOut:
    doc = await get_document_or_404(session, document_id, staff)
    return document_out(doc, (await _unit_names(session, [doc.unit_id])).get(doc.unit_id))


async def get_order_bundle(session: AsyncSession, order_id: uuid.UUID, staff: StaffPrincipal) -> OrderBundleOut:
    """{order, items, payments} — the shape of `GET /orders/{id}`."""
    o = await get_order_or_404(session, order_id, staff)
    items = await repo.items_of_order(session, o.id)
    payments = await repo.payments_of_order(session, o.id)
    return OrderBundleOut(
        order=order_out(o, (await _unit_names(session, [o.unit_id])).get(o.unit_id)),
        items=[item_out(i) for i in items],
        payments=[payment_out(p) for p in payments],
    )


async def _order_items_out(session: AsyncSession, o: Order) -> OrderItemsOut:
    return OrderItemsOut(order=order_out(o, (await _unit_names(session, [o.unit_id])).get(o.unit_id)), items=[item_out(i) for i in await repo.items_of_order(session, o.id)])


async def _order_payments_out(session: AsyncSession, o: Order) -> OrderPaymentsOut:
    return OrderPaymentsOut(
        order=order_out(o, (await _unit_names(session, [o.unit_id])).get(o.unit_id)), payments=[payment_out(p) for p in await repo.payments_of_order(session, o.id)]
    )


def _patient_ctx(p: Any) -> dict[str, Any]:
    return {
        "fullName": p.full_name,
        "phone": p.phone,
        "birthDate": p.birth_date,
        "gender": p.gender,
        "street": getattr(p, "street", None),
        "passportNumber": getattr(p, "passport_number", None),
        "pinfl": getattr(p, "pinfl", None),
        "workplace": getattr(p, "workplace", None),
        "discountPercent": getattr(p, "discount_percent", 0),
        "contractNumber": getattr(p, "contract_number", None),
        "note": getattr(p, "note", None),
        "tags": getattr(p, "tags", None),
    }


# ----------------------------------------------------------------------------- facility / templates


async def _facility_or_404(session: AsyncSession, unit_id: uuid.UUID) -> OrgUnit:
    """The facility a row belongs to (closed facilities included — their history still renders)."""
    unit = await units_repo.get_unit_any(session, unit_id)
    if unit is None:
        raise NotFoundError(UNIT_NOT_FOUND)
    return unit


def _template_rank(tpl: ResultTemplate, bound: set[uuid.UUID], path: list[uuid.UUID]) -> tuple[int, int] | None:
    """How specific a template is for the facility whose root → facility path is `path`:
    (depth of its nearest explicit `template_units` binding on the path — -1 when unbound; depth of its owner).
    None = not usable there (owner off the path, or bound only to units off the path)."""
    depth = {u: i for i, u in enumerate(path)}
    owner_depth = depth.get(tpl.owner_unit_id)
    if owner_depth is None:
        return None
    if bound:
        on_path = [depth[u] for u in bound if u in depth]
        if not on_path:
            return None
        return max(on_path), owner_depth
    return -1, owner_depth


def _rank_key(ranked: tuple[tuple[int, int], ResultTemplate]) -> tuple[int, int, float, str]:
    (binding, owner), tpl = ranked
    created = tpl.created_at.timestamp() if getattr(tpl, "created_at", None) else 0.0
    # the most specific binding first, then the nearest owner, then the oldest
    return (-binding, -owner, created, str(tpl.id))


async def _pick_template(session: AsyncSession, facility_id: uuid.UUID, *predicates: Callable[[ResultTemplate], bool]) -> ResultTemplate | None:
    """The best active template usable at the facility for the first predicate that matches any — explicit
    `template_units` binding to the facility > binding to a nearer ancestor > unbound; a nearer owner wins ties."""
    scope = await units_svc.scope_or_404(session, facility_id)
    active = await templates_svc.list_active_templates(session, facility_id)
    for predicate in predicates:
        candidates = [t for t in active if predicate(t)]
        if not candidates:
            continue
        bindings = await repo.template_units_for(session, [t.id for t in candidates])
        ranked = []
        for t in candidates:
            rank = _template_rank(t, bindings.get(t.id) or set(), scope.ancestor_ids)
            if rank is not None:
                ranked.append((rank, t))
        if ranked:
            ranked.sort(key=_rank_key)
            return ranked[0][1]
    return None


async def _template_usable_at(session: AsyncSession, tpl: ResultTemplate, facility_id: uuid.UUID) -> bool:
    """An explicitly chosen template may be used at the facility: owned on its path and not limited to other units."""
    scope = await units_svc.scope_or_404(session, facility_id)
    bound = (await repo.template_units_for(session, [tpl.id])).get(tpl.id) or set()
    return _template_rank(tpl, bound, scope.ancestor_ids) is not None


async def render_receipt_pdf(session: AsyncSession, order_id: uuid.UUID, template_id: uuid.UUID | None, staff: StaffPrincipal) -> tuple[bytes, str]:
    """The cheque rendered with a receipt template (explicit `template_id`, else the facility's active one — bound
    to it, else the nearest owner's). 404 `no_template` when there is none — the client then prints its built-in cheque."""
    o = await get_order_or_404(session, order_id, staff)
    tpl = await repo.get_template(session, template_id) if template_id else None
    if tpl is not None and (tpl.scope != "receipt" or not await _template_usable_at(session, tpl, o.unit_id)):
        tpl = None
    if tpl is None:
        tpl = await _pick_template(session, o.unit_id, lambda t: t.scope == "receipt")
    if tpl is None:
        raise NotFoundError("Chek andozasi yo‘q", code="no_template")
    items = [i for i in await repo.items_of_order(session, o.id) if i.status != "cancelled"]
    payments = [p for p in await repo.payments_of_order(session, o.id) if p.refunded_at is None]
    patient = await repo.get_patient(session, o.patient_id)
    # the renderer builds the `org` (organisation) and `unit` (facility) blocks — and their company / branch aliases
    org, unit = await templates_svc.org_context(session, o.unit_id)
    effective = await units_svc.effective_settings(session, o.unit_id)
    cashier = await repo.get_employee_name(session, o.created_by_employee_id)
    district_name = await repo.get_district_name(session, patient.district_id) if patient else None
    region_name = await repo.get_region_name(session, patient.region_id) if patient else None
    sts = await catalog_svc.get_service_types_by_ids(session, {i.service_type_id for i in items})
    language = str(tpl.language or effective.get("locale") or "uz")
    ctx = build_render_context(
        patient=_patient_ctx(patient) if patient else None,
        order={
            "number": o.number,
            "createdAt": o.created_at,
            "subtotal": o.subtotal,
            "discountPercent": o.discount_percent,
            "discountAmount": o.discount_amount,
            "total": o.total,
            "paidAmount": o.paid_amount,
            "itemCount": len(items),
            "note": o.note,
            "status": o.status,
            "payment": o.payment,
        },
        org=org,
        unit=unit,
        items=[
            to_render_item(
                code=(sts[i.service_type_id].code if i.service_type_id in sts and sts[i.service_type_id].code else str(i.service_type_id)),
                service_type_id=str(i.service_type_id),
                service_name=i.service_name,
                status=i.status,
                status_label=item_status_label(i.status, language),
                values={},
                schema=None,
                entered_at=i.entered_at,
                submitted_at=i.submitted_at,
                approved_at=i.approved_at,
                technician=i.technician_name,
                doctor=i.doctor_name,
                price=i.price,
                final_price=i.final_price,
                category=i.category_name,
                schema_version=i.schema_version,
            )
            for i in items
        ],
        payments=[{"createdAt": p.created_at, "method": p.method, "amount": p.amount, "note": p.note} for p in payments],
        cashier=cashier,
        district_name=district_name,
        region_name=region_name,
        language=language,
    )
    pdf = await templates_svc.render_doc_pdf(session, tpl.doc or {}, ctx)
    return pdf, f"chek-{o.number}.pdf"


# ----------------------------------------------------------------------------- recompute


def apply_recompute(order: Order, items: list[OrderItem], payments: list[Payment], now: datetime) -> None:
    """Pure DOMAIN_RULES section 7 `recompute` over already-loaded alive items/payments."""
    active = [i for i in items if i.status != "cancelled"]
    order.subtotal = sum(i.price for i in active)
    order.discount_amount = _round_half_up(order.subtotal * order.discount_percent / 100)
    order.total = order.subtotal - order.discount_amount
    order.item_count = len(active)
    progress = empty_progress()
    for i in items:
        progress[i.status] = progress.get(i.status, 0) + 1
    order.progress = progress
    order.paid_amount = sum(p.amount for p in payments if p.refunded_at is None)
    order.payment = "unpaid" if order.paid_amount <= 0 else ("paid" if order.paid_amount >= order.total else "partial")
    if order.status != "cancelled":
        if active and all(i.status == "approved" for i in active):
            if order.status != "completed":
                order.completed_at = now
            order.status = "completed"
        else:
            order.status = "open" if order.payment == "unpaid" else "in_progress"
            order.completed_at = None
    _touch(order, now)


async def recompute(session: AsyncSession, order: Order, now: datetime | None = None) -> Order:
    """Flush pending item/payment changes, reload them and recompute the order aggregate."""
    await session.flush()
    items = await repo.items_of_order(session, order.id)
    payments = await repo.payments_of_order(session, order.id)
    apply_recompute(order, items, payments, now or utcnow())
    await session.flush()
    return order


def _note_first_payment(order: Order, at: datetime) -> None:
    """Keep `first_paid_at` exact in the same transaction the DB trigger sets it (the service's own view of the
    cheque never lags behind the row): the first live payment opens the day-long reception edit window."""
    if order.first_paid_at is None:
        order.first_paid_at = at


# ----------------------------------------------------------------------------- list / get


async def list_orders(session: AsyncSession, scope: UnitScope, q: OrderListQuery) -> Page[OrderOut]:
    """Paged cheques of the unit and everything under it (narrowed by `unitId`)."""
    rows, total = await repo.list_orders(session, _unit_filter(scope, q.unit_id), q)
    ids = [o.id for o in rows]
    paid = await repo.payments_by_order(session, ids)
    names = await _unit_names(session, {o.unit_id for o in rows})
    return page_of([_with_methods(order_out(o, names.get(o.unit_id)), paid.get(o.id, {})) for o in rows], q, total)


def _with_methods(dto: OrderOut, by_method: dict[str, int]) -> OrderOut:
    return dto.model_copy(update={"payments_by_method": [MethodSumOut(method=m, amount=a) for m, a in sorted(by_method.items(), key=lambda x: -x[1])]})  # type: ignore[arg-type]


async def export_orders_xlsx(session: AsyncSession, scope: UnitScope, q: OrderExportQuery) -> tuple[bytes, str, int]:
    """The cheques the list's filters + sort select, as an Excel workbook (bytes, file name, rows)."""
    rows, truncated = await repo.export_orders(session, _unit_filter(scope, q.unit_id), q, order_export.EXPORT_LIMIT)
    ids = [r.Order.id for r in rows]
    data = order_export.build_xlsx(
        rows, await repo.payments_by_order(session, ids), await repo.service_names_by_order(session, ids),
        lang=q.lang, caption=q.caption, truncated=truncated, generated_at=utcnow(),
    )
    stamp = (q.date_from or "") + ("_" + q.date_to if q.date_to and q.date_to != q.date_from else "")
    return data, f"cheklar{'-' + stamp if stamp else ''}.xlsx", len(rows)


async def orders_summary(session: AsyncSession, scope: UnitScope, q: OrderListQuery) -> OrderSummaryOut:
    """The list's filters at a glance (same unit scope as the list)."""
    data = await repo.orders_summary(session, _unit_filter(scope, q.unit_id), q)
    return OrderSummaryOut.model_validate(data)


# ----------------------------------------------------------------------------- create / items


def _seed_values(fields: list[dict[str, Any]]) -> dict[str, Any]:
    """Table fields -> copy of presetRows, everything else -> null."""
    values: dict[str, Any] = {}
    for f in fields or []:
        key = f.get("key")
        if not key:
            continue
        values[key] = copy.deepcopy(f.get("presetRows") or []) if f.get("type") == "table" else None
    return values


NOT_IN_UNIT = "«{name}» xizmati bu muassasada ko‘rsatilmaydi"


async def _add_items(
    session: AsyncSession, order: Order, service_type_ids: list[str], actor_id: uuid.UUID, now: datetime
) -> list[OrderItem]:
    """Append items for the given service types (unknown ids skipped, duplicates allowed). A service the cheque's
    facility does not offer — owned outside its path, or switched off for it (`service_unit_settings`, nearest level
    wins) — is refused with 409 not_in_branch; reception never lists it there, this is the safety net. The price is
    the facility's effective one (nearest override, else the base price)."""
    wanted = repo.parse_uuids(service_type_ids)
    sts = await catalog_svc.get_service_types_by_ids(session, set(wanted))
    path = set((await units_svc.scope_or_404(session, order.unit_id)).ancestor_ids)
    effective = await catalog_svc.effective_for_unit(session, list(sts.values()), order.unit_id) if sts else {}
    cats = await repo.categories_by_ids(session, {s.category_id for s in sts.values()})
    schemas = await repo.schemas_by_ids(session, [s.schema_id for s in sts.values()])
    created: list[OrderItem] = []
    for st_id in wanted:
        st = sts.get(st_id)
        if st is None:
            continue
        enabled, price = effective.get(st.id, (True, st.price))
        if st.owner_unit_id not in path or not enabled:
            raise ConflictError(NOT_IN_UNIT.format(name=st.name), code="not_in_branch")
        schema = schemas.get(st.schema_id) if st.schema_id else None
        price = int(price if price is not None else st.price)
        item = OrderItem(
            order_id=order.id,
            unit_id=order.unit_id,
            service_type_id=st.id,
            service_name=st.name,
            category_id=st.category_id,
            category_name=cats[st.category_id].name if st.category_id in cats else "",
            price=price,
            final_price=_round_half_up(price - price * order.discount_percent / 100),
            status="pending",
            schema_id=st.schema_id,
            schema_version=schema.version if schema else None,
            values=_seed_values(schema.fields) if schema else {},
            created_by=actor_id,
            created_at=now,
            updated_at=now,
        )
        session.add(item)
        created.append(item)
    await session.flush()
    return created


async def create_order(session: AsyncSession, scope: UnitScope, staff: StaffPrincipal, body: CreateOrderIn, meta: RequestMeta) -> OrderItemsOut:
    """New cheque in a facility (the URL's unit when it is one, else `unitId` under it): number from the facility's
    counter (`next_order_number`), patient snapshots, patient stats, then items."""
    facility = await units_svc.facility_or_422(session, scope, body.unit_id)
    patient = await patients_svc.get_patient_or_404(session, body.patient_id, staff)
    if patient.country_id != facility.country_id:
        raise NotFoundError(PATIENT_NOT_FOUND)
    number = (await session.execute(sql_text("SELECT next_order_number(:u)"), {"u": facility.id})).scalar_one()
    now = utcnow()
    order = Order(
        unit_id=facility.id,
        number=number,
        patient_id=patient.id,
        patient_name=patient.full_name,
        patient_phone=patient.phone,
        created_by_employee_id=staff.id,
        status="open",
        payment="unpaid",
        discount_percent=patient.discount_percent,
        progress=empty_progress(),
        note=body.note or None,
        created_by=staff.id,
        created_at=now,
        updated_at=now,
    )
    session.add(order)
    await repo.bump_patient_stats(session, patient.id, orders=1, visit_at=now, now=now)
    await session.flush()
    await _add_items(session, order, body.service_type_ids, staff.id, now)
    await recompute(session, order, now)
    await audit(
        session,
        actor_type="staff",
        actor_id=staff.id,
        unit_id=facility.id,
        country_id=facility.country_id,
        action="order.create",
        entity="order",
        entity_id=order.id,
        after={
            "number": order.number,
            "patientId": str(patient.id),
            "unitId": str(facility.id),
            "total": order.total,
            "itemCount": order.item_count,
        },
        ip=meta.ip,
        request_id=meta.request_id,
    )
    return await _order_items_out(session, order)


async def add_items(session: AsyncSession, order_id: uuid.UUID, staff: StaffPrincipal, body: AddItemsIn, meta: RequestMeta) -> OrderItemsOut:
    """Append services to an open/in-progress cheque (closed -> 409)."""
    order = await get_order_or_404(session, order_id, staff, for_update=True)
    if order.status in ("cancelled", "completed"):
        raise ConflictError("Chek yopilgan", code="closed")
    now = utcnow()
    created = await _add_items(session, order, body.service_type_ids, staff.id, now)
    await recompute(session, order, now)
    await audit(
        session,
        actor_type="staff",
        actor_id=staff.id,
        unit_id=order.unit_id,
        action="order.items.add",
        entity="order",
        entity_id=order.id,
        after={"itemIds": [str(i.id) for i in created], "total": order.total},
        ip=meta.ip,
        request_id=meta.request_id,
    )
    return await _order_items_out(session, order)


#: reception may change a payment (refund it, give it back for a removed service, delete it with the cheque) only this
#: long after it was taken; later only `reception.payment.late_edit` (admin and rahbar roles; superadmin always)
PAYMENT_EDIT_WINDOW = timedelta(hours=24)
LATE_EDIT = "reception.payment.late_edit"
PAYMENT_LOCKED = "To‘lovga 1 kundan ko‘p vaqt o‘tgan — uni faqat administrator yoki rahbar o‘zgartira oladi"


def payment_locked(created_at: datetime, now: datetime) -> bool:
    """Whether a payment taken at `created_at` is past the reception's edit window."""
    return now - created_at >= PAYMENT_EDIT_WINDOW


def ensure_payments_editable(staff: StaffPrincipal, payments: list[Payment], now: datetime) -> None:
    """403 payment_locked when any of `payments` is older than a day and the caller lacks `late_edit`."""
    if not staff.has(LATE_EDIT) and any(payment_locked(p.created_at, now) for p in payments):
        raise ForbiddenError(PAYMENT_LOCKED, code="payment_locked")


def plan_overpayment_return(payments: list[Payment], excess: int) -> tuple[list[Payment], int]:
    """What a cheque gives back once a service left it: the newest live payments are refunded until `excess` is
    covered; returns them (newest first) and the part of the last one that stays paid (re-entered by the caller)."""
    picked: list[Payment] = []
    covered = 0
    for p in sorted(payments, key=lambda p: (p.created_at, str(p.id)), reverse=True):
        if covered >= excess:
            break
        picked.append(p)
        covered += int(p.amount)
    return picked, max(0, covered - excess)


async def _return_overpayment(session: AsyncSession, order: Order, staff: StaffPrincipal, now: datetime, reason: str) -> tuple[int, list[Payment]]:
    """After a service left a (partly) paid cheque: what is now paid over the new total goes back to the patient.

    Payments are refunded whole (the existing refund semantics every list, report and the cash desk already know); the
    part of the last refunded payment that stays paid is re-entered with its method, cashier and original time, so the
    day it was taken shows the net amount. The patient's total spent drops by the refund."""
    excess = int(order.paid_amount) - int(order.total)
    if excess <= 0:
        return 0, []
    live = [p for p in await repo.payments_of_order(session, order.id) if p.refunded_at is None]
    refunded, keep = plan_overpayment_return(live, excess)
    # only the payments that give money back count — dropping a service that just shrinks the debt stays open
    ensure_payments_editable(staff, refunded, now)
    for p in refunded:
        p.refunded_at = now
        p.refund_reason = reason
        _touch(p, now)
    if keep > 0:
        last = refunded[-1]
        session.add(
            Payment(
                order_id=order.id,
                unit_id=last.unit_id,
                amount=keep,
                method=last.method,
                employee_id=last.employee_id,
                note=f"Qayta hisoblandi — {reason}",
                created_at=last.created_at,
                updated_at=now,
                created_by=staff.id,
            )
        )
        _note_first_payment(order, last.created_at)
    await recompute(session, order, now)
    patient = await repo.get_patient(session, order.patient_id)
    if patient:
        await repo.bump_patient_stats(session, patient.id, spent=-excess, now=now)
    return excess, refunded


async def remove_item(
    session: AsyncSession, order_id: uuid.UUID, item_id: uuid.UUID, staff: StaffPrincipal, meta: RequestMeta, reason: str | None = None
) -> OrderItemsOut:
    """Soft-delete a service added by mistake — only while it is pending (the lab has not started on it).

    Paid or not: on a (partly) paid cheque whatever is now paid over the new total goes back to the patient
    (`_return_overpayment`) and the response says how much (`refunded`; 0 when the remaining debt just shrinks)."""
    order = await get_order_or_404(session, order_id, staff, for_update=True)
    item = await repo.get_item(session, item_id)
    if not item or item.order_id != order.id:
        raise NotFoundError(ITEM_NOT_FOUND)
    if item.status != "pending":
        raise ConflictError("Laboratoriya boshlagan tahlilni o‘chirib bo‘lmaydi", code="in_progress")
    now = utcnow()
    reason = (reason or "").strip() or None
    item.deleted_at = now
    item.deleted_by = staff.id
    _touch(item, now)
    await recompute(session, order, now)
    refund_reason = f"«{item.service_name}» chekdan o‘chirildi" + (f": {reason}" if reason else "")
    refunded, payments = await _return_overpayment(session, order, staff, now, refund_reason)
    await audit(
        session,
        actor_type="staff",
        actor_id=staff.id,
        unit_id=order.unit_id,
        action="order.items.remove",
        entity="order_item",
        entity_id=item.id,
        before={"serviceName": item.service_name, "price": item.price},
        after={"orderId": str(order.id), "total": order.total, "reason": reason, "refunded": refunded, "refundedPaymentIds": [str(p.id) for p in payments]},
        ip=meta.ip,
        request_id=meta.request_id,
    )
    out = await _order_items_out(session, order)
    return out.model_copy(update={"refunded": refunded})


# ----------------------------------------------------------------------------- payments


async def _telegram_push(
    session: AsyncSession,
    patient: Patient | None,
    order: Order,
    *,
    kind: str,
    text: str,
    document_id: uuid.UUID | None,
    payload: dict[str, Any],
) -> None:
    if patient and patient.telegram_chat_id:
        await messaging.enqueue(
            session,
            unit_id=order.unit_id,
            channel="telegram",
            kind=kind,
            to=patient.telegram_chat_id,
            text=text,
            patient_id=order.patient_id,
            order_id=order.id,
            document_id=document_id,
            payload=payload,
        )


def payment_parts(body: PayIn) -> list[tuple[str, int]]:
    """(method, amount) per method of one payment action: the split parts (same method summed, order kept) or the
    single `method` with the whole `amount`."""
    if not body.parts:
        return [(body.method, body.amount)]
    merged: dict[str, int] = {}
    for part in body.parts:
        merged[part.method] = merged.get(part.method, 0) + part.amount
    return list(merged.items())


async def pay(session: AsyncSession, order_id: uuid.UUID, staff: StaffPrincipal, body: PayIn, meta: RequestMeta) -> OrderPaymentsOut:
    """Record a payment (partial allowed), keep patient totalSpent, optionally SMS + Telegram receipt."""
    order = await get_order_or_404(session, order_id, staff, for_update=True)
    if order.item_count == 0:
        raise ValidationError("Chekda xizmat yo‘q", code="empty")
    if order.payment == "paid":
        raise ConflictError("Chek allaqachon to‘langan", code="already_paid")
    remaining = order.total - order.paid_amount
    parts = payment_parts(body)
    amount = sum(a for _, a in parts)
    if body.parts and amount != body.amount:
        raise ValidationError("To‘lov qismlarining yig‘indisi summaga teng emas", code="parts")
    if amount <= 0 or amount > remaining:
        raise ValidationError(f"Summa 1 – {remaining} oralig‘ida bo‘lishi kerak", code="amount")
    now = utcnow()
    # one row per method — a split payment (part cash, part card…) is still one action: one SMS, one audit entry
    payments = [
        Payment(
            order_id=order.id,
            unit_id=order.unit_id,
            amount=part_amount,
            method=method,
            employee_id=staff.id,
            created_by=staff.id,
            created_at=now,
            updated_at=now,
        )
        for method, part_amount in parts
    ]
    session.add_all(payments)
    payment = payments[0]
    _note_first_payment(order, now)
    await recompute(session, order, now)
    patient = await repo.get_patient(session, order.patient_id)
    if patient:
        await repo.bump_patient_stats(session, patient.id, spent=amount, now=now)
    text = await messaging.payment_receipt_text(
        session, order.unit_id, order.number, amount, order.patient_name,
        total=order.total, remaining=max(0, order.total - order.paid_amount),  # after this payment
    )
    if body.send_sms:
        await messaging.enqueue_sms_if_configured(
            session,
            order.unit_id,
            kind="payment_receipt",
            to=order.patient_phone,
            text=text,
            patient_id=order.patient_id,
            order_id=order.id,
            created_by=staff.id,
        )
    await _telegram_push(
        session,
        patient,
        order,
        kind="payment_receipt",
        text=text,
        document_id=None,
        payload={"orderId": str(order.id)},
    )
    await audit(
        session,
        actor_type="staff",
        actor_id=staff.id,
        unit_id=order.unit_id,
        action="order.pay",
        entity="payment",
        entity_id=payment.id,
        after={"orderId": str(order.id), "amount": amount, "method": parts[0][0], "parts": [{"method": m, "amount": a} for m, a in parts], "payment": order.payment},
        ip=meta.ip,
        request_id=meta.request_id,
    )
    return await _order_payments_out(session, order)


async def _payment_or_404(session: AsyncSession, payment_id: uuid.UUID, staff: StaffPrincipal) -> Payment:
    payment = await repo.get_payment(session, payment_id)
    if not payment:
        raise NotFoundError("To‘lov topilmadi")
    staff.require_unit(payment.unit_id)
    return payment


async def refund(session: AsyncSession, payment_id: uuid.UUID, staff: StaffPrincipal, reason: str, meta: RequestMeta) -> OrderPaymentsOut:
    """Mark a payment refunded; the order's paid amount / payment status and patient totalSpent follow."""
    payment = await _payment_or_404(session, payment_id, staff)
    if payment.refunded_at is not None:
        raise StateError("To‘lov allaqachon qaytarilgan", code="state")
    order = await get_order_or_404(session, payment.order_id, staff, for_update=True)
    await session.refresh(payment)
    if payment.refunded_at is not None:
        raise StateError("To‘lov allaqachon qaytarilgan", code="state")
    now = utcnow()
    ensure_payments_editable(staff, [payment], now)
    payment.refunded_at = now
    payment.refund_reason = reason or None
    _touch(payment, now)
    await recompute(session, order, now)
    patient = await repo.get_patient(session, order.patient_id)
    if patient:
        await repo.bump_patient_stats(session, patient.id, spent=-payment.amount, now=now)
    await audit(
        session,
        actor_type="staff",
        actor_id=staff.id,
        unit_id=order.unit_id,
        action="payment.refund",
        entity="payment",
        entity_id=payment.id,
        after={"orderId": str(order.id), "amount": payment.amount, "reason": reason, "payment": order.payment},
        ip=meta.ip,
        request_id=meta.request_id,
    )
    return await _order_payments_out(session, order)


def method_change_parts(current: str, amount: int, body: PaymentMethodIn) -> list[tuple[str, int]]:
    """Validated new (method, amount) parts of a payment: same method summed, the part that stays on the payment's
    own row first (its current method when kept, else the first given). 422 parts / same_method."""
    merged: dict[str, int] = {}
    for part in body.parts:
        merged[part.method] = merged.get(part.method, 0) + part.amount
    if sum(merged.values()) != amount:
        raise ValidationError("Qismlar yig‘indisi to‘lov summasiga teng bo‘lishi kerak", code="parts")
    if list(merged) == [current]:
        raise ValidationError("To‘lov usuli o‘zgarmadi", code="same_method")
    parts = list(merged.items())
    return sorted(parts, key=lambda p: p[0] != current)


async def change_payment_method(session: AsyncSession, payment_id: uuid.UUID, staff: StaffPrincipal, body: PaymentMethodIn, meta: RequestMeta) -> OrderPaymentsOut:
    """Correct how a payment was taken (e.g. card typed as cash) — the amount, time, cashier and facility stay.

    One part: the row just gets the new method. Several: the row keeps the first part and the rest become new rows
    with the same time / cashier / facility, so the day and the cashier of the money don't move; every touched row
    records what it was (`corrections`). Refunded → 409 state; reception only within a day (`late_edit` any time)."""
    payment = await _payment_or_404(session, payment_id, staff)
    order = await get_order_or_404(session, payment.order_id, staff, for_update=True)
    await session.refresh(payment)
    if payment.refunded_at is not None:
        raise StateError("Qaytarilgan to‘lovni o‘zgartirib bo‘lmaydi", code="state")
    parts = method_change_parts(payment.method, int(payment.amount), body)
    now = utcnow()
    ensure_payments_editable(staff, [payment], now)
    reason = body.reason.strip() or None
    entry = {
        "at": iso_z(now), "byId": str(staff.id), "byName": staff.employee.full_name,
        "fromMethod": payment.method, "fromAmount": int(payment.amount), "reason": reason,
    }
    before = {"method": payment.method, "amount": int(payment.amount)}
    (method, amount), rest = parts[0], parts[1:]
    payment.method, payment.amount = method, amount
    payment.corrections = [*(payment.corrections or []), entry]
    flag_modified(payment, "corrections")
    _touch(payment, now)
    split_off = [
        Payment(
            order_id=payment.order_id,
            unit_id=payment.unit_id,
            amount=part_amount,
            method=part_method,
            employee_id=payment.employee_id,
            note=payment.note,
            corrections=[{**entry, "splitFrom": str(payment.id)}],
            created_at=payment.created_at,
            updated_at=now,
            created_by=staff.id,
        )
        for part_method, part_amount in rest
    ]
    session.add_all(split_off)
    if split_off:
        _note_first_payment(order, payment.created_at)
    await recompute(session, order, now)
    await audit(
        session,
        actor_type="staff",
        actor_id=staff.id,
        unit_id=order.unit_id,
        action="payment.method_change",
        entity="payment",
        entity_id=payment.id,
        before=before,
        after={"orderId": str(order.id), "parts": [{"method": m, "amount": a} for m, a in parts], "reason": reason, "newPaymentIds": [str(p.id) for p in split_off]},
        ip=meta.ip,
        request_id=meta.request_id,
    )
    return await _order_payments_out(session, order)


async def cancel_order(session: AsyncSession, order_id: uuid.UUID, staff: StaffPrincipal, reason: str, meta: RequestMeta) -> OrderOut:
    """Cancel an unpaid cheque: order + all items -> cancelled; `note` untouched, reason kept separately."""
    order = await get_order_or_404(session, order_id, staff, for_update=True)
    if order.payment != "unpaid":
        raise ConflictError("To‘langan chekni bekor qilish uchun avval qaytarish qiling", code="paid")
    now = utcnow()
    before_status = order.status
    order.status = "cancelled"
    order.cancel_reason = reason or None
    order.cancelled_at = now
    for it in await repo.items_of_order(session, order.id):
        it.status = "cancelled"
        _touch(it, now)
    await recompute(session, order, now)
    await audit(
        session,
        actor_type="staff",
        actor_id=staff.id,
        unit_id=order.unit_id,
        action="order.cancel",
        entity="order",
        entity_id=order.id,
        before={"status": before_status},
        after={"status": "cancelled", "reason": reason},
        ip=meta.ip,
        request_id=meta.request_id,
    )
    return order_out(order, (await _unit_names(session, [order.unit_id])).get(order.unit_id))


async def delete_order(session: AsyncSession, order_id: uuid.UUID, staff: StaffPrincipal, meta: RequestMeta) -> OrderOut:
    """Soft-delete a cheque that never "happened" for accounting: no lab work started (items pending/cancelled).

    Paid or not — the cheque (and its payments) disappear from every list, report and the dashboard, and the
    patient's stats (orders, total spent) are rolled back. The rows stay in the database with `deleted_at` —
    nothing is physically removed, so the payment history and audit trail survive."""
    order = await get_order_or_404(session, order_id, staff, for_update=True)
    items = await repo.items_of_order(session, order.id)
    for it in items:
        if it.status not in ("pending", "cancelled"):
            raise ConflictError("Laboratoriya boshlagan tahlilni o‘chirib bo‘lmaydi", code="in_progress")
    payments = await repo.payments_of_order(session, order.id)
    now = utcnow()
    # every payment (refunded ones too) leaves the reports with the cheque
    ensure_payments_editable(staff, payments, now)
    order.deleted_at = now
    order.deleted_by = staff.id
    _touch(order, now)
    for it in items:
        it.deleted_at = now
        it.deleted_by = staff.id
        _touch(it, now)
    for p in payments:
        p.deleted_at = now
        p.deleted_by = staff.id
        _touch(p, now)
    if order.patient_id:
        await repo.bump_patient_stats(session, order.patient_id, orders=-1, spent=-order.paid_amount, now=now)
    await audit(
        session,
        actor_type="staff",
        actor_id=staff.id,
        unit_id=order.unit_id,
        action="order.delete",
        entity="order",
        entity_id=order.id,
        before={"number": order.number, "status": order.status, "paidAmount": order.paid_amount, "items": len(items), "payments": len(payments)},
        after={"deleted": True},
        ip=meta.ip,
        request_id=meta.request_id,
    )
    return order_out(order, (await _unit_names(session, [order.unit_id])).get(order.unit_id))


# ----------------------------------------------------------------------------- category restriction

CATEGORY_FORBIDDEN = "Bu kategoriya sizga biriktirilmagan"


async def allowed_category_ids(session: AsyncSession, staff: StaffPrincipal) -> set[uuid.UUID] | None:
    """Categories the employee may work with (own `category_ids` + all descendants).

    `None` = unrestricted (superadmin, or no categories assigned). Cached per employee for 60 s
    because the lab pages poll the worklist frequently."""
    ids = list(staff.employee.category_ids or [])
    if staff.is_super_admin or not ids:
        return None
    key = f"emp:{staff.id}:cats"
    cached = await cache.get_json(key)
    if isinstance(cached, list):
        return {uuid.UUID(x) for x in cached}
    allowed = await repo.descendant_category_ids(session, ids)
    allowed |= set(ids)  # keep the ids even if a category was archived/deleted (harmless)
    await cache.set_json(key, [str(x) for x in allowed], 60)
    return allowed


def ensure_category_access(item: OrderItem, allowed: set[uuid.UUID] | None) -> None:
    """403 when the employee is restricted to categories and the item is outside them."""
    if allowed is not None and item.category_id not in allowed:
        raise ForbiddenError(CATEGORY_FORBIDDEN)


# ----------------------------------------------------------------------------- lab


async def _restrict_categories(session: AsyncSession, q: WorklistQuery, staff: StaffPrincipal) -> WorklistQuery | None:
    """The worklist query narrowed to the employee's categories; None = nothing may be shown."""
    allowed = await allowed_category_ids(session, staff)
    if allowed is None:
        return q
    wanted = set(repo.parse_uuids(q.category_ids)) if q.category_ids else None
    effective = (wanted & allowed) if wanted is not None else allowed
    if not effective:
        return None
    return q.model_copy(update={"category_ids": [str(x) for x in effective]})


async def worklist(session: AsyncSession, scope: UnitScope, q: WorklistQuery, staff: StaffPrincipal) -> Page[WorklistItemOut]:
    """Lab worklist rows (item + order snapshots + live patient gender/birth date) of the unit's facilities.

    Employees bound to categories only ever see items of those categories (server-side)."""
    restricted = await _restrict_categories(session, q, staff)
    if restricted is None:
        return page_of([], q, 0)
    rows, total = await repo.worklist(session, _unit_filter(scope, q.unit_id), restricted)
    names = await _unit_names(session, {item.unit_id for item, *_ in rows})
    out = [
        WorklistItemOut(
            **_item_fields(item),
            unit_name=names.get(item.unit_id),
            order_number=number,
            patient_name=patient_name,
            patient_phone=patient_phone,
            patient_gender=gender,
            patient_birth_date=birth_date,
        )
        for item, number, patient_name, patient_phone, gender, birth_date in rows
    ]
    return page_of(out, q, total)


async def worklist_counts(session: AsyncSession, scope: UnitScope, q: WorklistQuery, staff: StaffPrincipal) -> WorklistCountsOut:
    """Status-tab counters for the lab/confirm pages (same filters & category restriction as `worklist`)."""
    keys = ("pending", "entered", "submitted", "approved", "rejected", "cancelled")
    restricted = await _restrict_categories(session, q, staff)
    if restricted is None:
        return WorklistCountsOut(all=0, **{k: 0 for k in keys})
    counts = await repo.worklist_counts(session, _unit_filter(scope, q.unit_id), restricted)
    return WorklistCountsOut(all=sum(counts.get(k, 0) for k in keys), **{k: counts.get(k, 0) for k in keys})


async def get_item_dto(session: AsyncSession, item_id: uuid.UUID, staff: StaffPrincipal) -> OrderItemOut:
    """Item of a facility the employee may act in."""
    return item_out(await get_item_or_404(session, item_id, staff))


async def save_values(session: AsyncSession, item_id: uuid.UUID, staff: StaffPrincipal, body: SaveValuesIn, meta: RequestMeta) -> OrderItemOut:
    """Replace result values wholesale; pending/rejected -> entered, entered/submitted keep their status."""
    item = await get_item_or_404(session, item_id, staff)
    order = await _lock_order_of_item(session, item, staff)
    ensure_category_access(item, await allowed_category_ids(session, staff))
    if item.status == "approved":
        raise ConflictError("Tasdiqlangan natijani o‘zgartirib bo‘lmaydi", code="approved")
    if item.status == "cancelled":
        raise StateError("Bekor qilingan tahlil", code="state")
    now = utcnow()
    item.values = dict(body.values)
    item.lab_note = body.lab_note
    item.technician_id = staff.id
    item.technician_name = staff.employee.full_name
    item.entered_at = now
    if item.status in ("pending", "rejected"):
        item.status = "entered"
        item.reject_reason = None
    _touch(item, now)
    await recompute(session, order, now)
    await audit(
        session,
        actor_type="staff",
        actor_id=staff.id,
        unit_id=item.unit_id,
        action="item.values",
        entity="order_item",
        entity_id=item.id,
        after={"status": item.status, "keys": sorted(item.values.keys())[:50]},
        ip=meta.ip,
        request_id=meta.request_id,
    )
    return item_out(item)


def _is_blank(v: Any) -> bool:
    return v is None or v == "" or (isinstance(v, list) and len(v) == 0)


async def submit_item(session: AsyncSession, item_id: uuid.UUID, staff: StaffPrincipal, meta: RequestMeta) -> OrderItemOut:
    """Toggle entered <-> submitted after validating required schema fields."""
    item = await get_item_or_404(session, item_id, staff)
    order = await _lock_order_of_item(session, item, staff)
    ensure_category_access(item, await allowed_category_ids(session, staff))
    if item.status not in ("entered", "submitted"):
        raise StateError("Avval natijalarni saqlang", code="state")
    schema = (await repo.schemas_by_ids(session, [item.schema_id])).get(item.schema_id) if item.schema_id else None
    values = item.values or {}
    missing = [
        f.get("label") or f.get("key")
        for f in (schema.fields if schema else [])
        if f.get("required") and _is_blank(values.get(f.get("key")))
    ]
    if missing:
        raise ValidationError("To‘ldirilmagan: " + ", ".join(str(m) for m in missing), code="required")
    now = utcnow()
    _seed_history(item)
    if item.status == "submitted":
        item.status = "entered"
        item.submitted_at = None
    else:
        item.status = "submitted"
        item.submitted_at = now
    _push_history(item, "submitted" if item.status == "submitted" else "unsubmitted", staff, now)
    _touch(item, now)
    await recompute(session, order, now)
    await audit(
        session,
        actor_type="staff",
        actor_id=staff.id,
        unit_id=item.unit_id,
        action="item.submit" if item.status == "submitted" else "item.unsubmit",
        entity="order_item",
        entity_id=item.id,
        after={"status": item.status},
        ip=meta.ip,
        request_id=meta.request_id,
    )
    return item_out(item)


async def reject_item(session: AsyncSession, item_id: uuid.UUID, staff: StaffPrincipal, reason: str, meta: RequestMeta) -> OrderItemOut:
    """Doctor sends a submitted result back to the lab."""
    item = await get_item_or_404(session, item_id, staff)
    order = await _lock_order_of_item(session, item, staff)
    ensure_category_access(item, await allowed_category_ids(session, staff))
    if item.status != "submitted":
        raise StateError("Faqat yuborilgan natijani qaytarish mumkin", code="state")
    now = utcnow()
    _seed_history(item)
    item.status = "rejected"
    item.reject_reason = reason or None
    item.submitted_at = None
    _push_history(item, "returned", staff, now, reason=reason or None)
    _touch(item, now)
    await recompute(session, order, now)
    await audit(
        session,
        actor_type="staff",
        actor_id=staff.id,
        unit_id=item.unit_id,
        action="item.reject",
        entity="order_item",
        entity_id=item.id,
        after={"reason": reason},
        ip=meta.ip,
        request_id=meta.request_id,
    )
    return item_out(item)


# ----------------------------------------------------------------------------- approval / documents


async def _resolve_item_template(session: AsyncSession, item: OrderItem, template_id: str | None) -> ResultTemplate:
    """DOMAIN_RULES section 7 chain: explicit/default (any status) -> active bound -> active generic -> 422.
    Every step only considers templates usable at the item's facility (owner on its path, bindings on its path)."""
    st = (await catalog_svc.get_service_types_by_ids(session, [item.service_type_id])).get(item.service_type_id)
    wanted = repo.to_uuid(template_id) or (st.default_template_id if st is not None else None)
    tpl = await repo.get_template(session, wanted) if wanted else None
    if tpl is not None and not await _template_usable_at(session, tpl, item.unit_id):
        tpl = None  # belongs to other units — fall through the chain
    if tpl is None:
        tpl = await _pick_template(
            session,
            item.unit_id,
            # bound to the service type or its category (the most specific binding first, then the oldest)
            lambda t: t.scope != "receipt" and (item.service_type_id in (t.service_type_ids or []) or item.category_id in (t.category_ids or [])),
            # the catch-all: no service-type bindings
            lambda t: t.scope != "receipt" and not (t.service_type_ids or []),
        )
    if tpl is None:
        raise ValidationError("Bu xizmat uchun faol shablon yo‘q", code="no_template")
    return tpl


def _covers(tpl: ResultTemplate, item: OrderItem) -> bool:
    """The (already facility-checked) template covers the item: bound to its service / category, or unbound."""
    st_ids, cat_ids = list(tpl.service_type_ids or []), list(tpl.category_ids or [])
    return item.service_type_id in st_ids or item.category_id in cat_ids or (not st_ids and not cat_ids)


async def _issue_document(
    session: AsyncSession,
    *,
    order: Order,
    template: ResultTemplate,
    primary_item: OrderItem,
    items: list[OrderItem],
    title: str,
    order_item_ids: list[uuid.UUID] | None,
    now: datetime,
    actor_id: uuid.UUID,
    meta: RequestMeta | None = None,
) -> tuple[ResultDocument, OrgUnit, Patient]:
    """Freeze the render snapshot, create the final document, render + store the PDF (best effort)."""
    facility = await _facility_or_404(session, order.unit_id)
    patient = await repo.get_patient(session, order.patient_id)
    if patient is None:
        raise NotFoundError(ORDER_NOT_FOUND)
    category = (await repo.categories_by_ids(session, [primary_item.category_id])).get(primary_item.category_id)
    schemas = await repo.schemas_by_ids(session, [i.schema_id for i in items])
    sts = await catalog_svc.get_service_types_by_ids(session, {i.service_type_id for i in items})
    service_codes = {
        i.service_type_id: (
            sts[i.service_type_id].code
            if i.service_type_id in sts and sts[i.service_type_id].code
            else str(i.service_type_id)
        )
        for i in items
    }
    district_name = await repo.get_district_name(session, patient.district_id)
    region_name = await repo.get_region_name(session, patient.region_id)
    token = secrets.token_urlsafe(24)
    verify_token = secrets.token_urlsafe(16)
    snapshot = await templates_svc.build_document_snapshot(
        session,
        template=template,
        order=order,
        patient=patient,
        unit_id=facility.id,
        category=category,
        primary_item=primary_item,
        items=items,
        schemas=schemas,
        service_codes=service_codes,
        district_name=district_name,
        region_name=region_name,
        approved_at=now,
        doc_link=public_verify_link_for_token(verify_token, meta),
    )
    at = iso_z(now)
    doc = ResultDocument(
        unit_id=order.unit_id,
        order_id=order.id,
        patient_id=order.patient_id,
        order_item_id=primary_item.id,
        order_item_ids=order_item_ids or [],
        template_id=template.id,
        template_version=template.version,
        title=title,
        status="final",
        deliveries=[
            {"channel": "portal", "status": "delivered", "at": at},
            {"channel": "sms", "status": "queued", "at": at},
        ],
        snapshot=snapshot,
        public_token=token,
        verify_token=verify_token,
        created_by=actor_id,
        created_at=now,
        updated_at=now,
    )
    session.add(doc)
    await session.flush()
    try:
        # SAVEPOINT: a DB-side failure while storing the PDF rolls back only this step,
        # so the approval itself still commits (the PDF is rendered lazily later).
        async with session.begin_nested():
            pdf = await templates_svc.render_snapshot_pdf(session, snapshot)
            stored = await files_svc.store_bytes(
                session,
                unit_id=order.unit_id,
                data=pdf,
                mime="application/pdf",
                filename=f"{order.number}-{slugify(title)}.pdf",
                created_by=actor_id,
            )
            doc.pdf_file_id = stored.id
            _touch(doc, now)
    except Exception:  # renderer failure must not block approval — PDF is rendered lazily later
        log.exception("PDF render failed for document %s (order %s)", doc.id, order.number)
    await templates_svc.increment_usage(session, template.id)
    return doc, facility, patient


async def _notify_result_ready(
    session: AsyncSession,
    *,
    order: Order,
    patient: Patient | None,
    doc: ResultDocument,
    text: str,
    actor_id: uuid.UUID,
) -> None:
    await messaging.enqueue_sms_if_configured(
        session,
        order.unit_id,
        kind="result_ready",
        to=order.patient_phone,
        text=text,
        patient_id=order.patient_id,
        order_id=order.id,
        document_id=doc.id,
        created_by=actor_id,
    )
    await _telegram_push(
        session,
        patient,
        order,
        kind="result_ready",
        text=text,
        document_id=doc.id,
        payload={"documentId": str(doc.id), "orderId": str(order.id)},
    )


async def approve_item(session: AsyncSession, item_id: uuid.UUID, staff: StaffPrincipal, body: ApproveItemIn, meta: RequestMeta) -> ApproveItemOut:
    """Doctor approves one submitted item -> item-scoped document + PDF + notifications."""
    item = await get_item_or_404(session, item_id, staff)
    order = await _lock_order_of_item(session, item, staff)
    ensure_category_access(item, await allowed_category_ids(session, staff))
    if item.status != "submitted":
        raise StateError("Faqat yuborilgan natijani tasdiqlash mumkin", code="state")
    template = await _resolve_item_template(session, item, body.template_id)
    now = utcnow()
    _seed_history(item)
    item.status = "approved"
    item.doctor_id = staff.id
    item.doctor_name = staff.employee.full_name
    item.approved_at = now
    _touch(item, now)
    doc, facility, patient = await _issue_document(
        session,
        order=order,
        template=template,
        primary_item=item,
        items=[item],
        title=f"{item.service_name} — natija",
        order_item_ids=None,
        now=now,
        actor_id=staff.id,
        meta=meta,
    )
    item.document_id = doc.id
    _push_history(item, "approved", staff, now, document_id=doc.id)
    _touch(item, now)
    await recompute(session, order, now)
    text = await messaging.result_ready_text(session, order.unit_id, item.service_name, order.patient_name, order.number, public_result_link(doc, meta))
    await _notify_result_ready(session, order=order, patient=patient, doc=doc, text=text, actor_id=staff.id)
    await audit(
        session,
        actor_type="staff",
        actor_id=staff.id,
        unit_id=item.unit_id,
        country_id=facility.country_id,
        action="item.approve",
        entity="order_item",
        entity_id=item.id,
        after={"documentId": str(doc.id), "templateId": str(template.id), "templateVersion": template.version},
        ip=meta.ip,
        request_id=meta.request_id,
    )
    return ApproveItemOut(item=item_out(item), document=document_out(doc, facility.name))


async def revoke_item(session: AsyncSession, item_id: uuid.UUID, staff: StaffPrincipal, reason: str, meta: RequestMeta) -> RevokeOut:
    """Doctor takes back an approved result (a mistake was found): its document is withdrawn — PDF, public link, portal
    and reports drop it, the row stays for the trail — and the result goes back to the lab with the reason. Approved
    items that shared that (order-scope) document return to the approval queue."""
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("Bekor qilish sababini yozing", code="reason")
    item = await get_item_or_404(session, item_id, staff)
    order = await _lock_order_of_item(session, item, staff)
    ensure_category_access(item, await allowed_category_ids(session, staff))
    if item.status != "approved":
        raise StateError("Faqat tasdiqlangan natijani bekor qilish mumkin", code="state")
    now = utcnow()
    _seed_history(item)
    doc = await repo.get_document(session, item.document_id) if item.document_id else None
    affected: list[OrderItem] = [item]
    if doc is not None:
        doc.status = "revoked"
        doc.deleted_at = now
        doc.deleted_by = staff.id
        _touch(doc, now)
        if doc.order_item_ids:
            for other in await repo.items_of_order(session, order.id):
                if other.id != item.id and other.document_id == doc.id and other.status == "approved":
                    _seed_history(other)
                    other.status = "submitted"
                    other.approved_at = None
                    other.doctor_id = None
                    other.doctor_name = None
                    other.document_id = None
                    _push_history(other, "reopened", staff, now, reason=reason, document_id=doc.id)
                    _touch(other, now)
                    affected.append(other)
    item.status = "rejected"
    item.reject_reason = reason
    item.submitted_at = None
    item.approved_at = None
    item.doctor_id = None
    item.doctor_name = None
    item.document_id = None
    _push_history(item, "revoked", staff, now, reason=reason, document_id=doc.id if doc else None)
    _touch(item, now)
    await recompute(session, order, now)
    await audit(
        session,
        actor_type="staff",
        actor_id=staff.id,
        unit_id=item.unit_id,
        action="item.revoke",
        entity="order_item",
        entity_id=item.id,
        before={"status": "approved", "documentId": str(doc.id) if doc else None},
        after={"status": "rejected", "reason": reason, "reopened": [str(x.id) for x in affected[1:]]},
        ip=meta.ip,
        request_id=meta.request_id,
    )
    return RevokeOut(item=item_out(item), items=[item_out(x) for x in affected], document_id=str(doc.id) if doc else None)


async def order_scope_items(session: AsyncSession, order_id: uuid.UUID, template_id: uuid.UUID, staff: StaffPrincipal) -> list[OrderItemOut]:
    """Non-cancelled items of the order an order-scope template would cover (any status)."""
    order = await get_order_or_404(session, order_id, staff)
    tpl = await repo.get_template(session, template_id)
    if not tpl or not await _template_usable_at(session, tpl, order.unit_id):
        raise NotFoundError("Shablon topilmadi")
    items = [i for i in await repo.items_of_order(session, order.id) if i.status != "cancelled" and _covers(tpl, i)]
    return [item_out(i) for i in items]


async def approve_order(session: AsyncSession, order_id: uuid.UUID, staff: StaffPrincipal, body: ApproveOrderIn, meta: RequestMeta) -> ApproveOrderOut:
    """Approve every submitted covered item with ONE order-scope document (covered approved items share it)."""
    order = await get_order_or_404(session, order_id, staff, for_update=True)
    tpl_id = repo.to_uuid(body.template_id)
    tpl = await repo.get_template(session, tpl_id) if tpl_id else None
    if not tpl or tpl.status != "active" or not await _template_usable_at(session, tpl, order.unit_id):
        raise ValidationError("Faol shablon topilmadi", code="no_template")
    if tpl.scope != "order":
        raise ValidationError("Bu shablon chek darajasidagi hujjat emas", code="scope")
    covered = [
        i
        for i in await repo.items_of_order(session, order.id)
        if i.status in ("submitted", "approved") and _covers(tpl, i)
    ]
    if body.item_ids:
        wanted = set(repo.parse_uuids(body.item_ids))
        covered = [i for i in covered if i.id in wanted]
    to_approve = [i for i in covered if i.status == "submitted"]
    if not to_approve:
        raise StateError("Tasdiqlash uchun yuborilgan tahlil yo‘q", code="state")
    allowed = await allowed_category_ids(session, staff)
    for it in to_approve:
        ensure_category_access(it, allowed)
    now = utcnow()
    for it in to_approve:
        _seed_history(it)
        it.status = "approved"
        it.doctor_id = staff.id
        it.doctor_name = staff.employee.full_name
        it.approved_at = now
        _touch(it, now)
    doc, facility, patient = await _issue_document(
        session,
        order=order,
        template=tpl,
        primary_item=to_approve[0],
        items=covered,
        title=tpl.name,
        order_item_ids=[i.id for i in covered],
        now=now,
        actor_id=staff.id,
        meta=meta,
    )
    for it in covered:
        if it in to_approve or it.document_id is None:
            it.document_id = doc.id
            _touch(it, now)
    for it in to_approve:
        _push_history(it, "approved", staff, now, document_id=doc.id)
    await recompute(session, order, now)
    text = await messaging.result_ready_order_text(session, order.unit_id, tpl.name, len(to_approve), order.patient_name, order.number, public_result_link(doc, meta))
    await _notify_result_ready(session, order=order, patient=patient, doc=doc, text=text, actor_id=staff.id)
    await audit(
        session,
        actor_type="staff",
        actor_id=staff.id,
        unit_id=order.unit_id,
        country_id=facility.country_id,
        action="order.approve",
        entity="order",
        entity_id=order.id,
        after={
            "documentId": str(doc.id),
            "templateId": str(tpl.id),
            "approved": [str(i.id) for i in to_approve],
            "covered": [str(i.id) for i in covered],
        },
        ip=meta.ip,
        request_id=meta.request_id,
    )
    return ApproveOrderOut(items=[item_out(i) for i in covered], document=document_out(doc, facility.name))


# ----------------------------------------------------------------------------- documents


RESEND_COOLDOWN_SECONDS = 60


async def resend_result_sms(session: AsyncSession, document_id: uuid.UUID, staff: StaffPrincipal, body: ResultSmsIn, meta: RequestMeta) -> ResultSmsOut:
    """Queue the result's "ready" SMS again — the same text the approval sent (with the result link), to the
    patient's current phone or the number given. A dry run only returns the recipient and the text.
    The SMS goes out through the document's facility (its own account, else the nearest inherited one)."""
    doc = await get_document_or_404(session, document_id, staff)
    order = await get_order_or_404(session, doc.order_id, staff)
    patient = await repo.get_patient(session, order.patient_id) if order.patient_id else None
    to = norm_phone(body.to if body.to and body.to.strip() else (patient.phone if patient else order.patient_phone))
    if not is_valid_uz_phone(to):
        raise ValidationError("Telefon raqam noto‘g‘ri", code="invalid_phone")
    link = public_result_link(doc, meta)
    if doc.order_item_ids:  # one document for several services of the cheque (order-scope template)
        tpl = await repo.get_template(session, doc.template_id)
        text = await messaging.result_ready_order_text(session, doc.unit_id, tpl.name if tpl else doc.title, len(doc.order_item_ids), order.patient_name, order.number, link)
    else:
        item = await repo.get_item(session, doc.order_item_id) if doc.order_item_id else None
        text = await messaging.result_ready_text(session, doc.unit_id, item.service_name if item else doc.title, order.patient_name, order.number, link)
    configured = await messaging.sms_account_for(session, doc.unit_id) is not None
    if body.dry_run:
        return ResultSmsOut(to=to, text=text, configured=configured, queued=False)
    # a double click (or an impatient second press) must not send the same SMS twice
    key = f"doc:{doc.id}:resend-sms"
    try:
        fresh = await get_redis().set(key, "1", ex=RESEND_COOLDOWN_SECONDS, nx=True)
    except Exception:  # pragma: no cover - Redis degraded: do not block the resend
        fresh = True
    if not fresh:
        raise RateLimitedError("SMS hozirgina yuborildi. Bir daqiqadan keyin qayta urinib ko‘ring.", details={"retryAfter": RESEND_COOLDOWN_SECONDS})
    msg = await messaging.enqueue_sms_if_configured(
        session, doc.unit_id, kind="result_ready", to=to, text=text, patient_id=order.patient_id, order_id=order.id,
        document_id=doc.id, created_by=staff.id,
    )
    status = msg.status if msg else None
    await messaging.mark_document_delivery(session, doc.id, "sms", "failed" if status == "failed" else "queued", msg.error if msg and status == "failed" else None)
    await audit(session, actor_type="staff", actor_id=staff.id, unit_id=doc.unit_id, action="document.sms_resend", entity="result_document", entity_id=doc.id, after={"to": to, "status": status}, ip=meta.ip, request_id=meta.request_id)
    return ResultSmsOut(to=to, text=text, configured=configured, queued=status is not None and status != "failed", status=status, message_id=str(msg.id) if msg else None)


async def list_documents(
    session: AsyncSession, scope: UnitScope, *, order_id: str | None, patient_id: str | None, unit_id: str | None = None
) -> list[ResultDocumentOut]:
    """Documents of the unit's facilities by order and/or patient, newest first (malformed ids -> empty list)."""
    oid = repo.to_uuid(order_id) if order_id else None
    pid = repo.to_uuid(patient_id) if patient_id else None
    if (order_id and oid is None) or (patient_id and pid is None):
        return []
    rows = await repo.list_documents(session, _unit_filter(scope, unit_id), order_id=oid, patient_id=pid)
    names = await _unit_names(session, {d.unit_id for d in rows})
    return [document_out(d, names.get(d.unit_id)) for d in rows]


async def ensure_document_pdf(session: AsyncSession, document: ResultDocument) -> bytes:
    """PDF bytes of a document — stored file when present, else render from the frozen snapshot and store."""
    if document.pdf_file_id:
        loaded = await files_svc.load_bytes(session, document.pdf_file_id)
        if loaded:
            return loaded[0]
    pdf = await templates_svc.render_snapshot_pdf(session, document.snapshot or {})
    stored = await files_svc.store_bytes(
        session,
        unit_id=document.unit_id,
        data=pdf,
        mime="application/pdf",
        filename=f"{slugify(document.title)}.pdf",
    )
    document.pdf_file_id = stored.id
    _touch(document, utcnow())
    await session.flush()
    return pdf


def pdf_filename(document: ResultDocument) -> str:
    """Download name for a document PDF."""
    return f"{slugify(document.title)}-{str(document.id)[-6:]}.pdf"


# ----------------------------------------------------------------------------- receipt tracking


async def mark_document_viewed(session: AsyncSession, doc: ResultDocument) -> None:
    """Record that the patient opened the result (never fails the download)."""
    try:
        async with session.begin_nested():
            await repo.mark_document_viewed(session, doc.id, utcnow())
    except Exception:  # pragma: no cover - tracking must never cost the patient their PDF
        log.warning("could not record the open of document %s", doc.id, exc_info=True)


async def mark_document_printed(session: AsyncSession, document_id: uuid.UUID, staff: StaffPrincipal) -> int:
    """Staff printed the result (TPrints or the browser dialog) — counts as handed over to the patient."""
    doc = await get_document_or_404(session, document_id, staff)
    return await repo.mark_document_printed(session, doc.id, utcnow())


# ----------------------------------------------------------------------------- public link access


def mask_phone(phone: str | None) -> str | None:
    """`+998 ** *** 03 13` - enough to recognize the number, not enough to leak it."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) < 7:
        return None
    if len(digits) >= 12:
        return f"+{digits[:3]} ** *** {digits[-4:-2]} {digits[-2:]}"
    return f"+{digits[:3]} *** {digits[-2:]}"


async def _public_document(session: AsyncSession, token: str) -> ResultDocument:
    doc = await repo.get_document_by_token(session, token) if 16 <= len(token) <= 64 else None
    if not doc:
        if 16 <= len(token) <= 64 and await repo.document_token_revoked(session, token):
            raise NotFoundError("Natija qayta ko'rib chiqilmoqda", code="revoked", status=410)
        raise NotFoundError(DOC_NOT_FOUND)
    return doc


async def _public_document_patient(session: AsyncSession, doc: ResultDocument) -> tuple[Patient, Order | None, str]:
    """The patient, the cheque and the phone the result SMS actually went to (the order snapshot first)."""
    patient = await session.get(Patient, doc.patient_id)
    order = await session.get(Order, doc.order_id)
    if not patient or patient.deleted_at is not None:
        raise NotFoundError(PATIENT_NOT_FOUND)
    phone = norm_phone((order.patient_phone if order else None) or patient.phone or "")
    if not is_valid_uz_phone(phone):
        raise StateError("Hujjatga telefon raqami bog‘lanmagan — SMS kod yuborib bo‘lmaydi. Klinikaga murojaat qiling.", code="no_phone")
    return patient, order, phone


async def _public_free_view_days(session: AsyncSession, doc: ResultDocument) -> int:
    """The code-free window of the document's facility (`documentFreeViewDays`, inherited root → leaf)."""
    return document_free_view_days(await units_svc.effective_settings(session, doc.unit_id))


def public_free_until(issued_at: datetime, days: int) -> datetime | None:
    """End of a result link's code-free window: `days` after the result was issued; None = a code is always needed."""
    return issued_at + timedelta(days=days) if days > 0 else None


def public_code_free(issued_at: datetime, days: int, now: datetime) -> datetime | None:
    """The window's end while it still runs at `now`, else None (the PDF needs an SMS code)."""
    until = public_free_until(issued_at, days)
    return until if until is not None and now < until else None


async def public_document_info(session: AsyncSession, token: str) -> PublicDocumentInfoOut:
    """Public verification info for a result link (QR / SMS): who issued it, when, and that it is authentic.

    Scanned any number of times — nothing is consumed. The PDF itself is behind an SMS code, except during the
    organisation's code-free window (`documentFreeViewDays` after issue): then `otpRequired` is false and `freeUntil`
    says until when the link opens it directly."""
    doc = await _public_document(session, token)
    free_until = public_code_free(doc.created_at, await _public_free_view_days(session, doc), utcnow())
    return PublicDocumentInfoOut(
        id=str(doc.id),
        title=doc.title,
        org_name=await units_svc.org_name(session, doc.unit_id),
        issued_at=iso_z(doc.created_at),
        otp_required=free_until is None,
        free_until=iso_z(free_until) if free_until else None,
    )


async def public_document_open(session: AsyncSession, token: str, meta: RequestMeta) -> tuple[bytes, str]:
    """The PDF straight from the SMS link — only inside the organisation's code-free window (`documentFreeViewDays`
    days after the result was issued); otherwise 403 `otp_required` and the page falls back to the SMS code.

    The window is read from the current setting on every request, so shortening it (or 0) closes the links at once."""
    doc = await _public_document(session, token)
    ip = meta.ip or "?"
    await rate_limit.enforce(f"rl:docopen:ip:{ip}", settings.auth_rate_limit_per_minute, 60)
    await rate_limit.enforce(f"rl:docopen:token:{token}", 30, 3600, "Juda ko'p urinish. Keyinroq qayta urinib ko'ring.")
    now = utcnow()
    free_until = public_code_free(doc.created_at, await _public_free_view_days(session, doc), now)
    if free_until is None:
        raise ForbiddenError("Hujjatni ochish uchun SMS kod kerak", code="otp_required")
    patient = await session.get(Patient, doc.patient_id)
    if not patient or patient.deleted_at is not None:
        raise NotFoundError(PATIENT_NOT_FOUND)
    await repo.mark_public_viewed(session, doc.id, now)
    await mark_document_viewed(session, doc)
    pdf = await ensure_document_pdf(session, doc)
    await audit(
        session,
        actor_type="patient",
        actor_id=patient.id,
        unit_id=doc.unit_id,
        action="document_public_open",
        entity="result_document",
        entity_id=doc.id,
        after={"via": "link", "freeUntil": iso_z(free_until)},
        ip=meta.ip,
        request_id=meta.request_id,
    )
    await session.flush()
    return pdf, pdf_filename(doc)


def mask_name(full_name: str | None) -> str:
    """`Haydarov Dilshodjon` → `H*** D***` — enough to match the paper, not enough to identify the patient."""
    return " ".join(f"{part[0].upper()}***" for part in (full_name or "").split()[:3])


async def public_document_verify(session: AsyncSession, token: str) -> PublicDocumentVerifyOut:
    """QR authenticity check (`GET /v/{token}`): was this document issued by the organisation and is it still valid.

    Only what lets someone holding the paper match it — issuer, date, numbers, masked holder; never the results, the
    PDF or an SMS code. A withdrawn result (revoked) or a cancelled / deleted cheque answers `revoked`."""
    doc = await repo.get_document_by_verify_token(session, token) if 16 <= len(token) <= 64 else None
    if not doc:
        raise NotFoundError(DOC_NOT_FOUND)
    ctx = (doc.snapshot or {}).get("context") or {}
    order = await session.get(Order, doc.order_id)
    unit = (await units_repo.units_by_ids(session, [doc.unit_id])).get(doc.unit_id)
    org_name = await units_svc.org_name(session, doc.unit_id)
    revoked_at: datetime | None = None
    if doc.status == "revoked" or doc.deleted_at:
        revoked_at = doc.deleted_at or doc.updated_at
    elif order is None or order.deleted_at or order.status == "cancelled":
        revoked_at = (order.deleted_at or order.cancelled_at or order.updated_at) if order else doc.updated_at
    doctor = (ctx.get("item") or {}).get("doctor") or (await repo.get_employee_name(session, doc.created_by) if doc.created_by else None)
    # documents issued before the port froze `company` / `branch` blocks — they still answer the check
    frozen_org = (ctx.get("org") or ctx.get("company") or {}).get("name") or ""
    frozen_unit = (ctx.get("unit") or ctx.get("branch") or {}).get("name") or ""
    return PublicDocumentVerifyOut(
        id=str(doc.id),
        status="revoked" if revoked_at else "valid",
        title=doc.title,
        org_name=org_name or frozen_org,
        unit_name=(unit.name if unit else None) or frozen_unit,
        order_number=(order.number if order else None) or (ctx.get("order") or {}).get("number") or "",
        doctor_name=doctor or "",
        patient=mask_name((ctx.get("patient") or {}).get("fullName") or (order.patient_name if order else None)),
        issued_at=iso_z(doc.created_at),
        revoked_at=iso_z(revoked_at) if revoked_at else None,
    )


async def public_document_otp_request(session: AsyncSession, token: str, meta: RequestMeta) -> PublicDocumentOtpOut:
    """Send an SMS code to the phone the document belongs to (shown masked) — viewing the PDF needs it.

    The code goes out through the document's facility (the one that issued the result and sent the result SMS — its
    own SMS account, else the nearest inherited one). When it cannot send, the request fails with
    `sms_not_configured` instead of pretending a code is on its way (dev mode still answers with `devCode`)."""
    doc = await _public_document(session, token)
    patient, order, phone = await _public_document_patient(session, doc)
    configured = await messaging.sms_account_for(session, doc.unit_id) is not None
    if not configured and not settings.otp_dev_mode:
        raise StateError(
            "Bu muassasada SMS yuborish sozlanmagan — hujjatni havola orqali ochib bo‘lmaydi. "
            "Natijani bemor kabinetidan ko‘ring yoki muassasaga murojaat qiling.",
            code="sms_not_configured",
        )
    ip = meta.ip or "?"
    await rate_limit.enforce(f"rl:docotp:ip:{ip}", settings.auth_rate_limit_per_minute, 60)
    await rate_limit.enforce(f"rl:docotp:token:{token}", 5, 3600, "Juda ko'p urinish. Keyinroq qayta urinib ko'ring.")
    await rate_limit.enforce(f"rl:docotp:phone:{phone}", 5, 3600, "Juda ko'p urinish. Keyinroq qayta urinib ko'ring.")
    cooldown_key = f"docotp:cooldown:{token}"
    try:
        # the real seconds left (-2 = no key, -1 = no expiry), so the page counts down to the moment resend works
        remaining = int(await get_redis().ttl(cooldown_key))
        if remaining > 0:
            raise RateLimitedError("Kod allaqachon yuborilgan. Biroz kuting.", details={"retryAfter": remaining})
    except RateLimitedError:
        raise
    except Exception:  # pragma: no cover - redis is optional for this guard
        pass

    code = random_digits(settings.otp_length)
    challenge = OtpChallenge(
        phone=phone,
        purpose="document",
        code_hash=sha256_hex(code),
        max_attempts=settings.otp_max_attempts,
        expires_at=utcnow() + timedelta(seconds=settings.otp_ttl_seconds),
        unit_id=doc.unit_id,
        meta={"token": token, "documentId": str(doc.id), "ip": meta.ip},
    )
    session.add(challenge)
    await session.flush()
    if configured:
        await messaging.enqueue_sms_if_configured(
            session,
            doc.unit_id,
            kind="otp",
            to=phone,
            text=await messaging.otp_text(session, doc.unit_id, code),
            patient_id=patient.id,
            order_id=order.id if order else None,
        )
    try:
        await get_redis().set(cooldown_key, "1", ex=settings.otp_resend_cooldown_seconds)
    except Exception:  # pragma: no cover
        pass
    if settings.otp_dev_mode:
        log.info("Document OTP for %s: %s (dev mode)", phone, code)
    await audit(
        session,
        actor_type="patient",
        actor_id=patient.id,
        unit_id=doc.unit_id,
        action="document_otp_request",
        entity="result_document",
        entity_id=doc.id,
        after={"unitId": str(doc.unit_id), "sent": configured},
        ip=meta.ip,
        request_id=meta.request_id,
    )
    return PublicDocumentOtpOut(
        challenge_id=str(challenge.id),
        phone_masked=mask_phone(phone) or "",
        expires_in=settings.otp_ttl_seconds,
        code_length=settings.otp_length,
        resend_in=settings.otp_resend_cooldown_seconds,
        dev_code=code if settings.otp_dev_mode else None,
    )


async def public_document_otp_verify(session: AsyncSession, token: str, challenge_id: str, code: str, meta: RequestMeta) -> tuple[bytes, str]:
    """Verify the SMS code: the patient (or whoever holds the patient's phone) gets the PDF."""
    doc = await _public_document(session, token)
    patient, _order, phone = await _public_document_patient(session, doc)
    try:
        cid = uuid.UUID(challenge_id)
    except (ValueError, TypeError) as exc:
        raise AuthError("Kod muddati tugagan", code="otp_expired") from exc
    ip = meta.ip or "?"
    await rate_limit.enforce(f"rl:docotp:verify:ip:{ip}", settings.auth_rate_limit_per_minute, 60)
    await rate_limit.enforce(f"rl:docotp:verify:ch:{cid}", settings.otp_max_attempts, settings.otp_ttl_seconds, "Kod muddati tugagan")
    challenge = await session.get(OtpChallenge, cid)
    now = utcnow()
    token_in_meta = (challenge.meta or {}).get("token") if challenge else None
    if (
        not challenge
        or challenge.consumed_at
        or challenge.expires_at < now
        or challenge.purpose != "document"
        or token_in_meta != token
        or challenge.phone != phone
    ):
        raise AuthError("Kod muddati tugagan", code="otp_expired")
    if challenge.attempts >= challenge.max_attempts:
        raise AuthError("Kod muddati tugagan", code="otp_expired")
    if not hmac.compare_digest(sha256_hex(code.strip()), challenge.code_hash):
        await session.execute(update(OtpChallenge).where(OtpChallenge.id == cid).values(attempts=OtpChallenge.attempts + 1))
        await session.commit()
        raise AuthError("Kod noto'g'ri", code="otp_invalid")
    challenge.consumed_at = now
    await repo.mark_public_viewed(session, doc.id, now)
    await mark_document_viewed(session, doc)
    pdf = await ensure_document_pdf(session, doc)
    await audit(
        session,
        actor_type="patient",
        actor_id=patient.id,
        unit_id=doc.unit_id,
        action="document_otp_verify",
        entity="result_document",
        entity_id=doc.id,
        ip=meta.ip,
        request_id=meta.request_id,
    )
    await session.flush()
    return pdf, pdf_filename(doc)
