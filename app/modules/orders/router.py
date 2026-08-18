"""Orders endpoints — cheques, items, payments, lab worklist, approvals, result documents. HTTP only."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response
from starlette.responses import Response as StarletteResponse

from app.api.deps import DbSession, Meta, Staff
from app.core.exceptions import NotFoundError
from app.core.schemas import Page
from app.modules.orders import repository as repo
from app.modules.orders import service
from app.modules.orders.schemas import (
    AddItemsIn,
    ApproveItemIn,
    ApproveItemOut,
    ApproveOrderIn,
    ApproveOrderOut,
    CreateOrderIn,
    OrderBundleOut,
    OrderItemOut,
    OrderItemsOut,
    OrderListQuery,
    OrderOut,
    OrderPaymentsOut,
    PayIn,
    ReasonIn,
    ResultDocumentOut,
    SaveValuesIn,
    WorklistCountsOut,
    WorklistItemOut,
    WorklistQuery,
)

router = APIRouter()

# Any staff role that works with cheques may read them (reception, lab, doctors, managers).
ORDER_READ = (
    "reception.order.create",
    "reception.payment.create",
    "reception.patient.read",
    "lab.worklist.read",
    "confirm.result.read",
    "reports.operations.read",
)
ITEM_READ = ("lab.worklist.read", "lab.result.write", "confirm.result.read", "reception.order.create")
DOC_READ = ("confirm.result.read", "reception.patient.read")


def _pdf_response(pdf: bytes, filename: str) -> Response:
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"', "Cache-Control": "private, max-age=3600"},
    )


# ----------------------------------------------------------------------------- orders


@router.get(
    "/companies/{company_id}/orders", response_model=Page[OrderOut], summary="List orders (paged, filters, search)"
)
async def list_orders(
    company_id: uuid.UUID, q: Annotated[OrderListQuery, Query()], staff: Staff, session: DbSession
) -> Page[OrderOut]:
    staff.require(*ORDER_READ).scope(company_id)
    return await service.list_orders(session, company_id, q)


@router.post(
    "/companies/{company_id}/orders",
    response_model=OrderItemsOut,
    status_code=201,
    summary="Create order (cheque) with items",
)
async def create_order(
    company_id: uuid.UUID, body: CreateOrderIn, staff: Staff, session: DbSession, meta: Meta
) -> OrderItemsOut:
    staff.require("reception.order.create").scope(company_id)
    return await service.create_order(session, company_id, staff, body, meta)


@router.get("/orders/{order_id}", response_model=OrderBundleOut, summary="Order + items + payments")
async def get_order(order_id: uuid.UUID, staff: Staff, session: DbSession) -> OrderBundleOut:
    staff.require(*ORDER_READ)
    return await service.get_order_bundle(session, order_id, service.scope_company(staff))


@router.post("/orders/{order_id}/items", response_model=OrderItemsOut, summary="Add services to an order")
async def add_items(
    order_id: uuid.UUID, body: AddItemsIn, staff: Staff, session: DbSession, meta: Meta
) -> OrderItemsOut:
    staff.require("reception.order.create")
    return await service.add_items(session, order_id, staff, body, meta)


@router.delete(
    "/orders/{order_id}/items/{item_id}",
    response_model=OrderItemsOut,
    summary="Remove a pending service from an unpaid order",
)
async def remove_item(
    order_id: uuid.UUID, item_id: uuid.UUID, staff: Staff, session: DbSession, meta: Meta
) -> OrderItemsOut:
    staff.require("reception.order.create")
    return await service.remove_item(session, order_id, item_id, staff, meta)


@router.post("/orders/{order_id}/pay", response_model=OrderPaymentsOut, summary="Record a payment")
async def pay(order_id: uuid.UUID, body: PayIn, staff: Staff, session: DbSession, meta: Meta) -> OrderPaymentsOut:
    staff.require("reception.payment.create")
    return await service.pay(session, order_id, staff, body, meta)


@router.post("/payments/{payment_id}/refund", response_model=OrderPaymentsOut, summary="Refund a payment")
async def refund(
    payment_id: uuid.UUID, body: ReasonIn, staff: Staff, session: DbSession, meta: Meta
) -> OrderPaymentsOut:
    staff.require("reception.payment.refund")
    return await service.refund(session, payment_id, staff, body.reason, meta)


@router.post("/orders/{order_id}/cancel", response_model=OrderOut, summary="Cancel an unpaid order")
async def cancel_order(order_id: uuid.UUID, body: ReasonIn, staff: Staff, session: DbSession, meta: Meta) -> OrderOut:
    staff.require("reception.order.cancel")
    return await service.cancel_order(session, order_id, staff, body.reason, meta)


# ----------------------------------------------------------------------------- lab


@router.get(
    "/companies/{company_id}/worklist",
    response_model=Page[WorklistItemOut],
    summary="Lab worklist (paid orders, items with a schema)",
)
async def worklist(
    company_id: uuid.UUID, q: Annotated[WorklistQuery, Query()], staff: Staff, session: DbSession
) -> Page[WorklistItemOut]:
    staff.require("lab.worklist.read").scope(company_id)
    return await service.worklist(session, company_id, q, staff)


@router.get(
    "/companies/{company_id}/worklist/counts",
    response_model=WorklistCountsOut,
    summary="Lab worklist counters per status (same filters as the worklist)",
)
async def worklist_counts(
    company_id: uuid.UUID, q: Annotated[WorklistQuery, Query()], staff: Staff, session: DbSession
) -> WorklistCountsOut:
    staff.require("lab.worklist.read").scope(company_id)
    return await service.worklist_counts(session, company_id, q, staff)


@router.get("/items/{item_id}", response_model=OrderItemOut, summary="Get order item")
async def get_item(item_id: uuid.UUID, staff: Staff, session: DbSession) -> OrderItemOut:
    staff.require(*ITEM_READ)
    return await service.get_item_dto(session, item_id, staff)


@router.put("/items/{item_id}/values", response_model=OrderItemOut, summary="Save result values (wholesale)")
async def save_values(
    item_id: uuid.UUID, body: SaveValuesIn, staff: Staff, session: DbSession, meta: Meta
) -> OrderItemOut:
    staff.require("lab.result.write")
    return await service.save_values(session, item_id, staff, body, meta)


@router.post("/items/{item_id}/submit", response_model=OrderItemOut, summary="Submit / un-submit result (toggle)")
async def submit_item(item_id: uuid.UUID, staff: Staff, session: DbSession, meta: Meta) -> OrderItemOut:
    staff.require("lab.result.submit")
    return await service.submit_item(session, item_id, staff, meta)


# ----------------------------------------------------------------------------- confirm


@router.post(
    "/items/{item_id}/approve", response_model=ApproveItemOut, summary="Approve a submitted result -> document"
)
async def approve_item(
    item_id: uuid.UUID, staff: Staff, session: DbSession, meta: Meta, body: ApproveItemIn | None = None
) -> ApproveItemOut:
    staff.require("confirm.result.approve")
    return await service.approve_item(session, item_id, staff, body or ApproveItemIn(), meta)


@router.post("/items/{item_id}/reject", response_model=OrderItemOut, summary="Send a submitted result back to the lab")
async def reject_item(item_id: uuid.UUID, body: ReasonIn, staff: Staff, session: DbSession, meta: Meta) -> OrderItemOut:
    staff.require("confirm.result.approve")
    return await service.reject_item(session, item_id, staff, body.reason, meta)


@router.post(
    "/orders/{order_id}/approve",
    response_model=ApproveOrderOut,
    summary="Order-scope approval (one document for many items)",
)
async def approve_order(
    order_id: uuid.UUID, body: ApproveOrderIn, staff: Staff, session: DbSession, meta: Meta
) -> ApproveOrderOut:
    staff.require("confirm.result.approve")
    return await service.approve_order(session, order_id, staff, body, meta)


@router.get(
    "/orders/{order_id}/scope-items",
    response_model=list[OrderItemOut],
    summary="Items an order-scope template would cover",
)
async def order_scope_items(
    order_id: uuid.UUID, staff: Staff, session: DbSession, template_id: Annotated[uuid.UUID, Query(alias="templateId")]
) -> list[OrderItemOut]:
    staff.require("confirm.result.read", "confirm.result.approve")
    return await service.order_scope_items(session, order_id, template_id, staff)


# ----------------------------------------------------------------------------- documents


@router.get(
    "/companies/{company_id}/documents",
    response_model=list[ResultDocumentOut],
    summary="Result documents by order / patient",
)
async def list_documents(
    company_id: uuid.UUID,
    staff: Staff,
    session: DbSession,
    order_id: Annotated[str | None, Query(alias="orderId")] = None,
    patient_id: Annotated[str | None, Query(alias="patientId")] = None,
) -> list[ResultDocumentOut]:
    staff.require(*DOC_READ).scope(company_id)
    return await service.list_documents(session, company_id, order_id=order_id, patient_id=patient_id)


@router.get("/documents/{document_id}", response_model=ResultDocumentOut, summary="Get result document")
async def get_document(document_id: uuid.UUID, staff: Staff, session: DbSession) -> ResultDocumentOut:
    staff.require(*DOC_READ)
    return service.document_out(await service.get_document_or_404(session, document_id, service.scope_company(staff)))


@router.get("/documents/{document_id}/pdf", summary="Result document PDF (rendered on demand when missing)")
async def document_pdf(document_id: uuid.UUID, staff: Staff, session: DbSession) -> StarletteResponse:
    doc = await service.get_document_or_404(session, document_id, service.scope_company(staff))
    pdf = await service.ensure_document_pdf(session, doc)
    return _pdf_response(pdf, service.pdf_filename(doc))


@router.get("/d/{token}", summary="Public result PDF by unguessable token (SMS / Telegram links)")
async def public_document_pdf(token: str, session: DbSession) -> StarletteResponse:
    doc = await repo.get_document_by_token(session, token) if 16 <= len(token) <= 64 else None
    if not doc:
        raise NotFoundError(service.DOC_NOT_FOUND)
    pdf = await service.ensure_document_pdf(session, doc)
    return _pdf_response(pdf, service.pdf_filename(doc))
