"""Staff endpoints: employees (list/get/create/update/overrides) and roles (list/create/update/delete).

Unit-scoped lists and creates live under `/units/{unit_id}/…` (the `Scope` dependency checks the caller may act
there); rows fetched by id are checked against the caller's scope in the service."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response

from app.api.deps import DbSession, Meta, Scope, Staff
from app.core.schemas import Page
from app.modules.staff import service
from app.modules.staff.schemas import (
    EmployeeCreateIn,
    EmployeeOut,
    EmployeeQuery,
    EmployeeUpdateIn,
    PermissionOverrides,
    RoleCreateIn,
    RoleOut,
    RoleUpdateIn,
)

router = APIRouter()


@router.get("/units/{unit_id}/employees", response_model=Page[EmployeeOut], summary="List employees under a unit")
async def list_employees(scope: Scope, q: Annotated[EmployeeQuery, Query()], staff: Staff, session: DbSession) -> Page[EmployeeOut]:
    staff.require("admin.employee.read")
    return await service.list_employees(session, scope, q, staff)


@router.post("/units/{unit_id}/employees", response_model=EmployeeOut, status_code=201, summary="Create an employee")
async def create_employee(scope: Scope, body: EmployeeCreateIn, staff: Staff, session: DbSession, meta: Meta) -> EmployeeOut:
    staff.require("admin.employee.write")
    return await service.create_employee(session, scope, body, staff, meta)


@router.get("/employees/{employee_id}", response_model=EmployeeOut, summary="Employee details")
async def get_employee(employee_id: uuid.UUID, staff: Staff, session: DbSession) -> EmployeeOut:
    # Self-read is always allowed (lab category restriction); operational roles may resolve
    # colleagues inside their own scope (order creator); unit scope is enforced in the service.
    if employee_id != staff.id:
        staff.require("admin.employee.read", "reception.order.create", "lab.worklist.read", "confirm.result.read")
    return await service.get_employee_dto(session, employee_id, staff)


@router.put("/employees/{employee_id}", response_model=EmployeeOut, summary="Update an employee (partial)")
async def update_employee(employee_id: uuid.UUID, body: EmployeeUpdateIn, staff: Staff, session: DbSession, meta: Meta) -> EmployeeOut:
    staff.require("admin.employee.write")
    return await service.update_employee(session, employee_id, body, staff, meta)


@router.put("/employees/{employee_id}/overrides", response_model=EmployeeOut, summary="Replace permission overrides")
async def set_overrides(employee_id: uuid.UUID, body: PermissionOverrides, staff: Staff, session: DbSession, meta: Meta) -> EmployeeOut:
    staff.require("admin.employee.write", "admin.role.write")
    return await service.set_overrides(session, employee_id, body, staff, meta)


@router.get("/units/{unit_id}/roles", response_model=list[RoleOut], summary="System roles + roles scoped to the unit's lineage")
async def list_roles(scope: Scope, session: DbSession) -> list[RoleOut]:
    return await service.list_roles(session, scope)


@router.post("/units/{unit_id}/roles", response_model=RoleOut, status_code=201, summary="Create a role scoped to the unit")
async def create_role(scope: Scope, body: RoleCreateIn, staff: Staff, session: DbSession, meta: Meta) -> RoleOut:
    staff.require("admin.role.write")
    return await service.create_role(session, scope, body, staff, meta)


@router.put("/roles/{role_id}", response_model=RoleOut, summary="Update a role (partial)")
async def update_role(role_id: uuid.UUID, body: RoleUpdateIn, staff: Staff, session: DbSession, meta: Meta) -> RoleOut:
    staff.require("admin.role.write")
    return await service.update_role(session, role_id, body, staff, meta)


@router.delete("/roles/{role_id}", status_code=204, summary="Delete a role (soft)")
async def delete_role(role_id: uuid.UUID, staff: Staff, session: DbSession, meta: Meta) -> Response:
    staff.require("admin.role.write")
    await service.delete_role(session, role_id, staff, meta)
    return Response(status_code=204)
