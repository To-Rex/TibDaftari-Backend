"""Staff SQL helpers: employees (paged list with filters/search), roles. No rules, no HTTP."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import paginate_query, sort_clause
from app.core.textutil import digits, fold
from app.infrastructure.db.base import alive
from app.infrastructure.db.models import Employee, Role
from app.modules.staff.schemas import EmployeeQuery


def _uuid_or_none(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


async def list_employees(session: AsyncSession, company_id: uuid.UUID, q: EmployeeQuery) -> tuple[list[Employee], int]:
    """Page of employees: filters branchId (array contains), roleId, status; search fullName/login/phone digits."""
    stmt = select(Employee).where(Employee.company_id == company_id, alive(Employee))
    branch_id = _uuid_or_none(q.branch_id)
    if q.branch_id:
        stmt = stmt.where(Employee.branch_ids.contains([branch_id])) if branch_id else stmt.where(false())
    role_id = _uuid_or_none(q.role_id)
    if q.role_id:
        stmt = stmt.where(Employee.role_id == role_id) if role_id else stmt.where(false())
    if q.status:
        stmt = stmt.where(Employee.status == q.status)
    needle = fold(q.search)
    if needle:
        like = f"%{needle}%"
        conds = [func.fold_text(Employee.full_name).like(like), func.lower(Employee.login).like(f"%{(q.search or '').strip().lower()}%")]
        d = digits(q.search)
        if d:
            conds.append(func.regexp_replace(func.coalesce(Employee.phone, ""), r"\D", "", "g").like(f"%{d}%"))
        stmt = stmt.where(or_(*conds))
    order = sort_clause(
        q.sort_by,
        q.sort_dir,
        {
            "fullName": Employee.full_name,
            "login": Employee.login,
            "status": Employee.status,
            "lastLoginAt": Employee.last_login_at,
            "createdAt": Employee.created_at,
            "updatedAt": Employee.updated_at,
        },
        default="fullName",
        default_dir="asc",
    )
    return await paginate_query(session, stmt, q, order_by=[order, Employee.id.asc()])


async def get_employee(session: AsyncSession, employee_id: uuid.UUID, company_id: uuid.UUID | None) -> Employee | None:
    """`company_id=None` means platform-wide lookup (superadmin only)."""
    stmt = select(Employee).where(Employee.id == employee_id, alive(Employee))
    if company_id is not None:
        stmt = stmt.where(Employee.company_id == company_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def login_taken(session: AsyncSession, login: str, exclude_id: uuid.UUID | None = None) -> bool:
    """Logins are global and case-insensitive (single staff login form)."""
    stmt = select(Employee.id).where(func.lower(Employee.login) == login.strip().lower(), alive(Employee))
    if exclude_id is not None:
        stmt = stmt.where(Employee.id != exclude_id)
    return (await session.execute(stmt.limit(1))).scalar_one_or_none() is not None


async def role_in_use(session: AsyncSession, role_id: uuid.UUID) -> bool:
    stmt = select(Employee.id).where(Employee.role_id == role_id, alive(Employee)).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def employee_counts_by_role(session: AsyncSession, company_id: uuid.UUID) -> dict[uuid.UUID, int]:
    """role_id → number of alive employees of the company (one GROUP BY)."""
    stmt = select(Employee.role_id, func.count()).where(Employee.company_id == company_id, alive(Employee)).group_by(Employee.role_id)
    return {rid: int(n) for rid, n in (await session.execute(stmt)).all()}


async def list_roles(session: AsyncSession, company_id: uuid.UUID) -> Sequence[Role]:
    """Company roles + platform roles (company_id NULL)."""
    stmt = (
        select(Role)
        .where(or_(Role.company_id == company_id, Role.company_id.is_(None)), alive(Role))
        .order_by(Role.company_id.is_(None).desc(), Role.is_system.desc(), Role.name.asc(), Role.id.asc())
    )
    return (await session.execute(stmt)).scalars().all()


async def get_role(session: AsyncSession, role_id: uuid.UUID) -> Role | None:
    return (await session.execute(select(Role).where(Role.id == role_id, alive(Role)))).scalar_one_or_none()


async def get_assignable_role(session: AsyncSession, role_id: uuid.UUID, company_id: uuid.UUID) -> Role | None:
    """A role an employee of `company_id` may hold: own company's or a platform role."""
    stmt = select(Role).where(Role.id == role_id, alive(Role), or_(Role.company_id == company_id, Role.company_id.is_(None)))
    return (await session.execute(stmt)).scalar_one_or_none()


async def role_key_taken(session: AsyncSession, company_id: uuid.UUID | None, key: str, exclude_id: uuid.UUID | None = None) -> bool:
    company_cond = Role.company_id.is_(None) if company_id is None else Role.company_id == company_id
    stmt = select(Role.id).where(company_cond, Role.key == key, alive(Role))
    if exclude_id is not None:
        stmt = stmt.where(Role.id != exclude_id)
    return (await session.execute(stmt.limit(1))).scalar_one_or_none() is not None
