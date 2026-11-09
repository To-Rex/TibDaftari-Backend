"""Templates service — result templates CRUD, template assets, preview PDF and the document renderer
entry points used by other modules (orders / portal / telegram):

* ``build_document_snapshot(...)`` → dict — freezes everything a PDF needs (template doc + language,
  fully built RenderContext per docs/RENDERER_SPEC.md §1, asset id → file id map). Stored in
  ``result_documents.snapshot`` at approval time so the PDF is reproducible forever.
* ``render_snapshot_pdf(session, snapshot)`` → PDF bytes.
* ``get_template_or_404``, ``list_active_templates`` (Redis 120 s per unit), ``ensure_default_receipt``,
  ``invalidate_template_cache``, ``increment_usage``, ``template_out``, ``schema_dict``, ``org_context``.

Ownership: a template / asset belongs to `owner_unit_id` (a unit of any level) and is visible to the owner's
subtree; `template_units` narrows a template to some units (and their subtrees) — no rows = the whole subtree.
A template is USABLE at a unit when its owner lies on the unit's path (root → unit) and it has no bindings or one
of them lies on that path. Management lists (`/units/{unit_id}/templates`) show the owners on the path AND in the
subtree (`scope.lineage_ids`); id-addressed writes need the owner in the caller's scope or above one of their units.

Snapshot format (JSON-serialisable)::

    {
      "version": 1,
      "language": "uz",
      "templateId": "<uuid>", "templateVersion": 3,
      "doc": <TemplateDoc as stored in result_templates.doc>,
      "context": <RenderContext dict: patient/order/item/org/unit/category/today/values/schema/items;
                  `company` / `branch` are aliases of `org` / `unit` for documents written before the tree>,
      "assets": {"<assetId>": {"fileId": "<uuid>", "mime": "image/png", "width": 186, "height": 164}}
    }
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import RequestMeta, StaffPrincipal
from app.core.audit import audit
from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.core.scope import UnitScope, ancestor_ids, descendant_ids, facility_ids_under, load_scope
from app.core.textutil import matches
from app.core.timeutil import today_local, utcnow
from app.infrastructure.db.models import (
    AttributeSchema,
    Category,
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
from app.modules.templates.defaults import DEFAULT_RECEIPT_NAME, default_receipt_doc
from app.modules.templates.renderer import build_render_context, render
from app.modules.templates.renderer.context import item_status_label, org_of, to_render_item, unit_dict
from app.modules.templates.schemas import (
    DefaultReceiptIn,
    TemplateAssetIn,
    TemplateAssetOut,
    TemplateCreateIn,
    TemplateDuplicateIn,
    TemplateOut,
    TemplateQuery,
    TemplateUpdateIn,
    empty_doc,
)
from app.modules.units import repository as units_repo

MSG_TEMPLATE_NOT_FOUND = "Shablon topilmadi"
MSG_ASSET_NOT_FOUND = "Rasm topilmadi"
MSG_UNIT_NOT_FOUND = "Birlik topilmadi"
MSG_FORBIDDEN = "Ruxsat yo‘q"
MSG_EMPTY_ACTIVATE = "Bo‘sh shablonni faollashtirib bo‘lmaydi"
MSG_DELETE_ACTIVE = "Faol shablonni o‘chirib bo‘lmaydi — avval arxivlang"
ACTIVE_CACHE_TTL = 120
CACHE_PREFIX = "tpl:"
SNAPSHOT_VERSION = 1

SAMPLE_PATIENT: dict[str, Any] = {
    "fullName": "Karimova Madina Aziz qizi",
    "phone": "998901234567",
    "birthDate": "1992-04-12",
    "gender": "female",
    "street": "Al-Xorazmiy ko‘chasi, 12-uy",
    "passportNumber": "AB1234567",
    "pinfl": "50101992040012",
    "workplace": "TibDaftari",
    "discountPercent": 0,
    "contractNumber": "SH-00124",
    "note": "Doimiy mijoz",
    "tags": ["VIP"],
}
SAMPLE_DISTRICT = "Urganch shahri"
SAMPLE_REGION = "Xorazm viloyati"
SAMPLE_ORG = {"name": "TibDaftari", "shortName": "TibDaftari", "legalName": "TibDaftari MChJ", "phone": "+998 62 228-82-81", "email": "info@tibdaftari.uz", "address": "Urganch sh., A. Bahodirxon 177", "logoUrl": None}
SAMPLE_UNIT = {"name": "Markaziy muassasa", "code": "M", "kind": "clinic", "address": "Urganch sh.", "timezone": "Asia/Tashkent"}
SAMPLE_CATEGORY = {"name": "Laboratoriya", "code": "LAB", "phone": "97-092-08-88; 97-457-83-89"}
SAMPLE_SERVICE_NAME = "Namuna xizmat"
SAMPLE_TECHNICIAN = "D. Rahimova"
SAMPLE_DOCTOR = "A. Jumaniyazov"


# ----------------------------------------------------------------------------- helpers


def _active_key(unit_id: uuid.UUID | str) -> str:
    return f"{CACHE_PREFIX}{unit_id}:active"


async def invalidate_template_cache(owner_unit_id: uuid.UUID | str | None = None) -> None:
    """Drop the cached per-unit template lists (call after any template write). A template owned by
    `owner_unit_id` is usable across the owner's whole subtree, so every unit's list is dropped."""
    await cache.delete_prefix(CACHE_PREFIX)


def _parse_uuid(value: str | None) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError as exc:
        raise ValidationError("So‘rov ma’lumotlari noto‘g‘ri") from exc


def _uuid_list(values: Iterable[str | uuid.UUID] | None) -> list[uuid.UUID]:
    out: list[uuid.UUID] = []
    for v in values or []:
        parsed = v if isinstance(v, uuid.UUID) else _parse_uuid(v)
        if parsed is not None and parsed not in out:
            out.append(parsed)
    return out


def template_out(t: ResultTemplate, unit_ids: Iterable[uuid.UUID] | None = None) -> TemplateOut:
    """ORM → ResultTemplate DTO (frontend shape). `unit_ids` = the `template_units` bindings, loaded by the
    caller (`repo.template_unit_ids`); rows from `list_active_templates` already carry them as `t.unit_ids`."""
    bound = list(unit_ids) if unit_ids is not None else list(getattr(t, "unit_ids", None) or [])
    return TemplateOut(
        id=str(t.id),
        owner_unit_id=str(t.owner_unit_id),
        name=t.name,
        description=t.description,
        status=t.status,
        version=t.version,
        service_type_ids=[str(x) for x in t.service_type_ids or []],
        category_ids=[str(x) for x in t.category_ids or []],
        unit_ids=[str(x) for x in bound],
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
        owner_unit_id=str(a.owner_unit_id),
        kind=a.kind,
        name=a.name,
        url=files.file_url(a.file_id),
        width=float(a.width),
        height=float(a.height),
        employee_id=str(a.employee_id) if a.employee_id else None,
    )


def _snapshot_of(t: ResultTemplate, unit_ids: Iterable[uuid.UUID]) -> dict[str, Any]:
    return {
        "name": t.name,
        "status": t.status,
        "version": t.version,
        "scope": t.scope,
        "language": t.language,
        "ownerUnitId": str(t.owner_unit_id),
        "serviceTypeIds": [str(x) for x in t.service_type_ids or []],
        "categoryIds": [str(x) for x in t.category_ids or []],
        "unitIds": [str(x) for x in unit_ids],
        "elements": len((t.doc or {}).get("elements") or []),
    }


def schema_dict(s: AttributeSchema | None) -> dict[str, Any] | None:
    """AttributeSchema ORM → the JSON shape the renderer expects (None stays None)."""
    if s is None:
        return None
    return {"id": str(s.id), "name": s.name, "version": s.version, "status": s.status, "fields": list(s.fields or [])}


async def _require_owner(session: AsyncSession, staff: StaffPrincipal | None, owner_unit_id: uuid.UUID) -> None:
    """A template may be read / changed when its owner lies in the caller's scope or on the path above one of the
    caller's units (superadmin: always; `staff=None` = internal call). 403 otherwise."""
    if staff is None or staff.can_see(owner_unit_id):
        return
    if owner_unit_id in await units_repo.ancestors_of(session, staff.unit_ids):
        return
    raise ForbiddenError(MSG_FORBIDDEN)


