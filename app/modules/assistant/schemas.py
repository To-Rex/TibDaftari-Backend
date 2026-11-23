"""Wire shapes of the assistant: settings of a unit's model account, threads, messages, proposed actions."""

from __future__ import annotations

import uuid
from datetime import date, datetime
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
    #: where the tool belongs in the "what I can do" list: units, staff, catalog, templates, reception, lab, results, reports, messaging, files, general
    group: str = "general"
    #: a write that cannot be taken back
    danger: bool = False
    #: a write the employee can take back afterwards (within a day, while nobody changed it since)
    undoable: bool = False


class AssistantContextOut(CamelModel):
    """One step of the working unit's lineage (company → … → the unit) — what the assistant is told about where the employee works."""

    level: Literal["company", "country", "region", "district", "facility"]
    id: str
    name: str
    code: str | None = None


class AssistantStatusOut(CamelModel):
    """Whether the caller may use the assistant here, with which model, and what it may do for them."""

    enabled: bool
    configured: bool
    provider: Provider | None = None
    model: str | None = None
    effective_unit_name: str | None = None
    effective_platform: bool = False
    tools: list[AssistantToolOut] = Field(default_factory=list)
    #: the working unit's lineage, company first (empty when no working unit could be resolved)
    context: list[AssistantContextOut] = Field(default_factory=list)
    #: messages per rate-limit window and the window length
    rate_limit: int
    rate_window_seconds: int


class ThreadOut(CamelModel):
    id: str
    title: str
    unit_id: str | None = None
    last_message_at: datetime | None = None
    created_at: datetime
    #: the scheduled task whose runs land in this conversation
    schedule_id: str | None = None
    #: a scheduled run left an answer the owner has not opened yet
    unread: bool = False


class ThreadCreateIn(CamelModel):
    title: str | None = Field(default=None, max_length=200)


class ThreadUpdateIn(CamelModel):
    """Rename a conversation — the title the employee gives replaces the one taken from the first message."""

    title: str = Field(min_length=1, max_length=200)


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


class ActionDetailOut(CamelModel):
    label: str
    value: str


class ActionUndoOut(CamelModel):
    """The way back of a done action (`POST /assistant/actions/{id}/undo`)."""

    #: can be taken back right now (not yet undone, still inside the window)
    available: bool
    #: what undoing does, in one line
    label: str = ""
    until: datetime | None = None
    undone_at: datetime | None = None


class StopOut(CamelModel):
    #: a run was working on the thread and has been told to stop; false = nothing was running
    stopping: bool


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
    #: what the write touches, ids resolved to names (as the employee could see them when it was proposed)
    details: list[ActionDetailOut] = Field(default_factory=list)
    #: cannot be taken back
    danger: bool = False
    #: while pending: the word the employee must type to confirm an irreversible write (`POST …/confirm {confirm}`)
    confirm_word: str | None = None
    #: this kind of write has a way back (known before it runs)
    undoable: bool = False
    #: once done: the way back, if this run left one
    undo: ActionUndoOut | None = None


class ConfirmIn(CamelModel):
    #: the word an irreversible action asks for, typed by the employee
    confirm: str | None = Field(default=None, max_length=200)


class ChoicesOut(CamelModel):
    """A question the assistant asked with ready answers — the employee taps one (it is sent as their next message)."""

    question: str
    options: list[str]


class MessageOut(CamelModel):
    id: str
    role: MessageRole
    content: str
    tool_calls: list[ToolCallOut] = Field(default_factory=list)
    tool_name: str | None = None
    is_error: bool = False
    #: the first write this turn proposed (kept for clients that know one write per turn)
    action: ActionOut | None = None
    #: every write this turn proposed, in the order the model asked for them — more than one = a batch the employee
    #: answers with one yes (`POST /assistant/actions/batch`)
    actions: list[ActionOut] = Field(default_factory=list)
    #: files the tools of this turn produced (also on the tool calls themselves)
    files: list[FileOut] = Field(default_factory=list)
    #: the assistant asked the employee to choose
    choices: ChoicesOut | None = None
    #: the employee stopped the run here (the content is a note for the model, not an answer)
    stopped: bool = False
    #: this message reports that the action with this id was taken back
    undo_of: str | None = None
    #: a user turn written by a scheduled task (the task's prompt), not typed by the employee
    scheduled: bool = False
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


# ----------------------------------------------------------------------------- several writes, one answer


class BatchIn(CamelModel):
    """The employee's answer to a turn that proposed several writes: the ones to run; every other pending write of
    that turn is declined. An empty `confirm` declines them all."""

    confirm: list[uuid.UUID] = Field(default_factory=list, max_length=60)
    cancel: list[uuid.UUID] = Field(default_factory=list, max_length=60)


class BatchUndoIn(CamelModel):
    """Take several done writes of one conversation back at once (newest first)."""

    ids: list[uuid.UUID] = Field(min_length=1, max_length=60)


# ----------------------------------------------------------------------------- scheduled tasks («Rejali topshiriqlar»)

ScheduleKind = Literal["daily", "weekly", "monthly", "once"]
TIME_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"


class ScheduleIn(CamelModel):
    """A task the assistant repeats for its owner: what to do (`prompt`) and when."""

    title: str = Field(min_length=1, max_length=200)
    #: the request the assistant gets at every run — complete on its own ("Kechagi kun bo‘yicha tushum hisobotini tayyorla")
    prompt: str = Field(min_length=3, max_length=4000)
    kind: ScheduleKind
    #: HH:MM in the unit's time zone
    time: str = Field(pattern=TIME_PATTERN)
    #: weekly: 1 = Monday … 7 = Sunday
    weekdays: list[int] = Field(default_factory=list, max_length=7)
    #: monthly: 1…31 (a shorter month runs on its last day)
    month_day: int | None = Field(default=None, ge=1, le=31)
    #: once: the day (YYYY-MM-DD)
    run_date: date | None = None
    #: the unit every run works in (default: the caller's working unit)
    unit_id: str | None = None
    is_active: bool = True


class ScheduleUpdateIn(CamelModel):
    """Only the given fields change; `isActive` pauses / resumes."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    prompt: str | None = Field(default=None, min_length=3, max_length=4000)
    kind: ScheduleKind | None = None
    time: str | None = Field(default=None, pattern=TIME_PATTERN)
    weekdays: list[int] | None = Field(default=None, max_length=7)
    month_day: int | None = Field(default=None, ge=1, le=31)
    run_date: date | None = None
    unit_id: str | None = None
    is_active: bool | None = None


class ScheduleOut(CamelModel):
    id: str
    title: str
    prompt: str
    kind: ScheduleKind
    time: str
    weekdays: list[int] = Field(default_factory=list)
    month_day: int | None = None
    run_date: date | None = None
    timezone: str
    #: the rule in words: «Har kuni 08:00», «Dushanba, juma 09:30», «Har oyning 1-kuni 10:00»
    when: str
    unit_id: str
    unit_name: str | None = None
    #: the conversation the runs land in (none before the first run)
    thread_id: str | None = None
    is_active: bool
    #: why the system switched it off (errors in a row, lost rights) — the owner switches it back on
    paused_reason: str | None = None
    #: a one-time task that has run
    finished: bool = False
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    last_status: Literal["ok", "failed"] | None = None
    last_error: str | None = None
    run_count: int = 0
    created_at: datetime


class ScheduleListOut(CamelModel):
    items: list[ScheduleOut]
    #: how many tasks one employee may keep
    limit: int
    #: the background worker that runs due tasks is on at this server (off = tasks only run when asked "now")
    running: bool = True
