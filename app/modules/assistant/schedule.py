"""Scheduled tasks of the assistant («Rejali topshiriqlar»): the rule that says when a task is due, and the owner's
own list of tasks. A task belongs to one employee; nobody else — not even a superadmin — reads or changes it here.

Nothing in this module talks to a model: the run itself lives in `schedule_runner` (it needs the chat loop). What a
run may do is decided THERE, at run time, from the owner's rights as they are at that moment — a task never keeps
a right its owner has lost."""

from __future__ import annotations

import calendar
import uuid
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import RequestMeta, StaffPrincipal
from app.core.audit import audit
from app.core.config import settings
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.permissions import ASSISTANT_PERMISSION
from app.core.timeutil import DEFAULT_TZ, utcnow
from app.infrastructure.db.base import alive
from app.infrastructure.db.models import AssistantSchedule, AssistantThread
from app.modules.assistant.schemas import ScheduleIn, ScheduleListOut, ScheduleOut
from app.modules.units import repository as units_repo
from app.modules.units import service as units_svc

KINDS = ("daily", "weekly", "monthly", "once")
#: how many tasks one employee may keep — every run is a paid model call
MAX_SCHEDULES = 10
#: a one-time task may be set this far ahead
ONCE_HORIZON = timedelta(days=366)
WEEKDAYS_UZ = ("dushanba", "seshanba", "chorshanba", "payshanba", "juma", "shanba", "yakshanba")
THREAD_MARK = "⏰ "
#: the wire names of the fields a task is made of (what `update_schedule` may change, what an undo puts back)
FIELDS = ("title", "prompt", "kind", "time", "weekdays", "monthDay", "runDate", "unitId", "isActive")


def zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name) if name else DEFAULT_TZ
    except (KeyError, ValueError):
        return DEFAULT_TZ


def _clock(at_time: str) -> clock:
    hh, mm = at_time.split(":")
    return clock(int(hh), int(mm))


def next_run(kind: str, at_time: str, weekdays: list[int] | None, month_day: int | None, run_date: date | None, tz: ZoneInfo, after: datetime) -> datetime | None:
    """The first moment strictly after `after` the rule points at, in UTC; None = never again (a `once` in the past).
    The time is wall-clock time in `tz`; a month shorter than `month_day` runs on its last day."""
    at = _clock(at_time)
    local = after.astimezone(tz)

    def moment(day: date) -> datetime:
        return datetime.combine(day, at, tzinfo=tz).astimezone(UTC)

    if kind == "once":
        if run_date is None:
            return None
        m = moment(run_date)
        return m if m > after else None
    if kind == "monthly":
        want = int(month_day or 1)
        year, month = local.year, local.month
        for _ in range(14):
            m = moment(date(year, month, min(want, calendar.monthrange(year, month)[1])))
            if m > after:
                return m
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        return None
    days = set(weekdays or []) if kind == "weekly" else set(range(1, 8))
    for offset in range(9):
        day = local.date() + timedelta(days=offset)
        if day.isoweekday() in days:
            m = moment(day)
            if m > after:
                return m
    return None


def when_text(kind: str, at_time: str, weekdays: list[int] | None, month_day: int | None, run_date: date | None) -> str:
    """The rule in words, as the owner reads it on the card and in the list."""
    if kind == "once":
        return f"Bir marta: {run_date.strftime('%d.%m.%Y') if run_date else '—'} {at_time}"
    if kind == "monthly":
        return f"Har oyning {month_day}-kuni {at_time}"
    if kind == "weekly":
        days = sorted(set(weekdays or []))
        if days == [1, 2, 3, 4, 5]:
            return f"Ish kunlari (dushanba–juma) {at_time}"
        if len(days) == 7:
            return f"Har kuni {at_time}"
        return f"Har {', '.join(WEEKDAYS_UZ[d - 1] for d in days)} {at_time}"
    return f"Har kuni {at_time}"


