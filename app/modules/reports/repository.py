"""Reports SQL — pure aggregates over orders / order_items / payments (no per-row loops)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Interval,
    Row,
    Select,
    String,
    and_,
    case,
    cast,
    distinct,
    extract,
    func,
    literal,
    literal_column,
    not_,
    or_,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import Date

from app.core.timeutil import DEFAULT_TZ
from app.infrastructure.db.base import alive
from app.infrastructure.db.models import (
    Branch,
    Category,
    District,
    Order,
    OrderItem,
    OutboxMessage,
    Patient,
    Payment,
    ResultDocument,
    ServiceType,
)
from app.modules.orders.repository import _order_search as order_search
from app.modules.patients.repository import in_branches as patient_in_branches

_TZ = DEFAULT_TZ.key


def _local_day(col: Any) -> Any:
    """`created_at` (UTC) → local calendar day in the clinic timezone (Asia/Tashkent)."""
    return cast(func.timezone(_TZ, col), Date)


async def dashboard_counters(
    session: AsyncSession, company_id: uuid.UUID, branches: list[uuid.UUID] | None, today_start: datetime, today_end: datetime
) -> dict[str, int]:
    """todayOrders / todayRevenue / pendingLab / pendingApproval / patients / smsQueued in 4 small queries."""
    o = select(func.count()).where(
        Order.company_id == company_id,
        alive(Order),
        Order.status != "cancelled",
        Order.created_at >= today_start,
        Order.created_at < today_end,
    )
    p = select(func.coalesce(func.sum(Payment.amount), 0)).where(
        Payment.company_id == company_id,
        alive(Payment),
        Payment.refunded_at.is_(None),
        Payment.created_at >= today_start,
        Payment.created_at < today_end,
    )
    i = select(
        func.coalesce(func.sum(case((OrderItem.status.in_(("pending", "entered")), 1), else_=0)), 0).label("pending_lab"),
        func.coalesce(func.sum(case((OrderItem.status == "submitted", 1), else_=0)), 0).label("pending_approval"),
    ).where(OrderItem.company_id == company_id, alive(OrderItem), OrderItem.status.in_(("pending", "entered", "submitted")))
    patients = select(func.count()).select_from(Patient).where(Patient.company_id == company_id, alive(Patient))
    sms = select(func.count()).select_from(OutboxMessage).where(OutboxMessage.company_id == company_id, alive(OutboxMessage), OutboxMessage.status.in_(("queued", "scheduled")))
    if branches is not None:
        o = o.where(Order.branch_id.in_(branches))
        p = p.where(Payment.branch_id.in_(branches))
        i = i.where(OrderItem.branch_id.in_(branches))
        patients = patients.where(patient_in_branches(branches))  # registered in / visited these branches
        sms = sms.where(OutboxMessage.branch_id.in_(branches))
    misc = select(patients.scalar_subquery().label("patients"), sms.scalar_subquery().label("sms_queued"))
    today_orders = (await session.execute(o)).scalar_one()
    today_revenue = (await session.execute(p)).scalar_one()
    items = (await session.execute(i)).one()
    other = (await session.execute(misc)).one()
    return {
        "today_orders": int(today_orders),
        "today_revenue": int(today_revenue),
        "pending_lab": int(items.pending_lab),
        "pending_approval": int(items.pending_approval),
        "patients": int(other.patients),
        "sms_queued": int(other.sms_queued),
    }


async def order_trend(
    session: AsyncSession, company_id: uuid.UUID, branches: list[uuid.UUID] | None, start: datetime, end: datetime
) -> dict[str, tuple[int, int]]:
    """{local day → (orders, Σ paid_amount)} for non-cancelled orders created in [start, end)."""
    day = _local_day(Order.created_at)
    stmt = (
        select(day.label("day"), func.count().label("orders"), func.coalesce(func.sum(Order.paid_amount), 0).label("revenue"))
        .where(Order.company_id == company_id, alive(Order), Order.status != "cancelled", Order.created_at >= start, Order.created_at < end)
        .group_by(day)
    )
    if branches is not None:
        stmt = stmt.where(Order.branch_id.in_(branches))
    rows = (await session.execute(stmt)).all()
    return {r.day.isoformat(): (int(r.orders), int(r.revenue)) for r in rows}


async def items_by_category(
    session: AsyncSession, company_id: uuid.UUID, branches: list[uuid.UUID] | None, start: datetime, end: datetime
) -> list[Row[Any]]:
    """(category_id, name, count, revenue=Σ final_price) for non-cancelled items in range."""
    stmt = (
        select(
            OrderItem.category_id,
            func.max(OrderItem.category_name).label("name"),
            func.count().label("count"),
            func.coalesce(func.sum(OrderItem.final_price), 0).label("revenue"),
        )
        .where(OrderItem.company_id == company_id, alive(OrderItem), OrderItem.status != "cancelled", OrderItem.created_at >= start, OrderItem.created_at < end)
        .group_by(OrderItem.category_id)
    )
    if branches is not None:
        stmt = stmt.where(OrderItem.branch_id.in_(branches))
    return list((await session.execute(stmt)).all())


async def categories(session: AsyncSession, company_id: uuid.UUID) -> list[Category]:
    """The company's (small) category tree — needed to roll items up to top-level categories."""
    return list((await session.execute(select(Category).where(Category.company_id == company_id, alive(Category)))).scalars().all())


