"""An employee's training: state, tasks, finishing, and what a manager may do about it.

State lives in `public.employee_training` (real data — it says WHO is in the sandbox) and is always read and written
on the real pool, whatever pool the request itself runs on. `is_active` is the hot path of every staff request, so
its answer is kept in Redis for a few minutes and dropped the moment the state changes.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.api.deps import RequestMeta, StaffPrincipal
from app.core.audit import audit
from app.core.config import settings
from app.core.exceptions import AppError, ForbiddenError, StateError, ValidationError
from app.core.permissions import INSTALLER_ROLE_KEY, PLATFORM_PERMISSIONS, resolve_permissions
from app.core.scope import facility_ids_under
from app.infrastructure.db import realm
from app.infrastructure.db.base import alive
from app.infrastructure.db.models import Employee, EmployeeTraining, OrgUnit, Role
from app.infrastructure.db.session import SessionLocal, session_scope
from app.infrastructure.redis.client import get_redis
from app.modules.training import samples, targets
from app.modules.training.schemas import EmployeeTrainingOut, TrainingOut, TrainingTargetOut, TrainingTaskOut
from app.modules.training.tasks import OPEN_ORDER, TASK_BY_KEY, TASKS, UNPAID_ORDER, tasks_for

log = logging.getLogger("app.training")

STATE_KEY = "training:st:{employee_id}"
STATE_TTL = 600  # seconds; every change of state drops the key at once
RECEPTION_TASKS = frozenset({"patient_create", "order_create"})
INCOMPLETE = "Avval barcha o‘quv topshiriqlarini bajaring"
SELF_ERROR = "O‘z o‘quvingizni o‘zingiz o‘zgartira olmaysiz"
NOTHING_TO_LEARN = "Bu xodimning rolida mashq qilinadigan topshiriq yo‘q"
NOTHING_FOR_ME = "Sizning rolingizda mashq qilinadigan topshiriq yo‘q"
UNKNOWN_TASK = "Bunday o‘quv topshirig‘i yo‘q"
NOT_IN_ROLE = "Tanlangan topshiriq bu rolga tegishli emas"
MANDATORY = "Bu o‘quv majburiy: undan faqat topshiriqlarni bajarib yoki rahbar ruxsati bilan chiqiladi"
NOT_READY = "O‘quv muhiti hozircha tayyor emas. Birozdan so‘ng qayta urinib ko‘ring."
#: tasks whose natural beginning is an earlier desk task: when that one is not in the plan, a sample stands in
_DESK_BEFORE_PAY = frozenset({"order_create", "order_add_item"})


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


def plan_of(staff: StaffPrincipal) -> list[str]:
    """Every task the caller's role has now, in the order they are practised — what a training can be made of."""
    if staff.is_super_admin or staff.is_installer:
        return []
    return [t.key for t in TASKS if staff.has(*t.perms)]


def choose(mine: Sequence[str], wanted: Sequence[str] | None) -> tuple[list[str], list[str] | None]:
    """The tasks of a round and what to remember of the choice: `wanted` narrowed to the role's own tasks, in their
    order. Nothing chosen — or everything — is the whole plan (remembered as None). 422 for a key that does not exist
    (`unknown_task`) or that the role does not have (`task_not_in_role`)."""
    asked = list(dict.fromkeys(wanted or []))
    if not asked:
        return list(mine), None
    if any(k not in TASK_BY_KEY for k in asked):
        raise ValidationError(UNKNOWN_TASK, code="unknown_task")
    if any(k not in mine for k in asked):
        raise ValidationError(NOT_IN_ROLE, code="task_not_in_role")
    picked = [k for k in mine if k in asked]
    return picked, (None if len(picked) == len(mine) else picked)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _snapshot(row: EmployeeTraining) -> dict[str, Any]:
    """The row as it stands — what a repeat the employee started themselves gives back when they leave it."""
    return {
        "status": row.status,
        "tasks": dict(row.tasks or {}),
        "chosen": list(row.chosen) if row.chosen else None,
        "startedAt": _iso(row.started_at),
        "completedAt": _iso(row.completed_at),
        "completedBy": str(row.completed_by) if row.completed_by else None,
        "startedBy": str(row.started_by) if row.started_by else None,
        "note": row.note,
        "attempt": row.attempt or 1,
    }


