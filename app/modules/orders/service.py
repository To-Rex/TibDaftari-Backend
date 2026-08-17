"""Orders workflow — cheques, items, payments/refunds, lab worklist, result values, submit/approve/reject,
order-scope approval and result documents (DOMAIN_RULES section 7).

Every mutation ends with `recompute()` (totals, progress, payment + order status) and keeps the
patient's denormalised stats in sync. Approval freezes a render snapshot (templates.service),
renders + stores the PDF, bumps template usage and queues SMS/Telegram notifications.
Other modules (portal, telegram) consume `get_order_bundle`, `order_out`, `item_out`,
`document_out` and `ensure_document_pdf`.
"""

from __future__ import annotations

import copy
import logging
import math
import secrets
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.api.deps import RequestMeta, StaffPrincipal
from app.core.audit import audit
from app.core.exceptions import ConflictError, NotFoundError, StateError, ValidationError
from app.core.pagination import page_of
from app.core.schemas import Page, iso_z
from app.core.textutil import slugify
from app.core.timeutil import utcnow
from app.infrastructure.db.models import (
    Company,
    Order,
    OrderItem,
    Patient,
    Payment,
    ResultDocument,
    ResultTemplate,
    empty_progress,
)
from app.modules.files import service as files_svc
from app.modules.messaging import service as messaging
from app.modules.orders import repository as repo
from app.modules.orders.schemas import (
    AddItemsIn,
    ApproveItemIn,
    ApproveItemOut,
    ApproveOrderIn,
    ApproveOrderOut,
    CreateOrderIn,
    DocumentDeliveryOut,
    OrderBundleOut,
    OrderItemOut,
    OrderItemsOut,
    OrderListQuery,
    OrderOut,
    OrderPaymentsOut,
    PayIn,
    PaymentOut,
    ProgressOut,
    ResultDocumentOut,
    SaveValuesIn,
    WorklistItemOut,
    WorklistQuery,
)
from app.modules.templates import service as templates_svc

log = logging.getLogger("app.orders")

ORDER_NOT_FOUND = "Chek topilmadi"
ITEM_NOT_FOUND = "Tahlil topilmadi"
DOC_NOT_FOUND = "Hujjat topilmadi"


def _round_half_up(x: float) -> int:
    """JS `Math.round` for non-negative money values."""
    return math.floor(x + 0.5)


def _touch(row: Any, now: datetime) -> None:
    """Set `updated_at` explicitly (forced into the UPDATE so the server-side onupdate never expires it)."""
    row.updated_at = now
    flag_modified(row, "updated_at")


def _s(v: uuid.UUID | None) -> str | None:
    return str(v) if v is not None else None


def scope_company(staff: StaffPrincipal) -> uuid.UUID | None:
    """Tenant filter for the caller: superadmin sees every company (None), others only their own."""
    return None if staff.is_super_admin else staff.company_id


# ----------------------------------------------------------------------------- projections


def order_out(o: Order) -> OrderOut:
    """ORM row -> frontend `Order`."""
    return OrderOut(
        id=str(o.id),
        company_id=str(o.company_id),
        branch_id=str(o.branch_id),
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
    )


def item_out(i: OrderItem) -> OrderItemOut:
    """ORM row -> frontend `OrderItem`."""
    return OrderItemOut(**_item_fields(i))


def _item_fields(i: OrderItem) -> dict[str, Any]:
    return {
        "id": str(i.id),
        "order_id": str(i.order_id),
        "company_id": str(i.company_id),
        "branch_id": str(i.branch_id),
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
    }


def payment_out(p: Payment) -> PaymentOut:
    """ORM row -> frontend `Payment`."""
    return PaymentOut(
        id=str(p.id),
        order_id=str(p.order_id),
        company_id=str(p.company_id),
        branch_id=str(p.branch_id),
        amount=p.amount,
        method=p.method,  # type: ignore[arg-type]
        employee_id=str(p.employee_id),
        note=p.note,
        refunded_at=p.refunded_at,
        created_at=p.created_at,
        updated_at=p.updated_at,
        created_by=_s(p.created_by),
    )


