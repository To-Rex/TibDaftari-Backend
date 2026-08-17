"""Patients SQL: tenant-scoped selects, search predicate (fold_text + trigram indexes), identity lookups."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import ColumnElement, Select, String, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.textutil import digits, fold
from app.infrastructure.db.base import alive
from app.infrastructure.db.models import District, Patient, Region

SORTABLE: dict[str, Any] = {
    "createdAt": Patient.created_at,
    "updatedAt": Patient.updated_at,
    "fullName": Patient.full_name,
    "phone": Patient.phone,
}


def base_select(company_id: uuid.UUID) -> Select:
    """Alive patients of one company."""
    return select(Patient).where(Patient.company_id == company_id, alive(Patient))


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


async def search(session: AsyncSession, company_id: uuid.UUID, query: str, limit: int) -> list[Patient]:
    """Quick-pick search: rank by last visit (nulls last), then newest."""
    stmt = base_select(company_id)
    pred = search_predicate(query)
    if pred is not None:
        stmt = stmt.where(pred)
    stmt = stmt.order_by(Patient.stats_last_visit_at.desc().nulls_last(), Patient.created_at.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def list_regions(session: AsyncSession) -> list[Region]:
    """All regions ordered by `order`, name."""
    return list((await session.execute(select(Region).order_by(Region.order, Region.name))).scalars().all())


async def list_districts(session: AsyncSession, region_id: uuid.UUID | None) -> list[District]:
    """Districts (optionally of one region) ordered by region, `order`, name."""
    stmt = select(District)
    if region_id is not None:
        stmt = stmt.where(District.region_id == region_id)
    return list(
        (await session.execute(stmt.order_by(District.region_id, District.order, District.name))).scalars().all()
    )