async def _audit(
    session: AsyncSession,
    *,
    staff: StaffPrincipal,
    meta: RequestMeta,
    action: str,
    entity: str,
    entity_id: uuid.UUID,
    owner_unit_id: uuid.UUID,
    country_id: uuid.UUID | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    """Audit row on the owner unit (and its country — looked up when the caller has no scope at hand)."""
    if country_id is None:
        owner = await units_repo.get_unit_any(session, owner_unit_id)
        country_id = owner.country_id if owner else None
    await audit(
        session,
        actor_type="staff",
        actor_id=staff.id,
        action=action,
        entity=entity,
        entity_id=entity_id,
        unit_id=owner_unit_id,
        country_id=country_id,
        before=before,
        after=after,
        ip=meta.ip,
        request_id=meta.request_id,
    )


async def _resolve_unit_ids(
    session: AsyncSession, owner_unit_id: uuid.UUID, values: Iterable[str | uuid.UUID] | None, subtree: Sequence[uuid.UUID] | None = None
) -> list[uuid.UUID]:
    """Unit bindings of a template: every id must lie inside the owner's subtree — 404 "Birlik topilmadi" otherwise."""
    ids = _uuid_list(values)
    if not ids:
        return []
    allowed = set(subtree if subtree is not None else await descendant_ids(session, owner_unit_id))
    if any(u not in allowed for u in ids):
        raise NotFoundError(MSG_UNIT_NOT_FOUND)
    return ids


async def _bindings(session: AsyncSession, rows: Iterable[ResultTemplate]) -> dict[uuid.UUID, list[uuid.UUID]]:
    return await repo.template_unit_ids(session, [t.id for t in rows])


async def _path_and_lineage(session: AsyncSession, unit_id: uuid.UUID) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    """(root → unit path, ancestors ∪ subtree) of a unit — from the cached scope, else straight from the closure."""
    scope = await load_scope(session, unit_id)
    if scope is not None:
        return scope.ancestor_ids, scope.lineage_ids
    path = await ancestor_ids(session, unit_id)
    return path, [*path[:-1], *await descendant_ids(session, unit_id)]


# ----------------------------------------------------------------------------- reads


async def get_template_or_404(session: AsyncSession, template_id: uuid.UUID, staff: StaffPrincipal | None = None) -> ResultTemplate:
    """Alive template or 404 "Shablon topilmadi"; with `staff`, 403 unless the owner unit is visible to the caller
    (`staff=None` = internal / platform-wide)."""
    row = await repo.get_template(session, template_id)
    if not row:
        raise NotFoundError(MSG_TEMPLATE_NOT_FOUND)
    await _require_owner(session, staff, row.owner_unit_id)
    return row


async def template_dto(session: AsyncSession, t: ResultTemplate) -> TemplateOut:
    """DTO of a loaded row with its bindings fetched."""
    return template_out(t, (await _bindings(session, [t])).get(t.id, []))


async def list_templates(session: AsyncSession, scope: UnitScope, q: TemplateQuery) -> list[TemplateOut]:
    """Templates managed from the unit (owner on its path or in its subtree): status exact, serviceTypeId → bound or
    generic, `unitId` → only those usable at that unit (must lie under the path unit, else nothing), folded search
    on name; updatedAt desc."""
    path_ids: list[uuid.UUID] | None = None
    if q.unit_id:
        uid = _parse_uuid(q.unit_id)
        if uid is None or not scope.contains(uid):
            return []
        path_ids = await ancestor_ids(session, uid)
    rows = await repo.list_templates(session, scope.lineage_ids, status=q.status, service_type_id=_parse_uuid(q.service_type_id), path_ids=path_ids)
    kept = [t for t in rows if matches(t.name, q.search)]
    bound = await _bindings(session, kept)
    return [template_out(t, bound.get(t.id, [])) for t in kept]


async def get_template_dto(session: AsyncSession, template_id: uuid.UUID, staff: StaffPrincipal) -> TemplateOut:
    """Template details (owner must be visible to the caller)."""
    return await template_dto(session, await get_template_or_404(session, template_id, staff))


def _template_from_cache(row: dict[str, Any]) -> ResultTemplate:
    """Rehydrate a cached DTO into a transient ORM object (read-only use by callers); bindings in `unit_ids`."""
    t = ResultTemplate(
        id=uuid.UUID(row["id"]),
        owner_unit_id=uuid.UUID(row["ownerUnitId"]),
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
    t.unit_ids = [uuid.UUID(x) for x in row.get("unitIds") or []]
    return t


async def list_active_templates(session: AsyncSession, unit_id: uuid.UUID | str) -> list[ResultTemplate]:
    """Active templates usable at `unit_id` (owner on its root → unit path; no bindings, or one on that path),
    updatedAt desc — Redis-cached 120 s per unit (dropped on every template write).

    Rows carry their bindings as `t.unit_ids`. Cache hits return transient (detached) ORM objects: read attributes
    freely, never `session.add` them.
    """
    uid = uuid.UUID(str(unit_id))
    key = _active_key(uid)
    hit = await cache.get_json(key)
    if hit is not None:
        return [_template_from_cache(x) for x in hit]
    path = await ancestor_ids(session, uid)
    rows = list(await repo.list_templates(session, path, status="active", path_ids=path))
    bound = await _bindings(session, rows)
    for t in rows:
        t.unit_ids = bound.get(t.id, [])
    await cache.set_json(key, [template_out(t).model_dump(by_alias=True, mode="json") for t in rows], ACTIVE_CACHE_TTL)
    return rows


async def increment_usage(session: AsyncSession, template_id: uuid.UUID, n: int = 1) -> None:
    """usage += n (called once per issued document)."""
    await repo.increment_usage(session, template_id, n)


# ----------------------------------------------------------------------------- writes


async def create_template(session: AsyncSession, scope: UnitScope, body: TemplateCreateIn, staff: StaffPrincipal, meta: RequestMeta) -> TemplateOut:
    """Create a template owned by the path unit with the frontend defaults: name 'Yangi shablon', draft, v1, scope
    item, uz, emptyDoc, usage 0. `unitIds` must lie in the unit's subtree.

    A non-draft `status` requires admin.template.publish and a non-empty doc (same rules as set_status)."""
    if body.status and body.status != "draft":
        staff.require("admin.template.publish")
        if body.status == "active" and not ((body.doc or {}).get("elements") or []):
            raise ValidationError(MSG_EMPTY_ACTIVATE, code="empty")
    unit_ids = await _resolve_unit_ids(session, scope.id, body.unit_ids, scope.descendant_ids)
    row = ResultTemplate(
        owner_unit_id=scope.id,
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
    await repo.set_template_units(session, row.id, unit_ids)
    await _audit(session, staff=staff, meta=meta, action="template.create", entity="result_template", entity_id=row.id, owner_unit_id=scope.id, country_id=scope.country_id, after=_snapshot_of(row, unit_ids))
    await invalidate_template_cache(scope.id)
    return template_out(row, unit_ids)


async def _receipt_language(session: AsyncSession, unit_id: uuid.UUID, requested: str | None) -> str:
    """Label language of a seeded cheque: the requested one, else the unit's effective `locale`, else uz."""
    if requested in DEFAULT_RECEIPT_NAME:
        return str(requested)
    locale = (await units_repo.effective_settings(session, unit_id)).get("locale")
    return str(locale) if locale in DEFAULT_RECEIPT_NAME else "uz"


async def create_default_receipt(session: AsyncSession, scope: UnitScope, body: DefaultReceiptIn, staff: StaffPrincipal, meta: RequestMeta) -> TemplateOut:
    """The standard cheque as a new draft receipt template owned by the path unit (the "Standart chek" start option)."""
    lang = await _receipt_language(session, scope.id, body.language)
    create = TemplateCreateIn.model_validate(
        {"name": body.name or DEFAULT_RECEIPT_NAME[lang], "scope": "receipt", "unitIds": body.unit_ids or [], "language": lang, "doc": default_receipt_doc(body.paper, lang)}
    )
    return await create_template(session, scope, create, staff, meta)


async def has_receipt_template(session: AsyncSession, owner_unit_id: uuid.UUID) -> bool:
    """Whether any (alive) receipt template, active or not, is usable somewhere in the unit's subtree."""
    path, lineage = await _path_and_lineage(session, owner_unit_id)
    return await repo.receipt_template_exists(session, path, lineage)


async def ensure_default_receipt(
    session: AsyncSession, owner_unit_id: uuid.UUID, unit_ids: Sequence[uuid.UUID], language: str | None, created_by: uuid.UUID | None
) -> ResultTemplate | None:
    """Seed the standard cheque — a draft owned by `owner_unit_id`, bound to `unit_ids` (empty = the whole
    subtree) — when no receipt template is usable in the owner's subtree yet; an organisation gets it together
    with its first facility. Returns the new row, or None when one already exists."""
    if await has_receipt_template(session, owner_unit_id):
        return None
    lang = await _receipt_language(session, owner_unit_id, language)
    bound = _uuid_list(unit_ids)
    row = ResultTemplate(
        owner_unit_id=owner_unit_id,
        name=DEFAULT_RECEIPT_NAME[lang],
        status="draft",
        version=1,
        service_type_ids=[],
        category_ids=[],
        scope="receipt",
        language=lang,
        doc=default_receipt_doc("Receipt80", lang),
        usage=0,
        created_by=created_by,
    )
    session.add(row)
    await session.flush()
    await repo.set_template_units(session, row.id, bound)
    row.unit_ids = bound
    await invalidate_template_cache(owner_unit_id)
    return row


async def update_template(session: AsyncSession, template_id: uuid.UUID, body: TemplateUpdateIn, staff: StaffPrincipal, meta: RequestMeta) -> TemplateOut:
    """Partial update; `version += 1` iff the payload carries `doc` and the template is active. `unitIds` are
    checked against the owner's subtree."""
    row = await get_template_or_404(session, template_id, staff)
    unit_ids = (await _bindings(session, [row])).get(row.id, [])
    before = _snapshot_of(row, unit_ids)
    data = body.model_dump(exclude_unset=True)
    if "name" in data and body.name:
        row.name = body.name
    if "description" in data:
        row.description = body.description
    if "service_type_ids" in data:
        row.service_type_ids = _uuid_list(body.service_type_ids)
    if "category_ids" in data:
        row.category_ids = _uuid_list(body.category_ids)
    if "unit_ids" in data:
        unit_ids = await _resolve_unit_ids(session, row.owner_unit_id, body.unit_ids)
        await repo.set_template_units(session, row.id, unit_ids)
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
    await _audit(session, staff=staff, meta=meta, action="template.update", entity="result_template", entity_id=row.id, owner_unit_id=row.owner_unit_id, before=before, after=_snapshot_of(row, unit_ids))
    await invalidate_template_cache(row.owner_unit_id)
    return template_out(row, unit_ids)


async def set_status(session: AsyncSession, template_id: uuid.UUID, status: str, staff: StaffPrincipal, meta: RequestMeta) -> TemplateOut:
    """draft | active | archived; activating an empty doc → 422 empty."""
    row = await get_template_or_404(session, template_id, staff)
    if status == "active" and not ((row.doc or {}).get("elements") or []):
        raise ValidationError(MSG_EMPTY_ACTIVATE, code="empty")
    unit_ids = (await _bindings(session, [row])).get(row.id, [])
    before = _snapshot_of(row, unit_ids)
    row.status = status
    row.updated_at = utcnow()
    await session.flush()
    await _audit(session, staff=staff, meta=meta, action="template.status", entity="result_template", entity_id=row.id, owner_unit_id=row.owner_unit_id, before=before, after=_snapshot_of(row, unit_ids))
    await invalidate_template_cache(row.owner_unit_id)
    return template_out(row, unit_ids)


async def duplicate_template(session: AsyncSession, template_id: uuid.UUID, staff: StaffPrincipal, meta: RequestMeta, body: TemplateDuplicateIn | None = None) -> TemplateOut:
    """Copy as draft v1 usage 0 named "<name> (nusxa)" with bindings and doc copied, owned by the source's owner;
    `body` may rename the copy and bind it to other units of the owner's subtree."""
    src = await get_template_or_404(session, template_id, staff)
    src_units = (await _bindings(session, [src])).get(src.id, [])
    unit_ids = await _resolve_unit_ids(session, src.owner_unit_id, body.unit_ids) if body and body.unit_ids is not None else list(src_units)
    row = ResultTemplate(
        owner_unit_id=src.owner_unit_id,
        name=(body.name.strip() if body and body.name else f"{src.name} (nusxa)"),
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
    await repo.set_template_units(session, row.id, unit_ids)
    await _audit(session, staff=staff, meta=meta, action="template.duplicate", entity="result_template", entity_id=row.id, owner_unit_id=row.owner_unit_id, before={"sourceId": str(src.id)}, after=_snapshot_of(row, unit_ids))
    await invalidate_template_cache(row.owner_unit_id)
    return template_out(row, unit_ids)


async def delete_template(session: AsyncSession, template_id: uuid.UUID, staff: StaffPrincipal, meta: RequestMeta) -> None:
    """Soft delete; active → 409 active."""
    row = await get_template_or_404(session, template_id, staff)
    if row.status == "active":
        raise ConflictError(MSG_DELETE_ACTIVE, code="active")
    unit_ids = (await _bindings(session, [row])).get(row.id, [])
    row.deleted_at = utcnow()
    row.deleted_by = staff.id
    await session.flush()
    await _audit(session, staff=staff, meta=meta, action="template.delete", entity="result_template", entity_id=row.id, owner_unit_id=row.owner_unit_id, before=_snapshot_of(row, unit_ids))
    await invalidate_template_cache(row.owner_unit_id)


# ----------------------------------------------------------------------------- assets


async def list_assets(session: AsyncSession, scope: UnitScope) -> list[TemplateAssetOut]:
    """Alive assets (logo / stamp / signature / image) owned on the unit's path or in its subtree."""
    return [asset_out(a) for a in await repo.list_assets(session, scope.lineage_ids)]


async def upload_asset(session: AsyncSession, scope: UnitScope, body: TemplateAssetIn, staff: StaffPrincipal, meta: RequestMeta) -> TemplateAssetOut:
    """Store the data-URL bytes in stored_files (deduplicated per unit) and register a TemplateAsset owned by the path unit."""
    stored, size = await files.store_data_url(session, unit_id=scope.id, data_url=body.url, filename=body.name, created_by=staff.id)
    width = body.width or (size[0] if size else 0)
    height = body.height or (size[1] if size else 0)
    row = TemplateAsset(
        owner_unit_id=scope.id,
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
    await _audit(
        session,
        staff=staff,
        meta=meta,
        action="asset.upload",
        entity="template_asset",
        entity_id=row.id,
        owner_unit_id=scope.id,
        country_id=scope.country_id,
        after={"kind": row.kind, "name": row.name, "fileId": str(stored.id), "size": stored.size},
    )
    return asset_out(row)


async def delete_asset(session: AsyncSession, scope: UnitScope, asset_id: uuid.UUID, staff: StaffPrincipal, meta: RequestMeta) -> None:
    """Soft-delete a template asset visible from the unit; document snapshots keep their file bytes, so printed
    PDFs stay intact."""
    row = await repo.get_asset(session, asset_id)
    if row is None or row.owner_unit_id not in scope.lineage_ids:
        raise NotFoundError(MSG_ASSET_NOT_FOUND)
    row.deleted_at = utcnow()
    row.deleted_by = staff.id
    await session.flush()
    await _audit(
        session,
        staff=staff,
        meta=meta,
        action="asset.delete",
        entity="template_asset",
        entity_id=row.id,
        owner_unit_id=row.owner_unit_id,
        country_id=scope.country_id,
        before={"kind": row.kind, "name": row.name, "fileId": str(row.file_id)},
    )


# ----------------------------------------------------------------------------- organisation blocks


async def org_context(session: AsyncSession, unit_id: uuid.UUID | str | None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """`(org, unit)` input dicts of `build_render_context` for a unit: `unit` = the unit itself (the facility a
    cheque / document belongs to), `org` = the nearest non-facility unit on its path — the organisation whose
    name and logo head the documents. `(None, None)` when the unit is unknown."""
    if unit_id is None:
        return None, None
    uid = uuid.UUID(str(unit_id))
    path = await ancestor_ids(session, uid)
    units = await units_repo.units_by_ids(session, path)
    rows = [units[i] for i in path if i in units]
    unit = rows[-1] if rows and rows[-1].id == uid else None
    return unit_dict(org_of(rows)), unit_dict(unit)


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


async def _sample_org_unit(session: AsyncSession, owner_unit_id: uuid.UUID) -> tuple[dict[str, Any], dict[str, Any]]:
    """Header data for a preview: the owner's first facility and its organisation; sample blocks where the tree
    has none yet (an owner without facilities previews with itself as the organisation)."""
    facilities = await facility_ids_under(session, owner_unit_id)
    if facilities:
        org, unit = await org_context(session, facilities[0])
    else:
        org, _ = await org_context(session, owner_unit_id)
        unit = None
    return org or SAMPLE_ORG, unit or SAMPLE_UNIT


def _sample_receipt_context(org: dict[str, Any], unit: dict[str, Any], now: datetime, language: str) -> dict[str, Any]:
    """Preview data for a receipt template: three services, a discount, a partial payment."""
    services = [("PAR", "Парозитологик тахлил", "Parazitologiya", 52000), ("BAK", "Бактериологик тахлил", "Bakteriologiya", 160000), ("IFA", "ИФА ВГ “B”", "Virusologiya", 41000)]
    subtotal = sum(p for *_, p in services)
    discount = 10
    total = subtotal - subtotal * discount // 100
    return build_render_context(
        patient=SAMPLE_PATIENT,
        order={"number": "UR-001240", "createdAt": now, "subtotal": subtotal, "discountPercent": discount, "discountAmount": subtotal * discount // 100, "total": total, "paidAmount": total - 50000, "itemCount": len(services), "status": "in_progress", "payment": "partial"},
        org=org,
        unit=unit,
        items=[to_render_item(code=c, service_type_id=c, service_name=n, status="pending", status_label="Navbatda", values={}, schema=None, price=p, final_price=p - p * discount // 100, category=cat, schema_version=1) for c, n, cat, p in services],
        payments=[{"createdAt": now, "method": "cash", "amount": total - 50000}],
        cashier="Umida Qodirova",
        district_name=SAMPLE_DISTRICT,
        region_name=SAMPLE_REGION,
        language=language,
    )


async def _sample_context(session: AsyncSession, template: ResultTemplate) -> dict[str, Any]:
    """Frontend `sampleRenderContext` / `sampleOrderRenderContext` for the template's bound services, headed by
    the owner's first facility and its organisation."""
    service_types = await repo.get_service_types(session, template.service_type_ids or [])
    by_id = {s.id: s for s in service_types}
    ordered = [by_id[i] for i in template.service_type_ids or [] if i in by_id]
    schema_rows = await repo.get_schemas(session, {s.schema_id for s in ordered if s.schema_id})
    schemas = {s.id: schema_dict(s) for s in schema_rows}
    first = ordered[0] if ordered else None
    schema = schemas.get(first.schema_id) if first and first.schema_id else None
    org, unit = await _sample_org_unit(session, template.owner_unit_id)
    category = await repo.get_category(session, first.category_id) if first else None
    now = utcnow()
    items = None
    if template.scope == "receipt":
        return _sample_receipt_context(org, unit, now, template.language)
    if template.scope == "order":
        items = [
            to_render_item(
                code=s.code or str(s.id),
                service_type_id=str(s.id),
                service_name=s.name,
                status="approved",
                status_label="Tasdiqlangan",
                values=sample_values(schemas.get(s.schema_id) if s.schema_id else None),
                schema=schemas.get(s.schema_id) if s.schema_id else None,
                entered_at=now,
                submitted_at=now,
                approved_at=now,
                technician=SAMPLE_TECHNICIAN,
                doctor=SAMPLE_DOCTOR,
                price=50000,
                final_price=45000,
                category=category.name if category else None,
                schema_version=1,
            )
            for s in ordered
        ]
    return build_render_context(
        patient=SAMPLE_PATIENT,
        order={"number": "UR-001240", "createdAt": now, "subtotal": 50000, "discountPercent": 0, "discountAmount": 0, "total": 50000, "paidAmount": 50000, "itemCount": 1, "note": "Namuna izoh", "status": "completed", "payment": "paid"},
        item={
            "serviceName": first.name if first else SAMPLE_SERVICE_NAME,
            "serviceCode": first.code if first else None,
            "categoryName": category.name if category else None,
            "status": "approved",
            "price": 50000,
            "finalPrice": 45000,
            "enteredAt": now,
            "submittedAt": now,
            "approvedAt": now,
            "technicianName": SAMPLE_TECHNICIAN,
            "doctorName": SAMPLE_DOCTOR,
            "labNote": "Namuna sifati qoniqarli",
            "schemaVersion": 1,
            "values": sample_values(schema),
        },
        org=org,
        unit=unit,
        category={"name": category.name, "code": category.code, "phone": category.phone} if category else SAMPLE_CATEGORY,
        schema=schema,
        district_name=SAMPLE_DISTRICT,
        region_name=SAMPLE_REGION,
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


async def _asset_map(session: AsyncSession, doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """assetId → {fileId, mime, width, height} for every image element of the doc (assets were picked from the
    editor's visible list; a template may use assets owned anywhere on the editor's lineage)."""
    assets = await repo.get_assets_by_ids(session, _referenced_asset_ids(doc))
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
    template = await get_template_or_404(session, template_id, staff)
    ctx = await _sample_context(session, template)
    use_doc = doc if doc is not None else (template.doc or empty_doc())
    loader = await _loader_for(session, await _asset_map(session, use_doc))
    return await asyncio.to_thread(render, use_doc, ctx, loader)


# ----------------------------------------------------------------------------- documents (used by orders / portal / telegram)


async def build_document_snapshot(
    session: AsyncSession,
    *,
    template: ResultTemplate,
    order: Order,
    patient: Patient,
    category: Category | None,
    primary_item: OrderItem | None,
    items: list[OrderItem],
    schemas: dict[uuid.UUID, AttributeSchema],
    service_codes: dict[uuid.UUID, str],
    district_name: str | None = None,
    region_name: str | None = None,
    approved_at: datetime | None = None,
    doc_link: str | None = None,
    unit_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Freeze template doc + RenderContext + asset map for a result document (see module docstring).

    `org` / `unit` come from the cheque's facility (`order.unit_id`, or `unit_id` when given). `approved_at`
    overrides the primary item's approval time (the caller approves in the same transaction). RenderItems are
    included when the template is order-scoped or several items are covered.
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
                status_label=item_status_label(it.status, template.language),
                values=it.values,
                schema=schema_dict(schemas.get(it.schema_id)) if it.schema_id else None,
                entered_at=it.entered_at,
                submitted_at=it.submitted_at,
                approved_at=it.approved_at or approved_at,
                technician=it.technician_name,
                doctor=it.doctor_name,
                price=it.price,
                final_price=it.final_price,
                category=it.category_name,
                schema_version=it.schema_version,
            )
            for it in items
        ]
    primary_code = service_codes.get(primary_item.service_type_id) if primary_item else None
    org, unit = await org_context(session, unit_id or order.unit_id)
    context = build_render_context(
        patient={
            "fullName": patient.full_name,
            "phone": patient.phone,
            "birthDate": patient.birth_date,
            "gender": patient.gender,
            "street": patient.street,
            "passportNumber": patient.passport_number,
            "pinfl": patient.pinfl,
            "workplace": patient.workplace,
            "discountPercent": patient.discount_percent,
            "contractNumber": patient.contract_number,
            "note": patient.note,
            "tags": patient.tags,
        },
        order={
            "number": order.number,
            "createdAt": order.created_at,
            "status": order.status,
            "payment": order.payment,
            "subtotal": order.subtotal,
            "discountPercent": order.discount_percent,
            "discountAmount": order.discount_amount,
            "total": order.total,
            "paidAmount": order.paid_amount,
            "itemCount": order.item_count,
            "note": order.note,
        },
        item={
            "serviceName": primary_item.service_name if primary_item else "",
            "serviceCode": primary_code,
            "categoryName": primary_item.category_name if primary_item else None,
            "status": primary_item.status if primary_item else None,
            "price": primary_item.price if primary_item else None,
            "finalPrice": primary_item.final_price if primary_item else None,
            "enteredAt": primary_item.entered_at if primary_item else None,
            "submittedAt": primary_item.submitted_at if primary_item else None,
            "approvedAt": (approved_at or primary_item.approved_at) if primary_item else approved_at,
            "technicianName": primary_item.technician_name if primary_item else None,
            "doctorName": primary_item.doctor_name if primary_item else None,
            "labNote": primary_item.lab_note if primary_item else None,
            "rejectReason": primary_item.reject_reason if primary_item else None,
            "schemaVersion": primary_item.schema_version if primary_item else None,
            "values": primary_item.values if primary_item else {},
        },
        org=org,
        unit=unit,
        category={"name": category.name, "code": category.code, "phone": category.phone} if category else None,
        schema=schema_dict(primary_schema),
        district_name=district_name,
        region_name=region_name,
        items=render_items,
        language=template.language,
        doc_link=doc_link,
    )
    doc = template.doc or empty_doc()
    return {
        "version": SNAPSHOT_VERSION,
        "language": template.language,
        "templateId": str(template.id),
        "templateVersion": template.version,
        "doc": doc,
        "context": context,
        "assets": await _asset_map(session, doc),
    }


async def render_doc_pdf(session: AsyncSession, doc: dict[str, Any], ctx: dict[str, Any]) -> bytes:
    """Render a template doc with a ready context (referenced assets resolved) — receipts, ad-hoc previews."""
    use_doc = doc or empty_doc()
    loader = await _loader_for(session, await _asset_map(session, use_doc))
    return await asyncio.to_thread(render, use_doc, ctx, loader)


async def render_snapshot_pdf(session: AsyncSession, snapshot: dict[str, Any]) -> bytes:
    """Render a frozen snapshot to PDF bytes (assets loaded from stored_files; data: URIs inline)."""
    loader = await _loader_for(session, snapshot.get("assets") or {})
    # CPU-bound (fpdf2, fonts, images): keep it off the event loop; the loader is a sync dict lookup.
    return await asyncio.to_thread(render, snapshot.get("doc") or empty_doc(), snapshot.get("context") or {}, loader)