def _rule(kind: str, at_time: str, weekdays: list[int] | None, month_day: int | None, run_date: date | None, tz: ZoneInfo, now: datetime, *, ahead: bool = True) -> tuple[list[int], int | None, date | None]:
    """The rule as it is kept: only the parts its kind uses, checked. `ahead` = a one-time task must still lie in
    the future (not asked of a task that is switched off — it keeps the day it ran on)."""
    if kind not in KINDS:
        raise ValidationError("Takrorlanish turi noto‘g‘ri", code="schedule_kind", details={"field": "kind"})
    try:
        _clock(at_time)
    except (ValueError, AttributeError):
        raise ValidationError("Vaqt HH:MM ko‘rinishida bo‘lishi kerak", code="schedule_time", details={"field": "time"}) from None
    if kind == "weekly":
        days = sorted({int(d) for d in weekdays or []})
        if not days or any(d < 1 or d > 7 for d in days):
            raise ValidationError("Hafta kunlarini tanlang (1 = dushanba … 7 = yakshanba)", code="schedule_weekdays", details={"field": "weekdays"})
        return days, None, None
    if kind == "monthly":
        if not month_day or not 1 <= int(month_day) <= 31:
            raise ValidationError("Oyning kunini kiriting (1–31)", code="schedule_month_day", details={"field": "monthDay"})
        return [], int(month_day), None
    if kind == "once":
        if run_date is None:
            raise ValidationError("Sanani kiriting", code="schedule_date", details={"field": "runDate"})
        at = next_run("once", at_time, None, None, run_date, tz, now)
        if not ahead:
            return [], None, run_date
        if at is None:
            raise ValidationError("Bu vaqt o‘tib ketgan — kelajakdagi sana va vaqtni kiriting", code="schedule_past", details={"field": "runDate"})
        if at - now > ONCE_HORIZON:
            raise ValidationError("Sana juda uzoq — bir yil ichidagi sanani kiriting", code="schedule_far", details={"field": "runDate"})
        return [], None, run_date
    return [], None, None


def due(row: AssistantSchedule, after: datetime) -> datetime | None:
    return next_run(row.kind, row.at_time, row.weekdays, row.month_day, row.run_date, zone(row.timezone), after)


def _snapshot(row: AssistantSchedule) -> dict[str, Any]:
    return {
        "title": row.title, "prompt": row.prompt, "kind": row.kind, "time": row.at_time, "weekdays": list(row.weekdays or []), "monthDay": row.month_day,
        "runDate": row.run_date.isoformat() if row.run_date else None, "unitId": str(row.unit_id), "isActive": row.is_active,
    }


def out_of(row: AssistantSchedule, unit_name: str | None = None) -> ScheduleOut:
    return ScheduleOut(
        id=str(row.id), title=row.title, prompt=row.prompt, kind=row.kind, time=row.at_time, weekdays=list(row.weekdays or []), month_day=row.month_day, run_date=row.run_date,  # type: ignore[arg-type]
        timezone=row.timezone, when=when_text(row.kind, row.at_time, row.weekdays, row.month_day, row.run_date), unit_id=str(row.unit_id), unit_name=unit_name,
        thread_id=str(row.thread_id) if row.thread_id else None, is_active=row.is_active, paused_reason=row.paused_reason,
        finished=row.kind == "once" and not row.is_active and row.run_count > 0 and not row.paused_reason, next_run_at=row.next_run_at if row.is_active else None,
        last_run_at=row.last_run_at, last_status=row.last_status, last_error=row.last_error, run_count=row.run_count, created_at=row.created_at,  # type: ignore[arg-type]
    )


async def _outs(session: AsyncSession, rows: list[AssistantSchedule]) -> list[ScheduleOut]:
    names = {u.id: u.name for u in (await units_repo.units_by_ids(session, list({r.unit_id for r in rows}))).values()} if rows else {}
    return [out_of(r, names.get(r.unit_id)) for r in rows]


