"""Patients endpoints (+ public countries/regions/districts reference data). HTTP only — rules live in service.py.

Lists live under the selected unit (`/units/{unit_id}/patients` — everything registered at / seen in its subtree);
id-addressed routes (`/patients/{id}`) check the caller's allowed units in the service."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query

from app.api.deps import DbSession, Meta, Scope, Staff
from app.core.schemas import Page
from app.modules.patients import portal_login, service
from app.modules.patients.schemas import (
    CountryOut,
    DistrictOut,
    PatientDuplicatesIn,
    PatientListQuery,
    PatientOut,
    PatientPatchIn,
    PatientUpsertIn,
    PortalAccessIn,
    PortalAccessOut,
    RegionOut,
)

router = APIRouter()


@router.get("/units/{unit_id}/patients", response_model=Page[PatientOut], summary="List patients of the unit's subtree (paged, search, tag, unitId narrowing)")
async def list_patients(scope: Scope, q: Annotated[PatientListQuery, Query()], staff: Staff, session: DbSession) -> Page[PatientOut]:
    staff.require("reception.patient.read")
    return await service.list_patients(session, scope, q, staff)


@router.get("/units/{unit_id}/patients/search", response_model=list[PatientOut], summary="Quick search (bare array)")
async def search_patients(
    scope: Scope,
    staff: Staff,
    session: DbSession,
    q: Annotated[str, Query(max_length=120)] = "",
    limit: Annotated[int, Query(ge=1, le=100)] = 12,
    sub_unit_id: Annotated[uuid.UUID | None, Query(alias="unitId")] = None,
) -> list[PatientOut]:
    staff.require("reception.patient.read")
    return await service.search_patients(session, scope, staff, q, limit, sub_unit_id)


@router.post("/units/{unit_id}/patients/duplicates", response_model=list[PatientOut], summary="Find identity duplicates (country-wide)")
async def find_duplicates(scope: Scope, body: PatientDuplicatesIn, staff: Staff, session: DbSession) -> list[PatientOut]:
    staff.require("reception.patient.read")
    return await service.find_duplicates(session, scope, body)


@router.post("/units/{unit_id}/patients", response_model=PatientOut, status_code=201, summary="Register a patient at a facility")
async def create_patient(scope: Scope, body: PatientUpsertIn, staff: Staff, session: DbSession, meta: Meta) -> PatientOut:
    staff.require("reception.patient.write")
    return await service.create_patient(session, scope, staff, body, meta)


@router.get("/patients/{patient_id}", response_model=PatientOut, summary="Get patient (statistics inside ?unitId's subtree)")
async def get_patient(
    patient_id: uuid.UUID, staff: Staff, session: DbSession, unit_id: Annotated[uuid.UUID | None, Query(alias="unitId")] = None
) -> PatientOut:
    staff.require("reception.patient.read", "confirm.result.view")  # the patient block of an approved result
    return await service.get_patient_dto(session, patient_id, staff, unit_id)


@router.put("/patients/{patient_id}", response_model=PatientOut, summary="Update patient (partial)")
async def update_patient(patient_id: uuid.UUID, body: PatientPatchIn, staff: Staff, session: DbSession, meta: Meta) -> PatientOut:
    staff.require("reception.patient.write")
    return await service.update_patient(session, patient_id, staff, body, meta)


@router.put(
    "/patients/{patient_id}/portal-access",
    response_model=PortalAccessOut,
    summary="Issue / renew the patient's portal login + password (the password is returned once)",
)
async def set_portal_access(patient_id: uuid.UUID, body: PortalAccessIn, staff: Staff, session: DbSession, meta: Meta) -> PortalAccessOut:
    staff.require("reception.patient.write")
    return await portal_login.set_portal_access(session, patient_id, staff, body, meta)


@router.delete("/patients/{patient_id}/portal-access", response_model=PatientOut, summary="Switch the portal login off")
async def remove_portal_access(patient_id: uuid.UUID, staff: Staff, session: DbSession, meta: Meta) -> PatientOut:
    staff.require("reception.patient.write")
    return await portal_login.remove_portal_access(session, patient_id, staff, meta)


@router.get("/countries", response_model=list[CountryOut], summary="Countries (public reference data)")
async def list_countries(session: DbSession) -> list[dict[str, Any]]:
    return await service.list_countries(session)


@router.get("/regions", response_model=list[RegionOut], summary="Regions of a country — default: Uzbekistan (public reference data)")
async def list_regions(session: DbSession, country_id: Annotated[uuid.UUID | None, Query(alias="countryId")] = None) -> list[dict[str, Any]]:
    return await service.list_regions(session, country_id)


@router.get("/districts", response_model=list[DistrictOut], summary="Districts, optionally by region (public reference data)")
async def list_districts(session: DbSession, region_id: Annotated[uuid.UUID | None, Query(alias="regionId")] = None) -> list[dict[str, Any]]:
    return await service.list_districts(session, region_id)
