"""Platform SQL: activity of every facility folded up the organisation tree. No rules, no HTTP.

Everything is keyed by the facility: a cheque, payment, result document or outbox row carries `unit_id`, a patient
`registered_unit_id`. `org_units` carries the geo columns down to the facility, so a territory row is a plain
GROUP BY on the facility's `country_id` / `region_id` / `district_id` (or its own `id`). Deleted facilities keep
counting — their cheques still happened — but only alive ones are counted as `units`."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Date, Select, Subquery, and_, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timeutil import DEFAULT_TZ
from app.infrastructure.db.base import alive
from app.infrastructure.db.models import (
    Country,
    District,
    Order,
    OrgUnit,
    OrgUnitClosure,
    OutboxMessage,
    Patient,
    Payment,
    Region,
    ResultDocument,
)

_TZ = DEFAULT_TZ.key

KEY_COLUMNS = {"country": OrgUnit.country_id, "region": OrgUnit.region_id, "district": OrgUnit.district_id, "unit": OrgUnit.id}


def scoped_facilities(group_by: str, country_id: uuid.UUID | None, region_id: uuid.UUID | None, district_id: uuid.UUID | None, root_id: uuid.UUID | None = None) -> Subquery:
    """Every facility (deleted ones too — their cheques still count) inside the territory and, with `root_id`,
    under that unit of the tree, with the grouping key of `group_by` and its local timezone."""
    stmt = select(
        OrgUnit.id.label("unit_id"),
        OrgUnit.deleted_at.label("deleted_at"),
        func.coalesce(OrgUnit.timezone, _TZ).label("tz"),
        KEY_COLUMNS[group_by].label("key"),
    ).where(OrgUnit.level == "facility")
    if root_id:
        stmt = stmt.join(OrgUnitClosure, OrgUnitClosure.descendant_id == OrgUnit.id).where(OrgUnitClosure.ancestor_id == root_id)
    if country_id:
        stmt = stmt.where(OrgUnit.country_id == country_id)
    if region_id:
        stmt = stmt.where(OrgUnit.region_id == region_id)
    if district_id:
        stmt = stmt.where(OrgUnit.district_id == district_id)
    return stmt.subquery("u")


async def _by_key(session: AsyncSession, stmt: Select) -> dict[uuid.UUID | None, int]:
    return {key: int(value) for key, value in (await session.execute(stmt)).all()}


async def structure(session: AsyncSession, u: Subquery) -> dict[uuid.UUID | None, int]:
    """key → alive facilities."""
    stmt = select(u.c.key, func.count()).where(u.c.deleted_at.is_(None)).group_by(u.c.key)
    return await _by_key(session, stmt)


async def orders(session: AsyncSession, u: Subquery, start: datetime, end: datetime) -> dict[uuid.UUID | None, int]:
    stmt = (
        select(u.c.key, func.count())
        .select_from(Order)
        .join(u, u.c.unit_id == Order.unit_id)
        .where(alive(Order), Order.status != "cancelled", Order.created_at >= start, Order.created_at < end)
        .group_by(u.c.key)
    )
    return await _by_key(session, stmt)


async def revenue(session: AsyncSession, u: Subquery, start: datetime, end: datetime) -> dict[uuid.UUID | None, int]:
    stmt = (
        select(u.c.key, func.coalesce(func.sum(Payment.amount), 0))
        .select_from(Payment)
        .join(u, u.c.unit_id == Payment.unit_id)
        .where(alive(Payment), Payment.refunded_at.is_(None), Payment.created_at >= start, Payment.created_at < end)
        .group_by(u.c.key)
    )
    return await _by_key(session, stmt)


async def patients_new(session: AsyncSession, u: Subquery, start: datetime, end: datetime) -> dict[uuid.UUID | None, int]:
    stmt = (
        select(u.c.key, func.count())
        .select_from(Patient)
        .join(u, u.c.unit_id == Patient.registered_unit_id)
        .where(alive(Patient), Patient.created_at >= start, Patient.created_at < end)
        .group_by(u.c.key)
    )
    return await _by_key(session, stmt)


async def results_ready(session: AsyncSession, u: Subquery, start: datetime, end: datetime) -> dict[uuid.UUID | None, int]:
    stmt = (
        select(u.c.key, func.count())
        .select_from(ResultDocument)
        .join(u, u.c.unit_id == ResultDocument.unit_id)
        .where(alive(ResultDocument), ResultDocument.status == "final", ResultDocument.created_at >= start, ResultDocument.created_at < end)
        .group_by(u.c.key)
    )
    return await _by_key(session, stmt)


async def sms_sent(session: AsyncSession, u: Subquery, start: datetime, end: datetime) -> dict[uuid.UUID | None, int]:
    stmt = (
        select(u.c.key, func.count())
        .select_from(OutboxMessage)
        .join(u, u.c.unit_id == OutboxMessage.unit_id)
        .where(alive(OutboxMessage), OutboxMessage.channel == "sms", OutboxMessage.status.in_(("sent", "delivered")), OutboxMessage.sent_at >= start, OutboxMessage.sent_at < end)
        .group_by(u.c.key)
    )
    return await _by_key(session, stmt)


def _local_day(col: Any, tz: Any) -> Any:
    """Calendar day of a timestamp in the facility's own timezone."""
    return cast(func.timezone(tz, col), Date)


