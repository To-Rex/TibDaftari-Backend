"""Templates service — CONTRACT STUB (the templates module replaces this file, keeping the signatures).

Two functions are consumed by other modules (orders, portal, telegram):

* ``build_document_snapshot(...)`` → dict — freezes everything a PDF needs (template doc + language,
  fully built RenderContext per docs/RENDERER_SPEC.md §1, asset id → file id map). Stored in
  ``result_documents.snapshot`` at approval time so the PDF is reproducible forever.
* ``render_snapshot_pdf(session, snapshot)`` → PDF bytes.

Snapshot format (JSON-serialisable)::

    {
      "version": 1,
      "language": "uz",
      "doc": <TemplateDoc as stored in result_templates.doc>,
      "context": <RenderContext dict: patient/order/item/company/branch/category/today/values/schema/items>,
      "assets": {"<assetId>": {"fileId": "<uuid>", "mime": "image/png", "width": 186, "height": 164}}
    }
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.models import (
    AttributeSchema,
    Branch,
    Category,
    Company,
    Order,
    OrderItem,
    Patient,
    ResultTemplate,
)


async def build_document_snapshot(
    session: AsyncSession,
    *,
    template: ResultTemplate,
    order: Order,
    patient: Patient,
    company: Company,
    branch: Branch | None,
    category: Category | None,
    primary_item: OrderItem | None,
    items: list[OrderItem],
    schemas: dict[uuid.UUID, AttributeSchema],
    service_codes: dict[uuid.UUID, str],
    district_name: str | None = None,
    approved_at: datetime | None = None,
) -> dict[str, Any]:
    raise NotImplementedError


async def render_snapshot_pdf(session: AsyncSession, snapshot: dict[str, Any]) -> bytes:
    raise NotImplementedError
