"""Patient portal endpoints (patient bearer token). Prefix `/portal` is applied by `api/router.py`."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Response

from app.api.deps import DbSession, Patient
from app.modules.portal import service
from app.modules.portal.schemas import PortalDocumentOut, PortalOrderOut, PortalOverviewOut

router = APIRouter()


@router.get("/overview", response_model=PortalOverviewOut, summary="Patient card, orders (all clinics), documents, clinics")
async def overview(patient: Patient, session: DbSession) -> PortalOverviewOut:
    return await service.overview(session, patient)


@router.get("/orders/{order_id}", response_model=PortalOrderOut, summary="Owned order with items and documents")
async def order(order_id: uuid.UUID, patient: Patient, session: DbSession) -> PortalOrderOut:
    return await service.order(session, order_id, patient)


@router.get("/documents/{document_id}", response_model=PortalDocumentOut, summary="Owned result document with render context")
async def document(document_id: uuid.UUID, patient: Patient, session: DbSession) -> PortalDocumentOut:
    return await service.document(session, document_id, patient)


@router.get("/documents/{document_id}/pdf", summary="Owned result document PDF")
async def document_pdf(document_id: uuid.UUID, patient: Patient, session: DbSession) -> Response:
    pdf, filename = await service.document_pdf(session, document_id, patient)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"', "Cache-Control": "private, max-age=3600"},
    )
