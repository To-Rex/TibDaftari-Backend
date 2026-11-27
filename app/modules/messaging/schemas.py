"""Messaging DTOs — mirror `Clinic-Web/src/domain/notification.ts`."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any, Literal

from pydantic import Field, field_validator

from app.core.schemas import CamelModel, PageQuery

MessageChannel = Literal["sms", "telegram", "portal"]
MessageStatus = Literal["scheduled", "queued", "sent", "delivered", "failed", "cancelled"]
MessageKind = Literal["payment_receipt", "result_ready", "reminder", "broadcast", "otp", "portal_access"]
NotificationKind = Literal["info", "success", "warning", "error"]


class OutboxQuery(PageQuery):
    """`listOutbox` filters (sort is fixed to createdAt desc; sortBy/sortDir are ignored).

    `unitId` narrows the path unit's subtree to the given sub-units (facilities, districts …); `dateFrom` /
    `dateTo` are inclusive calendar days in Asia/Tashkent the message was written on."""

    status: MessageStatus | None = None
    kind: MessageKind | None = None
    unit_id: list[str] | None = Field(default=None, max_length=200)
    date_from: date | None = None
    date_to: date | None = None


class OutboxCountsOut(CamelModel):
    """Outbox counters per status for the current filters."""

    all: int
    scheduled: int
    queued: int
    sending: int
    sent: int
    delivered: int
    failed: int
    cancelled: int = 0


class OutboxMessageOut(CamelModel):
    id: uuid.UUID
    #: the facility the message went out from (None for a message without one, e.g. a code for an unregistered phone)
    unit_id: uuid.UUID | None = None
    unit_name: str | None = None
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
    #: a number abroad: the ISO-2 country it was sent to
    country_code: str | None = None
    #: Eskiz: how many parts the message took and what it cost (so‘m), once Eskiz said so
    parts: int | None = None
    cost: float | None = None
    #: Eskiz: the provider's own state word (DELIVERED, EXPIRED …)
    provider_status: str | None = None
    #: who the message is for and which cheque it is about, by name (the row itself keeps only the ids)
    patient_name: str | None = None
    order_number: str | None = None
    #: the employee who wrote it or sent it again; None = the system sent it on its own (a receipt, a result, a code)
    sent_by_name: str | None = None
    #: a message waiting for another try after a provider hiccup: when that try is due
    next_attempt_at: datetime | None = None
    #: the text is a credential (a code, a password): the journal keeps it masked and it cannot be sent again
    secret: bool = False
    #: the message this one repeats (it was sent again from the journal)
    resend_of: uuid.UUID | None = None
    #: what the reader may do with it right now (their permission and the message's state together)
    can_cancel: bool = False
    can_resend: bool = False

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
    #: the facility the message goes out from — required when the path unit is not a facility itself
    unit_id: str | None = Field(default=None, max_length=64)
    #: ISO-2 country of the recipients (default UZ); another country = full international numbers, sent through Eskiz
    country_code: str | None = Field(default=None, min_length=2, max_length=2, pattern=r"^[A-Za-z]{2}$")

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
    #: a notice written by the system: what it is about and its values — shown in the reader's language when the
    #: interface knows the code, else `title` / `body` as they are
    code: str | None = None
    params: dict[str, Any] | None = None


class MarkReadIn(CamelModel):
    id: uuid.UUID | None = None
