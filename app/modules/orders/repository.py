"""Orders SQL: tenant-scoped selects for orders/items/payments/documents, list + worklist queries,
atomic cheque numbering, and the catalog/template look-ups the workflow needs (models only)."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, Row, Select, String, false, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import paginate_query, sort_clause
from app.core.textutil import digits, fold
from app.core.timeutil import day_range
from app.infrastructure.db.base import alive
from app.infrastructure.db.models import (
    AttributeSchema,
    Branch,
    Category,
    Company,
    District,
    Employee,
    Order,
    OrderItem,
    Patient,
    Payment,
    ResultDocument,
    ResultTemplate,
    ServiceType,
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


async def get_order(session: AsyncSession, order_id: uuid.UUID, company_id: uuid.UUID | None, *, for_update: bool = False) -> Order | None:
    """Alive order by id, optionally restricted to a company; `for_update` takes a row lock so that
    per-order mutations (items, payments, approvals) are serialised and `recompute` sees committed state."""
    stmt = select(Order).where(Order.id == order_id, alive(Order))
    if company_id is not None:
        stmt = stmt.where(Order.company_id == company_id)
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


async def get_item(session: AsyncSession, item_id: uuid.UUID, company_id: uuid.UUID | None) -> OrderItem | None:
    """Alive item by id, optionally restricted to a company."""
    stmt = select(OrderItem).where(OrderItem.id == item_id, alive(OrderItem))
    if company_id is not None:
        stmt = stmt.where(OrderItem.company_id == company_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_payment(session: AsyncSession, payment_id: uuid.UUID, company_id: uuid.UUID | None) -> Payment | None:
    """Alive payment by id, optionally restricted to a company."""
    stmt = select(Payment).where(Payment.id == payment_id, alive(Payment))
    if company_id is not None:
        stmt = stmt.where(Payment.company_id == company_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def allocate_order_number(session: AsyncSession, branch_id: uuid.UUID, company_id: uuid.UUID) -> str | None:
    """`UPDATE branches SET order_seq = order_seq + 1 ... RETURNING` on the request connection: concurrent
    creations on one branch serialise on the branch row until commit (gap-free numbers), and no second
    pooled connection is needed while the request already holds one."""
    row = (
        await session.execute(
            update(Branch)
            .where(Branch.id == branch_id, Branch.company_id == company_id, alive(Branch))
            .values(order_seq=Branch.order_seq + 1)
            .returning(Branch.order_seq, Branch.code)
        )
    ).one_or_none()
    if row is None:
        return None
    return f"{row.code}-{int(row.order_seq):06d}"


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


async def list_orders(session: AsyncSession, company_id: uuid.UUID, q: OrderListQuery) -> tuple[list[Order], int]:
    """Filtered + paged orders of one company (DOMAIN_RULES section 7 `list`)."""
    stmt: Select = select(Order).where(Order.company_id == company_id, alive(Order))
    if q.branch_id:
        stmt = stmt.where(Order.branch_id == to_uuid(q.branch_id))
    if q.status:
        stmt = stmt.where(Order.status == q.status)
    if q.payment:
        stmt = stmt.where(Order.payment == q.payment)
    if q.patient_id:
        stmt = stmt.where(Order.patient_id == to_uuid(q.patient_id))
    start, end = day_range(q.date_from, q.date_to)
    if start:
        stmt = stmt.where(Order.created_at >= start)
    if end:
        stmt = stmt.where(Order.created_at < end)
    pred = _order_search(q.search)
    if pred is not None:
        stmt = stmt.where(pred)
    order_by = [sort_clause(q.sort_by, q.sort_dir, ORDER_SORTABLE, "createdAt"), Order.id.desc()]
    return await paginate_query(session, stmt, q, order_by=order_by)


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


async def worklist(session: AsyncSession, company_id: uuid.UUID, q: WorklistQuery) -> tuple[list[Row[Any]], int]:
    """Items with a schema on paid/partial orders + order snapshots + live patient gender/birth date."""
    stmt: Select = (
        select(OrderItem, Order.number, Order.patient_name, Order.patient_phone, Patient.gender, Patient.birth_date)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Patient, Patient.id == Order.patient_id, isouter=True)
        .where(
            OrderItem.company_id == company_id,
            alive(OrderItem),
            alive(Order),
            OrderItem.schema_id.is_not(None),
            Order.payment != "unpaid",
        )
    )
    if q.branch_id:
        stmt = stmt.where(OrderItem.branch_id == to_uuid(q.branch_id))
    if q.category_ids:
        stmt = stmt.where(OrderItem.category_id.in_(parse_uuids(q.category_ids) or [uuid.UUID(int=0)]))
    if q.status:
        stmt = stmt.where(OrderItem.status.in_(list(q.status)))
    start, end = day_range(q.date_from, q.date_to)
    if start:
        stmt = stmt.where(OrderItem.created_at >= start)
    if end:
        stmt = stmt.where(OrderItem.created_at < end)
    pred = _worklist_search(q.search)
    if pred is not None:
        stmt = stmt.where(pred)
    order_by = [sort_clause(q.sort_by, q.sort_dir, WORKLIST_SORTABLE, "createdAt"), OrderItem.id.desc()]
    return await paginate_query(session, stmt, q, order_by=order_by, scalars=False)


# ----------------------------------------------------------------------------- documents


async def get_document(
    session: AsyncSession, document_id: uuid.UUID, company_id: uuid.UUID | None
) -> ResultDocument | None:
    """Alive document by id, optionally restricted to a company."""
    stmt = select(ResultDocument).where(ResultDocument.id == document_id, alive(ResultDocument))
    if company_id is not None:
        stmt = stmt.where(ResultDocument.company_id == company_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_document_by_token(session: AsyncSession, token: str) -> ResultDocument | None:
    """Public-link lookup (unguessable token)."""
    stmt = select(ResultDocument).where(ResultDocument.public_token == token, alive(ResultDocument))
    return (await session.execute(stmt.limit(1))).scalar_one_or_none()


async def list_documents(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    order_id: uuid.UUID | None,
    patient_id: uuid.UUID | None,
    limit: int = 500,
) -> list[ResultDocument]:
    """Company documents filtered by order and/or patient, newest first."""
    stmt = select(ResultDocument).where(ResultDocument.company_id == company_id, alive(ResultDocument))
    if order_id is not None:
        stmt = stmt.where(ResultDocument.order_id == order_id)
    if patient_id is not None:
        stmt = stmt.where(ResultDocument.patient_id == patient_id)
    stmt = stmt.order_by(ResultDocument.created_at.desc(), ResultDocument.id.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


# ----------------------------------------------------------------------------- look-ups (other aggregates, read-only)


async def get_patient(session: AsyncSession, patient_id: uuid.UUID, company_id: uuid.UUID) -> Patient | None:
    """Alive patient of the company."""
    stmt = select(Patient).where(Patient.id == patient_id, Patient.company_id == company_id, alive(Patient))
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_branch(session: AsyncSession, branch_id: uuid.UUID, company_id: uuid.UUID) -> Branch | None:
    """Alive branch of the company."""
    stmt = select(Branch).where(Branch.id == branch_id, Branch.company_id == company_id, alive(Branch))
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_company(session: AsyncSession, company_id: uuid.UUID) -> Company | None:
    """Alive company."""
    stmt = select(Company).where(Company.id == company_id, alive(Company))
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_employee_name(session: AsyncSession, employee_id: uuid.UUID) -> str | None:
    """Full name of an employee (technician/doctor snapshots)."""
    return (await session.execute(select(Employee.full_name).where(Employee.id == employee_id))).scalar_one_or_none()


async def get_district_name(session: AsyncSession, district_id: uuid.UUID | None) -> str | None:
    """District name for the render context (None when the patient has no district)."""
    if district_id is None:
        return None
    return (await session.execute(select(District.name).where(District.id == district_id))).scalar_one_or_none()


async def service_types_by_ids(
    session: AsyncSession, company_id: uuid.UUID, ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, ServiceType]:
    """Alive service types of the company by id."""
    ids = list(ids)
    if not ids:
        return {}
    stmt = select(ServiceType).where(ServiceType.company_id == company_id, ServiceType.id.in_(ids), alive(ServiceType))
    return {s.id: s for s in (await session.execute(stmt)).scalars().all()}


async def categories_by_ids(
    session: AsyncSession, company_id: uuid.UUID, ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, Category]:
    """Categories of the company by id (any state — snapshots must resolve)."""
    ids = list(ids)
    if not ids:
        return {}
    stmt = select(Category).where(Category.company_id == company_id, Category.id.in_(ids))
    return {c.id: c for c in (await session.execute(stmt)).scalars().all()}


async def schemas_by_ids(session: AsyncSession, ids: Iterable[uuid.UUID | None]) -> dict[uuid.UUID, AttributeSchema]:
    """Attribute schemas by id (None ids ignored)."""
    ids = list({i for i in ids if i is not None})
    if not ids:
        return {}
    stmt = select(AttributeSchema).where(AttributeSchema.id.in_(ids))
    return {s.id: s for s in (await session.execute(stmt)).scalars().all()}


# ----------------------------------------------------------------------------- templates (rows only; rendering is templates.service)


async def get_template(session: AsyncSession, template_id: uuid.UUID, company_id: uuid.UUID) -> ResultTemplate | None:
    """Alive template of the company in any status."""
    stmt = select(ResultTemplate).where(
        ResultTemplate.id == template_id, ResultTemplate.company_id == company_id, alive(ResultTemplate)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def find_active_template(
    session: AsyncSession, company_id: uuid.UUID, service_type_id: uuid.UUID, category_id: uuid.UUID
) -> ResultTemplate | None:
    """First active template bound to the service type or its category (oldest first, deterministic)."""
    stmt = (
        select(ResultTemplate)
        .where(
            ResultTemplate.company_id == company_id,
            alive(ResultTemplate),
            ResultTemplate.status == "active",
            or_(ResultTemplate.service_type_ids.any(service_type_id), ResultTemplate.category_ids.any(category_id)),
        )
        .order_by(ResultTemplate.created_at, ResultTemplate.id)
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def find_generic_template(session: AsyncSession, company_id: uuid.UUID) -> ResultTemplate | None:
    """First active template with no service-type bindings (catch-all)."""
    stmt = (
        select(ResultTemplate)
        .where(
            ResultTemplate.company_id == company_id,
            alive(ResultTemplate),
            ResultTemplate.status == "active",
            func.cardinality(ResultTemplate.service_type_ids) == 0,
        )
        .order_by(ResultTemplate.created_at, ResultTemplate.id)
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()
