"""Messaging endpoints — outbox listing, manual/broadcast SMS, in-app notifications. HTTP only."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Response

from app.api.deps import DbSession, Meta, Scope, Staff
from app.core.schemas import OkOut, Page
from app.modules.messaging import service
from app.modules.messaging.schemas import (
    MarkReadIn,
    NotificationOut,
    OutboxCountsOut,
    OutboxMessageOut,
    OutboxQuery,
    SendIn,
)

router = APIRouter()


@router.get("/units/{unit_id}/outbox", response_model=Page[OutboxMessageOut], summary="Outbox (SMS/Telegram) messages sent under the unit, newest first")
async def list_outbox(scope: Scope, q: Annotated[OutboxQuery, Query()], staff: Staff, session: DbSession) -> Page[OutboxMessageOut]:
    staff.require("messaging.send", "reports.operations.read")
    return await service.list_outbox(session, scope, q, staff)


@router.get("/units/{unit_id}/outbox/counts", response_model=OutboxCountsOut, summary="Outbox counters per status (same filters as the list)")
async def outbox_counts(scope: Scope, q: Annotated[OutboxQuery, Query()], staff: Staff, session: DbSession) -> OutboxCountsOut:
    staff.require("messaging.send", "reports.operations.read")
    return await service.outbox_counts(session, scope, q, staff)


@router.post("/units/{unit_id}/messages/send", response_model=list[OutboxMessageOut], status_code=201, summary="Queue an SMS to one or many recipients from a facility")
async def send_messages(scope: Scope, body: SendIn, staff: Staff, session: DbSession, meta: Meta, response: Response) -> list[OutboxMessageOut]:
    staff.require("messaging.send")
    created, invalid = await service.send(session, scope, staff, body, meta)
    if invalid:
        response.headers["X-Invalid-Recipients"] = ",".join(invalid)
    return created


@router.get("/notifications", response_model=list[NotificationOut], summary="My in-app notifications (newest 50)")
async def list_notifications(staff: Staff, session: DbSession) -> list[NotificationOut]:
    return await service.list_notifications(session, staff)


@router.post("/notifications/read", response_model=OkOut, summary="Mark one (id) or all notifications as read")
async def mark_read(staff: Staff, session: DbSession, body: MarkReadIn | None = None) -> OkOut:
    await service.mark_read(session, staff, body.id if body else None)
    return OkOut()
