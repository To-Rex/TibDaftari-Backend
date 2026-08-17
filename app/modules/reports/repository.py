"""Reports SQL — pure aggregates over orders / order_items / payments (no per-row loops)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Row, case, cast, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import Date

from app.core.timeutil import DEFAULT_TZ
from app.infrastructure.db.base import alive
from app.infrastructure.db.models import Branch, Category, Order, OrderItem, OutboxMessage, Patient, Payment

_TZ = DEFAULT_TZ.key


def _local_day(col: Any) -> Any:
    """`created_at` (UTC) → local calendar day in the clinic timezone (Asia/Tashkent)."""
    return cast(func.timezone(_TZ, col), Date)


async def dashboard_counters(
    session: AsyncSession, company_id: uuid.UUID, branch_id: uuid.UUID | None, today_start: datetime, today_end: datetime
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
    if branch_id:
        o = o.where(Order.branch_id == branch_id)
        p = p.where(Payment.branch_id == branch_id)
        i = i.where(OrderItem.branch_id == branch_id)
    misc = select(
        select(func.count()).select_from(Patient).where(Patient.company_id == company_id, alive(Patient)).scalar_subquery().label("patients"),
        select(func.count())
        .select_from(OutboxMessage)
        .where(OutboxMessage.company_id == company_id, alive(OutboxMessage), OutboxMessage.status.in_(("queued", "scheduled")))
        .scalar_subquery()
        .label("sms_queued"),
    )
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
    session: AsyncSession, company_id: uuid.UUID, branch_id: uuid.UUID | None, start: datetime, end: datetime
) -> dict[str, tuple[int, int]]:
    """{local day → (orders, Σ paid_amount)} for non-cancelled orders created in [start, end)."""
    day = _local_day(Order.created_at)
    stmt = (
        select(day.label("day"), func.count().label("orders"), func.coalesce(func.sum(Order.paid_amount), 0).label("revenue"))
        .where(Order.company_id == company_id, alive(Order), Order.status != "cancelled", Order.created_at >= start, Order.created_at < end)
        .group_by(day)
    )
    if branch_id:
        stmt = stmt.where(Order.branch_id == branch_id)
    rows = (await session.execute(stmt)).all()
    return {r.day.isoformat(): (int(r.orders), int(r.revenue)) for r in rows}


async def items_by_category(
    session: AsyncSession, company_id: uuid.UUID, branch_id: uuid.UUID | None, start: datetime, end: datetime
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
    if branch_id:
        stmt = stmt.where(OrderItem.branch_id == branch_id)
    return list((await session.execute(stmt)).all())


async def categories(session: AsyncSession, company_id: uuid.UUID) -> list[Category]:
    """The company's (small) category tree — needed to roll items up to top-level categories."""
    return list((await session.execute(select(Category).where(Category.company_id == company_id, alive(Category)))).scalars().all())


async def breakdown(
    session: AsyncSession, company_id: uuid.UUID, by: str, branch_id: uuid.UUID | None, start: datetime, end: datetime
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
    if branch_id:
        stmt = stmt.where(OrderItem.branch_id == branch_id)
    return list((await session.execute(stmt)).all())
