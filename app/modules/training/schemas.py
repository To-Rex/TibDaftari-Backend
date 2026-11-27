"""Training DTOs."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from app.core.schemas import CamelModel, PageQuery

#: `pending` is only ever reported for a colleague who has not signed in yet (no row exists for them)
TrainingState = Literal["active", "passed", "skipped", "exempt", "pending"]
#: what the overview can be narrowed to: `open` = everyone the training still concerns (in it, or not signed in
#: yet), `stalled` = in it longer than the reminder threshold, `ready` = every task done, «finish» not pressed
OverviewFilter = Literal["open", "active", "stalled", "ready", "pending", "passed", "skipped", "all"]


class TrainingTargetOut(CamelModel):
    """The very record a task is done on — the page opens there instead of at a list. Every id is of the
    employee's sandbox; a field is absent when the task has no such record."""

    patient_id: str | None = None
    order_id: str | None = None
    item_id: str | None = None


class TrainingTaskOut(CamelModel):
    key: str
    done: bool
    done_at: datetime | None = None
    #: false when there is nothing to practise it on here (no service with a result form in this facility):
    #: such a task does not hold the training back
    available: bool = True
    #: where to continue (the caller's own status only; absent for a task that is done or cannot be done)
    target: TrainingTargetOut | None = None


class TrainingOut(CamelModel):
    """The caller's own training: what is left to do. `active` = their requests work in the sandbox."""

    state: TrainingState
    active: bool = False
    tasks: list[TrainingTaskOut] = Field(default_factory=list)
    done: int = 0
    total: int = 0
    #: every task is done (or cannot be done here) — «finish» will be accepted
    can_complete: bool = False
    started_at: datetime | None = None
    completed_at: datetime | None = None
    #: the facility whose sandbox the employee practises in (where the samples were put)
    unit_id: str | None = None
    unit_name: str | None = None
    #: a repeat they started themselves: they may leave it at any time (`POST /training/leave`)
    voluntary: bool = False
    #: only some tasks of the role were chosen for this round
    partial: bool = False
    #: which time this is (1 = the first training)
    attempt: int = 1
    #: every task the caller's role has NOW — what a repeat can be made of (`POST /training/restart`)
    plan: list[str] = Field(default_factory=list)


class TrainingRestartIn(CamelModel):
    """Which tasks to go through (again). Omitted / empty = the whole plan of the role, as on the first day."""

    tasks: list[str] | None = Field(default=None, max_length=32)


class EmployeeTrainingOut(CamelModel):
    """A colleague's training as a manager sees it: on the employee card and in the overview."""

    state: TrainingState
    done: int = 0
    total: int = 0
    #: every task of theirs, done or not (for `pending`: the tasks they will get at their first sign-in)
    tasks: list[TrainingTaskOut] = Field(default_factory=list)
    #: every task is done or cannot be done where they work — only «finish» is left
    can_complete: bool = False
    #: in the training longer than the reminder threshold (`TRAINING_STALL_DAYS`)
    stalled: bool = False
    started_at: datetime | None = None
    #: when they last did a task
    last_task_at: datetime | None = None
    completed_at: datetime | None = None
    #: who let them through (state `skipped`)
    completed_by_name: str | None = None
    #: a repeat the employee started themselves (they may leave it; nobody is reminded about it)
    voluntary: bool = False
    #: only some tasks of the role were chosen for this round
    partial: bool = False
    #: which time this is (1 = the first training)
    attempt: int = 1
    #: the manager who sent them (back) to the training; absent for the first training and for a voluntary repeat
    started_by_name: str | None = None
    #: every task the employee's role has NOW — what a manager can choose from when sending them (back)
    plan: list[str] = Field(default_factory=list)


class TrainingOverviewQuery(PageQuery):
    #: narrow to one unit and everything under it (default: the caller's whole scope; a superadmin — everywhere)
    unit_id: str | None = None
    state: OverviewFilter = "open"


class TrainingOverviewRow(CamelModel):
    """One employee in the overview."""

    id: str
    full_name: str
    login: str
    avatar_hue: int = 160
    status: str
    role_id: str | None = None
    role_name: str | None = None
    unit_id: str | None = None
    unit_name: str | None = None
    last_login_at: datetime | None = None
    training: EmployeeTrainingOut


class TrainingOverviewCounts(CamelModel):
    """How many people of the same scope and search stand where (whatever `state` narrows the page to)."""

    active: int = 0
    stalled: int = 0
    ready: int = 0
    pending: int = 0
    passed: int = 0
    skipped: int = 0


class TrainingOverviewOut(CamelModel):
    items: list[TrainingOverviewRow]
    total: int
    page: int
    page_size: int
    total_pages: int
    counts: TrainingOverviewCounts
    #: days after which someone still in the training counts as `stalled` (0 = the reminder is off)
    stall_days: int = 0
