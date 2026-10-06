"""Tenant endpoints: companies (platform + own), branches, SMS test, Telegram bot settings."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import DbSession, Meta, Staff
from app.core.exceptions import ValidationError
from app.core.schemas import Page, PageQuery
from app.modules.tenant import reset, service
from app.modules.tenant.schemas import (
    BranchCreateIn,
    BranchOut,
    BranchSmsTemplatesIn,
    BranchSmsTemplatesOut,
    BranchUpdateIn,
    CompanyCreateIn,
    CompanyOut,
    CompanyUpdateIn,
    ResetIn,
    ResetOut,
    ResetPartOut,
    ResetPreviewOut,
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


@router.get("/branches/{branch_id}/sms-templates", response_model=BranchSmsTemplatesOut, summary="The branch's SMS texts (its own, or the company's while it has none)")
async def get_branch_sms_templates(branch_id: uuid.UUID, staff: Staff, session: DbSession) -> BranchSmsTemplatesOut:
    return await service.get_branch_sms_templates(session, branch_id, staff)


@router.put("/branches/{branch_id}/sms-templates", response_model=BranchSmsTemplatesOut, summary="Save the branch's own SMS texts (applyToAll: every branch)")
async def set_branch_sms_templates(branch_id: uuid.UUID, body: BranchSmsTemplatesIn, staff: Staff, session: DbSession, meta: Meta) -> BranchSmsTemplatesOut:
    staff.require("admin.settings.write", "admin.company.write")
    return await service.set_branch_sms_templates(session, branch_id, body, staff, meta)


# ----------------------------------------------------------------------------- superadmin reset ("like newborn")


@router.get("/companies/{company_id}/reset", response_model=ResetPreviewOut, summary="Superadmin: what a company reset would remove (no changes)")
async def company_reset_preview(company_id: uuid.UUID, staff: Staff, session: DbSession) -> ResetPreviewOut:
    reset.require_superadmin(staff)
    company = await service.get_company_or_404(session, company_id)
    counts = await reset.preview_company(session, company, staff)
    parts = [ResetPartOut(key=k, counts=counts[k], requires=list(reset.COMPANY_REQUIRES.get(k, ()))) for k in reset.COMPANY_PARTS]
    return ResetPreviewOut(target="company", id=str(company.id), name=company.name, confirm_word=company.slug, parts=parts)


@router.post("/companies/{company_id}/reset", response_model=ResetOut, summary="Superadmin: irreversibly reset a company (chosen parts)")
async def company_reset(company_id: uuid.UUID, body: ResetIn, staff: Staff, session: DbSession, meta: Meta) -> ResetOut:
    reset.require_superadmin(staff)
    company = await service.get_company_or_404(session, company_id)
    if body.confirm.strip().lower() != company.slug.lower():
        raise ValidationError(reset.CONFIRM_MISMATCH, code="confirm")
    parts = reset.close_parts(body.parts, reset.COMPANY_PARTS, reset.COMPANY_REQUIRES)
    return ResetOut(parts=parts, counts=await reset.reset_company(session, company, parts, staff, meta))


@router.get("/branches/{branch_id}/reset", response_model=ResetPreviewOut, summary="Superadmin: what a branch reset would remove (no changes)")
async def branch_reset_preview(branch_id: uuid.UUID, staff: Staff, session: DbSession) -> ResetPreviewOut:
    reset.require_superadmin(staff)
    branch = await service.get_branch_or_404(session, branch_id, None)
    counts = await reset.preview_branch(session, branch, staff)
    parts = [ResetPartOut(key=k, counts=counts[k], requires=list(reset.BRANCH_REQUIRES.get(k, ()))) for k in reset.BRANCH_PARTS]
    return ResetPreviewOut(target="branch", id=str(branch.id), name=branch.name, confirm_word=branch.code, parts=parts)


@router.post("/branches/{branch_id}/reset", response_model=ResetOut, summary="Superadmin: irreversibly reset one branch (chosen parts)")
async def branch_reset(branch_id: uuid.UUID, body: ResetIn, staff: Staff, session: DbSession, meta: Meta) -> ResetOut:
    reset.require_superadmin(staff)
    branch = await service.get_branch_or_404(session, branch_id, None)
    if body.confirm.strip().upper() != branch.code.upper():
        raise ValidationError(reset.CONFIRM_MISMATCH, code="confirm")
    parts = reset.close_parts(body.parts, reset.BRANCH_PARTS, reset.BRANCH_REQUIRES)
    return ResetOut(parts=parts, counts=await reset.reset_branch(session, branch, parts, staff, meta))


@router.post("/companies/{company_id}/sms/test", response_model=SmsTestOut, summary="Send a real test SMS via Xabarchi")
async def sms_test(company_id: uuid.UUID, staff: Staff, session: DbSession, meta: Meta, body: SmsTestIn | None = None) -> SmsTestOut:
    staff.require("admin.settings.write", "admin.company.write").scope(company_id)
    return await service.send_test_sms(session, company_id, body.to if body else None, staff, meta)


@router.put("/companies/{company_id}/telegram", response_model=CompanyOut, summary="Connect / disconnect the company Telegram bot")
async def set_telegram(company_id: uuid.UUID, body: TelegramSettingsIn, staff: Staff, session: DbSession, meta: Meta) -> CompanyOut:
    staff.require("admin.settings.write", "admin.company.write").scope(company_id)
    return await service.set_telegram(session, company_id, body, staff, meta)
