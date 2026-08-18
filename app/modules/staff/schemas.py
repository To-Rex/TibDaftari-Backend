"""Staff DTOs — mirror of Clinic-Web `Employee` (domain/tenant.ts) and `Role` / `PermissionOverrides`
(domain/access/permissions.ts)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from app.core.schemas import CamelModel, PageQuery

EmployeeStatus = Literal["active", "inactive"]


class PermissionOverrides(CamelModel):
    allow: list[str] = Field(default_factory=list, max_length=64)
    deny: list[str] = Field(default_factory=list, max_length=64)


class EmployeeOut(CamelModel):
    id: str
    company_id: str
    branch_ids: list[str]
    full_name: str
    login: str
    phone: str | None = None
    email: str | None = None
    role_id: str
    overrides: PermissionOverrides
    category_ids: list[str]
    status: EmployeeStatus
    avatar_hue: int
    last_login_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class EmployeeQuery(PageQuery):
    branch_id: str | None = None
    role_id: str | None = None
    status: EmployeeStatus | None = None


class EmployeeCreateIn(CamelModel):
    """Partial<Employee> + password; `isSuperAdmin` is honoured only for superadmin callers."""

    full_name: str = Field(min_length=1, max_length=200)
    login: str = Field(min_length=3, max_length=80)
    password: str | None = Field(default=None, min_length=4, max_length=200)
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=200)
    role_id: str
    branch_ids: list[str] = Field(default_factory=list, max_length=100)
    category_ids: list[str] = Field(default_factory=list, max_length=500)
    overrides: PermissionOverrides | None = None
    status: EmployeeStatus = "active"
    is_super_admin: bool | None = None


class EmployeeUpdateIn(CamelModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    login: str | None = Field(default=None, min_length=3, max_length=80)
    password: str | None = Field(default=None, min_length=4, max_length=200)
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=200)
    role_id: str | None = None
    branch_ids: list[str] | None = Field(default=None, max_length=100)
    category_ids: list[str] | None = Field(default=None, max_length=500)
    overrides: PermissionOverrides | None = None
    status: EmployeeStatus | None = None
    is_super_admin: bool | None = None


class RoleOut(CamelModel):
    id: str
    company_id: str | None
    key: str
    name: str
    description: str | None = None
    permissions: list[str]
    is_system: bool
    # denormalised for the roles page (employees of the requesting company using this role)
    employee_count: int = 0


class RoleCreateIn(CamelModel):
    name: str = Field(min_length=1, max_length=120)
    key: str | None = Field(default=None, min_length=1, max_length=60)
    description: str | None = Field(default=None, max_length=1000)
    permissions: list[str] = Field(default_factory=list, max_length=64)


class RoleUpdateIn(CamelModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    key: str | None = Field(default=None, min_length=1, max_length=60)
    description: str | None = Field(default=None, max_length=1000)
    permissions: list[str] | None = Field(default=None, max_length=64)
