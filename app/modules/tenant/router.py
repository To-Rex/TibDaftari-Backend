"""Tenant endpoints: companies (platform + own), branches, SMS test, Telegram bot settings."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import DbSession, Meta, Staff
from app.core.schemas import Page, PageQuery
from app.modules.tenant import service
from app.modules.tenant.schemas import (
    BranchCreateIn,
    BranchOut,
    BranchUpdateIn,
    CompanyCreateIn,
    CompanyOut,
    CompanyUpdateIn,
    SmsTestIn,
    SmsTestOut,
    TelegramSettingsIn,
)

router = APIRouter()


@router.get("/companies", response_model=Page[CompanyOut], summary="List companies (platform admin)")
async def list_companies(q: Annotated[PageQuery, Query()], staff: Staff, session: DbSession) -> Page[CompanyOut]:
    staff.require("platform.company.manage")
    return await service.list_companies(session, q)


@router.post("/companies", response_model=CompanyOut, status_code=201, summary="Create a company (platform admin)")
async def create_company(body: CompanyCreateIn, staff: Staff, session: DbSession, meta: Meta) -> CompanyOut:
    staff.require("platform.company.manage")
    return await service.create_company(session, body, staff, meta)


@router.get("/companies/{company_id}", response_model=CompanyOut, summary="Company details (own company or platform admin)")
async def get_company(company_id: uuid.UUID, staff: Staff, session: DbSession) -> CompanyOut:
    staff.scope(company_id)
    return await service.get_company_dto(session, company_id)


@router.put("/companies/{company_id}", response_model=CompanyOut, summary="Update company (partial; sms replaced wholesale)")
async def update_company(company_id: uuid.UUID, body: CompanyUpdateIn, staff: Staff, session: DbSession, meta: Meta) -> CompanyOut:
    # SMS settings page (admin.settings.write) may save `sms` / `smsTemplates` only; identity fields need admin.company.write.
    if set(body.model_fields_set) <= {"sms", "sms_templates"}:
        staff.require("admin.company.write", "admin.settings.write").scope(company_id)
    else:
        staff.require("admin.company.write").scope(company_id)
    return await service.update_company(session, company_id, body, staff, meta)


@router.get("/companies/{company_id}/branches", response_model=list[BranchOut], summary="Branches of a company")
async def list_branches(company_id: uuid.UUID, staff: Staff, session: DbSession) -> list[BranchOut]:
    staff.scope(company_id)
    return await service.list_branches(session, company_id)


@router.post("/companies/{company_id}/branches", response_model=BranchOut, status_code=201, summary="Create a branch")
async def create_branch(company_id: uuid.UUID, body: BranchCreateIn, staff: Staff, session: DbSession, meta: Meta) -> BranchOut:
    staff.require("admin.branch.write").scope(company_id)
    return await service.create_branch(session, company_id, body, staff, meta)


@router.put("/branches/{branch_id}", response_model=BranchOut, summary="Update a branch (partial)")
async def update_branch(branch_id: uuid.UUID, body: BranchUpdateIn, staff: Staff, session: DbSession, meta: Meta) -> BranchOut:
    staff.require("admin.branch.write")
    return await service.update_branch(session, branch_id, body, staff, meta)


@router.post("/companies/{company_id}/sms/test", response_model=SmsTestOut, summary="Send a real test SMS via Xabarchi")
async def sms_test(company_id: uuid.UUID, staff: Staff, session: DbSession, meta: Meta, body: SmsTestIn | None = None) -> SmsTestOut:
    staff.require("admin.settings.write", "admin.company.write").scope(company_id)
    return await service.send_test_sms(session, company_id, body.to if body else None, staff, meta)


@router.put("/companies/{company_id}/telegram", response_model=CompanyOut, summary="Connect / disconnect the company Telegram bot")
async def set_telegram(company_id: uuid.UUID, body: TelegramSettingsIn, staff: Staff, session: DbSession, meta: Meta) -> CompanyOut:
    staff.require("admin.settings.write", "admin.company.write").scope(company_id)
    return await service.set_telegram(session, company_id, body, staff, meta)