async def breakdown(
    session: AsyncSession, company_id: uuid.UUID, by: str, branches: list[uuid.UUID] | None, start: datetime, end: datetime
) -> list[Row[Any]]:
    """(name, count, revenue) grouped by category / service / branch / technician, revenue desc."""
    if by == "branch":
        key = func.coalesce(Branch.name, literal("—"))
    elif by == "employee":
        key = func.coalesce(OrderItem.technician_name, literal("—"))
    elif by == "service":
        key = OrderItem.service_name
    else:
        key = OrderItem.category_name
    revenue = func.coalesce(func.sum(OrderItem.final_price), 0)
    stmt = (
        select(key.label("name"), func.count().label("count"), revenue.label("revenue"))
        .where(OrderItem.company_id == company_id, alive(OrderItem), OrderItem.status != "cancelled", OrderItem.created_at >= start, OrderItem.created_at < end)
        .group_by(key)
        .order_by(revenue.desc(), key)
    )
    if by == "branch":
        stmt = stmt.outerjoin(Branch, Branch.id == OrderItem.branch_id)
    if branches is not None:
        stmt = stmt.where(OrderItem.branch_id.in_(branches))
    return list((await session.execute(stmt)).all())


# ----------------------------------------------------------------------------- patients / results / services

NOT_READY = ("pending", "entered", "submitted")
IN_LAB = ("pending", "entered")


def period_orders(company_id: uuid.UUID, branches: list[uuid.UUID] | None, start: datetime, end: datetime) -> list[ColumnElement[bool]]:
    """The period's non-cancelled cheques (inside the caller's branches)."""
    conds = [Order.company_id == company_id, alive(Order), Order.status != "cancelled", Order.created_at >= start, Order.created_at < end]
    if branches is not None:
        conds.append(Order.branch_id.in_(branches))
    return conds


def _first_orders(company_id: uuid.UUID, branches: list[uuid.UUID] | None, patient_ids: Select) -> Any:
    """patient → first ever non-cancelled cheque (in the same branches) — "new" patients start in the period."""
    stmt = select(Order.patient_id.label("pid"), func.min(Order.created_at).label("first_at")).where(
        Order.company_id == company_id, alive(Order), Order.status != "cancelled", Order.patient_id.in_(patient_ids)
    )
    if branches is not None:
        stmt = stmt.where(Order.branch_id.in_(branches))
    return stmt.group_by(Order.patient_id).subquery()


def _due(item_created: Any) -> Any:
    """When an item should be ready: cheque time + the service's turnaround days (1 when unknown)."""
    return item_created + func.make_interval(0, 0, 0, func.coalesce(ServiceType.turnaround_days, 1), type_=Interval)


