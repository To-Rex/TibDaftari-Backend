"""The AI assistant («Yordamchi»): the model account of a unit (inherited downwards), an employee's threads, their
messages, and the writes the model proposed that wait for the employee's confirmation."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
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