def _restore(row: EmployeeTraining) -> None:
    """Back to how it was before a voluntary repeat (as if it had never been started)."""
    was = row.previous or {}
    row.status = was.get("status") or "exempt"
    row.tasks = dict(was.get("tasks") or {})
    flag_modified(row, "tasks")
    row.chosen = was.get("chosen") or None
    row.started_at = _when(was.get("startedAt")) or row.started_at
    row.completed_at = _when(was.get("completedAt")) or _now()
    row.completed_by = uuid.UUID(was["completedBy"]) if was.get("completedBy") else None
    row.started_by = uuid.UUID(was["startedBy"]) if was.get("startedBy") else None
    row.note = was.get("note")
    row.attempt = int(was.get("attempt") or 1)
    row.previous = None
    row.unavailable = []
    row.reminded_at = None


def _begin(row: EmployeeTraining, keys: Sequence[str], chosen: list[str] | None, by: uuid.UUID | None) -> None:
    """A round starts: these tasks, none done, the clock from now."""
    row.status = "active"
    row.tasks = dict.fromkeys(keys)
    flag_modified(row, "tasks")
    row.chosen = chosen
    row.started_at = _now()
    row.started_by = by
    row.completed_at = None
    row.completed_by = None
    row.note = None
    row.unavailable = []
    row.reminded_at = None


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


def training_out(
    row: EmployeeTraining | None,
    available: dict[str, bool] | None = None,
    *,
    facility: OrgUnit | None = None,
    nowhere: bool = False,
    where: dict[str, TrainingTargetOut] | None = None,
    plan: Sequence[str] = (),
) -> TrainingOut:
    """The caller's own view. `available`: need → is there something to practise it on (None = not looked at);
    `nowhere`: the employee has no facility to practise in at all — nothing can be done, nothing holds them back;
    `where`: task key → the record to continue on; `plan`: every task their role has now."""
    if row is None:
        return TrainingOut(state="exempt", plan=list(plan))
    active = row.status == "active"
    stored = row.tasks or {}

    def _possible(t: Any) -> bool:
        if stored.get(t.key):
            return True
        if nowhere:
            return False
        return True if (available is None or t.needs is None) else bool(available.get(t.needs))

    items = [
        TrainingTaskOut(key=t.key, done=bool(stored.get(t.key)), done_at=_when(stored.get(t.key)), available=_possible(t), target=None if stored.get(t.key) else (where or {}).get(t.key))
        for t in TASKS
        if t.key in stored
    ]
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
        voluntary=active and row.previous is not None,
        partial=bool(row.chosen),
        attempt=row.attempt or 1,
        plan=list(plan),
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


def _round_tasks(mine: Sequence[str], chosen: Sequence[str] | None) -> list[str]:
    """The tasks a round consists of: the role's own, narrowed to what was chosen for it."""
    return [k for k in mine if k in chosen] if chosen else list(mine)


async def _reconcile(staff: StaffPrincipal) -> EmployeeTraining | None:
    """The employee's row with the tasks of the role they hold NOW: a role changed in the middle of the training
    must not leave tasks they may no longer do (they could never finish) — the marks of tasks already done stay,
    and a partial plan stays partial. Nothing left to practise ends the round: a voluntary repeat gives back what
    was before it, anything else becomes `exempt`."""
    mine = plan_of(staff)
    async with SessionLocal() as session:
        row = await load_or_init(session, staff.id)
        if row is None or row.status != "active" or list(row.tasks or {}) == _round_tasks(mine, row.chosen):
            await session.commit()
            return row
        row = (await session.execute(select(EmployeeTraining).where(EmployeeTraining.employee_id == staff.id).with_for_update())).scalar_one()
        if row.status == "active":
            keep = _round_tasks(mine, row.chosen)
            row.tasks = {k: (row.tasks or {}).get(k) for k in keep}
            flag_modified(row, "tasks")
            if not keep:
                if row.previous is not None:
                    _restore(row)
                else:
                    row.status, row.note, row.completed_at = "exempt", "no_tasks", _now()
        await session.commit()
        ended = row.status != "active"
    if ended:
        await _drop_state(staff.id)
    return row


