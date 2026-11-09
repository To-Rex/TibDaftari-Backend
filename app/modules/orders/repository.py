"""Orders SQL: selects for orders/items/payments/documents (rows by id carry no tenant filter — the service checks
`staff.require_unit(row.unit_id)`), list + worklist queries over a set of facilities, and the small catalogue /
template look-ups the workflow needs (models only, no rules)."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, Row, Select, String, and_, case, exists, false, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import paginate_query, sort_clause
from app.core.textutil import digits, fold
from app.core.timeutil import day_range
from app.infrastructure.db.base import alive
from app.infrastructure.db.models import (
    AttributeSchema,
    Category,
    District,
    Employee,
    Order,
    OrderItem,
    OrgUnit,
    Patient,
    Payment,
    Region,
    ResultDocument,
    ResultTemplate,
    TemplateUnit,
)
from app.modules.orders.schemas import OrderListQuery, WorklistQuery

ORDER_SORTABLE: dict[str, Any] = {
    "createdAt": Order.created_at,
    "updatedAt": Order.updated_at,
    "number": Order.number,
    "total": Order.total,
    "status": Order.status,
    "payment": Order.payment,
    "patientName": Order.patient_name,
    "paidAmount": Order.paid_amount,
    # still to pay; the number of services; the discount
    "remaining": Order.total - Order.paid_amount,
    "itemCount": Order.item_count,
    "discountAmount": Order.discount_amount,
    "discountPercent": Order.discount_percent,
    # when every result was ready (cheques not completed yet sort last)
    "completedAt": Order.completed_at,
}

WORKLIST_SORTABLE: dict[str, Any] = {
    "createdAt": OrderItem.created_at,
    "updatedAt": OrderItem.updated_at,
    "serviceName": OrderItem.service_name,
    "categoryName": OrderItem.category_name,
    "status": OrderItem.status,
    "patientName": Order.patient_name,
    "orderNumber": Order.number,
    "enteredAt": OrderItem.entered_at,
    "submittedAt": OrderItem.submitted_at,
}


def to_uuid(value: str | uuid.UUID | None) -> uuid.UUID | None:
    """Lenient UUID parse — a malformed id simply matches nothing."""
    if value is None or value == "":
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def parse_uuids(values: Iterable[str]) -> list[uuid.UUID]:
    """Client-supplied id list -> valid UUIDs (invalid ones dropped, order preserved)."""
    out: list[uuid.UUID] = []
    for v in values:
        u = to_uuid(v)
        if u is not None:
            out.append(u)
    return out


# ----------------------------------------------------------------------------- orders


async def get_order(session: AsyncSession, order_id: uuid.UUID, *, for_update: bool = False) -> Order | None:
    """Alive order by id; `for_update` takes a row lock so that per-order mutations (items, payments, approvals)
    are serialised and `recompute` sees committed state."""
    stmt = select(Order).where(Order.id == order_id, alive(Order))
    if for_update:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def items_of_order(session: AsyncSession, order_id: uuid.UUID) -> list[OrderItem]:
    """Alive items of an order, oldest first."""
    stmt = (
        select(OrderItem)
        .where(OrderItem.order_id == order_id, alive(OrderItem))
        .order_by(OrderItem.created_at, OrderItem.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def payments_of_order(session: AsyncSession, order_id: uuid.UUID) -> list[Payment]:
    """Alive payments of an order, oldest first."""
    stmt = select(Payment).where(Payment.order_id == order_id, alive(Payment)).order_by(Payment.created_at, Payment.id)
    return list((await session.execute(stmt)).scalars().all())


async def get_item(session: AsyncSession, item_id: uuid.UUID) -> OrderItem | None:
    """Alive item by id."""
    stmt = select(OrderItem).where(OrderItem.id == item_id, alive(OrderItem))
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_payment(session: AsyncSession, payment_id: uuid.UUID) -> Payment | None:
    """Alive payment by id."""
    stmt = select(Payment).where(Payment.id == payment_id, alive(Payment))
    return (await session.execute(stmt)).scalar_one_or_none()


async def bump_patient_stats(session: AsyncSession, patient_id: uuid.UUID, *, orders: int = 0, spent: int = 0, visit_at: datetime | None = None, now: datetime) -> None:
    """Atomic SQL-side counter update (no read-modify-write): stats_orders / stats_total_spent / last visit."""
    values: dict[str, Any] = {"updated_at": now}
    if orders:
        values["stats_orders"] = func.greatest(Patient.stats_orders + orders, 0)
    if spent:
        values["stats_total_spent"] = func.greatest(Patient.stats_total_spent + spent, 0)
    if visit_at is not None:
        values["stats_last_visit_at"] = func.greatest(func.coalesce(Patient.stats_last_visit_at, visit_at), visit_at)
    await session.execute(update(Patient).where(Patient.id == patient_id).values(**values).execution_options(synchronize_session="fetch"))


def _order_search(search: str | None) -> ColumnElement[bool] | None:
    """fold(patientName) contains OR lower(number) contains OR (digits present AND phone contains digits)."""
    if not search or not search.strip():
        return None
    clauses: list[ColumnElement[bool]] = []
    needle = fold(search)
    if needle:
        clauses.append(func.fold_text(Order.patient_name, type_=String).contains(needle, autoescape=True))
    clauses.append(func.lower(Order.number).contains(search.strip().lower(), autoescape=True))
    d = digits(search)
    if d:
        clauses.append(Order.patient_phone.contains(d, autoescape=True))
    return or_(*clauses) if clauses else false()


async def list_orders(session: AsyncSession, unit_ids: Sequence[uuid.UUID], q: OrderListQuery) -> tuple[list[Order], int]:
    """Filtered + paged orders of the given facilities (DOMAIN_RULES section 7 `list`).

    `unit_ids` is the resolved scope: every unit under the URL's unit, already narrowed by `q.unit_id`."""
    stmt: Select = select(Order).where(*order_conditions(unit_ids, q))
    order_by = [sort_clause(q.sort_by, q.sort_dir, ORDER_SORTABLE, "createdAt"), Order.id.desc()]
    return await paginate_query(session, stmt, q, order_by=order_by)


