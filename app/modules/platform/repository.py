"""Platform SQL: activity of every clinic folded by the location of its branches. No rules, no HTTP.

Everything is keyed by the branch: a cheque, payment, patient, result or SMS belongs to the territory of the branch it
was made at (`branch.country_id / region_id / district_id`), so a clinic with branches in two districts shows up in
both. `key` is the grouping column of the requested level; rows whose branch has no value there come back under
`None` (the "unassigned" bucket)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Date, Select, Subquery, case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timeutil import DEFAULT_TZ
from app.infrastructure.db.base import alive
from app.infrastructure.db.models import (
    Branch,
    Company,
    Country,
    District,
    Order,
    OrderItem,
    OutboxMessage,
    Patient,
    Payment,
    Region,
)

_TZ = DEFAULT_TZ.key

KEY_COLUMNS = {"country": Branch.country_id, "region": Branch.region_id, "district": Branch.district_id, "company": Branch.company_id}


def scoped_branches(group_by: str, country_id: uuid.UUID | None, region_id: uuid.UUID | None, district_id: uuid.UUID | None) -> Subquery:
    """Every branch (deleted ones too — their cheques still count) of a live company inside the territory, with the
    grouping key of `group_by`."""
    stmt = (
        select(Branch.id.label("branch_id"), Branch.company_id.label("company_id"), Branch.deleted_at.label("deleted_at"), KEY_COLUMNS[group_by].label("key"))
        .join(Company, Company.id == Branch.company_id)
        .where(alive(Company))
    )
    if country_id:
        stmt = stmt.where(Branch.country_id == country_id)
    if region_id:
        stmt = stmt.where(Branch.region_id == region_id)
    if district_id:
        stmt = stmt.where(Branch.district_id == district_id)
    return stmt.subquery("b")


async def _by_key(session: AsyncSession, stmt: Select) -> dict[uuid.UUID | None, int]:
    return {key: int(value) for key, value in (await session.execute(stmt)).all()}


async def structure(session: AsyncSession, b: Subquery) -> tuple[dict[uuid.UUID | None, int], dict[uuid.UUID | None, int]]:
    """key → live branches, key → distinct clinics with a live branch."""
    stmt = select(b.c.key, func.count(), func.count(func.distinct(b.c.company_id))).where(b.c.deleted_at.is_(None)).group_by(b.c.key)
    branches: dict[uuid.UUID | None, int] = {}
    companies: dict[uuid.UUID | None, int] = {}
    for key, n_branches, n_companies in (await session.execute(stmt)).all():
        branches[key] = int(n_branches)
        companies[key] = int(n_companies)
    return branches, companies


async def orders(session: AsyncSession, b: Subquery, start: datetime, end: datetime) -> dict[uuid.UUID | None, int]:
    stmt = (
        select(b.c.key, func.count())
        .select_from(Order)
        .join(b, b.c.branch_id == Order.branch_id)
        .where(alive(Order), Order.status != "cancelled", Order.created_at >= start, Order.created_at < end)
        .group_by(b.c.key)
    )
    return await _by_key(session, stmt)


async def revenue(session: AsyncSession, b: Subquery, start: datetime, end: datetime) -> dict[uuid.UUID | None, int]:
    stmt = (
        select(b.c.key, func.coalesce(func.sum(Payment.amount), 0))
        .select_from(Payment)
        .join(b, b.c.branch_id == Payment.branch_id)
        .where(alive(Payment), Payment.refunded_at.is_(None), Payment.created_at >= start, Payment.created_at < end)
        .group_by(b.c.key)
    )
    return await _by_key(session, stmt)


async def patients_new(session: AsyncSession, b: Subquery, start: datetime, end: datetime) -> dict[uuid.UUID | None, int]:
    stmt = (
        select(b.c.key, func.count())
        .select_from(Patient)
        .join(b, b.c.branch_id == Patient.branch_id)
        .where(alive(Patient), Patient.created_at >= start, Patient.created_at < end)
        .group_by(b.c.key)
    )
    return await _by_key(session, stmt)


async def results_ready(session: AsyncSession, b: Subquery, start: datetime, end: datetime) -> dict[uuid.UUID | None, int]:
    stmt = (
        select(b.c.key, func.count())
        .select_from(OrderItem)
        .join(b, b.c.branch_id == OrderItem.branch_id)
        .where(alive(OrderItem), OrderItem.status == "approved", OrderItem.approved_at >= start, OrderItem.approved_at < end)
        .group_by(b.c.key)
    )
    return await _by_key(session, stmt)


async def sms_sent(session: AsyncSession, b: Subquery, start: datetime, end: datetime) -> dict[uuid.UUID | None, int]:
    stmt = (
        select(b.c.key, func.count())
        .select_from(OutboxMessage)
        .join(b, b.c.branch_id == OutboxMessage.branch_id)
        .where(alive(OutboxMessage), OutboxMessage.channel == "sms", OutboxMessage.status.in_(("sent", "delivered")), OutboxMessage.sent_at >= start, OutboxMessage.sent_at < end)
        .group_by(b.c.key)
    )
    return await _by_key(session, stmt)


async def distinct_companies(session: AsyncSession, b: Subquery) -> int:
    """Clinics with a live branch anywhere in the scope, each counted once."""
    return int((await session.execute(select(func.count(func.distinct(b.c.company_id))).where(b.c.deleted_at.is_(None)))).scalar_one())


def _local_day(col: Any) -> Any:
    return cast(func.timezone(_TZ, col), Date)


async def trend(session: AsyncSession, b: Subquery, start: datetime, end: datetime) -> dict[str, tuple[int, int]]:
    """{local day → (cheques opened, money taken)} over the whole scope."""
    o_day = _local_day(Order.created_at)
    o = (
        select(o_day.label("day"), func.count().label("n"))
        .select_from(Order)
        .join(b, b.c.branch_id == Order.branch_id)
        .where(alive(Order), Order.status != "cancelled", Order.created_at >= start, Order.created_at < end)
        .group_by(o_day)
    )
    p_day = _local_day(Payment.created_at)
    p = (
        select(p_day.label("day"), func.coalesce(func.sum(Payment.amount), 0).label("n"))
        .select_from(Payment)
        .join(b, b.c.branch_id == Payment.branch_id)
        .where(alive(Payment), Payment.refunded_at.is_(None), Payment.created_at >= start, Payment.created_at < end)
        .group_by(p_day)
    )
    out: dict[str, tuple[int, int]] = {}
    for r in (await session.execute(o)).all():
        out[r.day.isoformat()] = (int(r.n), 0)
    for r in (await session.execute(p)).all():
        day = r.day.isoformat()
        out[day] = (out.get(day, (0, 0))[0], int(r.n))
    return out


async def names(session: AsyncSession, group_by: str, ids: set[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
    """id → {name, parentName, slug?, isActive?} for the rows of a level."""
    if not ids:
        return {}
    out: dict[uuid.UUID, dict[str, Any]] = {}
    if group_by == "country":
        for row in (await session.execute(select(Country.id, Country.name).where(Country.id.in_(ids)))).all():
            out[row.id] = {"name": row.name, "parent_name": None}
    elif group_by == "region":
        stmt = select(Region.id, Region.name, Country.name.label("parent")).join(Country, Country.id == Region.country_id).where(Region.id.in_(ids))
        for row in (await session.execute(stmt)).all():
            out[row.id] = {"name": row.name, "parent_name": row.parent}
    elif group_by == "district":
        stmt = select(District.id, District.name, Region.name.label("parent")).join(Region, Region.id == District.region_id).where(District.id.in_(ids))
        for row in (await session.execute(stmt)).all():
            out[row.id] = {"name": row.name, "parent_name": row.parent}
    else:
        reg = select(Region.id, Region.name).subquery()
        dis = select(District.id, District.name).subquery()
        stmt = (
            select(Company.id, Company.name, Company.slug, Company.is_active, reg.c.name.label("region"), dis.c.name.label("district"))
            .outerjoin(reg, reg.c.id == Company.region_id)
            .outerjoin(dis, dis.c.id == Company.district_id)
            .where(Company.id.in_(ids))
        )
        for row in (await session.execute(stmt)).all():
            out[row.id] = {"name": row.name, "parent_name": ", ".join(x for x in (row.region, row.district) if x) or None, "slug": row.slug, "is_active": row.is_active}
    return out


async def coverage(session: AsyncSession) -> dict[str, int]:
    """Live clinics / branches, and how many of them carry a district (the clinic: its own, or any live branch's)."""
    located_branch = select(Branch.id).where(Branch.company_id == Company.id, alive(Branch), Branch.district_id.is_not(None)).correlate(Company).exists()
    c = select(func.count(), func.coalesce(func.sum(case(((Company.district_id.is_not(None)) | located_branch, 1), else_=0)), 0)).where(alive(Company))
    b = select(func.count(), func.coalesce(func.sum(case((Branch.district_id.is_not(None), 1), else_=0)), 0)).select_from(Branch).join(Company, Company.id == Branch.company_id).where(alive(Branch), alive(Company))
    companies, companies_located = (await session.execute(c)).one()
    branches, branches_located = (await session.execute(b)).one()
    return {"companies": int(companies), "companies_located": int(companies_located), "branches": int(branches), "branches_located": int(branches_located)}