def document_pdf_url(document_id: uuid.UUID | str) -> str:
    """Staff PDF endpoint of a document."""
    return f"/api/v1/documents/{document_id}/pdf"


def document_out(d: ResultDocument) -> ResultDocumentOut:
    """ORM row -> frontend `ResultDocument` (`pdfUrl` always points at the staff PDF endpoint)."""
    return ResultDocumentOut(
        id=str(d.id),
        company_id=str(d.company_id),
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
    )


# ----------------------------------------------------------------------------- look-ups


async def get_order_or_404(session: AsyncSession, order_id: uuid.UUID, company_id: uuid.UUID | None, *, for_update: bool = False) -> Order:
    """Company-scoped order or 404 'Chek topilmadi'; `for_update` locks the row (all order mutations)."""
    o = await repo.get_order(session, order_id, company_id, for_update=for_update)
    if not o:
        raise NotFoundError(ORDER_NOT_FOUND)
    return o


async def _lock_order_of_item(session: AsyncSession, item: OrderItem) -> Order:
    """Lock the parent order (serialises item mutations per order) and re-read the item's committed state."""
    order = await get_order_or_404(session, item.order_id, item.company_id, for_update=True)
    await session.refresh(item)
    return order


async def get_item_or_404(session: AsyncSession, item_id: uuid.UUID, company_id: uuid.UUID | None) -> OrderItem:
    """Company-scoped item or 404 'Tahlil topilmadi'."""
    it = await repo.get_item(session, item_id, company_id)
    if not it:
        raise NotFoundError(ITEM_NOT_FOUND)
    return it


async def get_document_or_404(
    session: AsyncSession, document_id: uuid.UUID, company_id: uuid.UUID | None
) -> ResultDocument:
    """Company-scoped document or 404 'Hujjat topilmadi'."""
    d = await repo.get_document(session, document_id, company_id)
    if not d:
        raise NotFoundError(DOC_NOT_FOUND)
    return d


async def get_order_bundle(
    session: AsyncSession, order_id: uuid.UUID, company_id: uuid.UUID | None = None
) -> OrderBundleOut:
    """{order, items, payments} — the shape of `GET /orders/{id}` (also used by portal/telegram)."""
    o = await get_order_or_404(session, order_id, company_id)
    items = await repo.items_of_order(session, o.id)
    payments = await repo.payments_of_order(session, o.id)
    return OrderBundleOut(
        order=order_out(o), items=[item_out(i) for i in items], payments=[payment_out(p) for p in payments]
    )


async def _order_items_out(session: AsyncSession, o: Order) -> OrderItemsOut:
    return OrderItemsOut(order=order_out(o), items=[item_out(i) for i in await repo.items_of_order(session, o.id)])


async def _order_payments_out(session: AsyncSession, o: Order) -> OrderPaymentsOut:
    return OrderPaymentsOut(
        order=order_out(o), payments=[payment_out(p) for p in await repo.payments_of_order(session, o.id)]
    )


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


# ----------------------------------------------------------------------------- list / get


async def list_orders(session: AsyncSession, company_id: uuid.UUID, q: OrderListQuery) -> Page[OrderOut]:
    """Paged company orders."""
    rows, total = await repo.list_orders(session, company_id, q)
    return page_of([order_out(o) for o in rows], q, total)


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


