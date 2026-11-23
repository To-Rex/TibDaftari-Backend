"""The AI assistant («Yordamchi»): the model account of a unit (inherited downwards), an employee's threads, their
messages, and the writes the model proposed that wait for the employee's confirmation."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, LargeBinary, SmallInteger, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.db.base import AuditMixin, Base, PKMixin, SoftDeleteMixin, TimestampMixin


class AssistantAccount(PKMixin, AuditMixin, SoftDeleteMixin, Base):
    """Which model a unit (and everything under it without its own account) talks to. `provider='none'` switches the
    assistant off for the subtree."""

    __tablename__ = "assistant_accounts"

    #: NULL = the platform's own account — used wherever no unit on the path has one (superadmins manage it)
    unit_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("org_units.id"), index=True)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)  # anthropic | openai | deepseek | deploy | custom | none
    base_url: Mapped[str | None] = mapped_column(String(300))
    model: Mapped[str | None] = mapped_column(String(120))
    api_key_enc: Mapped[str | None] = mapped_column(Text)
    api_key_masked: Mapped[str | None] = mapped_column(String(80))
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class AssistantThread(PKMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "assistant_threads"

    employee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("employees.id"), nullable=False, index=True)
    unit_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("org_units.id"))
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: the scheduled task whose runs land in this conversation
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    #: a scheduled run left an answer the owner has not opened yet
    unread: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")


class AssistantMessage(PKMixin, TimestampMixin, Base):
    __tablename__ = "assistant_messages"

    thread_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("assistant_threads.id", ondelete="CASCADE"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(10), nullable=False)  # user | assistant | tool
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tool_calls: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    tool_call_id: Mapped[str | None] = mapped_column(String(80))
    tool_name: Mapped[str | None] = mapped_column(String(60))
    is_error: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    action_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class AssistantAction(PKMixin, TimestampMixin, Base):
    """A write the model asked for. Nothing runs until the employee who owns the thread confirms it."""

    __tablename__ = "assistant_actions"

    thread_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("assistant_threads.id", ondelete="CASCADE"), nullable=False, index=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("employees.id"), nullable=False)
    tool_call_id: Mapped[str] = mapped_column(String(80), nullable=False)
    tool: Mapped[str] = mapped_column(String(60), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    args: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="pending")  # pending | done | failed | cancelled | expired
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AssistantFile(PKMixin, TimestampMixin, Base):
    """A file the model built on request (export_data / export_table): the employee who owns the thread downloads
    it from the chat; purged after `expires_at`."""

    __tablename__ = "assistant_files"

    thread_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("assistant_threads.id", ondelete="CASCADE"), nullable=False, index=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("employees.id"), nullable=False)
    tool_call_id: Mapped[str | None] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    rows: Mapped[int | None] = mapped_column(Integer)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AssistantSchedule(PKMixin, TimestampMixin, SoftDeleteMixin, Base):
    """A task the employee left for the assistant to repeat («Rejali topshiriq»): every run happens with the
    owner's rights as they are at that moment, with read-only tools, and lands in the task's own conversation."""

    __tablename__ = "assistant_schedules"

    employee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("employees.id"), nullable=False, index=True)
    unit_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("org_units.id"), nullable=False)
    thread_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("assistant_threads.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)  # daily | weekly | monthly | once
    at_time: Mapped[str] = mapped_column(String(5), nullable=False)  # HH:MM in `timezone`
    weekdays: Mapped[list[int]] = mapped_column(ARRAY(SmallInteger), nullable=False, default=list)  # 1 = Monday … 7 = Sunday
    month_day: Mapped[int | None] = mapped_column(SmallInteger)
    run_date: Mapped[date | None] = mapped_column(Date)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Tashkent")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    paused_reason: Mapped[str | None] = mapped_column(Text)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(String(10))  # ok | failed
    last_error: Mapped[str | None] = mapped_column(Text)
    run_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fail_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
