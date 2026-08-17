"""Tenant SQL helpers: companies (with denormalised counts), branches. No rules, no HTTP."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import paginate_query, sort_clause
from app.core.schemas import PageQuery
from app.core.textutil import fold
from app.infrastructure.db.base import alive
from app.infrastructure.db.models import Branch, Company, Employee


def _branch_count() -> Select:
    return select(func.count()).where(Branch.company_id == Company.id, alive(Branch)).correlate(Company).scalar_subquery()


def _employee_count() -> Select:
    return select(func.count()).where(Employee.company_id == Company.id, alive(Employee)).correlate(Company).scalar_subquery()


async def list_companies(session: AsyncSession, q: PageQuery) -> tuple[list[tuple[Company, int, int]], int]:
    """Page of `(company, branchCount, employeeCount)`; search on name/legalName via fold_text."""
    bc = _branch_count().label("branch_count")
    ec = _employee_count().label("employee_count")
    stmt = select(Company, bc, ec).where(alive(Company))
    needle = fold(q.search)
    if needle:
        like = f"%{needle}%"
        stmt = stmt.where(func.fold_text(Company.name).like(like) | func.fold_text(func.coalesce(Company.legal_name, "")).like(like))
    order = sort_clause(
        q.sort_by,
        q.sort_dir,
        {
            "name": Company.name,
            "legalName": Company.legal_name,
            "slug": Company.slug,
            "isActive": Company.is_active,
            "createdAt": Company.created_at,
            "updatedAt": Company.updated_at,
            "branchCount": bc,
            "employeeCount": ec,
        },
        default="createdAt",
    )
    rows, total = await paginate_query(session, stmt, q, order_by=[order, Company.id.asc()], scalars=False)
    return [(row[0], int(row[1]), int(row[2])) for row in rows], total


async def get_company(session: AsyncSession, company_id: uuid.UUID) -> Company | None:
    return (await session.execute(select(Company).where(Company.id == company_id, alive(Company)))).scalar_one_or_none()


async def company_counts(session: AsyncSession, company_id: uuid.UUID) -> tuple[int, int]:
    """`(branchCount, employeeCount)` for one company in a single round trip."""
    bc = select(func.count()).where(Branch.company_id == company_id, alive(Branch)).scalar_subquery()
    ec = select(func.count()).where(Employee.company_id == company_id, alive(Employee)).scalar_subquery()
    row = (await session.execute(select(bc, ec))).one()
    return int(row[0]), int(row[1])


async def slug_taken(session: AsyncSession, slug: str, exclude_id: uuid.UUID | None = None) -> bool:
    stmt = select(Company.id).where(func.lower(Company.slug) == slug.lower(), alive(Company))
    if exclude_id is not None:
        stmt = stmt.where(Company.id != exclude_id)
    return (await session.execute(stmt.limit(1))).scalar_one_or_none() is not None


async def list_branches(session: AsyncSession, company_id: uuid.UUID) -> Sequence[Branch]:
    stmt = select(Branch).where(Branch.company_id == company_id, alive(Branch)).order_by(Branch.created_at.asc(), Branch.id.asc())
    return (await session.execute(stmt)).scalars().all()


async def get_branch(session: AsyncSession, branch_id: uuid.UUID, company_id: uuid.UUID | None) -> Branch | None:
    """`company_id=None` means platform-wide lookup (superadmin only)."""
    stmt = select(Branch).where(Branch.id == branch_id, alive(Branch))
    if company_id is not None:
        stmt = stmt.where(Branch.company_id == company_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def branch_code_taken(session: AsyncSession, company_id: uuid.UUID, code: str, exclude_id: uuid.UUID | None = None) -> bool:
    stmt = select(Branch.id).where(Branch.company_id == company_id, Branch.code == code, alive(Branch))
    if exclude_id is not None:
        stmt = stmt.where(Branch.id != exclude_id)
    return (await session.execute(stmt.limit(1))).scalar_one_or_none() is not None
