"""Patients endpoints (+ public regions/districts reference data). HTTP only — rules live in service.py."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query

from app.api.deps import DbSession, Meta, Staff
from app.core.schemas import Page, PageQuery
from app.modules.patients import service
from app.modules.patients.schemas import (
    DistrictOut,
    PatientDuplicatesIn,
    PatientOut,
    PatientPatchIn,
    PatientUpsertIn,
    RegionOut,
)

router = APIRouter()


class PatientListQuery(PageQuery):
    """PageQuery + `tag` (exact match)."""

    tag: str | None = None


@router.get(
    "/companies/{company_id}/patients", response_model=Page[PatientOut], summary="List patients (paged, search, tag)"
)
async def list_patients(
    company_id: uuid.UUID, q: Annotated[PatientListQuery, Query()], staff: Staff, session: DbSession
) -> Page[PatientOut]:
    staff.require("reception.patient.read").scope(company_id)
    return await service.list_patients(session, company_id, q, q.tag)


@router.get(
    "/companies/{company_id}/patients/search", response_model=list[PatientOut], summary="Quick search (bare array)"
)
async def search_patients(
    company_id: uuid.UUID,
    staff: Staff,
    session: DbSession,
    q: Annotated[str, Query(max_length=120)] = "",
    limit: Annotated[int, Query(ge=1, le=100)] = 12,
) -> list[PatientOut]:
    staff.require("reception.patient.read").scope(company_id)
    return await service.search_patients(session, company_id, q, limit)


@router.post(
    "/companies/{company_id}/patients/duplicates", response_model=list[PatientOut], summary="Find identity duplicates"
)
async def find_duplicates(
    company_id: uuid.UUID, body: PatientDuplicatesIn, staff: Staff, session: DbSession
) -> list[PatientOut]:
    staff.require("reception.patient.read").scope(company_id)
    return await service.find_duplicates(session, company_id, body)


@router.post("/companies/{company_id}/patients", response_model=PatientOut, status_code=201, summary="Create patient")
async def create_patient(
    company_id: uuid.UUID, body: PatientUpsertIn, staff: Staff, session: DbSession, meta: Meta
) -> PatientOut:
    staff.require("reception.patient.write").scope(company_id)
    return await service.create_patient(session, company_id, staff, body, meta)


@router.get("/patients/{patient_id}", response_model=PatientOut, summary="Get patient (company-scoped)")
async def get_patient(patient_id: uuid.UUID, staff: Staff, session: DbSession) -> PatientOut:
    staff.require("reception.patient.read")
    return service.patient_out(await service.get_patient_or_404(session, patient_id, service.scope_company(staff)))


@router.put("/patients/{patient_id}", response_model=PatientOut, summary="Update patient (partial)")
async def update_patient(
    patient_id: uuid.UUID, body: PatientPatchIn, staff: Staff, session: DbSession, meta: Meta
) -> PatientOut:
    staff.require("reception.patient.write")
    return await service.update_patient(session, patient_id, staff, body, meta)


@router.get("/regions", response_model=list[RegionOut], summary="Regions of Uzbekistan (public reference data)")
async def list_regions(session: DbSession) -> list[dict[str, Any]]:
    return await service.list_regions(session)


@router.get(
    "/districts", response_model=list[DistrictOut], summary="Districts, optionally by region (public reference data)"
)
async def list_districts(
    session: DbSession, region_id: Annotated[uuid.UUID | None, Query(alias="regionId")] = None
) -> list[dict[str, Any]]:
    return await service.list_districts(session, region_id)