def _hours(later: Any, earlier: Any) -> Any:
    return extract("epoch", later - earlier) / 3600.0


async def patient_summary(session: AsyncSession, company_id: uuid.UUID, branches: list[uuid.UUID] | None, start: datetime, end: datetime) -> Row[Any]:
    per = (
        select(
            Order.patient_id.label("pid"),
            func.count().label("orders"),
            func.coalesce(func.sum(Order.total), 0).label("total"),
            func.coalesce(func.sum(func.greatest(Order.total - Order.paid_amount, 0)), 0).label("debt"),
        )
        .where(*period_orders(company_id, branches, start, end))
        .group_by(Order.patient_id)
        .subquery()
    )
    first = _first_orders(company_id, branches, select(per.c.pid))
    stmt = (
        select(
            func.count().label("patients"),
            func.coalesce(func.sum(case((first.c.first_at >= start, 1), else_=0)), 0).label("new"),
            func.coalesce(func.sum(per.c.orders), 0).label("orders"),
            func.coalesce(func.sum(per.c.total), 0).label("total"),
            func.coalesce(func.sum(case((per.c.debt > 0, 1), else_=0)), 0).label("debtors"),
            func.coalesce(func.sum(per.c.debt), 0).label("debt"),
            func.coalesce(func.sum(case((Patient.telegram_chat_id.is_not(None), 1), else_=0)), 0).label("telegram"),
            func.coalesce(func.sum(case((Patient.portal_linked.is_(True), 1), else_=0)), 0).label("portal"),
        )
        .select_from(per)
        .join(first, first.c.pid == per.c.pid)
        .outerjoin(Patient, Patient.id == per.c.pid)
    )
    return (await session.execute(stmt)).one()


def _period_patient_ids(company_id: uuid.UUID, branches: list[uuid.UUID] | None, start: datetime, end: datetime) -> Select:
    return select(Order.patient_id).where(*period_orders(company_id, branches, start, end)).distinct()


async def patient_demographics(session: AsyncSession, company_id: uuid.UUID, branches: list[uuid.UUID] | None, start: datetime, end: datetime) -> list[Row[Any]]:
    """(gender, age in whole years | None, count) of the period's patients — bucketed by the service."""
    age = func.date_part(literal_column("'year'"), func.age(Patient.birth_date))  # a literal: SELECT and GROUP BY must match
    stmt = (
        select(Patient.gender.label("gender"), age.label("age"), func.count().label("count"))
        .where(Patient.company_id == company_id, Patient.id.in_(_period_patient_ids(company_id, branches, start, end)))
        .group_by(Patient.gender, age)
    )
    return list((await session.execute(stmt)).all())


async def patient_districts(session: AsyncSession, company_id: uuid.UUID, branches: list[uuid.UUID] | None, start: datetime, end: datetime, limit: int = 12) -> list[Row[Any]]:
    """(district name | None, count) of the period's patients, most first."""
    n = func.count()
    stmt = (
        select(District.name.label("name"), n.label("count"))
        .select_from(Patient)
        .outerjoin(District, District.id == Patient.district_id)
        .where(Patient.company_id == company_id, Patient.id.in_(_period_patient_ids(company_id, branches, start, end)))
        .group_by(District.name)
        .order_by(n.desc(), District.name)
        .limit(limit)
    )
    return list((await session.execute(stmt)).all())


async def patient_trend(session: AsyncSession, company_id: uuid.UUID, branches: list[uuid.UUID] | None, start: datetime, end: datetime) -> list[Row[Any]]:
    """(local day, distinct patients, new patients) — a patient is new on the day of their first cheque."""
    day = _local_day(Order.created_at)
    visits = select(day.label("day"), Order.patient_id.label("pid")).where(*period_orders(company_id, branches, start, end)).distinct().subquery()
    first = _first_orders(company_id, branches, select(visits.c.pid))
    stmt = (
        select(
            visits.c.day,
            func.count().label("patients"),
            func.coalesce(func.sum(case((_local_day(first.c.first_at) == visits.c.day, 1), else_=0)), 0).label("new"),
        )
        .select_from(visits)
        .join(first, first.c.pid == visits.c.pid)
        .group_by(visits.c.day)
    )
    return list((await session.execute(stmt)).all())


