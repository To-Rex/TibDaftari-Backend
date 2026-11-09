"""Messaging: provider accounts, SMS texts, outbox (SMS / Telegram / portal), in-app notifications, audit log.

SMS / Telegram credentials attach to ANY unit (`messaging_accounts`) and are inherited downwards; the DB
function `resolve_messaging_account(unit, channel)` (and `units.repository.resolve_account`) picks the nearest
active one. SMS texts (`sms_templates`) inherit the same way.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, SmallInteger, String, Text
from sqlalchemy import text as sql
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.db.base import AuditMixin, Base, PKMixin, SoftDeleteMixin


class MessagingAccount(PKMixin, AuditMixin, SoftDeleteMixin, Base):
    """One SMS (Xabarchi) or Telegram bot account of a unit. Telegram with `api_key_enc NULL` = the platform bot
    whose token comes from the environment (`settings.telegram_bot_token`)."""

    __tablename__ = "messaging_accounts"

    unit_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("org_units.id"), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(10), nullable=False)  # sms | telegram
    provider: Mapped[str] = mapped_column(String(20), nullable=False)  # sms: xabarchi | none ; telegram: telegram
    api_key_enc: Mapped[str | None] = mapped_column(Text)  # Fernet-encrypted key / bot token
    api_key_masked: Mapped[str | None] = mapped_column(String(80))
    default_priority: Mapped[str] = mapped_column(String(20), nullable=False, default="transactional")
    sender_note: Mapped[str | None] = mapped_column(String(200))
    bot_username: Mapped[str | None] = mapped_column(String(80))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class SmsTemplate(PKMixin, AuditMixin, SoftDeleteMixin, Base):
    """An SMS text of a unit (`key` = payment_receipt | result_ready | reminder | …), inherited downwards."""

    __tablename__ = "sms_templates"

    unit_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("org_units.id"), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(40), nullable=False)
    lang: Mapped[str] = mapped_column(String(2), nullable=False, default="uz")
    body: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class OutboxMessage(PKMixin, AuditMixin, SoftDeleteMixin, Base):
    """The organisation owns scheduling/idempotency/retries; the provider (Xabarchi) has neither."""

    __tablename__ = "outbox_messages"

    unit_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("org_units.id"), index=True)  # the facility that sent it
    account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("messaging_accounts.id"))
    patient_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("patients.id"), index=True)
    order_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("orders.id"), index=True)
    document_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("result_documents.id"))
    channel: Mapped[str] = mapped_column(String(10), nullable=False)  # sms | telegram | portal
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # payment_receipt | result_ready | reminder | broadcast | otp | portal_access
    to: Mapped[str] = mapped_column(String(64), nullable=False)  # phone (sms) or chat id (telegram)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="queued")  # scheduled|queued|sending|sent|delivered|failed|cancelled
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_message_id: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)  # e.g. {"documentId": ..} for telegram PDF

    __table_args__ = (
        Index("ix_outbox_unit_created", "unit_id", "created_at"),
        Index("ix_outbox_unit_status_created", "unit_id", "status", "created_at"),
        Index("ix_outbox_queue", "next_attempt_at", postgresql_where=sql("status IN ('queued','scheduled') AND deleted_at IS NULL")),
    )


class Notification(PKMixin, AuditMixin, SoftDeleteMixin, Base):
    __tablename__ = "notifications"

    unit_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("org_units.id"), index=True)  # scope: this unit and below; NULL = everyone
    employee_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("employees.id"), index=True)  # NULL = every employee in scope
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False, default="info")  # info | success | warning | error
    link: Mapped[str | None] = mapped_column(String(300))
    read_by: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=False, default=list)

    __table_args__ = (Index("ix_notifications_unit_created", "unit_id", "created_at"),)


class AuditLog(Base):
    """Append-only change log. Range-partitioned by month (partitions created by `ensure_audit_partitions`)."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True, server_default=sql("now()"))
    country_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    unit_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    actor_type: Mapped[str] = mapped_column(String(10), nullable=False)  # staff | patient | system
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(40), nullable=False)  # create | update | delete | login | approve ...
    entity: Mapped[str] = mapped_column(String(40), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64))
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    ip: Mapped[str | None] = mapped_column(String(64))
    request_id: Mapped[str | None] = mapped_column(String(40))

    __table_args__ = (
        Index("ix_audit_log_unit_created", "unit_id", "created_at"),
        Index("ix_audit_log_entity", "entity", "entity_id"),
        {"postgresql_partition_by": "RANGE (created_at)"},
    )