async def own_or_404(session: AsyncSession, schedule_id: uuid.UUID | str, staff: StaffPrincipal) -> AssistantSchedule:
    """The caller's own task — somebody else's does not exist for them."""
    try:
        sid = schedule_id if isinstance(schedule_id, uuid.UUID) else uuid.UUID(str(schedule_id))
    except ValueError:
        raise NotFoundError("Rejali topshiriq topilmadi") from None
    row = (await session.execute(select(AssistantSchedule).where(AssistantSchedule.id == sid, alive(AssistantSchedule)))).scalar_one_or_none()
    if row is None or row.employee_id != staff.id:
        raise NotFoundError("Rejali topshiriq topilmadi")
    return row


async def list_schedules(session: AsyncSession, staff: StaffPrincipal) -> ScheduleListOut:
    staff.require(ASSISTANT_PERMISSION)
    rows = (await session.execute(select(AssistantSchedule).where(AssistantSchedule.employee_id == staff.id, alive(AssistantSchedule)).order_by(AssistantSchedule.created_at.asc(), AssistantSchedule.id.asc()))).scalars().all()
    return ScheduleListOut(items=await _outs(session, list(rows)), limit=MAX_SCHEDULES, running=bool(settings.workers_enabled))


async def get_schedule(session: AsyncSession, schedule_id: uuid.UUID | str, staff: StaffPrincipal) -> ScheduleOut:
    staff.require(ASSISTANT_PERMISSION)
    return (await _outs(session, [await own_or_404(session, schedule_id, staff)]))[0]


async def _unit(session: AsyncSession, staff: StaffPrincipal, unit_id: Any) -> tuple[uuid.UUID, str]:
    """The unit a task works in — inside the owner's territory — and its time zone."""
    unit = await units_svc.get_unit_or_404(session, unit_id)
    staff.require_unit(unit.id)
    return unit.id, zone(unit.timezone).key


async def create_schedule(session: AsyncSession, staff: StaffPrincipal, body: ScheduleIn, default_unit: uuid.UUID | None, meta: RequestMeta | None = None) -> ScheduleOut:
    staff.require(ASSISTANT_PERMISSION)
    kept = (await session.execute(select(func.count()).select_from(AssistantSchedule).where(AssistantSchedule.employee_id == staff.id, alive(AssistantSchedule)))).scalar_one()
    if kept >= MAX_SCHEDULES:
        raise ConflictError(f"Rejali topshiriqlar soni chegaraga yetdi ({MAX_SCHEDULES} ta) — keraksizini o‘chiring", code="schedule_limit")
    target = body.unit_id or default_unit or staff.home_unit_id
    if not target:
        raise ValidationError("Ish birligi tanlanmagan", code="unit_required", details={"field": "unitId"})
    unit_id, tz_name = await _unit(session, staff, target)
    now = utcnow()
    days, month_day, run_date = _rule(body.kind, body.time, body.weekdays, body.month_day, body.run_date, zone(tz_name), now)
    row = AssistantSchedule(
        employee_id=staff.id, unit_id=unit_id, title=body.title.strip()[:200], prompt=body.prompt.strip(), kind=body.kind, at_time=body.time, weekdays=days, month_day=month_day,
        run_date=run_date, timezone=tz_name, is_active=body.is_active, run_count=0, fail_count=0,
    )
    row.next_run_at = due(row, now) if row.is_active else None
    session.add(row)
    await session.flush()
    await session.refresh(row)
    await audit(session, actor_type="staff", actor_id=staff.id, unit_id=unit_id, action="assistant_schedule_create", entity="assistant_schedule", entity_id=row.id, after=_snapshot(row), ip=meta.ip if meta else None, request_id=meta.request_id if meta else None)
    return (await _outs(session, [row]))[0]


