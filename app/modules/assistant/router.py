"""Assistant endpoints: status, threads, messages, the confirmation of proposed writes, and a unit's model account."""

from __future__ import annotations

import uuid
from urllib.parse import quote

from fastapi import APIRouter, Query, Response

from app.api.deps import DbSession, Meta, Staff
from app.core.exceptions import ForbiddenError
from app.modules.assistant import schedule, schedule_runner, service
from app.modules.assistant.schemas import (
    AssistantAccountIn,
    AssistantAccountOut,
    AssistantStatusOut,
    AssistantTestOut,
    BatchIn,
    BatchUndoIn,
    ConfirmIn,
    ScheduleIn,
    ScheduleListOut,
    ScheduleOut,
    ScheduleUpdateIn,
    SendIn,
    SendOut,
    StopOut,
    ThreadCreateIn,
    ThreadDetailOut,
    ThreadOut,
    ThreadUpdateIn,
)

router = APIRouter()
SETTINGS_WRITE = ("admin.settings.write",)
SETTINGS_READ = ("admin.settings.write", "admin.unit.read")


@router.get("/assistant/status", response_model=AssistantStatusOut, summary="Whether the assistant works for the caller here, with which model, and the tools they may use")
async def status(staff: Staff, session: DbSession, unit_id: str | None = Query(default=None, alias="unitId")) -> AssistantStatusOut:
    return await service.status(session, staff, unit_id)


@router.get("/assistant/threads", response_model=list[ThreadOut], summary="The caller's conversations")
async def list_threads(staff: Staff, session: DbSession) -> list[ThreadOut]:
    return await service.list_threads(session, staff)


@router.post("/assistant/threads", response_model=ThreadOut, status_code=201, summary="Start a conversation")
async def create_thread(staff: Staff, session: DbSession, body: ThreadCreateIn | None = None, unit_id: str | None = Query(default=None, alias="unitId")) -> ThreadOut:
    return await service.create_thread(session, staff, body.title if body else None, unit_id)


@router.get("/assistant/threads/{thread_id}", response_model=ThreadDetailOut, summary="A conversation with its turns and the write waiting for a yes")
async def get_thread(thread_id: uuid.UUID, staff: Staff, session: DbSession) -> ThreadDetailOut:
    return await service.get_thread(session, thread_id, staff)


@router.patch("/assistant/threads/{thread_id}", response_model=ThreadOut, summary="Rename a conversation")
async def rename_thread(thread_id: uuid.UUID, body: ThreadUpdateIn, staff: Staff, session: DbSession) -> ThreadOut:
    return await service.rename_thread(session, thread_id, staff, body.title)


@router.delete("/assistant/threads/{thread_id}", status_code=204, summary="Delete a conversation")
async def delete_thread(thread_id: uuid.UUID, staff: Staff, session: DbSession) -> None:
    await service.delete_thread(session, thread_id, staff)


@router.post("/assistant/threads/{thread_id}/messages", response_model=SendOut, summary="Send a message: the model answers, runs read tools, and proposes writes for confirmation")
async def send_message(thread_id: uuid.UUID, body: SendIn, staff: Staff, session: DbSession, meta: Meta) -> SendOut:
    return await service.send_message(session, thread_id, body, staff, meta)


@router.post("/assistant/actions/{action_id}/confirm", response_model=SendOut, summary="Run a write the model proposed — with the caller's own rights")
async def confirm_action(action_id: uuid.UUID, staff: Staff, session: DbSession, meta: Meta, body: ConfirmIn | None = None) -> SendOut:
    return await service.confirm_action(session, action_id, staff, meta, body.confirm if body else None)


@router.post("/assistant/threads/{thread_id}/stop", response_model=StopOut, summary="Stop what the assistant is doing in the thread right now (the running request answers with what it had done)")
async def stop_thread(thread_id: uuid.UUID, staff: Staff, session: DbSession) -> StopOut:
    return await service.stop_thread(session, thread_id, staff)


@router.post("/assistant/actions/{action_id}/undo", response_model=SendOut, summary="Take a confirmed write back — with the caller's own rights, only what nobody changed since")
async def undo_action(action_id: uuid.UUID, staff: Staff, session: DbSession, meta: Meta) -> SendOut:
    return await service.undo_action(session, action_id, staff, meta)


@router.post("/assistant/actions/{action_id}/cancel", response_model=SendOut, summary="Decline a write the model proposed")
async def cancel_action(action_id: uuid.UUID, staff: Staff, session: DbSession, meta: Meta) -> SendOut:
    return await service.cancel_action(session, action_id, staff, meta)


@router.post("/assistant/actions/batch", response_model=SendOut, summary="One answer to a turn that proposed several writes: run the chosen ones (each in its own savepoint), decline the rest")
async def resolve_batch(body: BatchIn, staff: Staff, session: DbSession, meta: Meta) -> SendOut:
    return await service.resolve_batch(session, staff, meta, body.confirm, body.cancel)