async def _remember_unavailable(row: EmployeeTraining, keys: list[str]) -> None:
    """Keep what the employee was just shown as «cannot be practised here», so a manager sees the same picture (and
    «ready to finish» means the same on both sides). Written only when it changes."""
    keys = sorted(keys)
    if sorted(row.unavailable or []) == keys:
        return
    try:
        async with SessionLocal() as real:
            await real.execute(update(EmployeeTraining).where(EmployeeTraining.employee_id == row.employee_id, EmployeeTraining.status == "active").values(unavailable=keys))
            await real.commit()
    except Exception:  # pragma: no cover — a note for the manager's view; the employee's own answer does not depend on it
        log.exception("could not note the unavailable tasks of %s", row.employee_id)


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
    plan = plan_of(staff)
    row = await _reconcile(staff)
    if row is None or row.status != "active":
        return training_out(row, plan=plan)
    if not realm.is_training():
        # the very request that started the training (or one that raced a reset) still runs on the real data: nothing
        # is seeded there — the next one is in the sandbox
        return training_out(row, plan=plan)
    pending = [TASK_BY_KEY[k] for k, done in (row.tasks or {}).items() if not done and k in TASK_BY_KEY]
    facility = await _facility(session, staff, unit_id)
    if facility is None:
        await _remember_unavailable(row, [t.key for t in pending])
        return training_out(row, nowhere=True, plan=plan)
    keys = {t.key for t in pending}
    needs = {t.needs for t in pending if t.needs}
    # the desk work is a chain — patient, cheque, service, payment. A plan that starts in its middle («only how to
    # take a payment»), or a trainee whose own cheque is gone, is given a cheque to continue with
    if "payment_take" in keys and not (_DESK_BEFORE_PAY & keys):
        needs.add(UNPAID_ORDER)
    elif "order_add_item" in keys and "order_create" not in keys:
        needs.add(OPEN_ORDER)
    available = await samples.ensure(session, staff, facility, needs, patients=any(t.key in RECEPTION_TASKS for t in pending))
    possible = [t for t in pending if t.needs is None or available.get(t.needs)]
    await _remember_unavailable(row, [t.key for t in pending if t not in possible])
    # the very record each remaining task is done on: the checklist leads there, not to a list
    where = await targets.find(session, staff, facility.id, [t.key for t in possible])
    return training_out(row, available, facility=facility, where=where, plan=plan)


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
        own_wish = row.previous is not None
        row.status = "passed"
        row.completed_at = _now()
        row.completed_by = None
        row.previous = None  # finished: there is nothing to go back to any more
        await audit(
            real, actor_type="staff", actor_id=staff.id, unit_id=staff.home_unit_id, action="training.complete", entity="employee", entity_id=staff.id,
            after={"done": out.done, "total": out.total, "attempt": row.attempt or 1, "voluntary": own_wish}, ip=meta.ip, request_id=meta.request_id,
        )  # fmt: skip
        await real.commit()
        result = training_out(row, plan=out.plan)
    await _drop_state(staff.id)
    if not own_wish:
        # their managers (and the superadmins) are told — never at the price of the employee's own «finish».
        # (A repeat the employee chose for themselves is their own business: nobody is told.)
        from app.modules.training import oversight

        await oversight.announce_passed(staff.id)
    return result


async def restart(session: AsyncSession, staff: StaffPrincipal, wanted: Sequence[str] | None, unit_id: uuid.UUID | None, meta: RequestMeta) -> TrainingOut:
    """The employee goes through the training again by their own wish — the whole plan of their role, or only the
    tasks they name. From the next request they work in the sandbox; because it is their own choice they may leave
    at any moment (`leave`) and are then exactly where they were before.

    Someone who is in a MANDATORY training (the first one, or one a manager sent them to) cannot shorten it this
    way: for them this starts the same tasks from the beginning. 422 `no_tasks` when the role has nothing to
    practise; 503 `training_unavailable` when the sandbox is not there (nobody is moved into a place that does not
    exist)."""
    mine = plan_of(staff)
    if not mine or not settings.training_enabled:
        raise ValidationError(NOTHING_FOR_ME, code="no_tasks")
    from app.infrastructure.db.session import engine
    from app.modules.training import sandbox

    if not await sandbox.ensure_ready(engine):
        raise AppError(NOT_READY, code="training_unavailable", status=503)
    async with SessionLocal() as real:
        await load_or_init(real, staff.id)
        row = (await real.execute(select(EmployeeTraining).where(EmployeeTraining.employee_id == staff.id).with_for_update())).scalar_one()
        mandatory = row.status == "active" and row.previous is None
        if mandatory:
            keys, chosen = _round_tasks(mine, row.chosen) or list(mine), row.chosen
            _begin(row, keys, chosen, row.started_by)
        else:
            keys, chosen = choose(mine, wanted)
            if row.status != "active":
                # a repeat begins: remember where to return to (a repeat restarted keeps the FIRST «before»)
                row.previous = _snapshot(row)
                row.attempt = (row.attempt or 1) + 1
            _begin(row, keys, chosen, staff.id)
        await audit(
            real, actor_type="staff", actor_id=staff.id, unit_id=staff.home_unit_id, action="training.restart", entity="employee", entity_id=staff.id,
            after={"tasks": list(keys), "voluntary": not mandatory, "attempt": row.attempt or 1}, ip=meta.ip, request_id=meta.request_id,
        )  # fmt: skip
        await real.commit()
    # a clean start: what was practised before is gone
    if realm.is_training():
        await samples.purge(session, staff.id)
    else:
        await _purge_sandbox(staff.id)
    await _drop_state(staff.id)
    return await status(session, staff, unit_id)


