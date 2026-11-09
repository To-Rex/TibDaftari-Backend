"""Reports SQL — pure aggregates over orders / order_items / payments / result_documents (no per-row loops).

Every query is scoped by `unit_ids`: the units whose operational rows the report covers (the selected unit's
subtree, or the `unitId` narrowing — see `service._units`). Catalogue lookups take the `UnitScope` and use its
lineage (ancestors + subtree), because items roll up to categories owned anywhere on the facilities' paths.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
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
    exists,
    extract,
    false,
    func,
    literal,
    literal_column,
    not_,
    or_,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import Date

from app.core.scope import UnitScope
from app.core.textutil import digits, fold
from app.core.timeutil import DEFAULT_TZ
from app.infrastructure.db.base import alive
from app.infrastructure.db.models import (
    Category,
    District,
    Order,
    OrderItem,
    OrgUnit,
    OrgUnitClosure,
    Patient,
    Payment,
    ResultDocument,
    ServiceType,
    ServiceUnitSetting,
)

_TZ = DEFAULT_TZ.key

UnitIds = Sequence[uuid.UUID]


def _local_day(col: Any) -> Any:
    """`created_at` (UTC) → local calendar day in the business timezone (Asia/Tashkent)."""
    return cast(func.timezone(_TZ, col), Date)


def order_search(search: str | None) -> ColumnElement[bool] | None:
    """Cheque search: fold(patientName) contains OR lower(number) contains OR (digits present AND phone contains digits)."""
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


def patient_in_units(unit_ids: UnitIds, country_id: uuid.UUID | None = None) -> ColumnElement[bool]:
    """A patient belongs to a unit when registered there or with a cheque there. Legacy rows registered nowhere and
    never seen (imports) are nobody's and stay visible across their country, as they did across the company before."""
    has_order = exists().where(Order.patient_id == Patient.id, alive(Order), Order.unit_id.in_(unit_ids))
    conds: list[ColumnElement[bool]] = [Patient.registered_unit_id.in_(unit_ids), has_order]
    if country_id is not None:
        conds.append(and_(Patient.registered_unit_id.is_(None), Patient.stats_orders == 0, Patient.country_id == country_id))
    return or_(*conds)


async def dashboard_counters(
    session: AsyncSession, unit_ids: UnitIds, today_start: datetime, today_end: datetime, *, country_id: uuid.UUID | None = None
) -> dict[str, int]:
    """todayOrders / todayRevenue / pendingLab / pendingApproval / patients in 4 small queries
    (smsQueued comes from `messaging.service.count_queued`)."""
    o = select(func.count()).where(
        Order.unit_id.in_(unit_ids),
        alive(Order),
        Order.status != "cancelled",
        Order.created_at >= today_start,
        Order.created_at < today_end,
    )
    p = select(func.coalesce(func.sum(Payment.amount), 0)).where(
        Payment.unit_id.in_(unit_ids),
        alive(Payment),
        Payment.refunded_at.is_(None),
        Payment.created_at >= today_start,
        Payment.created_at < today_end,
    )
    i = select(
        func.coalesce(func.sum(case((OrderItem.status.in_(("pending", "entered")), 1), else_=0)), 0).label("pending_lab"),
        func.coalesce(func.sum(case((OrderItem.status == "submitted", 1), else_=0)), 0).label("pending_approval"),
    ).where(OrderItem.unit_id.in_(unit_ids), alive(OrderItem), OrderItem.status.in_(("pending", "entered", "submitted")))
    patients = select(func.count()).select_from(Patient).where(alive(Patient), patient_in_units(unit_ids, country_id))
    today_orders = (await session.execute(o)).scalar_one()
    today_revenue = (await session.execute(p)).scalar_one()
    items = (await session.execute(i)).one()
    n_patients = (await session.execute(patients)).scalar_one()
    return {
        "today_orders": int(today_orders),
        "today_revenue": int(today_revenue),
        "pending_lab": int(items.pending_lab),
        "pending_approval": int(items.pending_approval),
        "patients": int(n_patients),
    }


