"""Auth DTOs — mirror of Clinic-Web `src/domain/access/auth.ts`."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from app.core.schemas import CamelModel


class StaffLoginIn(CamelModel):
    login: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)


class SessionUnitOut(CamelModel):
    """The employee's home unit as the UI needs it on first paint (the full card comes from `GET /units/{id}`)."""

    id: str
    level: Literal["country", "region", "district", "facility"]
    name: str
    code: str | None = None
    country_id: str
    #: "O‘zbekiston Respublikasi › Xorazm viloyati › Bog‘ot tumani › Bog‘ot tumani SES"
    path: str


class StaffSessionOut(CamelModel):
    actor: Literal["staff"] = "staff"
    employee_id: str
    #: home unit (any level); None for a superadmin, who picks any unit of the tree
    unit_id: str | None
    #: every scope root of the employee (home + extra units) — the UI lets them switch among these subtrees
    unit_ids: list[str] = []
    unit: SessionUnitOut | None = None
    #: the home unit when it is a facility — where this employee's cheques are written
    facility_id: str | None = None
    is_super_admin: bool
    role_key: str
    #: the held role's display name («Viloyat rahbari», «Registrator / Kassir» …) — what the UI shows next to the user
    role_name: str | None = None
    #: the held role (None for an employee without one) — the UI locks the editing of one's own role
    role_id: str | None = None
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


class PatientLoginIn(CamelModel):
    """Portal login + password issued at reception."""

    login: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)


class PatientPasswordChangeIn(CamelModel):
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=6, max_length=128)


class PatientSessionOut(CamelModel):
    actor: Literal["patient"] = "patient"
    patient_id: str
    phone: str
    full_name: str
    access_token: str
    expires_at: datetime
    # otp = phone + SMS code (every patient of the phone); login = portal login + password (this patient only)
    via: Literal["otp", "login"] = "otp"
    login: str | None = None