async def top_patients(session: AsyncSession, company_id: uuid.UUID, branches: list[uuid.UUID] | None, start: datetime, end: datetime, limit: int = 10) -> list[Row[Any]]:
    """Most frequent patients of the period: (pid, name, phone, orders, paid, last visit)."""
    per = (
        select(
            Order.patient_id.label("pid"),
            func.max(Order.patient_name).label("name"),
            func.max(Order.patient_phone).label("phone"),
            func.count().label("orders"),
            func.coalesce(func.sum(Order.paid_amount), 0).label("paid"),
            func.max(Order.created_at).label("last"),
        )
        .where(*period_orders(company_id, branches, start, end))
        .group_by(Order.patient_id)
        .subquery()
    )
    stmt = (
        select(per.c.pid, func.coalesce(Patient.full_name, per.c.name).label("name"), func.coalesce(Patient.phone, per.c.phone).label("phone"), per.c.orders, per.c.paid, per.c.last)
        .select_from(per)
        .outerjoin(Patient, Patient.id == per.c.pid)
        .order_by(per.c.orders.desc(), per.c.paid.desc(), per.c.last.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).all())


# --- results


def telegram_sent() -> ColumnElement[bool]:
    return ResultDocument.deliveries.contains([{"channel": "telegram", "status": "sent"}])


def sms_sent() -> ColumnElement[bool]:
    return or_(ResultDocument.deliveries.contains([{"channel": "sms", "status": "sent"}]), ResultDocument.deliveries.contains([{"channel": "sms", "status": "delivered"}]))


def received() -> ColumnElement[bool]:
    """The patient got the result: opened it (link / portal), it was printed for them, or Telegram sent the PDF."""
    return or_(ResultDocument.viewed_at.is_not(None), ResultDocument.printed_at.is_not(None), telegram_sent())


async def item_readiness(session: AsyncSession, company_id: uuid.UUID, branches: list[uuid.UUID] | None, start: datetime, end: datetime, now: datetime) -> Row[Any]:
    approved = OrderItem.status == "approved"
    stmt = (
        select(
            func.count().label("items"),
            func.coalesce(func.sum(case((OrderItem.status.in_(IN_LAB), 1), else_=0)), 0).label("waiting"),
            func.coalesce(func.sum(case((OrderItem.status == "submitted", 1), else_=0)), 0).label("submitted"),
            func.coalesce(func.sum(case((approved, 1), else_=0)), 0).label("ready"),
            func.coalesce(func.sum(case((and_(OrderItem.status.in_(NOT_READY), _due(Order.created_at) < now), 1), else_=0)), 0).label("overdue"),
            func.count(distinct(case((OrderItem.status.in_(NOT_READY), Order.patient_id)))).label("patients_waiting"),
            func.avg(case((and_(approved, OrderItem.approved_at.is_not(None)), _hours(OrderItem.approved_at, Order.created_at)))).label("avg_hours"),
            func.coalesce(func.sum(case((and_(approved, OrderItem.approved_at <= _due(Order.created_at)), 1), else_=0)), 0).label("on_time"),
        )
        .select_from(OrderItem)
        .join(Order, Order.id == OrderItem.order_id)
        .outerjoin(ServiceType, ServiceType.id == OrderItem.service_type_id)
        .where(OrderItem.company_id == company_id, alive(OrderItem), OrderItem.status != "cancelled", *period_orders(company_id, branches, start, end))
    )
    return (await session.execute(stmt)).one()


