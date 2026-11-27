"""The training seen from above.

* `overview` — one list of everyone the training concerns inside the caller's scope (a superadmin: every company and
  clinic at once): who is in it and how far, who has not signed in yet, who finished, who was let through — with
  every task's mark, so «3 of 4» also says WHICH one is missing.
* `announce_passed` / `remind_once` — the people who answer for an employee are told when they finish, and when they
  have been in the training for days without finishing. They are: every superadmin, and every employee whose scope
  covers the employee's unit and who may manage employees there (`admin.employee.write`).

Everything here is real data (`public.employee_training`, the real notifications): none of it ever runs in a
sandbox, whatever realm the request that triggered it is in.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, bindparam, false, func, literal, literal_column, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import StaffPrincipal
from app.core.config import settings
from app.core.pagination import total_pages
from app.core.permissions import INSTALLER_ROLE_KEY
from app.core.scope import ids_of
from app.core.textutil import fold
from app.infrastructure.db.base import alive
from app.infrastructure.db.models import Employee, EmployeeTraining, EmployeeUnit, OrgUnit, OrgUnitClosure, Role
from app.infrastructure.db.session import SessionLocal
from app.modules.training import service
from app.modules.training.schemas import (
    EmployeeTrainingOut,
    TrainingOverviewCounts,
    TrainingOverviewOut,
    TrainingOverviewQuery,
    TrainingOverviewRow,
)
from app.modules.training.tasks import TASKS

log = logging.getLogger("app.training")

#: who is told: those who can act on it (let the employee through, send them back)
MANAGER_PERMISSION = "admin.employee.write"
#: more stalled employees than this for one reader in one run become ONE notice with a link to the overview
DIGEST_FROM = 4
#: how many people one reminder run takes (the rest follow in the next run)
REMIND_BATCH = 200
#: how often the reminder looks (seconds) — see `app.workers.runner`
REMIND_POLL_SECONDS = 900
#: at most this many managers are told about one employee (the nearest in the tree first)
MAX_MANAGERS = 40
EMPLOYEE_LINK = "/admin/employees/{employee_id}"
OVERVIEW_LINK = "/admin/training?state=stalled"

_TASK_PERMISSIONS = sorted({p for t in TASKS for p in t.perms})
T = EmployeeTraining
# (`employee_training`, `employees` and `roles` are never aliased in these statements, so the raw fragments may name them)
_UNDONE = "SELECT 1 FROM jsonb_each(employee_training.tasks) x WHERE jsonb_typeof(x.value) = 'null' AND NOT jsonb_exists(employee_training.unavailable, x.key)"
_DONE_COUNT = literal_column("(SELECT count(*) FROM jsonb_each(employee_training.tasks) x WHERE jsonb_typeof(x.value) <> 'null')")
#: the role — or a personal extra permission — brings at least one task with it
_GIVEN_A_TASK = (
    "(roles.permissions && CAST(:task_permissions AS text[])"
    " OR jsonb_exists_any(coalesce(employees.overrides -> 'allow', '[]'::jsonb), CAST(:task_permissions AS text[])))"
)


# ----------------------------------------------------------------------------- the overview


def _conditions(cutoff: datetime | None) -> dict[str, Any]:
    """The states of the overview as SQL. `pending` — no row yet, and a role that will be given tasks (told from the
    role's and the personal extra permissions; a personal denial is settled later, row by row)."""
    active = T.status == "active"
    installer = and_(Role.scope_unit_id.is_(None), Role.key == INSTALLER_ROLE_KEY)
    has_tasks = text(_GIVEN_A_TASK).bindparams(bindparam("task_permissions", _TASK_PERMISSIONS))
    pending = and_(T.employee_id.is_(None), ~installer, has_tasks)
    return {
        "active": active,
        # (a repeat the employee chose for themselves is never overdue: nobody is waiting for it)
        "stalled": and_(active, T.previous.is_(None), T.started_at <= cutoff) if cutoff is not None else false(),
        "ready": and_(active, text(f"NOT EXISTS ({_UNDONE})")),
        "pending": pending,
        "passed": T.status == "passed",
        "skipped": T.status == "skipped",
        "open": or_(active, pending),
        "all": or_(T.status.in_(("active", "passed", "skipped")), pending),
    }


def _order(q: TrainingOverviewQuery) -> list[Any]:
    """Default: who has been in the training longest comes first (then those still to sign in, oldest account
    first); the finished — newest first."""
    columns = {
        "fullName": Employee.full_name,
        "startedAt": T.started_at,
        "completedAt": T.completed_at,
        "lastLoginAt": Employee.last_login_at,
        "progress": _DONE_COUNT,
    }
    column = columns.get(q.sort_by or "")
    if column is None:
        if q.state in ("passed", "skipped"):
            return [T.completed_at.desc().nulls_last(), Employee.full_name.asc()]
        return [T.started_at.asc().nulls_last(), Employee.created_at.asc(), Employee.full_name.asc()]
    descending = (q.sort_dir or ("asc" if q.sort_by == "fullName" else "desc")) == "desc"
    return [column.desc().nulls_last() if descending else column.asc().nulls_last(), Employee.full_name.asc()]


def _row(e: Employee, training: EmployeeTrainingOut, unit_name: str | None, role_name: str | None) -> TrainingOverviewRow:
    return TrainingOverviewRow(
        id=str(e.id), full_name=e.full_name, login=e.login, avatar_hue=e.avatar_hue, status=e.status,
        role_id=str(e.role_id) if e.role_id else None, role_name=role_name,
        unit_id=str(e.unit_id) if e.unit_id else None, unit_name=unit_name,
        last_login_at=e.last_login_at, training=training,
    )  # fmt: skip


async def overview(session: AsyncSession, staff: StaffPrincipal, q: TrainingOverviewQuery) -> TrainingOverviewOut:
    """Everyone the training concerns inside the caller's scope — active accounts only, never a superadmin."""
    base = (
        select(Employee)
        .select_from(Employee)
        .outerjoin(T, T.employee_id == Employee.id)
        .join(Role, Role.id == Employee.role_id)
        .where(alive(Employee), Employee.status == "active", Employee.is_super_admin.is_(False))
    )
    if q.unit_id:
        ids = ids_of([q.unit_id])
        if not ids:
            base = base.where(false())
        else:
            staff.require_unit(ids[0])
            base = base.where(Employee.unit_id.in_(select(OrgUnitClosure.descendant_id).where(OrgUnitClosure.ancestor_id == ids[0])))
    elif staff.allowed_unit_ids is None:
        base = base.where(Employee.unit_id.is_not(None))
    else:
        base = base.where(Employee.unit_id.in_(list(staff.allowed_unit_ids)) if staff.allowed_unit_ids else false())
    needle = fold(q.search)
    if needle:
        base = base.where(or_(func.fold_text(Employee.full_name).like(f"%{needle}%"), func.lower(Employee.login).like(f"%{(q.search or '').strip().lower()}%")))

    conds = _conditions(service.stall_cutoff())
    counted = (await session.execute(base.with_only_columns(*(func.count().filter(conds[k]).label(k) for k in TrainingOverviewCounts.model_fields)).order_by(None))).one()
    counts = TrainingOverviewCounts(**{k: int(getattr(counted, k) or 0) for k in TrainingOverviewCounts.model_fields})

    listed = base.where(conds[q.state])
    total = int((await session.execute(select(func.count()).select_from(listed.order_by(None).subquery()))).scalar_one())
    employees = list((await session.execute(listed.order_by(*_order(q), Employee.id.asc()).offset((q.page - 1) * q.page_size).limit(q.page_size))).scalars().all())

    training = await service.employee_training_map(session, employees)
    unit_ids = {e.unit_id for e in employees if e.unit_id}
    units = dict((await session.execute(select(OrgUnit.id, OrgUnit.name).where(OrgUnit.id.in_(unit_ids)))).all()) if unit_ids else {}
    role_ids = {e.role_id for e in employees if e.role_id}
    roles = dict((await session.execute(select(Role.id, Role.name).where(Role.id.in_(role_ids)))).all()) if role_ids else {}
    items = [_row(e, training[e.id], units.get(e.unit_id), roles.get(e.role_id)) for e in employees]
    return TrainingOverviewOut(
        items=items, total=total, page=q.page, page_size=q.page_size, total_pages=total_pages(total, q.page_size), counts=counts, stall_days=max(0, settings.training_stall_days)
    )  # fmt: skip


# ----------------------------------------------------------------------------- telling the people who should know


async def _superadmins(session: AsyncSession) -> list[uuid.UUID]:
    return list((await session.execute(select(Employee.id).where(Employee.is_super_admin.is_(True), Employee.status == "active", alive(Employee)))).scalars().all())


async def _managers(session: AsyncSession, unit_id: uuid.UUID | None) -> list[uuid.UUID]:
    """Active employees whose scope (home or extra unit) covers `unit_id` and who may manage employees — the nearest
    in the tree first."""
    if unit_id is None:
        return []
    above = select(OrgUnitClosure.ancestor_id, OrgUnitClosure.depth).where(OrgUnitClosure.descendant_id == unit_id).subquery()
    extra = select(EmployeeUnit.employee_id, func.min(above.c.depth).label("depth")).join(above, above.c.ancestor_id == EmployeeUnit.unit_id).group_by(EmployeeUnit.employee_id).subquery()
    nearest = func.least(func.coalesce(above.c.depth, literal(32767)), func.coalesce(extra.c.depth, literal(32767)))
    rows = (
        await session.execute(
            select(Employee, Role)
            .join(Role, Role.id == Employee.role_id)
            .outerjoin(above, above.c.ancestor_id == Employee.unit_id)
            .outerjoin(extra, extra.c.employee_id == Employee.id)
            .where(alive(Employee), Employee.status == "active", Employee.is_super_admin.is_(False), or_(above.c.ancestor_id.is_not(None), extra.c.employee_id.is_not(None)))
            .order_by(nearest, Employee.created_at)
        )
    ).all()
    return [emp.id for emp, role in rows if MANAGER_PERMISSION in service._permissions(emp, role)][:MAX_MANAGERS]


async def recipients(session: AsyncSession, emp: Employee, *, known: dict[Any, list[uuid.UUID]] | None = None) -> list[uuid.UUID]:
    """Who is told about `emp`'s training (never `emp` themselves). `known` keeps the answers of one run."""
    known = known if known is not None else {}
    if "super" not in known:
        known["super"] = await _superadmins(session)
    if emp.unit_id not in known:
        known[emp.unit_id] = await _managers(session, emp.unit_id)
    out: list[uuid.UUID] = []
    for who in (*known[emp.unit_id], *known["super"]):
        if who != emp.id and who not in out:
            out.append(who)
    return out


def _place(unit_name: str | None) -> str:
    return f" ({unit_name})" if unit_name else ""


async def announce_passed(employee_id: uuid.UUID) -> int:
    """«{name} finished the training» to everyone who answers for them. Returns how many were told; never raises —
    the employee's own «finish» is already done and must stay done."""
    from app.modules.messaging import service as messaging_svc

    try:
        async with SessionLocal() as session:
            emp = await session.get(Employee, employee_id)
            row = await session.get(T, employee_id)
            if emp is None or row is None or row.status != "passed":
                return 0
            unit_name = (await session.execute(select(OrgUnit.name).where(OrgUnit.id == emp.unit_id))).scalar_one_or_none() if emp.unit_id else None
            done, total = service._counts(row)
            days = max(0, ((row.completed_at or service._now()) - row.started_at).days)
            told = await recipients(session, emp)
            for who in told:
                await messaging_svc.notify(
                    session, unit_id=None, employee_id=who, kind="success", link=EMPLOYEE_LINK.format(employee_id=emp.id),
                    title="O‘quv yakunlandi", body=f"{emp.full_name}{_place(unit_name)} barcha topshiriqlarni bajarib, o‘quvni yakunladi.",
                    code="training.passed", params={"employeeId": str(emp.id), "name": emp.full_name, "unit": unit_name or "", "done": done, "total": total, "days": days},
                )  # fmt: skip
            await session.commit()
            return len(told)
    except Exception:
        log.exception("could not announce the finished training of %s", employee_id)
        return 0


async def remind_once() -> int:
    """One look for people who have been in a MANDATORY training `TRAINING_STALL_DAYS` or longer and were not
    mentioned in the last `TRAINING_REMIND_EVERY_DAYS`: their managers and the superadmins get a notice — one per
    employee, or one for all of them when there are many. (A repeat someone started for themselves concerns nobody
    else.) Returns how many employees were mentioned."""
    from app.modules.messaging import service as messaging_svc

    days = settings.training_stall_days
    if not settings.training_enabled or days <= 0:
        return 0
    now = service._now()
    cutoff = now - timedelta(days=days)
    again = now - timedelta(days=max(1, settings.training_remind_every_days))
    async with SessionLocal() as session:
        rows = list(
            (
                await session.execute(
                    select(T)
                    .join(Employee, Employee.id == T.employee_id)
                    .where(T.status == "active", T.previous.is_(None), T.started_at <= cutoff, or_(T.reminded_at.is_(None), T.reminded_at <= again))
                    .where(alive(Employee), Employee.status == "active", Employee.is_super_admin.is_(False))
                    .order_by(T.started_at)
                    .limit(REMIND_BATCH)
                    # a row is claimed before anyone is told: two runs can never mention the same person twice
                    .with_for_update(of=T, skip_locked=True)
                )
            ).scalars()
        )
        if not rows:
            return 0
        employees = {e.id: e for e in (await session.execute(select(Employee).where(Employee.id.in_([r.employee_id for r in rows])))).scalars()}
        unit_ids = {e.unit_id for e in employees.values() if e.unit_id}
        units = dict((await session.execute(select(OrgUnit.id, OrgUnit.name).where(OrgUnit.id.in_(unit_ids)))).all()) if unit_ids else {}
        known: dict[Any, list[uuid.UUID]] = {}
        per_reader: dict[uuid.UUID, list[EmployeeTraining]] = defaultdict(list)
        for row in rows:
            row.reminded_at = now
            for who in await recipients(session, employees[row.employee_id], known=known):
                per_reader[who].append(row)
        for who, mine in per_reader.items():
            if len(mine) >= DIGEST_FROM:
                await messaging_svc.notify(
                    session, unit_id=None, employee_id=who, kind="warning", link=OVERVIEW_LINK,
                    title="O‘quv tugatilmagan", body=f"{len(mine)} nafar xodim o‘quvni {days} kundan ortiq tugatmagan.",
                    code="training.stalled_many", params={"count": len(mine), "days": days},
                )  # fmt: skip
                continue
            for row in mine:
                emp = employees[row.employee_id]
                unit_name = units.get(emp.unit_id)
                done, total = service._counts(row)
                lasted = max(days, (now - row.started_at).days)
                await messaging_svc.notify(
                    session, unit_id=None, employee_id=who, kind="warning", link=EMPLOYEE_LINK.format(employee_id=emp.id),
                    title="O‘quv tugatilmagan", body=f"{emp.full_name}{_place(unit_name)} {lasted} kundan beri o‘quvda: {done}/{total} topshiriq bajarilgan.",
                    code="training.stalled", params={"employeeId": str(emp.id), "name": emp.full_name, "unit": unit_name or "", "days": lasted, "done": done, "total": total},
                )  # fmt: skip
        await session.commit()
    log.info("training reminder: %s employee(s) mentioned to %s reader(s)", len(rows), len(per_reader))
    return len(rows)