async def _add_items(
    session: AsyncSession, order: Order, service_type_ids: list[str], actor_id: uuid.UUID, now: datetime
) -> list[OrderItem]:
    """Append items for the given service types (unknown ids skipped, duplicates allowed)."""
    wanted = repo.parse_uuids(service_type_ids)
    sts = await repo.service_types_by_ids(session, order.company_id, set(wanted))
    cats = await repo.categories_by_ids(session, order.company_id, {s.category_id for s in sts.values()})
    schemas = await repo.schemas_by_ids(session, [s.schema_id for s in sts.values()])
    created: list[OrderItem] = []
    for st_id in wanted:
        st = sts.get(st_id)
        if st is None:
            continue
        schema = schemas.get(st.schema_id) if st.schema_id else None
        branch_price = (st.branch_prices or {}).get(str(order.branch_id))
        price = int(branch_price if branch_price is not None else st.price)
        item = OrderItem(
            order_id=order.id,
            company_id=order.company_id,
            branch_id=order.branch_id,
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


async def create_order(
    session: AsyncSession, company_id: uuid.UUID, staff: StaffPrincipal, body: CreateOrderIn, meta: RequestMeta
) -> OrderItemsOut:
    """New cheque: number allocated per branch, patient snapshots, patient stats, then items."""
    patient_id, branch_id = repo.to_uuid(body.patient_id), repo.to_uuid(body.branch_id)
    patient = await repo.get_patient(session, patient_id, company_id) if patient_id else None
    branch = await repo.get_branch(session, branch_id, company_id) if branch_id else None
    if not patient or not branch:
        raise NotFoundError("Bemor yoki filial topilmadi")
    number = await repo.allocate_order_number(session, branch.id, company_id)
    if number is None:
        raise NotFoundError("Bemor yoki filial topilmadi")
    now = utcnow()
    order = Order(
        company_id=company_id,
        branch_id=branch.id,
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
        company_id=company_id,
        action="order.create",
        entity="order",
        entity_id=order.id,
        after={
            "number": order.number,
            "patientId": str(patient.id),
            "branchId": str(branch.id),
            "total": order.total,
            "itemCount": order.item_count,
        },
        ip=meta.ip,
        request_id=meta.request_id,
    )
    return await _order_items_out(session, order)


async def add_items(
    session: AsyncSession, order_id: uuid.UUID, staff: StaffPrincipal, body: AddItemsIn, meta: RequestMeta
) -> OrderItemsOut:
    """Append services to an open/in-progress cheque (closed -> 409)."""
    order = await get_order_or_404(session, order_id, scope_company(staff), for_update=True)
    if order.status in ("cancelled", "completed"):
        raise ConflictError("Chek yopilgan", code="closed")
    now = utcnow()
    created = await _add_items(session, order, body.service_type_ids, staff.id, now)
    await recompute(session, order, now)
    await audit(
        session,
        actor_type="staff",
        actor_id=staff.id,
        company_id=order.company_id,
        action="order.items.add",
        entity="order",
        entity_id=order.id,
        after={"itemIds": [str(i.id) for i in created], "total": order.total},
        ip=meta.ip,
        request_id=meta.request_id,
    )
    return await _order_items_out(session, order)


async def remove_item(
    session: AsyncSession, order_id: uuid.UUID, item_id: uuid.UUID, staff: StaffPrincipal, meta: RequestMeta
) -> OrderItemsOut:
    """Soft-delete a pending item of an unpaid cheque."""
    order = await get_order_or_404(session, order_id, scope_company(staff), for_update=True)
    item = await repo.get_item(session, item_id, order.company_id)
    if not item or item.order_id != order.id:
        raise NotFoundError(ITEM_NOT_FOUND)
    if item.status != "pending":
        raise ConflictError("Laboratoriya boshlagan tahlilni o‘chirib bo‘lmaydi", code="in_progress")
    if order.payment != "unpaid":
        raise ConflictError("To‘langan chekdan xizmat o‘chirib bo‘lmaydi", code="paid")
    now = utcnow()
    item.deleted_at = now
    item.deleted_by = staff.id
    _touch(item, now)
    await recompute(session, order, now)
    await audit(
        session,
        actor_type="staff",
        actor_id=staff.id,
        company_id=order.company_id,
        action="order.items.remove",
        entity="order_item",
        entity_id=item.id,
        before={"serviceName": item.service_name, "price": item.price},
        ip=meta.ip,
        request_id=meta.request_id,
    )
    return await _order_items_out(session, order)


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
            company_id=order.company_id,
            channel="telegram",
            kind=kind,
            to=patient.telegram_chat_id,
            text=text,
            branch_id=order.branch_id,
            patient_id=order.patient_id,
            order_id=order.id,
            document_id=document_id,
            payload=payload,
        )


