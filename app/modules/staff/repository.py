"""Staff SQL helpers: employees (paged list with filters/search, extra units), roles. No rules, no HTTP.

Scope arrives as id lists (the service derives them from `UnitScope`): an employee lives where their home
`unit_id` is; a role is system-wide (`scope_unit_id` NULL) or scoped to one unit."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence

from sqlalchemy import delete, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import paginate_query, sort_clause
from app.core.textutil import digits, fold
from app.infrastructure.db.base import alive
from app.infrastructure.db.models import Employee, EmployeeUnit, Role
from app.modules.staff.schemas import EmployeeQuery


def _uuid_or_none(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


# ----------------------------------------------------------------------------- employees


async def list_employees(session: AsyncSession, unit_ids: Sequence[uuid.UUID], q: EmployeeQuery, *, include_super_admins: bool = False) -> tuple[list[Employee], int]:
    """Page of employees whose home unit is in `unit_ids` (+ superadmins — unit NULL — when asked);
    filters roleId, status; search fullName (folded) / login / phone digits."""
    home = Employee.unit_id.in_(unit_ids) if unit_ids else false()
    stmt = select(Employee).where(or_(home, Employee.unit_id.is_(None)) if include_super_admins else home, alive(Employee))
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


async def get_employee(session: AsyncSession, employee_id: uuid.UUID) -> Employee | None:
    """Alive employee by id — the service checks the caller's scope against `unit_id`."""
    return (await session.execute(select(Employee).where(Employee.id == employee_id, alive(Employee)))).scalar_one_or_none()


async def extra_unit_ids(session: AsyncSession, employee_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, list[uuid.UUID]]:
    """employee → its extra scope units (`employee_units`), for a page of employees in one query."""
    ids = list(dict.fromkeys(employee_ids))
    if not ids:
        return {}
    stmt = select(EmployeeUnit.employee_id, EmployeeUnit.unit_id).where(EmployeeUnit.employee_id.in_(ids)).order_by(EmployeeUnit.unit_id)
    out: dict[uuid.UUID, list[uuid.UUID]] = {}
    for emp_id, unit_id in (await session.execute(stmt)).all():
        out.setdefault(emp_id, []).append(unit_id)
    return out


async def replace_employee_units(session: AsyncSession, employee_id: uuid.UUID, unit_ids: Iterable[uuid.UUID]) -> None:
    """Set the employee's extra units wholesale."""
    await session.execute(delete(EmployeeUnit).where(EmployeeUnit.employee_id == employee_id))
    session.add_all([EmployeeUnit(employee_id=employee_id, unit_id=u) for u in dict.fromkeys(unit_ids)])


async def login_taken(session: AsyncSession, login: str, exclude_id: uuid.UUID | None = None) -> bool:
    """Logins are global and case-insensitive (single staff login form)."""
    stmt = select(Employee.id).where(func.lower(Employee.login) == login.strip().lower(), alive(Employee))
    if exclude_id is not None:
        stmt = stmt.where(Employee.id != exclude_id)
    return (await session.execute(stmt.limit(1))).scalar_one_or_none() is not None


async def role_in_use(session: AsyncSession, role_id: uuid.UUID) -> bool:
    stmt = select(Employee.id).where(Employee.role_id == role_id, alive(Employee)).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def employee_counts_by_role(session: AsyncSession, unit_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
    """role_id → number of alive employees whose home unit is in `unit_ids` (one GROUP BY)."""
    if not unit_ids:
        return {}
    stmt = select(Employee.role_id, func.count()).where(Employee.unit_id.in_(unit_ids), alive(Employee)).group_by(Employee.role_id)
    return {rid: int(n) for rid, n in (await session.execute(stmt)).all()}


# ----------------------------------------------------------------------------- roles


async def list_roles(session: AsyncSession, lineage_ids: Sequence[uuid.UUID]) -> Sequence[Role]:
    """System roles (scope NULL) + roles scoped to any unit of `lineage_ids` (ancestors + subtree of the scope)."""
    scoped = Role.scope_unit_id.in_(lineage_ids) if lineage_ids else false()
    stmt = (
        select(Role)
        .where(or_(Role.scope_unit_id.is_(None), scoped), alive(Role))
        .order_by(Role.scope_unit_id.is_(None).desc(), Role.is_system.desc(), Role.name.asc(), Role.id.asc())
    )
    return (await session.execute(stmt)).scalars().all()


async def get_role(session: AsyncSession, role_id: uuid.UUID) -> Role | None:
    return (await session.execute(select(Role).where(Role.id == role_id, alive(Role)))).scalar_one_or_none()


async def get_assignable_role(session: AsyncSession, role_id: uuid.UUID, path_unit_ids: Sequence[uuid.UUID]) -> Role | None:
    """A role an employee of a unit may hold: a system role, or one scoped to a unit on the employee's path
    (`path_unit_ids` = ancestors incl. the home unit; empty for a superadmin → system roles only)."""
    scoped = Role.scope_unit_id.in_(path_unit_ids) if path_unit_ids else false()
    stmt = select(Role).where(Role.id == role_id, alive(Role), or_(Role.scope_unit_id.is_(None), scoped))
    return (await session.execute(stmt)).scalar_one_or_none()


async def role_key_taken(session: AsyncSession, scope_unit_id: uuid.UUID | None, key: str, exclude_id: uuid.UUID | None = None) -> bool:
    """Keys are unique among alive roles of the same scope (system roles among themselves)."""
    scope_cond = Role.scope_unit_id.is_(None) if scope_unit_id is None else Role.scope_unit_id == scope_unit_id
    stmt = select(Role.id).where(scope_cond, Role.key == key, alive(Role))
    if exclude_id is not None:
        stmt = stmt.where(Role.id != exclude_id)
    return (await session.execute(stmt.limit(1))).scalar_one_or_none() is not None
