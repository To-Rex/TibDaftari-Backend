"""An employee's training: state, tasks, finishing, and what a manager may do about it.

State lives in `public.employee_training` (real data — it says WHO is in the sandbox) and is always read and written
on the real pool, whatever pool the request itself runs on. `is_active` is the hot path of every staff request, so
its answer is kept in Redis for a few minutes and dropped the moment the state changes.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.api.deps import RequestMeta, StaffPrincipal
from app.core.audit import audit
from app.core.config import settings
from app.core.exceptions import ForbiddenError, StateError, ValidationError
from app.core.permissions import INSTALLER_ROLE_KEY, PLATFORM_PERMISSIONS, resolve_permissions
from app.core.scope import facility_ids_under
from app.infrastructure.db import realm
from app.infrastructure.db.base import alive
from app.infrastructure.db.models import Employee, EmployeeTraining, OrgUnit, Role
from app.infrastructure.db.session import SessionLocal, session_scope
from app.infrastructure.redis.client import get_redis
from app.modules.training import samples
from app.modules.training.schemas import EmployeeTrainingOut, TrainingOut, TrainingTaskOut
from app.modules.training.tasks import TASK_BY_KEY, TASKS, tasks_for

log = logging.getLogger("app.training")

STATE_KEY = "training:st:{employee_id}"
STATE_TTL = 600  # seconds; every change of state drops the key at once
RECEPTION_TASKS = frozenset({"patient_create", "order_create"})
INCOMPLETE = "Avval barcha o‘quv topshiriqlarini bajaring"
SELF_ERROR = "O‘z o‘quvingizni o‘zingiz o‘zgartira olmaysiz"
NOTHING_TO_LEARN = "Bu xodimning rolida mashq qilinadigan topshiriq yo‘q"


def _now() -> datetime:
    return datetime.now(UTC)


async def _drop_state(employee_id: uuid.UUID | str) -> None:
    try:
        await get_redis().delete(STATE_KEY.format(employee_id=employee_id))
    except Exception:  # pragma: no cover — the key expires by itself
        pass


def _permissions(emp: Employee, role: Role | None) -> frozenset[str]:
    return frozenset(p for p in resolve_permissions(role.permissions if role else [], emp.overrides) if p not in PLATFORM_PERMISSIONS)


def _is_installer(role: Role | None) -> bool:
    """O‘rnatuvchi — "a superadmin of their own territory": they set clinics up and, like a superadmin, are not
    sent to practise the desk work first (their setup work would be locked until they did)."""
    return role is not None and role.scope_unit_id is None and role.key == INSTALLER_ROLE_KEY


async def _tasks_of(session: AsyncSession, emp: Employee) -> dict[str, Any]:
    """The task list an employee gets now: every task their role's permissions cover, none done yet."""
    if emp.is_super_admin:
        return {}
    role = await session.get(Role, emp.role_id) if emp.role_id else None
    if _is_installer(role):
        return {}
    return {t.key: None for t in tasks_for(_permissions(emp, role))}


async def load_or_init(session: AsyncSession, employee_id: uuid.UUID) -> EmployeeTraining | None:
    """The employee's row, made on first sight: `active` with their tasks, or `exempt` when there is nothing to
    practise (a superadmin, a role without daily operations). `session` must be a REAL-data session."""
    row = await session.get(EmployeeTraining, employee_id)
    if row is not None:
        return row
    emp = await session.get(Employee, employee_id)
    if emp is None or emp.deleted_at is not None:
        return None
    mine = await _tasks_of(session, emp)
    values: dict[str, Any] = {"employee_id": employee_id, "status": "active", "tasks": mine}
    if not mine:
        role = await session.get(Role, emp.role_id) if emp.role_id else None
        values |= {"status": "exempt", "note": "superadmin" if emp.is_super_admin else "installer" if _is_installer(role) else "no_tasks", "completed_at": _now()}
    # two first requests at once: one inserts, the other finds the row
    await session.execute(pg_insert(EmployeeTraining).values(**values).on_conflict_do_nothing(index_elements=["employee_id"]))
    await session.flush()
    return await session.get(EmployeeTraining, employee_id, populate_existing=True)


