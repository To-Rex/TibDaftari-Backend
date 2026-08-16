"""Auth DTOs — mirror of Clinic-Web `src/domain/access/auth.ts`."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from app.core.schemas import CamelModel


class StaffLoginIn(CamelModel):
    login: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)


class StaffSessionOut(CamelModel):
    actor: Literal["staff"] = "staff"
    employee_id: str
    company_id: str
    branch_id: str | None
    is_super_admin: bool
    role_key: str
    full_name: str
    permissions: list[str]
    access_token: str
    expires_at: datetime


class PatientOtpRequestIn(CamelModel):
    phone: str = Field(min_length=7, max_length=20)


class PatientOtpRequestOut(CamelModel):
    challenge_id: str
    dev_code: str | None = None
    expires_in: int


class PatientOtpVerifyIn(CamelModel):
    phone: str = Field(min_length=0, max_length=20)
    code: str = Field(min_length=3, max_length=8)
    challenge_id: str = Field(min_length=8, max_length=64)


class PatientSessionOut(CamelModel):
    actor: Literal["patient"] = "patient"
    patient_id: str
    phone: str
    full_name: str
    access_token: str
    expires_at: datetime
