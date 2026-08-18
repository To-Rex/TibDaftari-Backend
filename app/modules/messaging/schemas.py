"""Messaging DTOs — mirror `Clinic-Web/src/domain/notification.ts`."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import Field, field_validator

from app.core.schemas import CamelModel, PageQuery

MessageChannel = Literal["sms", "telegram", "portal"]
MessageStatus = Literal["scheduled", "queued", "sent", "delivered", "failed"]
MessageKind = Literal["payment_receipt", "result_ready", "reminder", "broadcast", "otp"]
NotificationKind = Literal["info", "success", "warning"]


class OutboxQuery(PageQuery):
    """`listOutbox` filters (sort is fixed to createdAt desc; sortBy/sortDir are ignored)."""

    status: MessageStatus | None = None
    kind: MessageKind | None = None


class OutboxCountsOut(CamelModel):
    """Outbox counters per status for the current filters."""

    all: int
    scheduled: int
    queued: int
    sending: int
    sent: int
    delivered: int
    failed: int


class OutboxMessageOut(CamelModel):
    id: uuid.UUID
    company_id: uuid.UUID
    branch_id: uuid.UUID | None = None
    patient_id: uuid.UUID | None = None
    order_id: uuid.UUID | None = None
    channel: MessageChannel
    kind: MessageKind
    to: str
    text: str
    status: MessageStatus
    scheduled_at: datetime | None = None
    sent_at: datetime | None = None
    attempts: int
    provider_message_id: str | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("status", mode="before")
    @classmethod
    def _public_status(cls, v: str) -> str:
        # `sending` is an internal lease state (worker picked the row up); the UI knows only `queued`.
        return "queued" if v == "sending" else v


class SendIn(CamelModel):
    to: list[str] = Field(min_length=1, max_length=1000)
    text: str = Field(min_length=1, max_length=1000)
    kind: MessageKind = "broadcast"
    scheduled_at: datetime | None = None

    @field_validator("scheduled_at")
    @classmethod
    def _aware(cls, v: datetime | None) -> datetime | None:
        # A naive value (e.g. datetime-local) is taken as UTC so comparisons with now() cannot fail.
        return v.replace(tzinfo=UTC) if v is not None and v.tzinfo is None else v


class NotificationOut(CamelModel):
    id: uuid.UUID
    title: str
    body: str
    kind: NotificationKind
    created_at: datetime
    read: bool
    link: str | None = None


class MarkReadIn(CamelModel):
    id: uuid.UUID | None = None