def order_conditions(unit_ids: Sequence[uuid.UUID], q: OrderListQuery) -> list[ColumnElement[bool]]:
    """WHERE clauses of the cheque list (shared by the list, its summary and the export)."""
    conds: list[ColumnElement[bool]] = [Order.unit_id.in_(list(unit_ids) or [uuid.UUID(int=0)]), alive(Order)]
    if q.status:
        conds.append(Order.status == q.status)
    if q.payment:
        conds.append(Order.payment == q.payment)
    if q.patient_id:
        conds.append(Order.patient_id == to_uuid(q.patient_id))
    start, end = day_range(q.date_from, q.date_to)
    if start:
        conds.append(Order.created_at >= start)
    if end:
        conds.append(Order.created_at < end)
    pred = _order_search(q.search)
    if pred is not None:
        conds.append(pred)
    if q.methods:
        conds.append(exists().where(Payment.order_id == Order.id, alive(Payment), Payment.refunded_at.is_(None), Payment.method.in_(q.methods)))
    if q.min_total is not None:
        conds.append(Order.total >= q.min_total)
    if q.max_total is not None:
        conds.append(Order.total <= q.max_total)
    if q.debt is True:
        conds.append(and_(Order.status != "cancelled", Order.paid_amount < Order.total))
    elif q.debt is False:
        conds.append(or_(Order.status == "cancelled", Order.paid_amount >= Order.total))
    if q.discount is True:
        conds.append(Order.discount_amount > 0)
    elif q.discount is False:
        conds.append(Order.discount_amount == 0)
    live_item = and_(OrderItem.order_id == Order.id, alive(OrderItem), OrderItem.status != "cancelled")
    if q.service_type_id:
        conds.append(exists().where(live_item, OrderItem.service_type_id == to_uuid(q.service_type_id)))
    if q.category_ids:
        conds.append(exists().where(live_item, OrderItem.category_id.in_(parse_uuids(q.category_ids))))
    if q.created_by:
        conds.append(Order.created_by_employee_id == to_uuid(q.created_by))
    approved = exists().where(live_item, OrderItem.status == "approved")
    not_ready = exists().where(live_item, OrderItem.status != "approved")
    if q.results == "ready":
        conds.append(and_(approved, ~not_ready))
    elif q.results == "partial":
        conds.append(and_(approved, not_ready))
    elif q.results == "none":
        conds.append(~approved)
    if q.min_items is not None:
        conds.append(Order.item_count >= q.min_items)
    if q.max_items is not None:
        conds.append(Order.item_count <= q.max_items)
    refunds = exists().where(Payment.order_id == Order.id, alive(Payment), Payment.refunded_at.is_not(None))
    if q.refunded is True:
        conds.append(refunds)
    elif q.refunded is False:
        conds.append(~refunds)
    return conds