async def is_active(employee_id: uuid.UUID) -> bool:
    """Is this employee in training right now (→ their requests use the sandbox)? Redis first, the table behind it."""
    if not settings.training_enabled:
        return False
    key = STATE_KEY.format(employee_id=employee_id)
    try:
        cached = await get_redis().get(key)
    except Exception:  # pragma: no cover
        cached = None
    if cached in ("0", "1"):
        return cached == "1"
    async with SessionLocal() as session:
        row = await load_or_init(session, employee_id)
        await session.commit()
        active = row is not None and row.status == "active"
    try:
        await get_redis().set(key, "1" if active else "0", ex=STATE_TTL)
    except Exception:  # pragma: no cover
        pass
    return active


async def mark_done(employee_id: uuid.UUID, task_key: str) -> bool:
    """Tick a task (first time only). True when it changed something."""
    async with SessionLocal() as session:
        row = (await session.execute(select(EmployeeTraining).where(EmployeeTraining.employee_id == employee_id).with_for_update())).scalar_one_or_none()
        if row is None or row.status != "active" or task_key not in (row.tasks or {}) or row.tasks.get(task_key):
            return False
        row.tasks = {**row.tasks, task_key: _now().isoformat()}
        flag_modified(row, "tasks")
        await session.commit()
        return True