async def pay(
    session: AsyncSession, order_id: uuid.UUID, staff: StaffPrincipal, body: PayIn, meta: RequestMeta
) -> OrderPaymentsOut:
    """Record a payment (partial allowed), keep patient totalSpent, optionally SMS + Telegram receipt."""
    order = await get_order_or_404(session, order_id, scope_company(staff), for_update=True)
    if order.item_count == 0:
        raise ValidationError("Chekda xizmat yo‘q", code="empty")
    if order.payment == "paid":
        raise ConflictError("Chek allaqachon to‘langan", code="already_paid")
    remaining = order.total - order.paid_amount
    if body.amount <= 0 or body.amount > remaining:
        raise ValidationError(f"Summa 1 – {remaining} oralig‘ida bo‘lishi kerak", code="amount")
    now = utcnow()
    payment = Payment(
        order_id=order.id,
        company_id=order.company_id,
        branch_id=order.branch_id,
        amount=body.amount,
        method=body.method,
        employee_id=staff.id,
        created_by=staff.id,
        created_at=now,
        updated_at=now,
    )
    session.add(payment)
    await recompute(session, order, now)
    patient = await repo.get_patient(session, order.patient_id, order.company_id)
    if patient:
        await repo.bump_patient_stats(session, patient.id, spent=body.amount, now=now)
    company = await repo.get_company(session, order.company_id)
    if company:
        text = messaging.payment_receipt_text(company, order.number, body.amount, order.patient_name)
        if body.send_sms:
            await messaging.enqueue_sms_if_configured(
                session,
                company,
                kind="payment_receipt",
                to=order.patient_phone,
                text=text,
                patient_id=order.patient_id,
                order_id=order.id,
                branch_id=order.branch_id,
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
        company_id=order.company_id,
        action="order.pay",
        entity="payment",
        entity_id=payment.id,
        after={"orderId": str(order.id), "amount": body.amount, "method": body.method, "payment": order.payment},
        ip=meta.ip,
        request_id=meta.request_id,
    )
    return await _order_payments_out(session, order)


async def refund(
    session: AsyncSession, payment_id: uuid.UUID, staff: StaffPrincipal, reason: str, meta: RequestMeta
) -> OrderPaymentsOut:
    """Mark a payment refunded; the order's paid amount / payment status and patient totalSpent follow."""
    payment = await repo.get_payment(session, payment_id, scope_company(staff))
    if not payment:
        raise NotFoundError("To‘lov topilmadi")
    if payment.refunded_at is not None:
        raise StateError("To‘lov allaqachon qaytarilgan", code="state")
    order = await get_order_or_404(session, payment.order_id, payment.company_id, for_update=True)
    await session.refresh(payment)
    if payment.refunded_at is not None:
        raise StateError("To‘lov allaqachon qaytarilgan", code="state")
    now = utcnow()
    payment.refunded_at = now
    payment.refund_reason = reason or None
    _touch(payment, now)
    await recompute(session, order, now)
    patient = await repo.get_patient(session, order.patient_id, order.company_id)
    if patient:
        await repo.bump_patient_stats(session, patient.id, spent=-payment.amount, now=now)
    await audit(
        session,
        actor_type="staff",
        actor_id=staff.id,
        company_id=order.company_id,
        action="payment.refund",
        entity="payment",
        entity_id=payment.id,
        after={"orderId": str(order.id), "amount": payment.amount, "reason": reason, "payment": order.payment},
        ip=meta.ip,
        request_id=meta.request_id,
    )
    return await _order_payments_out(session, order)