async def leave(session: AsyncSession, staff: StaffPrincipal, meta: RequestMeta) -> TrainingOut:
    """The employee leaves a repeat they started themselves: back to the real data at once, their training as it was
    before. 409 `training_mandatory` for a training that is not theirs to leave; a no-op when not in one."""
    plan = plan_of(staff)
    async with SessionLocal() as real:
        row = (await real.execute(select(EmployeeTraining).where(EmployeeTraining.employee_id == staff.id).with_for_update())).scalar_one_or_none()
        if row is None or row.status != "active":
            await real.commit()
            return training_out(row, plan=plan)
        if row.previous is None:
            raise StateError(MANDATORY, code="training_mandatory")
        done, total = _counts(row)
        _restore(row)
        await audit(
            real, actor_type="staff", actor_id=staff.id, unit_id=staff.home_unit_id, action="training.leave", entity="employee", entity_id=staff.id,
            after={"done": done, "total": total, "back": row.status}, ip=meta.ip, request_id=meta.request_id,
        )  # fmt: skip
        await real.commit()
        result = training_out(row, plan=plan)
    if realm.is_training():
        await samples.purge(session, staff.id)
    else:
        await _purge_sandbox(staff.id)
    await _drop_state(staff.id)
    return result


# ----------------------------------------------------------------------------- a manager's side


def will_get(emp: Employee, role: Role | None) -> list[str]:
    """The tasks an employee gets when their training starts (their role now) — in the order they are practised."""
    if emp.is_super_admin or _is_installer(role):
        return []
    return [t.key for t in tasks_for(_permissions(emp, role))]


def stall_cutoff() -> datetime | None:
    """Someone whose training started before this moment has been in it «too long»; None = nobody is counted."""
    days = settings.training_stall_days
    return _now() - timedelta(days=days) if days > 0 else None


def colleague_out(
    row: EmployeeTraining | None, *, plan: Sequence[str] = (), by_name: str | None = None, started_by_name: str | None = None
) -> EmployeeTrainingOut:
    """A colleague's training as a manager sees it: every task with its mark, and whether only «finish» is left.
    `plan`: every task their role has now — without a row (never signed in) these are the tasks the first sign-in
    will give (none → nothing to practise); with one, what a manager may choose from when sending them back."""
    if row is None:
        if not plan:
            return EmployeeTrainingOut(state="exempt")
        return EmployeeTrainingOut(state="pending", total=len(plan), tasks=[TrainingTaskOut(key=k, done=False) for k in plan], plan=list(plan))
    active = row.status == "active"
    own_wish = active and row.previous is not None
    stored = row.tasks or {}
    # what the employee was last shown as «cannot be practised here» (kept by their own status request)
    off = set(row.unavailable or []) if active else set()
    items = [TrainingTaskOut(key=t.key, done=bool(stored.get(t.key)), done_at=_when(stored.get(t.key)), available=bool(stored.get(t.key)) or t.key not in off) for t in TASKS if t.key in stored]
    times = [i.done_at for i in items if i.done_at is not None]
    cutoff = stall_cutoff()
    return EmployeeTrainingOut(
        state=row.status,  # type: ignore[arg-type]
        done=sum(1 for i in items if i.done),
        total=len(items),
        tasks=items,
        can_complete=active and all(i.done or not i.available for i in items),
        # a repeat the employee chose for themselves is never «overdue»: nobody is waiting for it
        stalled=active and not own_wish and cutoff is not None and row.started_at <= cutoff,
        started_at=row.started_at,
        last_task_at=max(times) if times else None,
        completed_at=row.completed_at,
        completed_by_name=by_name,
        voluntary=own_wish,
        partial=bool(row.chosen),
        attempt=row.attempt or 1,
        started_by_name=None if own_wish else started_by_name,
        plan=list(plan),
    )