async def trend(session: AsyncSession, u: Subquery, start: datetime, end: datetime) -> dict[str, tuple[int, int]]:
    """{local day → (cheques opened, money taken)} over the whole scope."""
    o_day = _local_day(Order.created_at, u.c.tz)
    o = (
        select(o_day.label("day"), func.count().label("n"))
        .select_from(Order)
        .join(u, u.c.unit_id == Order.unit_id)
        .where(alive(Order), Order.status != "cancelled", Order.created_at >= start, Order.created_at < end)
        .group_by(o_day)
    )
    p_day = _local_day(Payment.created_at, u.c.tz)
    p = (
        select(p_day.label("day"), func.coalesce(func.sum(Payment.amount), 0).label("n"))
        .select_from(Payment)
        .join(u, u.c.unit_id == Payment.unit_id)
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
    """id → {name, parent_name, unit_id, code?, facility_kind?, is_active?} for the rows of a level. `unit_id` is
    the alive organisation-tree node of the territory (there is at most one per country / region / district)."""
    if not ids:
        return {}
    out: dict[uuid.UUID, dict[str, Any]] = {}
    node = OrgUnit.__table__.alias("node")
    if group_by == "country":
        stmt = (
            select(Country.id, Country.name, node.c.id.label("node_id"))
            .outerjoin(node, and_(node.c.country_id == Country.id, node.c.level == "country", node.c.deleted_at.is_(None)))
            .where(Country.id.in_(ids))
        )
        for row in (await session.execute(stmt)).all():
            out[row.id] = {"name": row.name, "parent_name": None, "unit_id": row.node_id}
    elif group_by == "region":
        stmt = (
            select(Region.id, Region.name, Country.name.label("parent"), node.c.id.label("node_id"))
            .join(Country, Country.id == Region.country_id)
            .outerjoin(node, and_(node.c.region_id == Region.id, node.c.level == "region", node.c.deleted_at.is_(None)))
            .where(Region.id.in_(ids))
        )
        for row in (await session.execute(stmt)).all():
            out[row.id] = {"name": row.name, "parent_name": row.parent, "unit_id": row.node_id}
    elif group_by == "district":
        stmt = (
            select(District.id, District.name, Region.name.label("parent"), node.c.id.label("node_id"))
            .join(Region, Region.id == District.region_id)
            .outerjoin(node, and_(node.c.district_id == District.id, node.c.level == "district", node.c.deleted_at.is_(None)))
            .where(District.id.in_(ids))
        )
        for row in (await session.execute(stmt)).all():
            out[row.id] = {"name": row.name, "parent_name": row.parent, "unit_id": row.node_id}
    else:
        stmt = (
            select(OrgUnit.id, OrgUnit.name, OrgUnit.code, OrgUnit.kind, OrgUnit.is_active, Region.name.label("region"), District.name.label("district"))
            .outerjoin(Region, Region.id == OrgUnit.region_id)
            .outerjoin(District, District.id == OrgUnit.district_id)
            .where(OrgUnit.id.in_(ids))
        )
        for row in (await session.execute(stmt)).all():
            out[row.id] = {
                "name": row.name,
                "parent_name": ", ".join(x for x in (row.region, row.district) if x) or None,
                "unit_id": row.id,
                "code": row.code,
                "facility_kind": row.kind,
                "is_active": row.is_active,
            }
    return out