async def cancel_order(
    session: AsyncSession, order_id: uuid.UUID, staff: StaffPrincipal, reason: str, meta: RequestMeta
) -> OrderOut:
    """Cancel an unpaid cheque: order + all items -> cancelled; `note` untouched, reason kept separately."""
    order = await get_order_or_404(session, order_id, scope_company(staff), for_update=True)
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
        company_id=order.company_id,
        action="order.cancel",
        entity="order",
        entity_id=order.id,
        before={"status": before_status},
        after={"status": "cancelled", "reason": reason},
        ip=meta.ip,
        request_id=meta.request_id,
    )
    return order_out(order)


# ----------------------------------------------------------------------------- lab


async def worklist(session: AsyncSession, company_id: uuid.UUID, q: WorklistQuery) -> Page[WorklistItemOut]:
    """Lab worklist rows (item + order snapshots + live patient gender/birth date)."""
    rows, total = await repo.worklist(session, company_id, q)
    out = [
        WorklistItemOut(
            **_item_fields(item),
            order_number=number,
            patient_name=patient_name,
            patient_phone=patient_phone,
            patient_gender=gender,
            patient_birth_date=birth_date,
        )
        for item, number, patient_name, patient_phone, gender, birth_date in rows
    ]
    return page_of(out, q, total)


async def get_item_dto(session: AsyncSession, item_id: uuid.UUID, staff: StaffPrincipal) -> OrderItemOut:
    """Company-scoped item."""
    return item_out(await get_item_or_404(session, item_id, scope_company(staff)))


async def save_values(
    session: AsyncSession, item_id: uuid.UUID, staff: StaffPrincipal, body: SaveValuesIn, meta: RequestMeta
) -> OrderItemOut:
    """Replace result values wholesale; pending/rejected -> entered, entered/submitted keep their status."""
    item = await get_item_or_404(session, item_id, scope_company(staff))
    order = await _lock_order_of_item(session, item)
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
        company_id=item.company_id,
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


async def submit_item(
    session: AsyncSession, item_id: uuid.UUID, staff: StaffPrincipal, meta: RequestMeta
) -> OrderItemOut:
    """Toggle entered <-> submitted after validating required schema fields."""
    item = await get_item_or_404(session, item_id, scope_company(staff))
    order = await _lock_order_of_item(session, item)
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
    if item.status == "submitted":
        item.status = "entered"
        item.submitted_at = None
    else:
        item.status = "submitted"
        item.submitted_at = now
    _touch(item, now)
    await recompute(session, order, now)
    await audit(
        session,
        actor_type="staff",
        actor_id=staff.id,
        company_id=item.company_id,
        action="item.submit" if item.status == "submitted" else "item.unsubmit",
        entity="order_item",
        entity_id=item.id,
        after={"status": item.status},
        ip=meta.ip,
        request_id=meta.request_id,
    )
    return item_out(item)


