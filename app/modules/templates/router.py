"""Templates endpoints: result templates, assets, preview PDF (see ARCHITECTURE.md endpoint map)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Query, Response

from app.api.deps import DbSession, Meta, Staff
from app.modules.templates import service
from app.modules.templates.schemas import (
    TemplateAssetIn,
    TemplateAssetOut,
    TemplateCreateIn,
    TemplateOut,
    TemplatePreviewIn,
    TemplateQuery,
    TemplateStatusIn,
    TemplateUpdateIn,
)

router = APIRouter()

TEMPLATE_WRITE = "admin.template.write"
TEMPLATE_PUBLISH = "admin.template.publish"


@router.get("/companies/{company_id}/templates", response_model=list[TemplateOut], summary="Templates of a company (status, serviceTypeId, search)")
async def list_templates(company_id: uuid.UUID, q: Annotated[TemplateQuery, Query()], staff: Staff, session: DbSession) -> list[TemplateOut]:
    staff.scope(company_id)
    return await service.list_templates(session, company_id, q)


@router.post("/companies/{company_id}/templates", response_model=TemplateOut, status_code=201, summary="Create a template (draft v1)")
async def create_template(company_id: uuid.UUID, body: TemplateCreateIn, staff: Staff, session: DbSession, meta: Meta) -> TemplateOut:
    staff.require(TEMPLATE_WRITE).scope(company_id)
    return await service.create_template(session, company_id, body, staff, meta)


@router.get("/templates/{template_id}", response_model=TemplateOut, summary="Template details (full doc)")
async def get_template(template_id: uuid.UUID, staff: Staff, session: DbSession) -> TemplateOut:
    return await service.get_template_dto(session, template_id, staff)


@router.put("/templates/{template_id}", response_model=TemplateOut, summary="Update a template (version bump when active and doc changes)")
async def update_template(template_id: uuid.UUID, body: TemplateUpdateIn, staff: Staff, session: DbSession, meta: Meta) -> TemplateOut:
    staff.require(TEMPLATE_WRITE)
    return await service.update_template(session, template_id, body, staff, meta)


@router.post("/templates/{template_id}/status", response_model=TemplateOut, summary="Set template status (draft | active | archived)")
async def set_status(template_id: uuid.UUID, body: TemplateStatusIn, staff: Staff, session: DbSession, meta: Meta) -> TemplateOut:
    staff.require(TEMPLATE_PUBLISH)
    return await service.set_status(session, template_id, body.status, staff, meta)


@router.post("/templates/{template_id}/duplicate", response_model=TemplateOut, status_code=201, summary="Duplicate a template as a draft copy")
async def duplicate_template(template_id: uuid.UUID, staff: Staff, session: DbSession, meta: Meta) -> TemplateOut:
    staff.require(TEMPLATE_WRITE)
    return await service.duplicate_template(session, template_id, staff, meta)


@router.delete("/templates/{template_id}", status_code=204, summary="Soft-delete a template (not while active)")
async def delete_template(template_id: uuid.UUID, staff: Staff, session: DbSession, meta: Meta) -> Response:
    staff.require(TEMPLATE_WRITE)
    await service.delete_template(session, template_id, staff, meta)
    return Response(status_code=204)


@router.post("/templates/{template_id}/preview.pdf", summary="Render the template (or an unsaved doc) with sample data", response_class=Response, responses={200: {"content": {"application/pdf": {}}}})
async def preview_pdf(template_id: uuid.UUID, staff: Staff, session: DbSession, body: Annotated[TemplatePreviewIn | None, Body()] = None) -> Response:
    pdf = await service.preview_pdf(session, template_id, staff, body.doc if body else None)
    return Response(content=pdf, media_type="application/pdf", headers={"Content-Disposition": 'inline; filename="preview.pdf"', "Cache-Control": "no-store"})


@router.get("/companies/{company_id}/assets", response_model=list[TemplateAssetOut], summary="Template assets of a company (logo, stamp, signature, image)")
async def list_assets(company_id: uuid.UUID, staff: Staff, session: DbSession) -> list[TemplateAssetOut]:
    staff.scope(company_id)
    return await service.list_assets(session, company_id)


@router.post("/companies/{company_id}/assets", response_model=TemplateAssetOut, status_code=201, summary="Upload an asset (data URL) → stored file")
async def upload_asset(company_id: uuid.UUID, body: TemplateAssetIn, staff: Staff, session: DbSession, meta: Meta) -> TemplateAssetOut:
    staff.require(TEMPLATE_WRITE).scope(company_id)
    return await service.upload_asset(session, company_id, body, staff, meta)
