"""Templates endpoints: result templates, assets, preview PDF (see ARCHITECTURE.md endpoint map).

`/units/{unit_id}/…` routes take the selected unit as a `Scope` (access already checked); id-addressed routes
(`/templates/{template_id}`) load the row and check that its owner unit is visible to the caller.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Query, Response

from app.api.deps import DbSession, Meta, Scope, Staff
from app.modules.templates import service
from app.modules.templates.schemas import (
    DefaultReceiptIn,
    TemplateAssetIn,
    TemplateAssetOut,
    TemplateCreateIn,
    TemplateDuplicateIn,
    TemplateOut,
    TemplatePreviewIn,
    TemplateQuery,
    TemplateStatusIn,
    TemplateUpdateIn,
)

router = APIRouter()

TEMPLATE_WRITE = "admin.template.write"
TEMPLATE_PUBLISH = "admin.template.publish"


@router.get("/units/{unit_id}/templates", response_model=list[TemplateOut], summary="Templates managed from a unit (status, serviceTypeId, unitId, search)")
async def list_templates(scope: Scope, q: Annotated[TemplateQuery, Query()], session: DbSession) -> list[TemplateOut]:
    return await service.list_templates(session, scope, q)


@router.post("/units/{unit_id}/templates", response_model=TemplateOut, status_code=201, summary="Create a template owned by the unit (draft v1)")
async def create_template(scope: Scope, body: TemplateCreateIn, staff: Staff, session: DbSession, meta: Meta) -> TemplateOut:
    staff.require(TEMPLATE_WRITE)
    return await service.create_template(session, scope, body, staff, meta)


@router.post("/units/{unit_id}/templates/default-receipt", response_model=TemplateOut, status_code=201, summary="Create the standard cheque as a new draft receipt template owned by the unit")
async def create_default_receipt(scope: Scope, staff: Staff, session: DbSession, meta: Meta, body: DefaultReceiptIn | None = None) -> TemplateOut:
    staff.require(TEMPLATE_WRITE)
    return await service.create_default_receipt(session, scope, body or DefaultReceiptIn(), staff, meta)


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


@router.post("/templates/{template_id}/duplicate", response_model=TemplateOut, status_code=201, summary="Duplicate a template as a draft copy (optionally renamed / re-bound to units)")
async def duplicate_template(template_id: uuid.UUID, staff: Staff, session: DbSession, meta: Meta, body: Annotated[TemplateDuplicateIn | None, Body()] = None) -> TemplateOut:
    staff.require(TEMPLATE_WRITE)
    return await service.duplicate_template(session, template_id, staff, meta, body)


@router.delete("/templates/{template_id}", status_code=204, summary="Soft-delete a template (not while active)")
async def delete_template(template_id: uuid.UUID, staff: Staff, session: DbSession, meta: Meta) -> Response:
    staff.require(TEMPLATE_WRITE)
    await service.delete_template(session, template_id, staff, meta)
    return Response(status_code=204)


@router.post("/templates/{template_id}/preview.pdf", summary="Render the template (or an unsaved doc) with sample data", response_class=Response, responses={200: {"content": {"application/pdf": {}}}})
async def preview_pdf(template_id: uuid.UUID, staff: Staff, session: DbSession, body: Annotated[TemplatePreviewIn | None, Body()] = None) -> Response:
    pdf = await service.preview_pdf(session, template_id, staff, body.doc if body else None)
    return Response(content=pdf, media_type="application/pdf", headers={"Content-Disposition": 'inline; filename="preview.pdf"', "Cache-Control": "no-store"})


@router.get("/units/{unit_id}/assets", response_model=list[TemplateAssetOut], summary="Template assets visible from a unit (logo, stamp, signature, image)")
async def list_assets(scope: Scope, session: DbSession) -> list[TemplateAssetOut]:
    return await service.list_assets(session, scope)


@router.post("/units/{unit_id}/assets", response_model=TemplateAssetOut, status_code=201, summary="Upload an asset owned by the unit (data URL) → stored file")
async def upload_asset(scope: Scope, body: TemplateAssetIn, staff: Staff, session: DbSession, meta: Meta) -> TemplateAssetOut:
    staff.require(TEMPLATE_WRITE)
    return await service.upload_asset(session, scope, body, staff, meta)


@router.delete("/units/{unit_id}/assets/{asset_id}", status_code=204, summary="Delete a template asset (soft; printed documents keep their bytes)")
async def delete_asset(scope: Scope, asset_id: uuid.UUID, staff: Staff, session: DbSession, meta: Meta) -> Response:
    staff.require(TEMPLATE_WRITE)
    await service.delete_asset(session, scope, asset_id, staff, meta)
    return Response(status_code=204)
