"""Running the assistant's scheduled tasks («Rejali topshiriqlar»).

A run is an ordinary turn of the chat loop with three differences, all on the safe side:
* it acts as the task's OWNER with the rights they hold at that very moment — the principal is built fresh, so a
  role taken away, a unit moved out of their territory or a deactivated account stops the task (it is switched off
  and the owner is told why), it never keeps an old door open;
* nobody is there to say yes or to answer: the run is offered read and file tools only (`service._start(scheduled=…)`),
  no write is ever proposed, no question asked;
* it lands in the task's own conversation, marked unread, with an in-app notice for the owner.

The worker (`run_due_once`, every 30 s under a Redis lease) first CLAIMS a due task — moves its next run forward
under a row lock, in a transaction of its own — and only then runs it: a crash, a timeout or two workers at once
can never run the same occurrence twice or loop on it. Failures are counted; three in a row switch the task off."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import RequestMeta, StaffPrincipal, build_staff_principal
from app.core.exceptions import AppError, AuthError, ForbiddenError
from app.core.permissions import ASSISTANT_PERMISSION
from app.core.timeutil import utcnow
from app.infrastructure.db.base import alive
from app.infrastructure.db.models import AssistantSchedule, AssistantThread
from app.infrastructure.db.session import session_scope
from app.modules.assistant import schedule as sch
from app.modules.assistant import service as svc
from app.modules.assistant.schemas import ScheduleOut
from app.modules.messaging import service as messaging_svc

log = logging.getLogger("assistant.schedule")

#: one run may take this long (the model call included) before it is given up as failed
RUN_TIMEOUT_SECONDS = 300
#: failures in a row after which a task is switched off
MAX_FAILS = 3
#: tasks one tick takes on
TICK_BATCH = 5
#: where a notice leads — the web app turns it into its own assistant page (staff app or admin panel)
THREAD_LINK = "/assistant?c={thread_id}"
TASKS_LINK = "/assistant?tasks=1"
PREVIEW_CHARS = 280
MSG_NO_RIGHT = "Yordamchidan foydalanish huquqi olib qo‘yilgan"
MSG_NO_UNIT = "Topshiriq birligi endi hududingizga kirmaydi"
MSG_NO_OWNER = "Egasining hisobi faol emas"
MSG_TIMEOUT = "Javob kutish vaqti tugadi"
MSG_INTERNAL = "Ichki xato"


class _Pause(Exception):  # noqa: N818 — a verdict, not a failure
    """The task cannot run for its owner any more — it is switched off until they switch it back on."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _now_words(tz: ZoneInfo) -> str:
    local = datetime.now(tz)
    return f"{local.strftime('%Y-%m-%d %H:%M')}, {sch.WEEKDAYS_UZ[local.isoweekday() - 1]}, vaqt mintaqasi {tz.key}"


def _preview(text: str) -> str:
    flat = " ".join((text or "").split())
    return (flat[:PREVIEW_CHARS] + "…") if len(flat) > PREVIEW_CHARS else (flat or "Topshiriq bajarildi — natija suhbatda.")


async def _thread_for(session: AsyncSession, row: AssistantSchedule, staff: StaffPrincipal) -> AssistantThread:
    """The task's own conversation; one the owner deleted is not brought back — a new one starts."""
    thread = None
    if row.thread_id:
        thread = (await session.execute(select(AssistantThread).where(AssistantThread.id == row.thread_id, alive(AssistantThread)))).scalar_one_or_none()
        if thread is not None and thread.employee_id != staff.id:
            thread = None
    if thread is None:
        thread = AssistantThread(employee_id=staff.id, unit_id=row.unit_id, title=f"{sch.THREAD_MARK}{row.title}"[:200], schedule_id=row.id, unread=False)
        session.add(thread)
        await session.flush()
        await session.refresh(thread)
        row.thread_id = thread.id
    return thread


async def _execute(session: AsyncSession, row: AssistantSchedule, staff: StaffPrincipal, *, attended: bool) -> str:
    """One run inside the caller's transaction → what the assistant answered. `attended` = the owner asked for it
    right now and is waiting (nothing to mark unread, a one-time task keeps its appointed time)."""
    if not staff.has(ASSISTANT_PERMISSION):
        raise _Pause(MSG_NO_RIGHT)
    if not staff.can_see(row.unit_id):
        raise _Pause(MSG_NO_UNIT)
    thread = await _thread_for(session, row, staff)
    meta = RequestMeta(ip=None, request_id=f"schedule-{row.id.hex[:12]}", user_agent="assistant-scheduler")
    run = await svc._start(session, staff, meta, thread, row.unit_id, scheduled=_now_words(sch.zone(row.timezone)))
    # a write the owner left unanswered in this conversation is declined — the task's turn may not sit on top of it
    await svc.drop_pendings(session, run)
    await run.add(role="user", content=row.prompt, usage={"schedule": str(row.id)})
    await run.loop()
    now = utcnow()
    thread.last_message_at = now
    thread.unit_id = row.unit_id
    thread.unread = not attended
    row.last_run_at = now
    row.last_status = "ok"
    row.last_error = None
    row.run_count = (row.run_count or 0) + 1
    row.fail_count = 0
    if row.kind == "once" and not attended:
        row.is_active = False
        row.next_run_at = None
    await session.flush()
    said = [m.content for m in run.new if m.role == "assistant" and (m.content or "").strip() and not (m.usage or {}).get("stopped")]
    return said[-1] if said else ""


