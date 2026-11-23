"""The assistant's tools for the employee's own scheduled tasks («Rejali topshiriqlar»): «har kuni ertalab kechagi
hisobotni yubor» becomes a task the employee confirms once; from then on it runs by itself.

The tools only keep the list — a task is the owner's (nobody else's exists for them), creating or changing one is a
write that waits for their yes like any other. What a RUN may do is decided at run time in `schedule_runner`."""

from __future__ import annotations

from typing import Any

from app.core.permissions import ASSISTANT_PERMISSION
from app.modules.assistant import schedule as sch
from app.modules.assistant.schemas import ScheduleIn
from app.modules.assistant.tools import ToolContext, _dump, _scope, tool

#: the tasks are the employee's own: the key of the assistant itself is the key to them (a leader role holds it too)
OWN = (ASSISTANT_PERMISSION,)
SCHEDULE_KEYS = ("id", "title", "prompt", "kind", "time", "weekdays", "monthDay", "runDate", "when", "timezone", "unitId", "unitName", "isActive", "pausedReason", "finished", "nextRunAt", "lastRunAt", "lastStatus", "lastError", "runCount")
RULE = {
    "kind": {"type": "string", "enum": list(sch.KINDS), "description": "daily = every day; weekly = on `weekdays` (working days = [1,2,3,4,5]); monthly = on `monthDay`; once = one time on `runDate`"},
    "time": {"type": "string", "description": "HH:MM (24h) in the unit's local time — «ertalab» without an hour = 08:00"},
    "weekdays": {"type": "array", "items": {"type": "integer"}, "description": "weekly only: 1 = Monday … 7 = Sunday"},
    "monthDay": {"type": "integer", "description": "monthly only: 1–31 (a shorter month runs on its last day)"},
    "runDate": {"type": "string", "description": "once only: YYYY-MM-DD"},
}


@tool("list_schedules", "Rejali topshiriqlarim", "The employee's own scheduled tasks: what each one asks, when it runs (`when`, `nextRunAt`), how the last run went, whether it is switched off and why.", perms=OWN, group="schedule")
async def list_schedules(ctx: ToolContext, args: dict[str, Any]) -> Any:
    out = await sch.list_schedules(ctx.session, ctx.staff)
    return {"limit": out.limit, "items": [_dump(x, SCHEDULE_KEYS) for x in out.items]}


@tool("create_schedule", "Rejali topshiriq yaratish",
      "Leave a task for the assistant to repeat on a schedule for this employee — «har kuni ertalab 8 da kechagi hisobotni yubor», «har dushanba o‘tgan hafta tushumini chiqar», «ertaga 9 da eslat». "
      "`prompt` is what YOU will be asked at every run, with nobody there to answer questions: write it complete and self-contained in the user's language — what to prepare, for which period (keep it relative: «kechagi kun», «o‘tgan hafta», "
      "each run knows its own date), which figures or file. `title` is a short name. A run works with read-only and file tools and the employee's rights of that moment; the answer lands in the task's own conversation and as a notification. "
      f"At most {sch.MAX_SCHEDULES} tasks per employee. Ask for the time if the user gave none and it cannot be guessed.",
      params={"title": {"type": "string", "req": True}, "prompt": {"type": "string", "req": True}, **{k: ({**v, "req": True} if k in ("kind", "time") else v) for k, v in RULE.items()}, "unitId": {"type": "string", "description": "the unit the runs work in (default: the working unit)"}},
      kind="write", perms=OWN, summary_keys=("title",), group="schedule")
async def create_schedule(ctx: ToolContext, args: dict[str, Any]) -> Any:
    scope = await _scope(ctx, args.get("unitId"))
    body = ScheduleIn.model_validate({**args, "unitId": str(scope.id)})
    return _dump(await sch.create_schedule(ctx.session, ctx.staff, body, scope.id, ctx.meta), SCHEDULE_KEYS)


@tool("update_schedule", "Rejali topshiriqni o‘zgartirish", "Change one of the employee's scheduled tasks (scheduleId from list_schedules): its name, what it asks, when it runs — or switch it off / back on with isActive. Only the given fields change.",
      params={"scheduleId": {"type": "string", "req": True}, "title": {"type": "string"}, "prompt": {"type": "string"}, **RULE, "isActive": {"type": "boolean", "description": "false = pause, true = resume"}},
      kind="write", perms=OWN, summary_keys=("scheduleId",), group="schedule")
async def update_schedule(ctx: ToolContext, args: dict[str, Any]) -> Any:
    changes = {k: v for k, v in args.items() if k != "scheduleId"}
    return _dump(await sch.update_schedule(ctx.session, args["scheduleId"], ctx.staff, changes, ctx.meta), SCHEDULE_KEYS)


@tool("delete_schedule", "Rejali topshiriqni o‘chirish", "Delete one of the employee's scheduled tasks for good (to stop it for a while use update_schedule with isActive=false). The conversation with what it produced stays.",
      params={"scheduleId": {"type": "string", "req": True}}, kind="write", perms=OWN, summary_keys=("scheduleId",), group="schedule")
async def delete_schedule(ctx: ToolContext, args: dict[str, Any]) -> Any:
    await sch.delete_schedule(ctx.session, args["scheduleId"], ctx.staff, ctx.meta)
    return {"deleted": True}
