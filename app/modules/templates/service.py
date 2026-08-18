"""Templates service — result templates CRUD, template assets, preview PDF and the document renderer
entry points used by other modules (orders / portal / telegram):

* ``build_document_snapshot(...)`` → dict — freezes everything a PDF needs (template doc + language,
  fully built RenderContext per docs/RENDERER_SPEC.md §1, asset id → file id map). Stored in
  ``result_documents.snapshot`` at approval time so the PDF is reproducible forever.
* ``render_snapshot_pdf(session, snapshot)`` → PDF bytes.
* ``get_template_or_404``, ``list_active_templates`` (Redis 120 s), ``increment_usage``.

Snapshot format (JSON-serialisable)::

    {
      "version": 1,
      "language": "uz",
      "templateId": "<uuid>", "templateVersion": 3,
      "doc": <TemplateDoc as stored in result_templates.doc>,
      "context": <RenderContext dict: patient/order/item/company/branch/category/today/values/schema/items>,
      "assets": {"<assetId>": {"fileId": "<uuid>", "mime": "image/png", "width": 186, "height": 164}}
    }
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import RequestMeta, StaffPrincipal
from app.core.audit import audit
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.textutil import matches
from app.core.timeutil import today_local, utcnow
from app.infrastructure.db.models import (
    AttributeSchema,
    Branch,
    Category,
    Company,
    Order,
    OrderItem,
    Patient,
    ResultTemplate,
    StoredFile,
    TemplateAsset,
)
from app.infrastructure.redis import cache
from app.modules.files import service as files
from app.modules.templates import repository as repo
from app.modules.templates.renderer import build_render_context, render
from app.modules.templates.renderer.context import to_render_item
from app.modules.templates.schemas import (
    TemplateAssetIn,
    TemplateAssetOut,
    TemplateCreateIn,
    TemplateOut,
    TemplateQuery,
    TemplateUpdateIn,
    empty_doc,
)

MSG_TEMPLATE_NOT_FOUND = "Shablon topilmadi"
MSG_EMPTY_ACTIVATE = "Bo‘sh shablonni faollashtirib bo‘lmaydi"
MSG_DELETE_ACTIVE = "Faol shablonni o‘chirib bo‘lmaydi — avval arxivlang"
ACTIVE_CACHE_TTL = 120
SNAPSHOT_VERSION = 1

SAMPLE_PATIENT: dict[str, Any] = {
    "fullName": "Karimova Madina Aziz qizi",
    "phone": "998901234567",
    "birthDate": "1992-04-12",
    "gender": "female",
    "street": "Al-Xorazmiy ko‘chasi, 12-uy",
    "passportNumber": "AB1234567",
}
SAMPLE_DISTRICT = "Urganch shahri"
SAMPLE_COMPANY = {"name": "Shifo Med", "phone": "+998 62 228-82-81", "address": "Urganch sh., A. Bahodirxon 177"}
SAMPLE_BRANCH = {"name": "Markaziy filial", "address": "Urganch sh."}
SAMPLE_CATEGORY = {"name": "Laboratoriya", "phone": "97-092-08-88; 97-457-83-89"}
SAMPLE_SERVICE_NAME = "Namuna xizmat"
SAMPLE_TECHNICIAN = "D. Rahimova"
SAMPLE_DOCTOR = "A. Jumaniyazov"


# ----------------------------------------------------------------------------- helpers


def _cache_prefix(company_id: uuid.UUID | str) -> str:
    return f"co:{company_id}:templates:"


def _active_key(company_id: uuid.UUID | str) -> str:
    return _cache_prefix(company_id) + "active"


async def invalidate_template_cache(company_id: uuid.UUID | str) -> None:
    """Drop cached template lists of a company (call after any template write)."""
    await cache.delete_prefix(_cache_prefix(company_id))


def _scope_of(staff: StaffPrincipal) -> uuid.UUID | None:
    """Tenant filter for id-addressed endpoints: superadmin sees every company."""
    return None if staff.is_super_admin else staff.company_id


def _parse_uuid(value: str | None) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError as exc:
        raise ValidationError("So‘rov ma’lumotlari noto‘g‘ri") from exc


def _uuid_list(values: list[str] | None) -> list[uuid.UUID]:
    out: list[uuid.UUID] = []
    for v in values or []:
        parsed = _parse_uuid(v)
        if parsed is not None and parsed not in out:
            out.append(parsed)
    return out


def template_out(t: ResultTemplate) -> TemplateOut:
    """ORM → ResultTemplate DTO (frontend shape)."""
    return TemplateOut(
        id=str(t.id),
        company_id=str(t.company_id),
        name=t.name,
        description=t.description,
        status=t.status,
        version=t.version,
        service_type_ids=[str(x) for x in t.service_type_ids or []],
        category_ids=[str(x) for x in t.category_ids or []],
        scope=t.scope,
        language=t.language,
        doc=t.doc or empty_doc(),
        thumbnail_url=t.thumbnail_url,
        usage=t.usage,
        created_at=t.created_at,
        updated_at=t.updated_at,
    )


def asset_out(a: TemplateAsset) -> TemplateAssetOut:
    """ORM → TemplateAsset DTO; `url` is the public file route."""
    return TemplateAssetOut(
        id=str(a.id),
        company_id=str(a.company_id),
        kind=a.kind,
        name=a.name,
        url=files.file_url(a.file_id),
        width=float(a.width),
        height=float(a.height),
        employee_id=str(a.employee_id) if a.employee_id else None,
    )


def _snapshot_of(t: ResultTemplate) -> dict[str, Any]:
    return {
        "name": t.name,
        "status": t.status,
        "version": t.version,
        "scope": t.scope,
        "language": t.language,
        "serviceTypeIds": [str(x) for x in t.service_type_ids or []],
        "categoryIds": [str(x) for x in t.category_ids or []],
        "elements": len((t.doc or {}).get("elements") or []),
    }


def schema_dict(s: AttributeSchema | None) -> dict[str, Any] | None:
    """AttributeSchema ORM → the JSON shape the renderer expects (None stays None)."""
    if s is None:
        return None
    return {"id": str(s.id), "name": s.name, "version": s.version, "status": s.status, "fields": list(s.fields or [])}


# ----------------------------------------------------------------------------- reads


async def get_template_or_404(session: AsyncSession, template_id: uuid.UUID, company_id: uuid.UUID | None = None) -> ResultTemplate:
    """Alive template or 404 "Shablon topilmadi"; `company_id=None` = platform-wide (superadmin / internal)."""
    row = await repo.get_template(session, template_id, company_id)
    if not row:
        raise NotFoundError(MSG_TEMPLATE_NOT_FOUND)
    return row


async def list_templates(session: AsyncSession, company_id: uuid.UUID, q: TemplateQuery) -> list[TemplateOut]:
    """Templates of a company: status exact, serviceTypeId → bound or generic, folded search on name; updatedAt desc."""
    rows = await repo.list_templates(session, company_id, status=q.status, service_type_id=_parse_uuid(q.service_type_id))
    return [template_out(t) for t in rows if matches(t.name, q.search)]


async def get_template_dto(session: AsyncSession, template_id: uuid.UUID, staff: StaffPrincipal) -> TemplateOut:
    """Template details (tenant-scoped 404)."""
    return template_out(await get_template_or_404(session, template_id, _scope_of(staff)))


def _template_from_cache(row: dict[str, Any]) -> ResultTemplate:
    """Rehydrate a cached DTO into a transient ORM object (read-only use by callers)."""
    return ResultTemplate(
        id=uuid.UUID(row["id"]),
        company_id=uuid.UUID(row["companyId"]),
        name=row["name"],
        description=row.get("description"),
        status=row["status"],
        version=row["version"],
        service_type_ids=[uuid.UUID(x) for x in row["serviceTypeIds"]],
        category_ids=[uuid.UUID(x) for x in row["categoryIds"]],
        scope=row["scope"],
        language=row["language"],
        doc=row["doc"],
        thumbnail_url=row.get("thumbnailUrl"),
        usage=row["usage"],
        created_at=datetime.fromisoformat(row["createdAt"].replace("Z", "+00:00")),
        updated_at=datetime.fromisoformat(row["updatedAt"].replace("Z", "+00:00")),
    )


async def list_active_templates(session: AsyncSession, company_id: uuid.UUID) -> list[ResultTemplate]:
    """Active templates of a company, updatedAt desc — Redis-cached 120 s (invalidated on every write).

    Cache hits return transient (detached) ORM objects: read attributes freely, never `session.add` them.
    """
    key = _active_key(company_id)
    hit = await cache.get_json(key)
    if hit is not None:
        return [_template_from_cache(x) for x in hit]
    rows = list(await repo.list_templates(session, company_id, status="active"))
    await cache.set_json(key, [template_out(t).model_dump(by_alias=True, mode="json") for t in rows], ACTIVE_CACHE_TTL)
    return rows


async def increment_usage(session: AsyncSession, template_id: uuid.UUID, n: int = 1) -> None:
    """usage += n (called once per issued document)."""
    await repo.increment_usage(session, template_id, n)


# ----------------------------------------------------------------------------- writes


async def create_template(session: AsyncSession, company_id: uuid.UUID, body: TemplateCreateIn, staff: StaffPrincipal, meta: RequestMeta) -> TemplateOut:
    """Create with the frontend defaults: name 'Yangi shablon', draft, v1, scope item, uz, emptyDoc, usage 0.

    A non-draft `status` requires admin.template.publish and a non-empty doc (same rules as set_status)."""
    if body.status and body.status != "draft":
        staff.require("admin.template.publish")
        if body.status == "active" and not ((body.doc or {}).get("elements") or []):
            raise ValidationError(MSG_EMPTY_ACTIVATE, code="empty")
    row = ResultTemplate(
        company_id=company_id,
        name=body.name or "Yangi shablon",
        description=body.description,
        status=body.status or "draft",
        version=1,
        service_type_ids=_uuid_list(body.service_type_ids),
        category_ids=_uuid_list(body.category_ids),
        scope=body.scope or "item",
        language=body.language or "uz",
        doc=body.doc if body.doc is not None else empty_doc(),
        thumbnail_url=body.thumbnail_url,
        usage=0,
        created_by=staff.id,
    )
    session.add(row)
    await session.flush()
    await audit(session, actor_type="staff", actor_id=staff.id, company_id=company_id, action="template.create", entity="result_template", entity_id=row.id, after=_snapshot_of(row), ip=meta.ip, request_id=meta.request_id)
    await invalidate_template_cache(company_id)
    return template_out(row)


async def update_template(session: AsyncSession, template_id: uuid.UUID, body: TemplateUpdateIn, staff: StaffPrincipal, meta: RequestMeta) -> TemplateOut:
    """Partial update; `version += 1` iff the payload carries `doc` and the template is active."""
    row = await get_template_or_404(session, template_id, _scope_of(staff))
    before = _snapshot_of(row)
    data = body.model_dump(exclude_unset=True)
    if "name" in data and body.name:
        row.name = body.name
    if "description" in data:
        row.description = body.description
    if "service_type_ids" in data:
        row.service_type_ids = _uuid_list(body.service_type_ids)
    if "category_ids" in data:
        row.category_ids = _uuid_list(body.category_ids)
    if "scope" in data and body.scope:
        row.scope = body.scope
    if "language" in data and body.language:
        row.language = body.language
    if "thumbnail_url" in data:
        row.thumbnail_url = body.thumbnail_url
    if "doc" in data and body.doc is not None:
        row.doc = body.doc
        if row.status == "active":
            row.version += 1
    row.updated_at = utcnow()
    await session.flush()
    await audit(session, actor_type="staff", actor_id=staff.id, company_id=row.company_id, action="template.update", entity="result_template", entity_id=row.id, before=before, after=_snapshot_of(row), ip=meta.ip, request_id=meta.request_id)
    await invalidate_template_cache(row.company_id)
    return template_out(row)


async def set_status(session: AsyncSession, template_id: uuid.UUID, status: str, staff: StaffPrincipal, meta: RequestMeta) -> TemplateOut:
    """draft | active | archived; activating an empty doc → 422 empty."""
    row = await get_template_or_404(session, template_id, _scope_of(staff))
    if status == "active" and not ((row.doc or {}).get("elements") or []):
        raise ValidationError(MSG_EMPTY_ACTIVATE, code="empty")
    before = _snapshot_of(row)
    row.status = status
    row.updated_at = utcnow()
    await session.flush()
    await audit(session, actor_type="staff", actor_id=staff.id, company_id=row.company_id, action="template.status", entity="result_template", entity_id=row.id, before=before, after=_snapshot_of(row), ip=meta.ip, request_id=meta.request_id)
    await invalidate_template_cache(row.company_id)
    return template_out(row)


async def duplicate_template(session: AsyncSession, template_id: uuid.UUID, staff: StaffPrincipal, meta: RequestMeta) -> TemplateOut:
    """Copy as draft v1 usage 0 named "<name> (nusxa)" with bindings and doc copied."""
    src = await get_template_or_404(session, template_id, _scope_of(staff))
    row = ResultTemplate(
        company_id=src.company_id,
        name=f"{src.name} (nusxa)",
        description=src.description,
        status="draft",
        version=1,
        service_type_ids=list(src.service_type_ids or []),
        category_ids=list(src.category_ids or []),
        scope=src.scope,
        language=src.language,
        doc=dict(src.doc or empty_doc()),
        thumbnail_url=src.thumbnail_url,
        usage=0,
        created_by=staff.id,
    )
    session.add(row)
    await session.flush()
    await audit(session, actor_type="staff", actor_id=staff.id, company_id=row.company_id, action="template.duplicate", entity="result_template", entity_id=row.id, before={"sourceId": str(src.id)}, after=_snapshot_of(row), ip=meta.ip, request_id=meta.request_id)
    await invalidate_template_cache(row.company_id)
    return template_out(row)


async def delete_template(session: AsyncSession, template_id: uuid.UUID, staff: StaffPrincipal, meta: RequestMeta) -> None:
    """Soft delete; active → 409 active."""
    row = await get_template_or_404(session, template_id, _scope_of(staff))
    if row.status == "active":
        raise ConflictError(MSG_DELETE_ACTIVE, code="active")
    row.deleted_at = utcnow()
    row.deleted_by = staff.id
    await session.flush()
    await audit(session, actor_type="staff", actor_id=staff.id, company_id=row.company_id, action="template.delete", entity="result_template", entity_id=row.id, before=_snapshot_of(row), ip=meta.ip, request_id=meta.request_id)
    await invalidate_template_cache(row.company_id)


# ----------------------------------------------------------------------------- assets


async def list_assets(session: AsyncSession, company_id: uuid.UUID) -> list[TemplateAssetOut]:
    """All alive assets of a company (logo / stamp / signature / image)."""
    return [asset_out(a) for a in await repo.list_assets(session, company_id)]


async def upload_asset(session: AsyncSession, company_id: uuid.UUID, body: TemplateAssetIn, staff: StaffPrincipal, meta: RequestMeta) -> TemplateAssetOut:
    """Store the data-URL bytes in stored_files (deduplicated) and register a TemplateAsset row."""
    stored, size = await files.store_data_url(session, company_id=company_id, data_url=body.url, filename=body.name, created_by=staff.id)
    width = body.width or (size[0] if size else 0)
    height = body.height or (size[1] if size else 0)
    row = TemplateAsset(
        company_id=company_id,
        kind=body.kind,
        name=body.name,
        file_id=stored.id,
        width=Decimal(str(round(width, 2))),
        height=Decimal(str(round(height, 2))),
        employee_id=_parse_uuid(body.employee_id),
        created_by=staff.id,
    )
    session.add(row)
    await session.flush()
    await audit(session, actor_type="staff", actor_id=staff.id, company_id=company_id, action="asset.upload", entity="template_asset", entity_id=row.id, after={"kind": row.kind, "name": row.name, "fileId": str(stored.id), "size": stored.size}, ip=meta.ip, request_id=meta.request_id)
    return asset_out(row)


# ----------------------------------------------------------------------------- sample context (preview)


def sample_values(schema: dict[str, Any] | None) -> dict[str, Any]:
    """Frontend `sampleValues(schema)`: one plausible value per field type."""
    values: dict[str, Any] = {}
    for f in (schema or {}).get("fields") or []:
        ftype, key = f.get("type"), f.get("key")
        if not key:
            continue
        if ftype == "text":
            values[key] = "Namuna matn"
        elif ftype == "longtext":
            values[key] = "Izoh matni. Qayta tahlil 30 kundan so‘ng tavsiya etiladi."
        elif ftype == "number":
            refs = f.get("references") or []
            r = refs[0] if refs else None
            if r and r.get("min") is not None and r.get("max") is not None:
                decimals = f.get("decimals") if f.get("decimals") is not None else 1
                values[key] = round((float(r["min"]) + float(r["max"])) / 2, int(decimals))
            else:
                values[key] = 12.5
        elif ftype == "select":
            opts = f.get("options") or []
            values[key] = opts[0].get("value") if opts else None
        elif ftype == "multiselect":
            values[key] = [o.get("value") for o in (f.get("options") or [])[:2]]
        elif ftype == "boolean":
            values[key] = True
        elif ftype == "date":
            values[key] = today_local().isoformat()
        elif ftype == "table":
            values[key] = _sample_table_rows(f)
    return values


def _sample_table_rows(f: dict[str, Any]) -> list[dict[str, Any]]:
    """Preview rows for a table field — mirrors the frontend `sampleValues`.

    Preset (seeded) tables preview exactly like the empty blank: EVERY preset row, preset cells
    untouched, result columns empty. Tables without presets get 4 synthetic rows.
    """
    preset = f.get("presetRows") or []
    columns = f.get("columns") or []
    if preset:
        # every preset row, preset cells untouched, result columns empty (filled by the lab, never by the template)
        return [dict(r) for r in preset]
    rows = [{} for _ in range(4)]
    for i, row in enumerate(rows):
        for c in columns:
            ck = c.get("key")
            if not ck:
                continue
            ctype = c.get("type")
            opts = c.get("options") or []
            if ctype == "select":
                row[ck] = opts[i % len(opts)].get("value") if opts else ""
            elif ctype == "number":
                row[ck] = 10 + i
            elif ctype == "boolean":
                row[ck] = i % 2 == 0
            elif ctype == "multiselect":
                row[ck] = [opts[0].get("value")] if opts else []
            else:
                row[ck] = f"Namuna {i + 1}"
    return rows


async def _sample_context(session: AsyncSession, template: ResultTemplate, company: Company | None) -> dict[str, Any]:
    """Frontend `sampleRenderContext` / `sampleOrderRenderContext` for the template's bound services."""
    service_types = await repo.get_service_types(session, template.company_id, template.service_type_ids or [])
    by_id = {s.id: s for s in service_types}
    ordered = [by_id[i] for i in template.service_type_ids or [] if i in by_id]
    schema_rows = await repo.get_schemas(session, template.company_id, {s.schema_id for s in ordered if s.schema_id})
    schemas = {s.id: schema_dict(s) for s in schema_rows}
    first = ordered[0] if ordered else None
    schema = schemas.get(first.schema_id) if first and first.schema_id else None
    branch = await repo.first_branch(session, template.company_id)
    category = await repo.get_category(session, template.company_id, first.category_id) if first else None
    now = utcnow()
    items = None
    if template.scope == "order":
        items = [
            to_render_item(
                code=s.code or str(s.id),
                service_type_id=str(s.id),
                service_name=s.name,
                status="approved",
                values=sample_values(schemas.get(s.schema_id) if s.schema_id else None),
                schema=schemas.get(s.schema_id) if s.schema_id else None,
                approved_at=now,
                technician=SAMPLE_TECHNICIAN,
                doctor=SAMPLE_DOCTOR,
            )
            for s in ordered
        ]
    return build_render_context(
        patient=SAMPLE_PATIENT,
        order={"number": "UR-001240", "createdAt": now},
        item={
            "serviceName": first.name if first else SAMPLE_SERVICE_NAME,
            "approvedAt": now,
            "technicianName": SAMPLE_TECHNICIAN,
            "doctorName": SAMPLE_DOCTOR,
            "labNote": "Namuna sifati qoniqarli",
            "values": sample_values(schema),
        },
        company={"name": company.name, "phone": company.phone, "address": company.address} if company else SAMPLE_COMPANY,
        branch={"name": branch.name, "address": branch.address} if branch else SAMPLE_BRANCH,
        category={"name": category.name, "phone": category.phone} if category else SAMPLE_CATEGORY,
        schema=schema,
        district_name=SAMPLE_DISTRICT,
        items=items,
        language=template.language,
    )


