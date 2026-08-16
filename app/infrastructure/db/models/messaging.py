"""Outbox (SMS / Telegram / portal), in-app notifications, audit log."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Index, SmallInteger, String, Text
from sqlalchemy import text as sql
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.db.base import AuditMixin, Base, PKMixin, SoftDeleteMixin, TenantMixin


class OutboxMessage(PKMixin, AuditMixin, SoftDeleteMixin, TenantMixin, Base):
    """The clinic owns scheduling/idempotency/retries; the provider (Xabarchi) has neither."""

    __tablename__ = "outbox_messages"

    branch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    patient_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    order_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    document_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    channel: Mapped[str] = mapped_column(String(10), nullable=False)  # sms | telegram | portal
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # payment_receipt | result_ready | reminder | broadcast | otp
    to: Mapped[str] = mapped_column(String(64), nullable=False)  # phone (sms) or chat id (telegram)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="queued")  # scheduled|queued|sending|sent|delivered|failed
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_message_id: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)  # e.g. {"documentId": ..} for telegram PDF

    __table_args__ = (
        Index("ix_outbox_company_created", "company_id", "created_at"),
        Index("ix_outbox_company_status_created", "company_id", "status", "created_at"),
        # queue scan: only rows that are still to be sent
        Index(
            "ix_outbox_queue",
            "next_attempt_at",
            postgresql_where=sql("status IN ('queued','scheduled') AND deleted_at IS NULL"),
        ),
    )


class Notification(PKMixin, AuditMixin, SoftDeleteMixin, TenantMixin, Base):
    __tablename__ = "notifications"

    employee_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)  # NULL = whole company
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False, default="info")  # info | success | warning
    link: Mapped[str | None] = mapped_column(String(300))
    read_by: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=False, default=list)

    __table_args__ = (Index("ix_notifications_company_created", "company_id", "created_at"),)


class AuditLog(Base):
    """Append-only change log. Range-partitioned by month (created in the initial migration)."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True, server_default=sql("now()"))
    company_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
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
        Index("ix_audit_log_company_created", "company_id", "created_at"),
        Index("ix_audit_log_entity", "entity", "entity_id"),
        {"postgresql_partition_by": "RANGE (created_at)"},
    )