async def order_trend(session: AsyncSession, unit_ids: UnitIds, start: datetime, end: datetime) -> dict[str, tuple[int, int]]:
    """{local day → (orders, Σ paid_amount)} for non-cancelled orders created in [start, end)."""
    day = _local_day(Order.created_at)
    stmt = (
        select(day.label("day"), func.count().label("orders"), func.coalesce(func.sum(Order.paid_amount), 0).label("revenue"))
        .where(Order.unit_id.in_(unit_ids), alive(Order), Order.status != "cancelled", Order.created_at >= start, Order.created_at < end)
        .group_by(day)
    )
    rows = (await session.execute(stmt)).all()
    return {r.day.isoformat(): (int(r.orders), int(r.revenue)) for r in rows}


def _period_items(unit_ids: UnitIds, start: datetime, end: datetime) -> list[ColumnElement[bool]]:
    """The period's non-cancelled items inside the units."""
    return [OrderItem.unit_id.in_(unit_ids), alive(OrderItem), OrderItem.status != "cancelled", OrderItem.created_at >= start, OrderItem.created_at < end]


async def items_by_category(session: AsyncSession, unit_ids: UnitIds, start: datetime, end: datetime) -> list[Row[Any]]:
    """(category_id, name, count, revenue=Σ final_price) for non-cancelled items in range."""
    stmt = (
        select(
            OrderItem.category_id,
            func.max(OrderItem.category_name).label("name"),
            func.count().label("count"),
            func.coalesce(func.sum(OrderItem.final_price), 0).label("revenue"),
        )
        .where(*_period_items(unit_ids, start, end))
        .group_by(OrderItem.category_id)
    )
    return list((await session.execute(stmt)).all())


async def categories(session: AsyncSession, scope: UnitScope) -> list[Category]:
    """The (small) category tree the scope's facilities use — needed to roll items up to top-level categories."""
    stmt = select(Category).where(Category.owner_unit_id.in_(scope.lineage_ids), alive(Category))
    return list((await session.execute(stmt)).scalars().all())


async def export_items(session: AsyncSession, unit_ids: UnitIds, start: datetime, end: datetime, limit: int = 20_000) -> list[Row[Any]]:
    """One row per non-cancelled item of the period (patient × analysis × price) for the Excel export.
    `unit_id` is the facility — the caller resolves names in one batch (`units_by_ids`)."""
    stmt = (
        select(
            OrderItem.created_at,
            Order.number.label("order_number"),
            Order.patient_name,
            Order.patient_phone,
            OrderItem.service_name,
            OrderItem.category_id,
            OrderItem.category_name,
            OrderItem.final_price,
            OrderItem.status,
            OrderItem.unit_id,
        )
        .select_from(OrderItem)
        .join(Order, Order.id == OrderItem.order_id)
        .where(*_period_items(unit_ids, start, end))
        .order_by(OrderItem.created_at.asc(), OrderItem.id)
        .limit(limit)
    )
    return list((await session.execute(stmt)).all())


async def breakdown(session: AsyncSession, unit_ids: UnitIds, by: str, start: datetime, end: datetime) -> list[Row[Any]]:
    """(name, count, revenue) grouped by category / service / unit (facility) / technician, revenue desc."""
    count = func.count().label("count")
    revenue = func.coalesce(func.sum(OrderItem.final_price), 0)
    where = _period_items(unit_ids, start, end)
    if by == "unit":
        # grouped by the facility id (two same-named facilities stay apart); the label is the unit's name
        name = func.coalesce(func.max(OrgUnit.name), literal("—"))
        stmt = (
            select(name.label("name"), count, revenue.label("revenue"))
            .select_from(OrderItem)
            .outerjoin(OrgUnit, OrgUnit.id == OrderItem.unit_id)
            .where(*where)
            .group_by(OrderItem.unit_id)
            .order_by(revenue.desc(), name)
        )
        return list((await session.execute(stmt)).all())
    if by == "employee":
        key = func.coalesce(OrderItem.technician_name, literal("—"))
    elif by == "service":
        key = OrderItem.service_name
    else:
        key = OrderItem.category_name
    stmt = select(key.label("name"), count, revenue.label("revenue")).where(*where).group_by(key).order_by(revenue.desc(), key)
    return list((await session.execute(stmt)).all())


# ----------------------------------------------------------------------------- patients / results / services

NOT_READY = ("pending", "entered", "submitted")
IN_LAB = ("pending", "entered")


def period_orders(unit_ids: UnitIds, start: datetime, end: datetime) -> list[ColumnElement[bool]]:
    """The period's non-cancelled cheques (inside the units)."""
    return [Order.unit_id.in_(unit_ids), alive(Order), Order.status != "cancelled", Order.created_at >= start, Order.created_at < end]