async def turnaround_by_category(session: AsyncSession, company_id: uuid.UUID, branches: list[uuid.UUID] | None, start: datetime, end: datetime) -> list[Row[Any]]:
    n = func.count()
    stmt = (
        select(
            OrderItem.category_name.label("name"),
            n.label("approved"),
            func.avg(_hours(OrderItem.approved_at, Order.created_at)).label("avg_hours"),
            func.coalesce(func.sum(case((OrderItem.approved_at <= _due(Order.created_at), 1), else_=0)), 0).label("on_time"),
        )
        .select_from(OrderItem)
        .join(Order, Order.id == OrderItem.order_id)
        .outerjoin(ServiceType, ServiceType.id == OrderItem.service_type_id)
        .where(OrderItem.company_id == company_id, alive(OrderItem), OrderItem.status == "approved", OrderItem.approved_at.is_not(None), *period_orders(company_id, branches, start, end))
        .group_by(OrderItem.category_name)
        .order_by(n.desc(), OrderItem.category_name)
    )
    return list((await session.execute(stmt)).all())


async def document_receipt(session: AsyncSession, company_id: uuid.UUID, branches: list[uuid.UUID] | None, start: datetime, end: datetime) -> Row[Any]:
    got = received()
    stmt = (
        select(
            func.count().label("documents"),
            func.coalesce(func.sum(case((got, 1), else_=0)), 0).label("received"),
            func.count(distinct(case((got, ResultDocument.patient_id)))).label("received_patients"),
            func.count(distinct(case((not_(got), ResultDocument.patient_id)))).label("not_received_patients"),
            func.coalesce(func.sum(case((ResultDocument.viewed_at.is_not(None), 1), else_=0)), 0).label("viewed"),
            func.coalesce(func.sum(case((ResultDocument.printed_at.is_not(None), 1), else_=0)), 0).label("printed"),
            func.coalesce(func.sum(case((telegram_sent(), 1), else_=0)), 0).label("telegram"),
            func.coalesce(func.sum(case((sms_sent(), 1), else_=0)), 0).label("sms_sent"),
        )
        .select_from(ResultDocument)
        .join(Order, Order.id == ResultDocument.order_id)
        .where(ResultDocument.company_id == company_id, alive(ResultDocument), *period_orders(company_id, branches, start, end))
    )
    return (await session.execute(stmt)).one()


async def result_documents_page(
    session: AsyncSession, company_id: uuid.UUID, branches: list[uuid.UUID] | None, start: datetime, end: datetime, *, got: bool, search: str | None, page: int, page_size: int
) -> tuple[list[Row[Any]], int]:
    """Ready results of the period's cheques that the patient did (not) get — oldest waiting first / newest first."""
    stmt = (
        select(
            ResultDocument.id.label("document_id"),
            ResultDocument.title,
            ResultDocument.created_at.label("ready_at"),
            ResultDocument.viewed_at,
            ResultDocument.view_count,
            ResultDocument.printed_at,
            ResultDocument.print_count,
            ResultDocument.deliveries,
            Order.id.label("order_id"),
            Order.number,
            Order.patient_id,
            Order.patient_name,
            Order.patient_phone,
            Order.created_at.label("ordered_at"),
        )
        .select_from(ResultDocument)
        .join(Order, Order.id == ResultDocument.order_id)
        .where(ResultDocument.company_id == company_id, alive(ResultDocument), *period_orders(company_id, branches, start, end), received() if got else not_(received()))
    )
    pred = order_search(search)
    if pred is not None:
        stmt = stmt.where(pred)
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    order = (ResultDocument.created_at.desc(), ResultDocument.id) if got else (ResultDocument.created_at.asc(), ResultDocument.id)
    rows = (await session.execute(stmt.order_by(*order).offset((page - 1) * page_size).limit(page_size))).all()
    return list(rows), int(total)


