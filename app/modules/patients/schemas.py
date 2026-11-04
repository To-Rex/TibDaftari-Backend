"""Patient DTOs — mirror of Clinic-Web `src/domain/patient.ts` (Patient, PatientUpsertInput, Country, Region, District)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import Field

from app.core.schemas import CamelModel

Gender = Literal["male", "female"]


class PatientAddress(CamelModel):
    region_id: str | None = None
    district_id: str | None = None
    street: str | None = Field(default=None, max_length=300)


class PatientStatsOut(CamelModel):
    orders: int
    last_visit_at: datetime | None = None
    total_spent: int


class PatientPortalOut(CamelModel):
    linked: bool
    telegram_chat_id: str | None = None
    # portal login issued at reception (the password itself is never returned)
    login: str | None = None
    login_set_at: datetime | None = None


class PatientOut(CamelModel):
    id: str
    company_id: str
    branch_id: str | None = None
    full_name: str
    phone: str
    phone_extra: str | None = None
    gender: Gender | None = None
    birth_date: date | None = None
    passport_number: str | None = None
    pinfl: str | None = None
    address: PatientAddress
    workplace: str | None = None
    discount_percent: int
    contract_number: str | None = None
    note: str | None = None
    tags: list[str]
    stats: PatientStatsOut
    portal: PatientPortalOut
    created_at: datetime
    updated_at: datetime
    created_by: str | None = None


class PatientUpsertIn(CamelModel):
    """`PatientUpsertInput` — create payload (fullName + phone required)."""

    full_name: str = Field(min_length=1, max_length=200)
    phone: str = Field(min_length=1, max_length=30)
    phone_extra: str | None = Field(default=None, max_length=30)
    gender: Gender | None = None
    birth_date: date | None = None
    passport_number: str | None = Field(default=None, max_length=20)
    pinfl: str | None = Field(default=None, max_length=20)
    address: PatientAddress | None = None
    workplace: str | None = Field(default=None, max_length=200)
    discount_percent: int | None = None
    contract_number: str | None = Field(default=None, max_length=60)
    note: str | None = Field(default=None, max_length=5000)
    tags: list[str] | None = None
    # the branch registering the patient (default: the employee's own branch)
    branch_id: str | None = Field(default=None, max_length=64)


class PatientPatchIn(CamelModel):
    """`Partial<PatientUpsertInput>` — only the keys present in the body are applied."""

    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    phone: str | None = Field(default=None, min_length=1, max_length=30)
    phone_extra: str | None = Field(default=None, max_length=30)
    gender: Gender | None = None
    birth_date: date | None = None
    passport_number: str | None = Field(default=None, max_length=20)
    pinfl: str | None = Field(default=None, max_length=20)
    address: PatientAddress | None = None
    workplace: str | None = Field(default=None, max_length=200)
    discount_percent: int | None = None
    contract_number: str | None = Field(default=None, max_length=60)
    note: str | None = Field(default=None, max_length=5000)
    tags: list[str] | None = None


class PatientDuplicatesIn(CamelModel):
    """`Partial<PatientUpsertInput>` — only the identity keys matter."""

    phone: str | None = Field(default=None, max_length=30)
    passport_number: str | None = Field(default=None, max_length=20)
    pinfl: str | None = Field(default=None, max_length=20)


class PortalAccessIn(CamelModel):
    """Issue / renew the portal login: an empty login keeps the current one (or picks a free one), an empty
    password is generated. Every call sets a new password."""

    login: str | None = Field(default=None, max_length=40)
    password: str | None = Field(default=None, min_length=6, max_length=128)
    send_sms: bool = False
    # the branch the employee works in (top bar) — its SMS account sends the text; default: the patient's branch
    branch_id: str | None = None


class PortalAccessOut(CamelModel):
    """The credentials, shown to the employee once (only the hash is stored)."""

    login: str
    password: str
    # None = no SMS asked for
    sms: Literal["queued", "not_configured", "invalid_phone"] | None = None
    patient: PatientOut


class CountryOut(CamelModel):
    id: str
    code: str
    name: str
    name_ru: str | None = None
    name_en: str | None = None
    phone_code: str | None = None


class RegionOut(CamelModel):
    id: str
    country_id: str
    name: str


class DistrictOut(CamelModel):
    id: str
    region_id: str
    name: str