def _when(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _counts(row: EmployeeTraining | None) -> tuple[int, int]:
    tasks = (row.tasks or {}) if row is not None else {}
    known = [k for k in tasks if k in TASK_BY_KEY]
    return sum(1 for k in known if tasks[k]), len(known)


def training_out(row: EmployeeTraining | None, available: dict[str, bool] | None = None, *, facility: OrgUnit | None = None, nowhere: bool = False) -> TrainingOut:
    """The caller's own view. `available`: need → is there something to practise it on (None = not looked at);
    `nowhere`: the employee has no facility to practise in at all — nothing can be done, nothing holds them back."""
    if row is None:
        return TrainingOut(state="exempt")
    active = row.status == "active"
    stored = row.tasks or {}

    def _possible(t: Any) -> bool:
        if stored.get(t.key):
            return True
        if nowhere:
            return False
        return True if (available is None or t.needs is None) else bool(available.get(t.needs))

    items = [TrainingTaskOut(key=t.key, done=bool(stored.get(t.key)), done_at=_when(stored.get(t.key)), available=_possible(t)) for t in TASKS if t.key in stored]
    done = sum(1 for i in items if i.done)
    return TrainingOut(
        state=row.status,  # type: ignore[arg-type]
        active=active,
        tasks=items,
        done=done,
        total=len(items),
        can_complete=active and all(i.done or not i.available for i in items),
        started_at=row.started_at,
        completed_at=row.completed_at,
        unit_id=str(facility.id) if facility is not None else None,
        unit_name=facility.name if facility is not None else None,
    )


async def _facility(session: AsyncSession, staff: StaffPrincipal, unit_id: uuid.UUID | None) -> OrgUnit | None:
    """Where the trainee practises: the facility they are working in (`unit_id`), else their home facility. Someone
    who works above the facilities (a territory's administrator) gets the first facility under where they are.
    None = there is no facility they may act in."""
    wanted = unit_id if unit_id is not None and staff.can_see(unit_id) else None
    for candidate in (wanted, staff.home_facility_id):
        if candidate is None:
            continue
        unit = await session.get(OrgUnit, candidate)
        if unit is not None and unit.deleted_at is None and unit.level == "facility":
            return unit
    for root in [wanted] if wanted is not None else list(staff.unit_ids):
        ids = await facility_ids_under(session, root)
        if ids:
            units = {u.id: u for u in (await session.execute(select(OrgUnit).where(OrgUnit.id.in_(ids[:50]), alive(OrgUnit), OrgUnit.is_active.is_(True)))).scalars()}
            first = next((units[i] for i in ids[:50] if i in units), None)
            if first is not None:
                return first
    return None


async def _reconcile(staff: StaffPrincipal) -> EmployeeTraining | None:
    """The employee's row with the tasks of the role they hold NOW: a role changed in the middle of the training
    must not leave tasks they may no longer do (they could never finish) — the marks of tasks already done stay.
    A role with nothing to practise ends the training (`exempt`)."""
    mine = [] if staff.is_installer else [t.key for t in TASKS if staff.has(*t.perms)]
    async with SessionLocal() as session:
        row = await load_or_init(session, staff.id)
        if row is None or row.status != "active" or list(row.tasks or {}) == mine:
            await session.commit()
            return row
        row = (await session.execute(select(EmployeeTraining).where(EmployeeTraining.employee_id == staff.id).with_for_update())).scalar_one()
        if row.status == "active":
            row.tasks = {k: (row.tasks or {}).get(k) for k in mine}
            flag_modified(row, "tasks")
            if not mine:
                row.status, row.note, row.completed_at = "exempt", "no_tasks", _now()
        await session.commit()
        ended = row.status != "active"
    if ended:
        await _drop_state(staff.id)
    return row


async def session_flag(staff: StaffPrincipal) -> bool:
    """For the session payload: is this employee in training."""
    if staff.is_super_admin:
        return False
    return await is_active(staff.id)


async def status(session: AsyncSession, staff: StaffPrincipal, unit_id: uuid.UUID | None = None) -> TrainingOut:
    """The caller's training with what is left to do. For a trainee this also makes sure the sandbox of the facility
    they work in holds something to practise each remaining task on (`samples.ensure`)."""
    if staff.is_super_admin or not settings.training_enabled:
        return TrainingOut(state="exempt")
    row = await _reconcile(staff)
    if row is None or row.status != "active":
        return training_out(row)
    if not realm.is_training():
        # the very request that started the training (or one that raced a reset) still runs on the real data: nothing
        # is seeded there — the next one is in the sandbox
        return training_out(row)
    pending = [TASK_BY_KEY[k] for k, done in (row.tasks or {}).items() if not done and k in TASK_BY_KEY]
    facility = await _facility(session, staff, unit_id)
    if facility is None:
        return training_out(row, nowhere=True)
    available = await samples.ensure(session, staff, facility, {t.needs for t in pending if t.needs}, patients=any(t.key in RECEPTION_TASKS for t in pending))
    return training_out(row, available, facility=facility)


async def complete(session: AsyncSession, staff: StaffPrincipal, unit_id: uuid.UUID | None, meta: RequestMeta) -> TrainingOut:
    """«Finish»: every task done → the employee works with the real data from the next request on, and what they
    made in the sandbox is removed. 409 `training_incomplete` while something is left."""
    out = await status(session, staff, unit_id)
    if not out.active:
        return out
    if not out.can_complete:
        raise StateError(INCOMPLETE, code="training_incomplete")
    if realm.is_training():
        await samples.purge(session, staff.id)
    async with SessionLocal() as real:
        row = (await real.execute(select(EmployeeTraining).where(EmployeeTraining.employee_id == staff.id).with_for_update())).scalar_one()
        row.status = "passed"
        row.completed_at = _now()
        row.completed_by = None
        await audit(
            real, actor_type="staff", actor_id=staff.id, unit_id=staff.home_unit_id, action="training.complete", entity="employee", entity_id=staff.id,
            after={"done": out.done, "total": out.total}, ip=meta.ip, request_id=meta.request_id,
        )  # fmt: skip
        await real.commit()
        result = training_out(row)
    await _drop_state(staff.id)
    return result


# ----------------------------------------------------------------------------- a manager's side


async def employee_training_map(session: AsyncSession, employees: Sequence[Employee]) -> dict[uuid.UUID, EmployeeTrainingOut]:
    """Training of a page of employees for their cards (one query). Someone who has not signed in since the training
    exists has no row yet: `pending` — it starts at their first sign-in."""
    if not employees:
        return {}
    rows = {r.employee_id: r for r in (await session.execute(select(EmployeeTraining).where(EmployeeTraining.employee_id.in_([e.id for e in employees])))).scalars()}
    by = {r.completed_by for r in rows.values() if r.completed_by}
    names = dict((await session.execute(select(Employee.id, Employee.full_name).where(Employee.id.in_(by)))).all()) if by else {}
    out: dict[uuid.UUID, EmployeeTrainingOut] = {}
    for e in employees:
        row = rows.get(e.id)
        if row is None:
            out[e.id] = EmployeeTrainingOut(state="exempt" if e.is_super_admin else "pending")
            continue
        done, total = _counts(row)
        out[e.id] = EmployeeTrainingOut(
            state=row.status,  # type: ignore[arg-type]
            done=done, total=total, started_at=row.started_at, completed_at=row.completed_at, completed_by_name=names.get(row.completed_by) if row.completed_by else None,
        )  # fmt: skip
    return out


async def _target(session: AsyncSession, employee_id: uuid.UUID, staff: StaffPrincipal) -> Employee:
    """The employee a manager acts on: inside their scope, not a protected account, never themselves."""
    from app.modules.staff import service as staff_svc

    emp = await staff_svc.get_employee_or_404(session, employee_id, staff)
    await staff_svc._guard_protected_target(session, emp, staff)
    if emp.id == staff.id and not staff.is_super_admin:
        raise ForbiddenError(SELF_ERROR, code="self_edit")
    return emp


async def _purge_sandbox(employee_id: uuid.UUID) -> None:
    try:
        async with session_scope(training=True) as sandbox_session:
            await samples.purge(sandbox_session, employee_id)
    except Exception:  # the practice rows are harmless leftovers; the decision about the person must still stand
        log.exception("could not clear the sandbox of %s", employee_id)


async def skip(session: AsyncSession, employee_id: uuid.UUID, staff: StaffPrincipal, meta: RequestMeta) -> Employee:
    """A manager lets an employee through without the tasks (an experienced person): they work with the real data at
    once. Nothing to do when they are not in training."""
    emp = await _target(session, employee_id, staff)
    row = await load_or_init(session, emp.id)
    if row is not None and row.status == "active":
        before = _counts(row)
        row.status = "skipped"
        row.completed_at = _now()
        row.completed_by = staff.id
        await session.flush()
        await audit(
            session, actor_type="staff", actor_id=staff.id, unit_id=emp.unit_id, action="training.skip", entity="employee", entity_id=emp.id,
            after={"done": before[0], "total": before[1]}, ip=meta.ip, request_id=meta.request_id,
        )  # fmt: skip
        await _purge_sandbox(emp.id)
        await _drop_state(emp.id)
    return emp


async def reset(session: AsyncSession, employee_id: uuid.UUID, staff: StaffPrincipal, meta: RequestMeta) -> Employee:
    """A manager sends an employee (back) to the training: from their next request they work in the sandbox until the
    tasks of their present role are done. 422 `no_tasks` when the role has nothing to practise."""
    emp = await _target(session, employee_id, staff)
    if emp.is_super_admin:
        raise ValidationError(NOTHING_TO_LEARN, code="no_tasks")
    mine = await _tasks_of(session, emp)
    if not mine:
        raise ValidationError(NOTHING_TO_LEARN, code="no_tasks")
    row = await load_or_init(session, emp.id)
    if row is None:
        raise ValidationError(NOTHING_TO_LEARN, code="no_tasks")
    row.status = "active"
    row.tasks = mine
    flag_modified(row, "tasks")
    row.started_at = _now()
    row.completed_at = None
    row.completed_by = None
    row.note = None
    await session.flush()
    await audit(
        session, actor_type="staff", actor_id=staff.id, unit_id=emp.unit_id, action="training.reset", entity="employee", entity_id=emp.id,
        after={"tasks": sorted(mine)}, ip=meta.ip, request_id=meta.request_id,
    )  # fmt: skip
    await _purge_sandbox(emp.id)
    await _drop_state(emp.id)
    return emp