def _first_orders(unit_ids: UnitIds, patient_ids: Select) -> Any:
    """patient → first ever non-cancelled cheque (in the same units) — "new" patients start in the period."""
    stmt = (
        select(Order.patient_id.label("pid"), func.min(Order.created_at).label("first_at"))
        .where(Order.unit_id.in_(unit_ids), alive(Order), Order.status != "cancelled", Order.patient_id.in_(patient_ids))
        .group_by(Order.patient_id)
    )
    return stmt.subquery()


def _due(item_created: Any) -> Any:
    """When an item should be ready: cheque time + the service's turnaround days (1 when unknown)."""
    return item_created + func.make_interval(0, 0, 0, func.coalesce(ServiceType.turnaround_days, 1), type_=Interval)


def _hours(later: Any, earlier: Any) -> Any:
    return extract("epoch", later - earlier) / 3600.0


async def patient_summary(session: AsyncSession, unit_ids: UnitIds, start: datetime, end: datetime) -> Row[Any]:
    per = (
        select(
            Order.patient_id.label("pid"),
            func.count().label("orders"),
            func.coalesce(func.sum(Order.total), 0).label("total"),
            func.coalesce(func.sum(func.greatest(Order.total - Order.paid_amount, 0)), 0).label("debt"),
        )
        .where(*period_orders(unit_ids, start, end))
        .group_by(Order.patient_id)
        .subquery()
    )
    first = _first_orders(unit_ids, select(per.c.pid))
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


def _period_patient_ids(unit_ids: UnitIds, start: datetime, end: datetime) -> Select:
    return select(Order.patient_id).where(*period_orders(unit_ids, start, end)).distinct()


async def patient_demographics(session: AsyncSession, unit_ids: UnitIds, start: datetime, end: datetime) -> list[Row[Any]]:
    """(gender, age in whole years | None, count) of the period's patients — bucketed by the service."""
    age = func.date_part(literal_column("'year'"), func.age(Patient.birth_date))  # a literal: SELECT and GROUP BY must match
    stmt = (
        select(Patient.gender.label("gender"), age.label("age"), func.count().label("count"))
        .where(Patient.id.in_(_period_patient_ids(unit_ids, start, end)))
        .group_by(Patient.gender, age)
    )
    return list((await session.execute(stmt)).all())


async def patient_districts(session: AsyncSession, unit_ids: UnitIds, start: datetime, end: datetime, limit: int = 12) -> list[Row[Any]]:
    """(district name | None, count) of the period's patients, most first."""
    n = func.count()
    stmt = (
        select(District.name.label("name"), n.label("count"))
        .select_from(Patient)
        .outerjoin(District, District.id == Patient.district_id)
        .where(Patient.id.in_(_period_patient_ids(unit_ids, start, end)))
        .group_by(District.name)
        .order_by(n.desc(), District.name)
        .limit(limit)
    )
    return list((await session.execute(stmt)).all())


async def patient_trend(session: AsyncSession, unit_ids: UnitIds, start: datetime, end: datetime) -> list[Row[Any]]:
    """(local day, distinct patients, new patients) — a patient is new on the day of their first cheque."""
    day = _local_day(Order.created_at)
    visits = select(day.label("day"), Order.patient_id.label("pid")).where(*period_orders(unit_ids, start, end)).distinct().subquery()
    first = _first_orders(unit_ids, select(visits.c.pid))
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


async def top_patients(session: AsyncSession, unit_ids: UnitIds, start: datetime, end: datetime, limit: int = 10) -> list[Row[Any]]:
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
        .where(*period_orders(unit_ids, start, end))
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


async def item_readiness(session: AsyncSession, unit_ids: UnitIds, start: datetime, end: datetime, now: datetime) -> Row[Any]:
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
        .where(OrderItem.unit_id.in_(unit_ids), alive(OrderItem), OrderItem.status != "cancelled", *period_orders(unit_ids, start, end))
    )
    return (await session.execute(stmt)).one()


async def turnaround_by_category(session: AsyncSession, unit_ids: UnitIds, start: datetime, end: datetime) -> list[Row[Any]]:
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
        .where(OrderItem.unit_id.in_(unit_ids), alive(OrderItem), OrderItem.status == "approved", OrderItem.approved_at.is_not(None), *period_orders(unit_ids, start, end))
        .group_by(OrderItem.category_name)
        .order_by(n.desc(), OrderItem.category_name)
    )
    return list((await session.execute(stmt)).all())


