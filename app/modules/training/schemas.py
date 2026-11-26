"""Training DTOs."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from app.core.schemas import CamelModel

#: `pending` is only ever reported for a colleague who has not signed in yet (no row exists for them)
TrainingState = Literal["active", "passed", "skipped", "exempt", "pending"]


class TrainingTaskOut(CamelModel):
    key: str
    done: bool
    done_at: datetime | None = None
    #: false when there is nothing to practise it on here (no service with a result form in this facility):
    #: such a task does not hold the training back
    available: bool = True


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


class EmployeeTrainingOut(CamelModel):
    """A colleague's training as a manager sees it on the employee card."""

    state: TrainingState
    done: int = 0
    total: int = 0
    started_at: datetime | None = None
    completed_at: datetime | None = None
    #: who let them through (state `skipped`)
    completed_by_name: str | None = None