def _referenced_asset_ids(doc: dict[str, Any]) -> list[uuid.UUID]:
    out: list[uuid.UUID] = []
    for el in doc.get("elements") or []:
        if isinstance(el, dict) and el.get("type") == "image" and el.get("assetId"):
            try:
                aid = uuid.UUID(str(el["assetId"]))
            except ValueError:
                continue
            if aid not in out:
                out.append(aid)
    return out


async def _asset_map(session: AsyncSession, company_id: uuid.UUID, doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """assetId → {fileId, mime, width, height} for every image element of the doc."""
    assets = await repo.get_assets_by_ids(session, company_id, _referenced_asset_ids(doc))
    if not assets:
        return {}
    file_ids = {a.file_id for a in assets}
    mimes = dict((await session.execute(select(StoredFile.id, StoredFile.mime).where(StoredFile.id.in_(file_ids)))).all())
    return {
        str(a.id): {"fileId": str(a.file_id), "mime": mimes.get(a.file_id, "application/octet-stream"), "width": float(a.width), "height": float(a.height)}
        for a in assets
    }


async def _loader_for(session: AsyncSession, assets: dict[str, dict[str, Any]]) -> Callable[[str], tuple[bytes, str] | None]:
    """Pre-load stored bytes for every asset (async) and hand the renderer a sync lookup."""
    blobs: dict[str, tuple[bytes, str]] = {}
    for asset_id, meta in assets.items():
        loaded = await files.load_bytes(session, (meta or {}).get("fileId"))
        if loaded:
            blobs[asset_id] = loaded
    return blobs.get


async def preview_pdf(session: AsyncSession, template_id: uuid.UUID, staff: StaffPrincipal, doc: dict[str, Any] | None = None) -> bytes:
    """Render the template (or an unsaved `doc`) with a sample context → PDF bytes."""
    template = await get_template_or_404(session, template_id, _scope_of(staff))
    company = await session.get(Company, template.company_id)
    ctx = await _sample_context(session, template, company)
    use_doc = doc if doc is not None else (template.doc or empty_doc())
    loader = await _loader_for(session, await _asset_map(session, template.company_id, use_doc))
    return await asyncio.to_thread(render, use_doc, ctx, loader)


# ----------------------------------------------------------------------------- documents (used by orders / portal / telegram)


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
    """Freeze template doc + RenderContext + asset map for a result document (see module docstring).

    `approved_at` overrides the primary item's approval time (the caller approves in the same
    transaction). RenderItems are included when the template is order-scoped or several items are covered.
    """
    primary_schema = schemas.get(primary_item.schema_id) if primary_item and primary_item.schema_id else None
    render_items = None
    if template.scope == "order" or len(items) > 1:
        render_items = [
            to_render_item(
                code=service_codes.get(it.service_type_id) or str(it.service_type_id),
                service_type_id=str(it.service_type_id),
                service_name=it.service_name,
                status=it.status,
                values=it.values,
                schema=schema_dict(schemas.get(it.schema_id)) if it.schema_id else None,
                approved_at=it.approved_at or approved_at,
                technician=it.technician_name,
                doctor=it.doctor_name,
            )
            for it in items
        ]
    context = build_render_context(
        patient={
            "fullName": patient.full_name,
            "phone": patient.phone,
            "birthDate": patient.birth_date,
            "gender": patient.gender,
            "street": patient.street,
            "passportNumber": patient.passport_number,
        },
        order={"number": order.number, "createdAt": order.created_at},
        item={
            "serviceName": primary_item.service_name if primary_item else "",
            "approvedAt": (approved_at or primary_item.approved_at) if primary_item else approved_at,
            "technicianName": primary_item.technician_name if primary_item else None,
            "doctorName": primary_item.doctor_name if primary_item else None,
            "labNote": primary_item.lab_note if primary_item else None,
            "values": primary_item.values if primary_item else {},
        },
        company={"name": company.name, "phone": company.phone, "address": company.address},
        branch={"name": branch.name, "address": branch.address} if branch else None,
        category={"name": category.name, "phone": category.phone} if category else None,
        schema=schema_dict(primary_schema),
        district_name=district_name,
        items=render_items,
        language=template.language,
    )
    doc = template.doc or empty_doc()
    return {
        "version": SNAPSHOT_VERSION,
        "language": template.language,
        "templateId": str(template.id),
        "templateVersion": template.version,
        "doc": doc,
        "context": context,
        "assets": await _asset_map(session, template.company_id, doc),
    }


async def render_snapshot_pdf(session: AsyncSession, snapshot: dict[str, Any]) -> bytes:
    """Render a frozen snapshot to PDF bytes (assets loaded from stored_files; data: URIs inline)."""
    loader = await _loader_for(session, snapshot.get("assets") or {})
    # CPU-bound (fpdf2, fonts, images): keep it off the event loop; the loader is a sync dict lookup.
    return await asyncio.to_thread(render, snapshot.get("doc") or empty_doc(), snapshot.get("context") or {}, loader)