def _chunks(ids: list[uuid.UUID], size: int = 2000) -> Iterable[list[uuid.UUID]]:
    for i in range(0, len(ids), size):
        yield ids[i : i + size]


async def payments_by_order(session: AsyncSession, order_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, int]]:
    """order → {method: Σ non-refunded payments}."""
    out: dict[uuid.UUID, dict[str, int]] = {}
    for part in _chunks(order_ids):
        rows = (
            await session.execute(
                select(Payment.order_id, Payment.method, func.coalesce(func.sum(Payment.amount), 0))
                .where(Payment.order_id.in_(part), alive(Payment), Payment.refunded_at.is_(None))
                .group_by(Payment.order_id, Payment.method)
            )
        ).all()
        for oid, method, amount in rows:
            out.setdefault(oid, {})[str(method)] = int(amount)
    return out


async def service_names_by_order(session: AsyncSession, order_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[str]]:
    """order → names of its non-cancelled services (cheque order)."""
    out: dict[uuid.UUID, list[str]] = {}
    for part in _chunks(order_ids):
        rows = (
            await session.execute(
                select(OrderItem.order_id, OrderItem.service_name)
                .where(OrderItem.order_id.in_(part), alive(OrderItem), OrderItem.status != "cancelled")
                .order_by(OrderItem.order_id, OrderItem.created_at, OrderItem.id)
            )
        ).all()
        for oid, name in rows:
            out.setdefault(oid, []).append(name)
    return out


async def export_orders(session: AsyncSession, unit_ids: Sequence[uuid.UUID], q: OrderListQuery, limit: int) -> tuple[list[Row[Any]], bool]:
    """Every cheque the list's filters select, in the list's order (+ who opened it, the facility's name) — at most `limit`."""
    stmt = (
        select(Order, Employee.full_name.label("creator"), OrgUnit.name.label("unit"))
        .outerjoin(Employee, Employee.id == Order.created_by_employee_id)
        .outerjoin(OrgUnit, OrgUnit.id == Order.unit_id)
        .where(*order_conditions(unit_ids, q))
        .order_by(sort_clause(q.sort_by, q.sort_dir, ORDER_SORTABLE, "createdAt"), Order.id.desc())
        .limit(limit + 1)
    )
    rows = list((await session.execute(stmt)).all())
    return rows[:limit], len(rows) > limit


