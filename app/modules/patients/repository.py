"""Patients SQL: tenant-scoped selects, search predicate (fold_text + trigram indexes), identity lookups."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, Select, String, and_, exists, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.textutil import digits, fold
from app.infrastructure.db.base import alive
from app.infrastructure.db.models import Branch, Country, District, Order, Patient, Region

SORTABLE: dict[str, Any] = {
    "createdAt": Patient.created_at,
    "updatedAt": Patient.updated_at,
    "fullName": Patient.full_name,
    "phone": Patient.phone,
}


def base_select(company_id: uuid.UUID) -> Select:
    """Alive patients of one company."""
    return select(Patient).where(Patient.company_id == company_id, alive(Patient))


def in_branches(branches: list[uuid.UUID]) -> ColumnElement[bool]:
    """A patient belongs to a branch when it was registered there or has an order there. Patients with no
    branch and no orders at all (legacy imports) are nobody's and stay visible everywhere."""
    has_order = exists().where(Order.patient_id == Patient.id, Order.company_id == Patient.company_id, alive(Order), Order.branch_id.in_(branches))
    return or_(Patient.branch_id.in_(branches), has_order, and_(Patient.branch_id.is_(None), Patient.stats_orders == 0))


async def branch_stats(session: AsyncSession, company_id: uuid.UUID, patient_ids: list[uuid.UUID], branches: list[uuid.UUID]) -> dict[uuid.UUID, tuple[int, datetime | None, int]]:
    """patient → (orders, last visit, total paid) counted inside `branches` only (non-cancelled orders)."""
    if not patient_ids:
        return {}
    stmt = (
        select(Order.patient_id, func.count(), func.max(Order.created_at), func.coalesce(func.sum(Order.paid_amount), 0))
        .where(Order.company_id == company_id, alive(Order), Order.status != "cancelled", Order.patient_id.in_(patient_ids), Order.branch_id.in_(branches))
        .group_by(Order.patient_id)
    )
    return {pid: (int(n), last, int(spent)) for pid, n, last, spent in (await session.execute(stmt)).all()}


async def company_branch(session: AsyncSession, branch_id: uuid.UUID, company_id: uuid.UUID) -> uuid.UUID | None:
    """The id back when it is an alive branch of the company."""
    return (await session.execute(select(Branch.id).where(Branch.id == branch_id, Branch.company_id == company_id, alive(Branch)))).scalar_one_or_none()


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


async def get_by_id(session: AsyncSession, patient_id: uuid.UUID, company_id: uuid.UUID | None) -> Patient | None:
    """Alive patient by id, optionally restricted to a company."""
    stmt = select(Patient).where(Patient.id == patient_id, alive(Patient))
    if company_id is not None:
        stmt = stmt.where(Patient.company_id == company_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def find_by_passport(
    session: AsyncSession, company_id: uuid.UUID, passport: str, exclude_id: uuid.UUID | None = None
) -> Patient | None:
    """Case-insensitive passport match inside the company (optionally excluding one patient)."""
    stmt = base_select(company_id).where(func.upper(Patient.passport_number) == passport.upper())
    if exclude_id:
        stmt = stmt.where(Patient.id != exclude_id)
    return (await session.execute(stmt.limit(1))).scalar_one_or_none()


async def find_by_pinfl(
    session: AsyncSession, company_id: uuid.UUID, pinfl: str, exclude_id: uuid.UUID | None = None
) -> Patient | None:
    """Exact PINFL match inside the company (optionally excluding one patient)."""
    stmt = base_select(company_id).where(Patient.pinfl == pinfl)
    if exclude_id:
        stmt = stmt.where(Patient.id != exclude_id)
    return (await session.execute(stmt.limit(1))).scalar_one_or_none()


async def find_by_phone(
    session: AsyncSession, company_id: uuid.UUID, phone: str, exclude_id: uuid.UUID | None = None
) -> Patient | None:
    """Exact normalised phone match inside the company (optionally excluding one patient)."""
    stmt = base_select(company_id).where(Patient.phone == phone)
    if exclude_id:
        stmt = stmt.where(Patient.id != exclude_id)
    return (await session.execute(stmt.order_by(Patient.created_at).limit(1))).scalar_one_or_none()


async def find_duplicates(
    session: AsyncSession, company_id: uuid.UUID, *, phone: str | None, passport: str | None, pinfl: str | None
) -> list[Patient]:
    """Same company; phone exact OR passport case-insensitive OR pinfl exact (any identity key given)."""
    clauses: list[ColumnElement[bool]] = []
    if phone:
        clauses.append(Patient.phone == phone)
    if passport:
        clauses.append(func.upper(Patient.passport_number) == passport.upper())
    if pinfl:
        clauses.append(Patient.pinfl == pinfl)
    if not clauses:
        return []
    stmt = base_select(company_id).where(or_(*clauses)).order_by(Patient.created_at.desc()).limit(50)
    return list((await session.execute(stmt)).scalars().all())


async def search(session: AsyncSession, company_id: uuid.UUID, query: str, limit: int, branches: list[uuid.UUID] | None = None) -> list[Patient]:
    """Quick-pick search: rank by last visit (nulls last), then newest.

    With a branch scope: a typed query still searches the whole company (a patient of another branch must be
    found, not registered twice) but lists the scope's own patients first; the empty query — "recent visitors" —
    only ever lists the scope's own patients."""
    stmt = base_select(company_id)
    pred = search_predicate(query)
    order = [Patient.stats_last_visit_at.desc().nulls_last(), Patient.created_at.desc()]
    if pred is not None:
        stmt = stmt.where(pred)
        if branches is not None:
            order.insert(0, in_branches(branches).desc())
    elif branches is not None:
        stmt = stmt.where(in_branches(branches))
    stmt = stmt.order_by(*order).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def list_countries(session: AsyncSession) -> list[Country]:
    """All countries ordered by `order`, name."""
    return list((await session.execute(select(Country).order_by(Country.order, Country.name))).scalars().all())


async def country_by_code(session: AsyncSession, code: str) -> Country | None:
    return (await session.execute(select(Country).where(Country.code == code.upper()))).scalar_one_or_none()


async def list_regions(session: AsyncSession, country_id: uuid.UUID | None) -> list[Region]:
    """Regions (optionally of one country) ordered by `order`, name."""
    stmt = select(Region)
    if country_id is not None:
        stmt = stmt.where(Region.country_id == country_id)
    return list((await session.execute(stmt.order_by(Region.order, Region.name))).scalars().all())


async def list_districts(session: AsyncSession, region_id: uuid.UUID | None) -> list[District]:
    """Districts (optionally of one region) ordered by region, `order`, name."""
    stmt = select(District)
    if region_id is not None:
        stmt = stmt.where(District.region_id == region_id)
    return list(
        (await session.execute(stmt.order_by(District.region_id, District.order, District.name))).scalars().all()
    )