async def run_now(session: AsyncSession, schedule_id: uuid.UUID, staff: StaffPrincipal) -> ScheduleOut:
    """The owner runs their task right now (a try-out, or yesterday's report once more) — the same run as the
    scheduled one, read-only tools included; its appointed times do not move."""
    staff.require(ASSISTANT_PERMISSION)
    row = await sch.own_or_404(session, schedule_id, staff)
    await svc._rate_limit(staff)
    try:
        await _execute(session, row, staff, attended=True)
    except _Pause as p:
        raise ForbiddenError(p.reason, code="schedule_unavailable") from None
    return await sch.get_schedule(session, row.id, staff)


async def _owner(session: AsyncSession, row: AssistantSchedule) -> StaffPrincipal:
    """The owner as they are right now: role, permissions, territory — read fresh, never remembered by the task."""
    try:
        return await build_staff_principal(session, row.employee_id, f"schedule:{row.id}", utcnow() + timedelta(hours=1))
    except (AuthError, ForbiddenError):
        raise _Pause(MSG_NO_OWNER) from None


async def run_one(schedule_id: uuid.UUID) -> str | None:
    """Claim one due task and run it. Returns 'ok' / 'failed' / 'paused', or None when it was not due (any more)."""
    now = utcnow()
    async with session_scope() as s:
        stmt = select(AssistantSchedule).where(AssistantSchedule.id == schedule_id, alive(AssistantSchedule), AssistantSchedule.is_active.is_(True), AssistantSchedule.next_run_at <= now).with_for_update(skip_locked=True)
        row = (await s.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        # claimed: this occurrence is spent whatever happens next (a `once` has nothing ahead)
        row.next_run_at = sch.due(row, now)

    failure: tuple[str, bool] | None = None
    try:
        async with session_scope() as s:
            row = await s.get(AssistantSchedule, schedule_id)
            if row is None or row.deleted_at is not None:
                return None
            staff = await _owner(s, row)
            async with asyncio.timeout(RUN_TIMEOUT_SECONDS):
                text = await _execute(s, row, staff, attended=False)
            await messaging_svc.notify(s, unit_id=None, employee_id=row.employee_id, title=f"{sch.THREAD_MARK}{row.title}"[:200], body=_preview(text), kind="success", link=THREAD_LINK.format(thread_id=row.thread_id))
        return "ok"
    except _Pause as p:
        failure = (p.reason, True)
    except TimeoutError:
        failure = (MSG_TIMEOUT, False)
    except AppError as e:
        failure = (e.message, False)
    except Exception:  # a task must never take the worker down
        log.exception("scheduled task %s failed", schedule_id)
        failure = (MSG_INTERNAL, False)

    # the run's own transaction is gone with everything it wrote — the outcome is kept in a new one
    reason, pause = failure
    async with session_scope() as s:
        row = await s.get(AssistantSchedule, schedule_id)
        if row is None:
            return "failed"
        row.last_run_at = now
        row.last_status = "failed"
        row.last_error = reason[:500]
        row.fail_count = (row.fail_count or 0) + 1
        off = pause or row.fail_count >= MAX_FAILS or row.kind == "once"
        if off:
            row.is_active = False
            row.next_run_at = None
            row.paused_reason = (reason if pause or row.kind == "once" else f"{row.fail_count} marta ketma-ket bajarilmadi: {reason}")[:500]
        body = reason + (" Topshiriq o‘chirib qo‘yildi — sababini bartaraf etib, «Rejali topshiriqlar»dan qayta yoqing." if off else " Keyingi safar yana urinib ko‘riladi.")
        await messaging_svc.notify(s, unit_id=None, employee_id=row.employee_id, title=f"{sch.THREAD_MARK}{row.title} — bajarilmadi"[:200], body=body, kind="warning", link=TASKS_LINK)
    return "paused" if off else "failed"


async def run_due_once() -> dict[str, int]:
    """One tick of the worker: the tasks that are due, oldest first, one after another."""
    now = utcnow()
    async with session_scope() as s:
        stmt = select(AssistantSchedule.id).where(alive(AssistantSchedule), AssistantSchedule.is_active.is_(True), AssistantSchedule.next_run_at <= now).order_by(AssistantSchedule.next_run_at.asc()).limit(TICK_BATCH)
        ids = list((await s.execute(stmt)).scalars().all())
    out = {"due": len(ids), "ok": 0, "failed": 0, "paused": 0}
    for sid in ids:
        res = await run_one(sid)
        if res:
            out[res] += 1
    if ids:
        log.info("scheduled tasks: %s", out)
    return out