async def orders_summary(session: AsyncSession, unit_ids: Sequence[uuid.UUID], q: OrderListQuery) -> dict[str, Any]:
    """Count + money (non-cancelled) of the filtered cheques, their payments by method, and who opened them."""
    conds = order_conditions(unit_ids, q)
    live = Order.status != "cancelled"
    row = (
        await session.execute(
            select(
                func.count().label("count"),
                func.coalesce(func.sum(case((live, Order.total), else_=0)), 0).label("total"),
                func.coalesce(func.sum(case((live, Order.paid_amount), else_=0)), 0).label("paid"),
                func.coalesce(func.sum(case((live, func.greatest(Order.total - Order.paid_amount, 0)), else_=0)), 0).label("debt"),
            ).where(*conds)
        )
    ).one()
    methods = (
        await session.execute(
            select(Payment.method, func.coalesce(func.sum(Payment.amount), 0))
            .where(Payment.order_id.in_(select(Order.id).where(*conds)), alive(Payment), Payment.refunded_at.is_(None))
            .group_by(Payment.method)
        )
    ).all()
    n = func.count()
    cashiers = (
        await session.execute(
            select(Order.created_by_employee_id, func.max(Employee.full_name), n)
            .outerjoin(Employee, Employee.id == Order.created_by_employee_id)
            .where(*order_conditions(unit_ids, q.model_copy(update={"created_by": None})))
            .group_by(Order.created_by_employee_id)
            .order_by(n.desc())
            .limit(50)
        )
    ).all()
    return {
        "count": int(row.count),
        "total": int(row.total),
        "paid": int(row.paid),
        "debt": int(row.debt),
        "methods": [{"method": m, "amount": int(a)} for m, a in methods],
        "cashiers": [{"id": str(eid), "name": name or "—", "count": int(c)} for eid, name, c in cashiers if eid is not None],
    }


# ----------------------------------------------------------------------------- worklist


def _worklist_search(search: str | None) -> ColumnElement[bool] | None:
    if not search or not search.strip():
        return None
    needle = fold(search)
    clauses: list[ColumnElement[bool]] = [func.lower(Order.number).contains(search.strip().lower(), autoescape=True)]
    if needle:
        clauses.append(func.fold_text(Order.patient_name, type_=String).contains(needle, autoescape=True))
        clauses.append(func.fold_text(OrderItem.service_name, type_=String).contains(needle, autoescape=True))
    return or_(*clauses)


def _worklist_filters(stmt: Select, unit_ids: Sequence[uuid.UUID], q: WorklistQuery, *, with_status: bool) -> Select:
    """Shared WHERE clause of the worklist (items with a schema on paid/partial orders of the given facilities)."""
    stmt = stmt.where(
        OrderItem.unit_id.in_(list(unit_ids) or [uuid.UUID(int=0)]),
        alive(OrderItem),
        alive(Order),
        OrderItem.schema_id.is_not(None),
        Order.payment != "unpaid",
    )
    if q.category_ids:
        stmt = stmt.where(OrderItem.category_id.in_(parse_uuids(q.category_ids) or [uuid.UUID(int=0)]))
    if with_status and q.status:
        stmt = stmt.where(OrderItem.status.in_(list(q.status)))
    start, end = day_range(q.date_from, q.date_to)
    if start:
        stmt = stmt.where(OrderItem.created_at >= start)
    if end:
        stmt = stmt.where(OrderItem.created_at < end)
    pred = _worklist_search(q.search)
    if pred is not None:
        stmt = stmt.where(pred)
    return stmt


async def worklist(session: AsyncSession, unit_ids: Sequence[uuid.UUID], q: WorklistQuery) -> tuple[list[Row[Any]], int]:
    """Items with a schema on paid/partial orders + order snapshots + live patient gender/birth date."""
    stmt: Select = (
        select(OrderItem, Order.number, Order.patient_name, Order.patient_phone, Patient.gender, Patient.birth_date)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Patient, Patient.id == Order.patient_id, isouter=True)
    )
    stmt = _worklist_filters(stmt, unit_ids, q, with_status=True)
    order_by = [sort_clause(q.sort_by, q.sort_dir, WORKLIST_SORTABLE, "createdAt"), OrderItem.id.desc()]
    return await paginate_query(session, stmt, q, order_by=order_by, scalars=False)


async def worklist_counts(session: AsyncSession, unit_ids: Sequence[uuid.UUID], q: WorklistQuery) -> dict[str, int]:
    """Per-status counts for the same filters (one GROUP BY instead of one request per status tab)."""
    stmt: Select = select(OrderItem.status, func.count()).join(Order, Order.id == OrderItem.order_id)
    stmt = _worklist_filters(stmt, unit_ids, q, with_status=False).group_by(OrderItem.status)
    rows = (await session.execute(stmt)).all()
    return {str(status): int(n) for status, n in rows}