async def reject_item(
    session: AsyncSession, item_id: uuid.UUID, staff: StaffPrincipal, reason: str, meta: RequestMeta
) -> OrderItemOut:
    """Doctor sends a submitted result back to the lab."""
    item = await get_item_or_404(session, item_id, scope_company(staff))
    order = await _lock_order_of_item(session, item)
    if item.status != "submitted":
        raise StateError("Faqat yuborilgan natijani qaytarish mumkin", code="state")
    now = utcnow()
    item.status = "rejected"
    item.reject_reason = reason or None
    item.submitted_at = None
    _touch(item, now)
    await recompute(session, order, now)
    await audit(
        session,
        actor_type="staff",
        actor_id=staff.id,
        company_id=item.company_id,
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
    """DOMAIN_RULES section 7 chain: explicit/default (any status) -> active bound -> active generic -> 422."""
    st = await repo.service_types_by_ids(session, item.company_id, [item.service_type_id])
    wanted = repo.to_uuid(template_id) or (
        st[item.service_type_id].default_template_id if item.service_type_id in st else None
    )
    tpl = await repo.get_template(session, wanted, item.company_id) if wanted else None
    if tpl is None:
        tpl = await repo.find_active_template(session, item.company_id, item.service_type_id, item.category_id)
    if tpl is None:
        tpl = await repo.find_generic_template(session, item.company_id)
    if tpl is None:
        raise ValidationError("Bu xizmat uchun faol shablon yo‘q", code="no_template")
    return tpl


def _covers(tpl: ResultTemplate, item: OrderItem) -> bool:
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
) -> tuple[ResultDocument, Company | None, Patient | None]:
    """Freeze the render snapshot, create the final document, render + store the PDF (best effort)."""
    company = await repo.get_company(session, order.company_id)
    branch = await repo.get_branch(session, order.branch_id, order.company_id)
    patient = await repo.get_patient(session, order.patient_id, order.company_id)
    if company is None or patient is None:
        raise NotFoundError(ORDER_NOT_FOUND)
    category = (await repo.categories_by_ids(session, order.company_id, [primary_item.category_id])).get(
        primary_item.category_id
    )
    schemas = await repo.schemas_by_ids(session, [i.schema_id for i in items])
    sts = await repo.service_types_by_ids(session, order.company_id, {i.service_type_id for i in items})
    service_codes = {
        i.service_type_id: (
            sts[i.service_type_id].code
            if i.service_type_id in sts and sts[i.service_type_id].code
            else str(i.service_type_id)
        )
        for i in items
    }
    district_name = await repo.get_district_name(session, patient.district_id)
    snapshot = await templates_svc.build_document_snapshot(
        session,
        template=template,
        order=order,
        patient=patient,
        company=company,
        branch=branch,
        category=category,
        primary_item=primary_item,
        items=items,
        schemas=schemas,
        service_codes=service_codes,
        district_name=district_name,
        approved_at=now,
    )
    at = iso_z(now)
    doc = ResultDocument(
        company_id=order.company_id,
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
        public_token=secrets.token_urlsafe(24),
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
                company_id=order.company_id,
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
    return doc, company, patient


async def _notify_result_ready(
    session: AsyncSession,
    *,
    order: Order,
    company: Company | None,
    patient: Patient | None,
    doc: ResultDocument,
    text: str,
    actor_id: uuid.UUID,
) -> None:
    if company is None:
        return
    await messaging.enqueue_sms_if_configured(
        session,
        company,
        kind="result_ready",
        to=order.patient_phone,
        text=text,
        patient_id=order.patient_id,
        order_id=order.id,
        branch_id=order.branch_id,
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


async def approve_item(
    session: AsyncSession, item_id: uuid.UUID, staff: StaffPrincipal, body: ApproveItemIn, meta: RequestMeta
) -> ApproveItemOut:
    """Doctor approves one submitted item -> item-scoped document + PDF + notifications."""
    item = await get_item_or_404(session, item_id, scope_company(staff))
    order = await _lock_order_of_item(session, item)
    if item.status != "submitted":
        raise StateError("Faqat yuborilgan natijani tasdiqlash mumkin", code="state")
    template = await _resolve_item_template(session, item, body.template_id)
    now = utcnow()
    item.status = "approved"
    item.doctor_id = staff.id
    item.doctor_name = staff.employee.full_name
    item.approved_at = now
    _touch(item, now)
    doc, company, patient = await _issue_document(
        session,
        order=order,
        template=template,
        primary_item=item,
        items=[item],
        title=f"{item.service_name} — natija",
        order_item_ids=None,
        now=now,
        actor_id=staff.id,
    )
    item.document_id = doc.id
    _touch(item, now)
    await recompute(session, order, now)
    if company:
        text = messaging.result_ready_text(company, item.service_name, order.patient_name, order.number)
        await _notify_result_ready(
            session, order=order, company=company, patient=patient, doc=doc, text=text, actor_id=staff.id
        )
    await audit(
        session,
        actor_type="staff",
        actor_id=staff.id,
        company_id=item.company_id,
        action="item.approve",
        entity="order_item",
        entity_id=item.id,
        after={"documentId": str(doc.id), "templateId": str(template.id), "templateVersion": template.version},
        ip=meta.ip,
        request_id=meta.request_id,
    )
    return ApproveItemOut(item=item_out(item), document=document_out(doc))


async def order_scope_items(
    session: AsyncSession, order_id: uuid.UUID, template_id: uuid.UUID, staff: StaffPrincipal
) -> list[OrderItemOut]:
    """Non-cancelled items of the order an order-scope template would cover (any status)."""
    order = await get_order_or_404(session, order_id, scope_company(staff))
    tpl = await repo.get_template(session, template_id, order.company_id)
    if not tpl:
        raise NotFoundError("Shablon topilmadi")
    items = [i for i in await repo.items_of_order(session, order.id) if i.status != "cancelled" and _covers(tpl, i)]
    return [item_out(i) for i in items]


async def approve_order(
    session: AsyncSession, order_id: uuid.UUID, staff: StaffPrincipal, body: ApproveOrderIn, meta: RequestMeta
) -> ApproveOrderOut:
    """Approve every submitted covered item with ONE order-scope document (covered approved items share it)."""
    order = await get_order_or_404(session, order_id, scope_company(staff), for_update=True)
    tpl_id = repo.to_uuid(body.template_id)
    tpl = await repo.get_template(session, tpl_id, order.company_id) if tpl_id else None
    if not tpl or tpl.status != "active":
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
    now = utcnow()
    for it in to_approve:
        it.status = "approved"
        it.doctor_id = staff.id
        it.doctor_name = staff.employee.full_name
        it.approved_at = now
        _touch(it, now)
    doc, company, patient = await _issue_document(
        session,
        order=order,
        template=tpl,
        primary_item=to_approve[0],
        items=covered,
        title=tpl.name,
        order_item_ids=[i.id for i in covered],
        now=now,
        actor_id=staff.id,
    )
    for it in covered:
        if it in to_approve or it.document_id is None:
            it.document_id = doc.id
            _touch(it, now)
    await recompute(session, order, now)
    if company:
        text = messaging.result_ready_order_text(company, tpl.name, len(to_approve), order.patient_name, order.number)
        await _notify_result_ready(
            session, order=order, company=company, patient=patient, doc=doc, text=text, actor_id=staff.id
        )
    await audit(
        session,
        actor_type="staff",
        actor_id=staff.id,
        company_id=order.company_id,
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
    return ApproveOrderOut(items=[item_out(i) for i in covered], document=document_out(doc))


# ----------------------------------------------------------------------------- documents


async def list_documents(
    session: AsyncSession, company_id: uuid.UUID, *, order_id: str | None, patient_id: str | None
) -> list[ResultDocumentOut]:
    """Company documents by order and/or patient, newest first (malformed ids -> empty list)."""
    oid = repo.to_uuid(order_id) if order_id else None
    pid = repo.to_uuid(patient_id) if patient_id else None
    if (order_id and oid is None) or (patient_id and pid is None):
        return []
    rows = await repo.list_documents(session, company_id, order_id=oid, patient_id=pid)
    return [document_out(d) for d in rows]


async def ensure_document_pdf(session: AsyncSession, document: ResultDocument) -> bytes:
    """PDF bytes of a document — stored file when present, else render from the frozen snapshot and store."""
    if document.pdf_file_id:
        loaded = await files_svc.load_bytes(session, document.pdf_file_id)
        if loaded:
            return loaded[0]
    pdf = await templates_svc.render_snapshot_pdf(session, document.snapshot or {})
    stored = await files_svc.store_bytes(
        session,
        company_id=document.company_id,
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