async def update_schedule(session: AsyncSession, schedule_id: uuid.UUID | str, staff: StaffPrincipal, changes: dict[str, Any], meta: RequestMeta | None = None) -> ScheduleOut:
    """Apply `changes` (wire names; a key that is present changes its field, None clears what may be empty).
    Switching a task back on forgets why the system paused it and counts its failures from zero."""
    staff.require(ASSISTANT_PERMISSION)
    row = await own_or_404(session, schedule_id, staff)
    unknown = sorted(set(changes) - set(FIELDS))
    if unknown:
        raise ValidationError(f"Bunday maydon yo‘q: {', '.join(unknown)}", code="unknown_argument")
    before = _snapshot(row)
    now = utcnow()
    if changes.get("title") is not None:
        title = str(changes["title"]).strip()[:200]
        if not title:
            raise ValidationError("Nomi bo‘sh bo‘lmasin", details={"field": "title"})
        if row.thread_id and title != row.title:
            thread = await session.get(AssistantThread, row.thread_id)
            if thread is not None and thread.title == f"{THREAD_MARK}{row.title}"[:200]:
                thread.title = f"{THREAD_MARK}{title}"[:200]  # the conversation follows the task's name while nobody renamed it
        row.title = title
    if changes.get("prompt") is not None:
        prompt = str(changes["prompt"]).strip()
        if len(prompt) < 3 or len(prompt) > 4000:
            raise ValidationError("Topshiriq matni 3–4000 belgi bo‘lishi kerak", details={"field": "prompt"})
        row.prompt = prompt
    if changes.get("unitId"):
        row.unit_id, row.timezone = await _unit(session, staff, changes["unitId"])
    ruled = any(k in changes for k in ("kind", "time", "weekdays", "monthDay", "runDate", "unitId"))
    if ruled:
        kind = changes.get("kind") or row.kind
        raw_date = changes["runDate"] if "runDate" in changes else row.run_date
        if isinstance(raw_date, str):
            try:
                raw_date = date.fromisoformat(raw_date[:10])
            except ValueError:
                raise ValidationError("Sana YYYY-MM-DD ko‘rinishida bo‘lishi kerak", code="schedule_date", details={"field": "runDate"}) from None
        active = changes["isActive"] if changes.get("isActive") is not None else row.is_active
        days, month_day, run_date = _rule(
            kind, changes.get("time") or row.at_time, changes["weekdays"] if "weekdays" in changes else row.weekdays, changes["monthDay"] if "monthDay" in changes else row.month_day,
            raw_date, zone(row.timezone), now, ahead=bool(active),
        )
        row.kind, row.at_time, row.weekdays, row.month_day, row.run_date = kind, changes.get("time") or row.at_time, days, month_day, run_date
    if changes.get("isActive") is not None:
        turned_on = bool(changes["isActive"]) and not row.is_active
        row.is_active = bool(changes["isActive"])
        if turned_on:
            row.paused_reason = None
            row.fail_count = 0
    if row.is_active:
        row.next_run_at = due(row, now)
        if row.next_run_at is None:
            raise ValidationError("Bu vaqt o‘tib ketgan — yangi sana va vaqtni kiriting", code="schedule_past", details={"field": "runDate"})
    else:
        row.next_run_at = None
    await session.flush()
    await session.refresh(row)
    await audit(session, actor_type="staff", actor_id=staff.id, unit_id=row.unit_id, action="assistant_schedule_update", entity="assistant_schedule", entity_id=row.id, before=before, after=_snapshot(row), ip=meta.ip if meta else None, request_id=meta.request_id if meta else None)
    return (await _outs(session, [row]))[0]


async def delete_schedule(session: AsyncSession, schedule_id: uuid.UUID | str, staff: StaffPrincipal, meta: RequestMeta | None = None) -> None:
    """The task goes; the conversation with what it had produced stays with its owner."""
    staff.require(ASSISTANT_PERMISSION)
    row = await own_or_404(session, schedule_id, staff)
    before = _snapshot(row)
    row.deleted_at = utcnow()
    row.deleted_by = staff.id
    row.is_active = False
    row.next_run_at = None
    await session.flush()
    await audit(session, actor_type="staff", actor_id=staff.id, unit_id=row.unit_id, action="assistant_schedule_delete", entity="assistant_schedule", entity_id=row.id, before=before, ip=meta.ip if meta else None, request_id=meta.request_id if meta else None)