# ----------------------------------------------------------------------------- documents


async def get_document(session: AsyncSession, document_id: uuid.UUID) -> ResultDocument | None:
    """Alive document by id."""
    stmt = select(ResultDocument).where(ResultDocument.id == document_id, alive(ResultDocument))
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_document_by_token(session: AsyncSession, token: str) -> ResultDocument | None:
    """Public-link lookup (unguessable token)."""
    stmt = select(ResultDocument).where(ResultDocument.public_token == token, alive(ResultDocument))
    return (await session.execute(stmt.limit(1))).scalar_one_or_none()


async def get_document_by_verify_token(session: AsyncSession, token: str) -> ResultDocument | None:
    """QR authenticity lookup — revoked (soft-deleted) documents included: the check must say they were withdrawn."""
    stmt = select(ResultDocument).where(ResultDocument.verify_token == token)
    return (await session.execute(stmt.limit(1))).scalar_one_or_none()


async def document_token_revoked(session: AsyncSession, token: str) -> bool:
    """The link belongs to a result the doctor took back (it is being corrected)."""
    stmt = select(ResultDocument.id).where(ResultDocument.public_token == token, ResultDocument.status == "revoked").limit(1)
    return (await session.execute(stmt)).first() is not None


async def list_documents(
    session: AsyncSession,
    unit_ids: Sequence[uuid.UUID],
    *,
    order_id: uuid.UUID | None,
    patient_id: uuid.UUID | None,
    limit: int = 500,
) -> list[ResultDocument]:
    """Documents of the given facilities filtered by order and/or patient, newest first."""
    stmt = select(ResultDocument).where(ResultDocument.unit_id.in_(list(unit_ids) or [uuid.UUID(int=0)]), alive(ResultDocument))
    if order_id is not None:
        stmt = stmt.where(ResultDocument.order_id == order_id)
    if patient_id is not None:
        stmt = stmt.where(ResultDocument.patient_id == patient_id)
    stmt = stmt.order_by(ResultDocument.created_at.desc(), ResultDocument.id.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


# ----------------------------------------------------------------------------- look-ups (other aggregates, read-only)


async def get_patient(session: AsyncSession, patient_id: uuid.UUID) -> Patient | None:
    """Alive patient by id (the cheque already proved the patient belongs here)."""
    stmt = select(Patient).where(Patient.id == patient_id, alive(Patient))
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_employee_name(session: AsyncSession, employee_id: uuid.UUID) -> str | None:
    """Full name of an employee (technician/doctor snapshots)."""
    return (await session.execute(select(Employee.full_name).where(Employee.id == employee_id))).scalar_one_or_none()


async def get_district_name(session: AsyncSession, district_id: uuid.UUID | None) -> str | None:
    """District name for the render context (None when the patient has no district)."""
    if district_id is None:
        return None
    return (await session.execute(select(District.name).where(District.id == district_id))).scalar_one_or_none()


async def get_region_name(session: AsyncSession, region_id: uuid.UUID | None) -> str | None:
    """Region name for the render context (None when the patient has no region)."""
    if region_id is None:
        return None
    return (await session.execute(select(Region.name).where(Region.id == region_id))).scalar_one_or_none()


async def categories_by_ids(session: AsyncSession, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, Category]:
    """Categories by id (any state — item snapshots and render contexts must still resolve)."""
    ids = list(ids)
    if not ids:
        return {}
    stmt = select(Category).where(Category.id.in_(ids))
    return {c.id: c for c in (await session.execute(stmt)).scalars().all()}


async def schemas_by_ids(session: AsyncSession, ids: Iterable[uuid.UUID | None]) -> dict[uuid.UUID, AttributeSchema]:
    """Attribute schemas by id (None ids ignored; any state — frozen items keep rendering)."""
    ids = list({i for i in ids if i is not None})
    if not ids:
        return {}
    stmt = select(AttributeSchema).where(AttributeSchema.id.in_(ids))
    return {s.id: s for s in (await session.execute(stmt)).scalars().all()}


async def descendant_category_ids(session: AsyncSession, root_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
    """The roots plus every transitive alive descendant category (one recursive CTE for all roots)."""
    roots = list(root_ids)
    if not roots:
        return set()
    base = select(Category.id).where(Category.id.in_(roots), alive(Category)).cte("tree", recursive=True)
    child = select(Category.id).join(base, Category.parent_id == base.c.id).where(alive(Category))
    tree = base.union(child)
    rows = (await session.execute(select(tree.c.id))).scalars().all()
    return set(rows)


# ----------------------------------------------------------------------------- templates (rows only; rendering is templates.service)


async def get_template(session: AsyncSession, template_id: uuid.UUID) -> ResultTemplate | None:
    """Alive template by id, any status (the service decides whether it is usable at the facility)."""
    stmt = select(ResultTemplate).where(ResultTemplate.id == template_id, alive(ResultTemplate))
    return (await session.execute(stmt)).scalar_one_or_none()


async def template_units_for(session: AsyncSession, template_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, set[uuid.UUID]]:
    """template → the units it is explicitly limited to (`template_units`); absent = usable in the owner's whole subtree."""
    ids = list({i for i in template_ids})
    if not ids:
        return {}
    rows = (await session.execute(select(TemplateUnit.template_id, TemplateUnit.unit_id).where(TemplateUnit.template_id.in_(ids)))).all()
    out: dict[uuid.UUID, set[uuid.UUID]] = {}
    for tid, uid in rows:
        out.setdefault(tid, set()).add(uid)
    return out


# ----------------------------------------------------------------------------- receipt tracking


async def mark_document_viewed(session: AsyncSession, document_id: uuid.UUID, now: datetime) -> None:
    """The patient opened the result (public link / portal): first + last open and a counter."""
    await session.execute(
        update(ResultDocument)
        .where(ResultDocument.id == document_id)
        .values(view_count=ResultDocument.view_count + 1, viewed_at=func.coalesce(ResultDocument.viewed_at, now), last_viewed_at=now)
        .execution_options(synchronize_session=False)
    )


async def claim_public_first_view(session: AsyncSession, document_id: uuid.UUID, now: datetime) -> bool:
    """Atomically claim the one direct public open; False means a code is required."""
    res = await session.execute(
        update(ResultDocument)
        .where(ResultDocument.id == document_id, ResultDocument.public_view_count == 0)
        .values(public_view_count=1, public_first_viewed_at=now, public_last_viewed_at=now)
        .returning(ResultDocument.id)
        .execution_options(synchronize_session=False)
    )
    return res.scalar_one_or_none() is not None


async def mark_public_viewed(session: AsyncSession, document_id: uuid.UUID, now: datetime) -> None:
    """Count an OTP-confirmed public open; the direct-open gate is already closed."""
    await session.execute(
        update(ResultDocument)
        .where(ResultDocument.id == document_id)
        .values(
            public_view_count=ResultDocument.public_view_count + 1,
            public_first_viewed_at=func.coalesce(ResultDocument.public_first_viewed_at, now),
            public_last_viewed_at=now,
        )
        .execution_options(synchronize_session=False)
    )


async def mark_document_printed(session: AsyncSession, document_id: uuid.UUID, now: datetime) -> int:
    """Staff printed the result (it is handed over on paper): first print + a counter; returns the new count."""
    res = await session.execute(
        update(ResultDocument)
        .where(ResultDocument.id == document_id)
        .values(print_count=ResultDocument.print_count + 1, printed_at=func.coalesce(ResultDocument.printed_at, now))
        .returning(ResultDocument.print_count)
        .execution_options(synchronize_session=False)
    )
    return int(res.scalar_one_or_none() or 0)
