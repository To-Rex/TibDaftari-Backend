"""Patient DTOs — mirror of Clinic-Web `src/domain/patient.ts` (Patient, PatientUpsertInput, Country, Region, District)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import Field

from app.core.schemas import CamelModel, PageQuery

Gender = Literal["male", "female"]
#: who a patient is to the person they share a phone number with (`relatedPatientId`)
Relation = Literal[
    "child", "spouse", "father", "mother", "brother_older", "brother_younger",
    "sister_older", "sister_younger", "grandchild", "grandparent", "relative", "other",
]  # fmt: skip
RELATIONS: tuple[str, ...] = Relation.__args__  # type: ignore[attr-defined]


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
    # identity scope: one registry per country
    country_id: str
    # the facility that registered the patient (null: legacy, before units owned patients)
    registered_unit_id: str | None = None
    registered_unit_name: str | None = None
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
    #: one phone, a whole family: who this patient is to `relatedPatientId` (usually the number's owner)
    relation: Relation | None = None
    related_patient_id: str | None = None
    related_patient_name: str | None = None
    #: how many OTHER patients of the country use the same phone number
    phone_shared: int = 0


class PatientListQuery(PageQuery):
    """PageQuery + `tag` (exact match) + `unitId` (a sub-unit of the path unit — only its subtree)."""

    tag: str | None = None
    unit_id: str | None = None


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
    # the facility registering the patient (default: the path unit when it is a facility)
    registered_unit_id: str | None = Field(default=None, max_length=64)
    #: the number already belongs to someone: who this person is to them (required when there is no passport / PINFL)
    relation: Relation | None = None
    #: the person on the number the relation points to (default: the number's owner)
    related_patient_id: str | None = Field(default=None, max_length=64)


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
    relation: Relation | None = None
    related_patient_id: str | None = Field(default=None, max_length=64)


class FamilyMemberOut(CamelModel):
    """A person on the same phone number (or linked by a relation): enough to recognise them and open their card."""

    id: str
    full_name: str
    phone: str
    gender: Gender | None = None
    birth_date: date | None = None
    relation: Relation | None = None
    related_patient_id: str | None = None
    #: the number's owner — the person without a relation the others point to
    is_owner: bool = False
    registered_unit_name: str | None = None
    last_visit_at: datetime | None = None


class PatientFamilyOut(CamelModel):
    """The patient's family as the registry sees it: everyone on the same number, plus relatives linked to / from them."""

    patient_id: str
    phone: str
    members: list[FamilyMemberOut]


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
    # the facility the employee works in (top bar) — its SMS account sends the text; default: the patient's
    # registered facility (else the facility of their latest cheque)
    unit_id: str | None = None


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
    # per-country operating settings
    currency_code: str | None = None
    default_locale: str | None = None
    timezone: str | None = None
    phone_mask: str | None = None
    is_enabled: bool | None = None


class RegionOut(CamelModel):
    id: str
    country_id: str
    name: str


class DistrictOut(CamelModel):
    id: str
    region_id: str
    name: str
    # tuman | shahar
    kind: Literal["district", "city"] = "district"
