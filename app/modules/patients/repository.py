"""Patients SQL: country-scoped selects, unit membership predicate, search predicate (fold_text + trigram
indexes), identity lookups, geo reference lists. No rules, no HTTP."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, Select, String, and_, exists, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.textutil import digits, fold
from app.infrastructure.db.base import alive
from app.infrastructure.db.models import Country, District, Order, Patient, Region

SORTABLE: dict[str, Any] = {
    "createdAt": Patient.created_at,
    "updatedAt": Patient.updated_at,
    "fullName": Patient.full_name,
    "phone": Patient.phone,
}


def base_select(country_id: uuid.UUID) -> Select:
    """Alive patients of one country (the identity scope)."""
    return select(Patient).where(Patient.country_id == country_id, alive(Patient))


def in_units(unit_ids: Sequence[uuid.UUID]) -> ColumnElement[bool]:
    """A patient belongs to a set of units when it was registered at one of them or has an order there.
    Patients with no registering unit and no orders at all (legacy imports) are nobody's and stay visible
    everywhere."""
    has_order = exists().where(Order.patient_id == Patient.id, alive(Order), Order.unit_id.in_(unit_ids))
    return or_(
        Patient.registered_unit_id.in_(unit_ids),
        has_order,
        and_(Patient.registered_unit_id.is_(None), Patient.stats_orders == 0),
    )


async def unit_stats(
    session: AsyncSession, patient_ids: Sequence[uuid.UUID], unit_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, tuple[int, datetime | None, int]]:
    """patient → (orders, last visit, total paid) counted inside `unit_ids` only (non-cancelled orders)."""
    if not patient_ids:
        return {}
    stmt = (
        select(Order.patient_id, func.count(), func.max(Order.created_at), func.coalesce(func.sum(Order.paid_amount), 0))
        .where(alive(Order), Order.status != "cancelled", Order.patient_id.in_(patient_ids), Order.unit_id.in_(unit_ids))
        .group_by(Order.patient_id)
    )
    return {pid: (int(n), last, int(spent)) for pid, n, last, spent in (await session.execute(stmt)).all()}


def search_predicate(search: str | None) -> ColumnElement[bool] | None:
    """DOMAIN_RULES section 4: fold(fullName) contains OR (digits >= 3 AND phone contains) OR fold(passport) contains.

    Expressed over `fold_text()` so the GIN trigram indexes apply. `None` when the search is empty.
    """
    if not search or not search.strip():
        return None
    needle = fold(search)
    clauses: list[ColumnElement[bool]] = []
    if needle:
        clauses.append(func.fold_text(Patient.full_name, type_=String).contains(needle, autoescape=True))
        clauses.append(
            func.fold_text(func.coalesce(Patient.passport_number, ""), type_=String).contains(needle, autoescape=True)
        )
    d = digits(search)
    if len(d) >= 3:
        clauses.append(Patient.phone.contains(d, autoescape=True))
    return or_(*clauses) if clauses else false()


async def get_by_id(session: AsyncSession, patient_id: uuid.UUID, unit_ids: Sequence[uuid.UUID] | None = None) -> Patient | None:
    """Alive patient by id; with `unit_ids` only when registered at / seen in one of them (one EXISTS over orders)."""
    stmt = select(Patient).where(Patient.id == patient_id, alive(Patient))
    if unit_ids is not None:
        stmt = stmt.where(in_units(unit_ids))
    return (await session.execute(stmt)).scalar_one_or_none()


async def find_by_passport(
    session: AsyncSession, country_id: uuid.UUID, passport: str, exclude_id: uuid.UUID | None = None
) -> Patient | None:
    """Case-insensitive passport match inside the country (optionally excluding one patient)."""
    stmt = base_select(country_id).where(func.upper(Patient.passport_number) == passport.upper())
    if exclude_id:
        stmt = stmt.where(Patient.id != exclude_id)
    return (await session.execute(stmt.limit(1))).scalar_one_or_none()


async def find_by_pinfl(
    session: AsyncSession, country_id: uuid.UUID, pinfl: str, exclude_id: uuid.UUID | None = None
) -> Patient | None:
    """Exact PINFL match inside the country (optionally excluding one patient)."""
    stmt = base_select(country_id).where(Patient.pinfl == pinfl)
    if exclude_id:
        stmt = stmt.where(Patient.id != exclude_id)
    return (await session.execute(stmt.limit(1))).scalar_one_or_none()


async def find_by_phone(
    session: AsyncSession, country_id: uuid.UUID, phone: str, exclude_id: uuid.UUID | None = None
) -> Patient | None:
    """Exact normalised phone match inside the country (optionally excluding one patient)."""
    stmt = base_select(country_id).where(Patient.phone == phone)
    if exclude_id:
        stmt = stmt.where(Patient.id != exclude_id)
    return (await session.execute(stmt.order_by(Patient.created_at).limit(1))).scalar_one_or_none()


async def find_duplicates(
    session: AsyncSession, country_id: uuid.UUID, *, phone: str | None, passport: str | None, pinfl: str | None
) -> list[Patient]:
    """Same country; phone exact OR passport case-insensitive OR pinfl exact (any identity key given)."""
    clauses: list[ColumnElement[bool]] = []
    if phone:
        clauses.append(Patient.phone == phone)
    if passport:
        clauses.append(func.upper(Patient.passport_number) == passport.upper())
    if pinfl:
        clauses.append(Patient.pinfl == pinfl)
    if not clauses:
        return []
    stmt = base_select(country_id).where(or_(*clauses)).order_by(Patient.created_at.desc()).limit(50)
    return list((await session.execute(stmt)).scalars().all())


async def search(
    session: AsyncSession, country_id: uuid.UUID, query: str, limit: int, unit_ids: Sequence[uuid.UUID] | None = None
) -> list[Patient]:
    """Quick-pick search: rank by last visit (nulls last), then newest.

    With a unit scope: a typed query still searches the whole country (a patient of another facility must be
    found, not registered twice) but lists the scope's own patients first; the empty query — "recent visitors" —
    only ever lists the scope's own patients."""
    stmt = base_select(country_id)
    pred = search_predicate(query)
    order = [Patient.stats_last_visit_at.desc().nulls_last(), Patient.created_at.desc()]
    if pred is not None:
        stmt = stmt.where(pred)
        if unit_ids is not None:
            order.insert(0, in_units(unit_ids).desc())
    elif unit_ids is not None:
        stmt = stmt.where(in_units(unit_ids))
    stmt = stmt.order_by(*order).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


# ----------------------------------------------------------------------------- geo reference


async def list_countries(session: AsyncSession) -> list[Country]:
    """All countries ordered by `sort_order`, name."""
    return list((await session.execute(select(Country).order_by(Country.sort_order, Country.name))).scalars().all())


async def country_by_code(session: AsyncSession, code: str) -> Country | None:
    return (await session.execute(select(Country).where(Country.code == code.upper()))).scalar_one_or_none()


async def list_regions(session: AsyncSession, country_id: uuid.UUID | None) -> list[Region]:
    """Regions (optionally of one country) ordered by `sort_order`, name."""
    stmt = select(Region)
    if country_id is not None:
        stmt = stmt.where(Region.country_id == country_id)
    return list((await session.execute(stmt.order_by(Region.sort_order, Region.name))).scalars().all())


async def list_districts(session: AsyncSession, region_id: uuid.UUID | None) -> list[District]:
    """Districts (optionally of one region) ordered by region, `sort_order`, name."""
    stmt = select(District)
    if region_id is not None:
        stmt = stmt.where(District.region_id == region_id)
    return list(
        (await session.execute(stmt.order_by(District.region_id, District.sort_order, District.name))).scalars().all()
    )