@router.post("/assistant/actions/batch-undo", response_model=SendOut, summary="Take several confirmed writes of one conversation back at once — newest first, each under the rules of a single undo")
async def undo_batch(body: BatchUndoIn, staff: Staff, session: DbSession, meta: Meta) -> SendOut:
    return await service.undo_batch(session, staff, meta, body.ids)


# ------------------------------------------------------------------ scheduled tasks (the caller's own)


@router.get("/assistant/schedules", response_model=ScheduleListOut, summary="The caller's scheduled tasks: what each asks, when it runs, how the last run went")
async def list_schedules(staff: Staff, session: DbSession) -> ScheduleListOut:
    return await schedule.list_schedules(session, staff)


@router.post("/assistant/schedules", response_model=ScheduleOut, status_code=201, summary="Leave a task for the assistant to repeat (daily / on week days / monthly / once) in a unit of the caller's territory")
async def create_schedule(body: ScheduleIn, staff: Staff, session: DbSession, meta: Meta, unit_id: str | None = Query(default=None, alias="unitId")) -> ScheduleOut:
    default_unit = uuid.UUID(unit_id) if unit_id and not body.unit_id else None
    return await schedule.create_schedule(session, staff, body, default_unit, meta)


@router.patch("/assistant/schedules/{schedule_id}", response_model=ScheduleOut, summary="Change a scheduled task; `isActive` pauses / resumes it (resuming clears why the system switched it off)")
async def update_schedule(schedule_id: uuid.UUID, body: ScheduleUpdateIn, staff: Staff, session: DbSession, meta: Meta) -> ScheduleOut:
    return await schedule.update_schedule(session, schedule_id, staff, body.model_dump(by_alias=True, exclude_unset=True, mode="json"), meta)


@router.delete("/assistant/schedules/{schedule_id}", status_code=204, summary="Delete a scheduled task (its conversation stays)")
async def delete_schedule(schedule_id: uuid.UUID, staff: Staff, session: DbSession, meta: Meta) -> None:
    await schedule.delete_schedule(session, schedule_id, staff, meta)


@router.post("/assistant/schedules/{schedule_id}/run", response_model=ScheduleOut, summary="Run a scheduled task right now, exactly as the schedule would (read-only tools); the answer lands in its conversation")
async def run_schedule(schedule_id: uuid.UUID, staff: Staff, session: DbSession) -> ScheduleOut:
    return await schedule_runner.run_now(session, schedule_id, staff)


@router.get("/assistant/files/{file_id}", summary="Download a file the assistant built for the caller (Excel / CSV / JSON)", response_class=Response)
async def download_file(file_id: uuid.UUID, staff: Staff, session: DbSession) -> Response:
    f = await service.get_file(session, file_id, staff)
    ascii_name = f.name.encode("ascii", "ignore").decode() or "export"
    return Response(content=f.data, media_type=f.content_type, headers={"Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(f.name)}", "Cache-Control": "private, no-store"})


# ------------------------------------------------------------------ the platform account (superadmins; everyone without a closer one uses it)


@router.get("/assistant/platform-account", response_model=AssistantAccountOut, summary="The platform's AI account — entered once, used by every unit without its own")
async def get_platform_account(staff: Staff, session: DbSession) -> AssistantAccountOut:
    return await service.get_platform_account(session, staff)


@router.put("/assistant/platform-account", response_model=AssistantAccountOut, summary="Save the platform's AI account (superadmin)")
async def set_platform_account(body: AssistantAccountIn, staff: Staff, session: DbSession, meta: Meta) -> AssistantAccountOut:
    return await service.set_platform_account(session, body, staff, meta)


@router.post("/assistant/platform-account/test", response_model=AssistantTestOut, summary="One short round-trip with the platform account")
async def test_platform_account(staff: Staff, session: DbSession) -> AssistantTestOut:
    return await service.test_platform_account(session, staff)


# ------------------------------------------------------------------ the model account of a unit (inherited downwards)


@router.get("/units/{unit_id}/assistant", response_model=AssistantAccountOut, summary="The unit's AI account (its own, or the inherited one) — the key never leaves the server")
async def get_account(unit_id: uuid.UUID, staff: Staff, session: DbSession) -> AssistantAccountOut:
    if not staff.has(*SETTINGS_READ):
        raise ForbiddenError("Ruxsat yo‘q")
    return await service.get_account(session, unit_id, staff)


@router.put("/units/{unit_id}/assistant", response_model=AssistantAccountOut, summary="Save the unit's own AI account (provider, model, key); units below inherit it")
async def set_account(unit_id: uuid.UUID, body: AssistantAccountIn, staff: Staff, session: DbSession, meta: Meta) -> AssistantAccountOut:
    staff.require(*SETTINGS_WRITE)
    return await service.set_account(session, unit_id, body, staff, meta)


@router.post("/units/{unit_id}/assistant/test", response_model=AssistantTestOut, summary="One short round-trip with the account in effect at the unit")
async def test_account(unit_id: uuid.UUID, staff: Staff, session: DbSession) -> AssistantTestOut:
    staff.require(*SETTINGS_WRITE)
    return await service.test_account(session, unit_id, staff)
