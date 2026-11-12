"""Wire shapes of the assistant: settings of a unit's model account, threads, messages, proposed actions."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from app.core.schemas import CamelModel

Provider = Literal["anthropic", "openai", "deepseek", "deploy", "custom", "none"]
AccountMode = Literal["inherited", "own", "off"]
ToolKind = Literal["read", "write"]
ActionStatus = Literal["pending", "done", "failed", "cancelled", "expired"]
MessageRole = Literal["user", "assistant", "tool"]


# ----------------------------------------------------------------------------- settings (per unit, inherited)


class AssistantAccountOut(CamelModel):
    """The unit's own model account, or the one it inherits. The key never leaves the server — only its masked tail."""

    mode: AccountMode
    provider: Provider
    base_url: str | None = None
    model: str | None = None
    api_key_masked: str | None = None
    effort: str | None = None
    max_tokens: int | None = None
    #: the unit whose account is in effect (itself or an ancestor); null = the platform account or nothing at all
    effective_unit_id: str | None = None
    effective_unit_name: str | None = None
    #: the account in effect is the platform's (entered once by a superadmin for everyone)
    effective_platform: bool = False
    #: whether the assistant works at this unit (an account with a key is in effect)
    configured: bool = False


class AssistantAccountIn(CamelModel):
    mode: AccountMode
    provider: Provider | None = None
    base_url: str | None = Field(default=None, max_length=300)
    model: str | None = Field(default=None, max_length=120)
    #: write-only plaintext; omitted = keep the saved key
    api_key: str | None = Field(default=None, max_length=500)
    effort: Literal["low", "medium", "high"] | None = None
    #: 0 = the model's own maximum output (Claude: looked up through the Models API — 128K on the 5.x line)
    max_tokens: int | None = Field(default=None, ge=0, le=200_000)


class AssistantTestOut(CamelModel):
    ok: bool
    provider: Provider
    model: str | None = None
    reply: str | None = None
    error: str | None = None
    latency_ms: int


# ----------------------------------------------------------------------------- status, threads, messages


class AssistantToolOut(CamelModel):
    name: str
    title: str
    kind: ToolKind


class AssistantStatusOut(CamelModel):
    """Whether the caller may use the assistant here, with which model, and what it may do for them."""

    enabled: bool
    configured: bool
    provider: Provider | None = None
    model: str | None = None
    effective_unit_name: str | None = None
    effective_platform: bool = False
    tools: list[AssistantToolOut] = Field(default_factory=list)
    #: messages per rate-limit window and the window length
    rate_limit: int
    rate_window_seconds: int


class ThreadOut(CamelModel):
    id: str
    title: str
    unit_id: str | None = None
    last_message_at: datetime | None = None
    created_at: datetime


class ThreadCreateIn(CamelModel):
    title: str | None = Field(default=None, max_length=200)


class FileOut(CamelModel):
    """A file a tool built (export_data / export_table) — downloaded from `GET /assistant/files/{id}` by its owner."""

    id: str
    name: str
    content_type: str
    size: int
    rows: int | None = None
    expires_at: datetime
    created_at: datetime


class ToolCallOut(CamelModel):
    id: str
    name: str
    title: str
    kind: ToolKind
    #: a short, human-readable line of the arguments (never the raw payload of a secret)
    summary: str
    #: read tools: ran (ok / error); write tools: pending / done / failed / cancelled / expired
    status: str
    #: the file this call produced, if any
    file: FileOut | None = None


class ActionOut(CamelModel):
    id: str
    thread_id: str
    tool: str
    title: str
    summary: str
    args: dict[str, Any]
    status: ActionStatus
    error: str | None = None
    expires_at: datetime
    created_at: datetime


class MessageOut(CamelModel):
    id: str
    role: MessageRole
    content: str
    tool_calls: list[ToolCallOut] = Field(default_factory=list)
    tool_name: str | None = None
    is_error: bool = False
    action: ActionOut | None = None
    #: files the tools of this turn produced (also on the tool calls themselves)
    files: list[FileOut] = Field(default_factory=list)
    created_at: datetime


class ThreadDetailOut(CamelModel):
    thread: ThreadOut
    messages: list[MessageOut]
    pending: ActionOut | None = None


class SendIn(CamelModel):
    content: str = Field(min_length=1, max_length=8000)
    #: the unit the employee is working in (the header scope); defaults to the thread's unit, else the home unit
    unit_id: str | None = None


class SendOut(CamelModel):
    """The turns appended by this request (the user's message, tool activity, the reply) and a write waiting for a yes."""

    messages: list[MessageOut]
    pending: ActionOut | None = None
    thread: ThreadOut