async def document_receipt(session: AsyncSession, unit_ids: UnitIds, start: datetime, end: datetime) -> Row[Any]:
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
        .where(ResultDocument.unit_id.in_(unit_ids), alive(ResultDocument), *period_orders(unit_ids, start, end))
    )
    return (await session.execute(stmt)).one()


async def result_documents_page(
    session: AsyncSession, unit_ids: UnitIds, start: datetime, end: datetime, *, got: bool, search: str | None, page: int, page_size: int
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
        .where(ResultDocument.unit_id.in_(unit_ids), alive(ResultDocument), *period_orders(unit_ids, start, end), received() if got else not_(received()))
    )
    pred = order_search(search)
    if pred is not None:
        stmt = stmt.where(pred)
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    order = (ResultDocument.created_at.desc(), ResultDocument.id) if got else (ResultDocument.created_at.asc(), ResultDocument.id)
    rows = (await session.execute(stmt.order_by(*order).offset((page - 1) * page_size).limit(page_size))).all()
    return list(rows), int(total)


async def waiting_orders_page(
    session: AsyncSession, unit_ids: UnitIds, start: datetime, end: datetime, now: datetime, *, search: str | None, page: int, page_size: int
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
        .where(*period_orders(unit_ids, start, end))
        .group_by(Order.id)
    )
    pred = order_search(search)
    if pred is not None:
        stmt = stmt.where(pred)
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await session.execute(stmt.order_by(Order.created_at.asc(), Order.id).offset((page - 1) * page_size).limit(page_size))).all()
    return list(rows), int(total)


# --- services


async def service_usage(session: AsyncSession, unit_ids: UnitIds, start: datetime, end: datetime) -> list[Row[Any]]:
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
        .where(*_period_items(unit_ids, start, end))
        .group_by(OrderItem.service_type_id)
        .order_by(n.desc())
    )
    return list((await session.execute(stmt)).all())


async def service_counts(session: AsyncSession, unit_ids: UnitIds, start: datetime, end: datetime) -> dict[uuid.UUID, int]:
    stmt = select(OrderItem.service_type_id, func.count()).where(*_period_items(unit_ids, start, end)).group_by(OrderItem.service_type_id)
    return {sid: int(c) for sid, c in (await session.execute(stmt)).all()}


async def active_services(session: AsyncSession, scope: UnitScope) -> list[Row[Any]]:
    """(id, name, category name, owner_unit_id) of every active catalogue service the scope's facilities may use
    (owned on their paths or inside the subtree), in catalogue order."""
    stmt = (
        select(ServiceType.id, ServiceType.name, func.coalesce(Category.name, literal("—")).label("category"), ServiceType.owner_unit_id)
        .outerjoin(Category, Category.id == ServiceType.category_id)
        .where(ServiceType.owner_unit_id.in_(scope.lineage_ids), alive(ServiceType), ServiceType.is_active.is_(True))
        .order_by(Category.name, ServiceType.sort_order, ServiceType.name)
    )
    return list((await session.execute(stmt)).all())


async def facility_paths(session: AsyncSession, facility_ids: UnitIds) -> dict[uuid.UUID, list[uuid.UUID]]:
    """facility → its ancestors root first, itself last: the units whose catalogue and service settings apply to it."""
    if not facility_ids:
        return {}
    stmt = (
        select(OrgUnitClosure.descendant_id, OrgUnitClosure.ancestor_id)
        .where(OrgUnitClosure.descendant_id.in_(facility_ids))
        .order_by(OrgUnitClosure.descendant_id, OrgUnitClosure.depth.desc())
    )
    out: dict[uuid.UUID, list[uuid.UUID]] = {}
    for did, aid in (await session.execute(stmt)).all():
        out.setdefault(did, []).append(aid)
    return out


async def service_settings(session: AsyncSession, service_ids: UnitIds) -> list[Row[Any]]:
    """(service_type_id, unit_id, is_enabled) per-unit overrides of the given services, any level — the caller picks the nearest."""
    if not service_ids:
        return []
    stmt = select(ServiceUnitSetting.service_type_id, ServiceUnitSetting.unit_id, ServiceUnitSetting.is_enabled).where(ServiceUnitSetting.service_type_id.in_(service_ids))
    return list((await session.execute(stmt)).all())