async def waiting_orders_page(
    session: AsyncSession, company_id: uuid.UUID, branches: list[uuid.UUID] | None, start: datetime, end: datetime, now: datetime, *, search: str | None, page: int, page_size: int
) -> tuple[list[Row[Any]], int]:
    """The period's cheques with results not ready yet (in the lab / waiting for approval), oldest first."""
    stmt = (
        select(
            Order.id.label("order_id"),
            Order.number,
            Order.patient_id,
            Order.patient_name,
            Order.patient_phone,
            Order.created_at.label("ordered_at"),
            func.string_agg(OrderItem.service_name, cast(literal(", "), String)).label("title"),
            func.coalesce(func.sum(case((OrderItem.status.in_(IN_LAB), 1), else_=0)), 0).label("waiting"),
            func.coalesce(func.sum(case((OrderItem.status == "submitted", 1), else_=0)), 0).label("submitted"),
            func.bool_or(_due(Order.created_at) < now).label("overdue"),
        )
        .select_from(Order)
        .join(OrderItem, and_(OrderItem.order_id == Order.id, alive(OrderItem), OrderItem.status.in_(NOT_READY)))
        .outerjoin(ServiceType, ServiceType.id == OrderItem.service_type_id)
        .where(*period_orders(company_id, branches, start, end))
        .group_by(Order.id)
    )
    pred = order_search(search)
    if pred is not None:
        stmt = stmt.where(pred)
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await session.execute(stmt.order_by(Order.created_at.asc(), Order.id).offset((page - 1) * page_size).limit(page_size))).all()
    return list(rows), int(total)


# --- services


async def service_usage(session: AsyncSession, company_id: uuid.UUID, branches: list[uuid.UUID] | None, start: datetime, end: datetime) -> list[Row[Any]]:
    """Per service in [start, end): items, distinct patients, Σ final price, not ready yet, avg hours to approval."""
    n = func.count()
    stmt = (
        select(
            OrderItem.service_type_id.label("sid"),
            func.max(OrderItem.service_name).label("name"),
            func.max(OrderItem.category_name).label("category"),
            n.label("count"),
            func.count(distinct(Order.patient_id)).label("patients"),
            func.coalesce(func.sum(OrderItem.final_price), 0).label("revenue"),
            func.coalesce(func.sum(case((OrderItem.status.in_(NOT_READY), 1), else_=0)), 0).label("pending"),
            func.avg(case((and_(OrderItem.status == "approved", OrderItem.approved_at.is_not(None)), _hours(OrderItem.approved_at, Order.created_at)))).label("avg_hours"),
        )
        .select_from(OrderItem)
        .join(Order, Order.id == OrderItem.order_id)
        .where(OrderItem.company_id == company_id, alive(OrderItem), OrderItem.status != "cancelled", OrderItem.created_at >= start, OrderItem.created_at < end)
        .group_by(OrderItem.service_type_id)
        .order_by(n.desc())
    )
    if branches is not None:
        stmt = stmt.where(OrderItem.branch_id.in_(branches))
    return list((await session.execute(stmt)).all())


async def service_counts(session: AsyncSession, company_id: uuid.UUID, branches: list[uuid.UUID] | None, start: datetime, end: datetime) -> dict[uuid.UUID, int]:
    stmt = (
        select(OrderItem.service_type_id, func.count())
        .where(OrderItem.company_id == company_id, alive(OrderItem), OrderItem.status != "cancelled", OrderItem.created_at >= start, OrderItem.created_at < end)
        .group_by(OrderItem.service_type_id)
    )
    if branches is not None:
        stmt = stmt.where(OrderItem.branch_id.in_(branches))
    return {sid: int(c) for sid, c in (await session.execute(stmt)).all()}


async def active_services(session: AsyncSession, company_id: uuid.UUID) -> list[Row[Any]]:
    """(id, name, category name) of every active catalog service, in catalog order."""
    stmt = (
        select(ServiceType.id, ServiceType.name, func.coalesce(Category.name, literal("—")).label("category"))
        .outerjoin(Category, Category.id == ServiceType.category_id)
        .where(ServiceType.company_id == company_id, alive(ServiceType), ServiceType.is_active.is_(True))
        .order_by(Category.name, ServiceType.order, ServiceType.name)
    )
    return list((await session.execute(stmt)).all())
