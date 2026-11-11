"""Assistant endpoints: status, threads, messages, the confirmation of proposed writes, and a unit's model account."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query

from app.api.deps import DbSession, Meta, Staff
from app.core.exceptions import ForbiddenError
from app.modules.assistant import service
from app.modules.assistant.schemas import (
    AssistantAccountIn,
    AssistantAccountOut,
    AssistantStatusOut,
    AssistantTestOut,
    SendIn,
    SendOut,
    ThreadCreateIn,
    ThreadDetailOut,
    ThreadOut,
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


@router.delete("/assistant/threads/{thread_id}", status_code=204, summary="Delete a conversation")
async def delete_thread(thread_id: uuid.UUID, staff: Staff, session: DbSession) -> None:
    await service.delete_thread(session, thread_id, staff)


@router.post("/assistant/threads/{thread_id}/messages", response_model=SendOut, summary="Send a message: the model answers, runs read tools, and proposes writes for confirmation")
async def send_message(thread_id: uuid.UUID, body: SendIn, staff: Staff, session: DbSession, meta: Meta) -> SendOut:
    return await service.send_message(session, thread_id, body, staff, meta)


@router.post("/assistant/actions/{action_id}/confirm", response_model=SendOut, summary="Run a write the model proposed — with the caller's own rights")
async def confirm_action(action_id: uuid.UUID, staff: Staff, session: DbSession, meta: Meta) -> SendOut:
    return await service.confirm_action(session, action_id, staff, meta)


@router.post("/assistant/actions/{action_id}/cancel", response_model=SendOut, summary="Decline a write the model proposed")
async def cancel_action(action_id: uuid.UUID, staff: Staff, session: DbSession, meta: Meta) -> SendOut:
    return await service.cancel_action(session, action_id, staff, meta)


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