async def employee_training_map(session: AsyncSession, employees: Sequence[Employee]) -> dict[uuid.UUID, EmployeeTrainingOut]:
    """Training of a page of employees for their cards and the overview (three queries). Someone who has not signed
    in since the training exists has no row yet: `pending` with the tasks their role will give them — it starts at
    their first sign-in — or `exempt` when the role has nothing to practise."""
    if not employees:
        return {}
    rows = {r.employee_id: r for r in (await session.execute(select(EmployeeTraining).where(EmployeeTraining.employee_id.in_([e.id for e in employees])))).scalars()}
    # who let them through / who sent them (back): a manager — never the employee's own name for a repeat of their own
    by = {x for r in rows.values() for x in (r.completed_by, r.started_by) if x and x != r.employee_id}
    names = dict((await session.execute(select(Employee.id, Employee.full_name).where(Employee.id.in_(by)))).all()) if by else {}
    role_ids = {e.role_id for e in employees if e.role_id}
    roles = {r.id: r for r in (await session.execute(select(Role).where(Role.id.in_(role_ids)))).scalars()} if role_ids else {}
    out: dict[uuid.UUID, EmployeeTrainingOut] = {}
    for e in employees:
        row = rows.get(e.id)
        plan = will_get(e, roles.get(e.role_id))
        if row is None:
            out[e.id] = colleague_out(None, plan=plan)
        else:
            out[e.id] = colleague_out(row, plan=plan, by_name=names.get(row.completed_by) if row.completed_by else None, started_by_name=names.get(row.started_by) if row.started_by else None)
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
    once. A repeat the employee started themselves is simply ended — their training is again what it was before.
    Nothing to do when they are not in training."""
    emp = await _target(session, employee_id, staff)
    row = await load_or_init(session, emp.id)
    if row is not None and row.status == "active":
        before = _counts(row)
        own_wish = row.previous is not None
        if own_wish:
            _restore(row)
        else:
            row.status = "skipped"
            row.completed_at = _now()
            row.completed_by = staff.id
        await session.flush()
        await audit(
            session, actor_type="staff", actor_id=staff.id, unit_id=emp.unit_id, action="training.skip", entity="employee", entity_id=emp.id,
            after={"done": before[0], "total": before[1], "back": row.status if own_wish else None}, ip=meta.ip, request_id=meta.request_id,
        )  # fmt: skip
        await _purge_sandbox(emp.id)
        await _drop_state(emp.id)
    return emp


async def reset(session: AsyncSession, employee_id: uuid.UUID, staff: StaffPrincipal, meta: RequestMeta, wanted: Sequence[str] | None = None) -> Employee:
    """A manager sends an employee (back) to the training: from their next request they work in the sandbox until the
    tasks are done — the whole plan of their present role, as on the first day, or only the tasks named (`wanted`).
    It is mandatory: the employee cannot leave it by themselves. 422 `no_tasks` when the role has nothing to
    practise, `unknown_task` / `task_not_in_role` for a wrong choice."""
    emp = await _target(session, employee_id, staff)
    if emp.is_super_admin:
        raise ValidationError(NOTHING_TO_LEARN, code="no_tasks")
    role = await session.get(Role, emp.role_id) if emp.role_id else None
    mine = will_get(emp, role)
    if not mine:
        raise ValidationError(NOTHING_TO_LEARN, code="no_tasks")
    keys, chosen = choose(mine, wanted)
    known = await session.get(EmployeeTraining, emp.id) is not None
    row = await load_or_init(session, emp.id)
    if row is None:
        raise ValidationError(NOTHING_TO_LEARN, code="no_tasks")
    _begin(row, keys, chosen, staff.id)
    row.previous = None  # a manager's decision: not the employee's to leave
    if known:
        row.attempt = (row.attempt or 1) + 1
    await session.flush()
    await audit(
        session, actor_type="staff", actor_id=staff.id, unit_id=emp.unit_id, action="training.reset", entity="employee", entity_id=emp.id,
        after={"tasks": list(keys), "attempt": row.attempt or 1}, ip=meta.ip, request_id=meta.request_id,
    )  # fmt: skip
    await _purge_sandbox(emp.id)
    await _drop_state(emp.id)
    return emp
