"""Training endpoints: the employee's own (`/training/status|complete|restart|leave`) and a manager's
(`/training/overview`, `/employees/{id}/training/*`)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Request

from app.api.deps import DbSession, Meta, Staff
from app.modules.staff import service as staff_service
from app.modules.staff.schemas import EmployeeOut
from app.modules.training import oversight, service
from app.modules.training.schemas import TrainingOut, TrainingOverviewOut, TrainingOverviewQuery, TrainingRestartIn

router = APIRouter()

UnitId = Annotated[uuid.UUID | None, Query(alias="unitId", description="the facility the employee is working in (where the practice samples are put)")]


@router.get("/training/status", response_model=TrainingOut, summary="My training: the tasks of my role and what is left")
async def training_status(request: Request, staff: Staff, session: DbSession, unit_id: UnitId = None) -> TrainingOut:
    out = await service.status(session, staff, unit_id)
    # the answer's `X-Training` says what is true NOW (the role may just have ended the training)
    request.state.training = out.active
    return out


@router.post("/training/complete", response_model=TrainingOut, summary="Finish the training — from now on I work with the real data")
async def training_complete(request: Request, staff: Staff, session: DbSession, meta: Meta, unit_id: UnitId = None) -> TrainingOut:
    out = await service.complete(session, staff, unit_id, meta)
    request.state.training = out.active  # this very answer already tells the page: the real data from now on
    return out


@router.post("/training/restart", response_model=TrainingOut, summary="Go through the training again by my own wish — the whole plan, or only the tasks I name")
async def training_restart(request: Request, staff: Staff, session: DbSession, meta: Meta, body: TrainingRestartIn | None = None, unit_id: UnitId = None) -> TrainingOut:
    out = await service.restart(session, staff, body.tasks if body else None, unit_id, meta)
    request.state.training = out.active  # this very answer tells the page: the practice data from now on
    return out


@router.post("/training/leave", response_model=TrainingOut, summary="Leave a repeat I started myself — back to the real data")
async def training_leave(request: Request, staff: Staff, session: DbSession, meta: Meta) -> TrainingOut:
    out = await service.leave(session, staff, meta)
    request.state.training = out.active
    return out


@router.get("/training/overview", response_model=TrainingOverviewOut, summary="Everyone the training concerns in my scope: who is in it and how far, who has not signed in, who finished")
async def training_overview(q: Annotated[TrainingOverviewQuery, Query()], staff: Staff, session: DbSession) -> TrainingOverviewOut:
    staff.require("admin.employee.read")
    return await oversight.overview(session, staff, q)


@router.post("/employees/{employee_id}/training/skip", response_model=EmployeeOut, summary="Let an employee work with the real data without the training")
async def training_skip(employee_id: uuid.UUID, staff: Staff, session: DbSession, meta: Meta) -> EmployeeOut:
    staff.require("admin.employee.write")
    emp = await service.skip(session, employee_id, staff, meta)
    return await staff_service.employee_dto(session, emp, staff)


@router.post("/employees/{employee_id}/training/reset", response_model=EmployeeOut, summary="Send an employee (back) to the training — the whole plan, or only the tasks named")
async def training_reset(employee_id: uuid.UUID, staff: Staff, session: DbSession, meta: Meta, body: TrainingRestartIn | None = None) -> EmployeeOut:
    staff.require("admin.employee.write")
    emp = await service.reset(session, employee_id, staff, meta, body.tasks if body else None)
    return await staff_service.employee_dto(session, emp, staff)
