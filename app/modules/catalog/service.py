"""Catalog business rules: category tree, service types, attribute schemas.

Read-mostly lists are cached per company in Redis (`co:{cid}:catalog:*`, 5 min) and invalidated on
every write. Public helpers reused by other modules: `get_service_types_by_ids`, `get_category`,
`get_schemas_by_ids`, `invalidate_catalog_cache`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import RequestMeta, StaffPrincipal
from app.core.audit import audit
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.textutil import matches
from app.core.timeutil import utcnow
from app.infrastructure.db.models import AttributeSchema, Category, ServiceType
from app.infrastructure.redis import cache
from app.modules.catalog import repository as repo
from app.modules.catalog.schemas import (
    AttributeSchemaOut,
    CategoryCreateIn,
    CategoryOut,
    CategoryUpdateIn,
    FieldDefIn,
    SchemaCreateIn,
    SchemaUpdateIn,
    ServiceStats,
    ServiceTypeCreateIn,
    ServiceTypeOut,
    ServiceTypeQuery,
    ServiceTypeUpdateIn,
)

CATALOG_CACHE_TTL = 300

MSG_CATEGORY_NOT_FOUND = "Kategoriya topilmadi"
MSG_SERVICE_NOT_FOUND = "Xizmat topilmadi"
MSG_SCHEMA_NOT_FOUND = "Sxema topilmadi"
MSG_TEMPLATE_NOT_FOUND = "Shablon topilmadi"


def _cache_prefix(company_id: uuid.UUID | str) -> str:
    return f"co:{company_id}:catalog:"


def _categories_key(company_id: uuid.UUID | str) -> str:
    return _cache_prefix(company_id) + "categories"


def _service_types_key(company_id: uuid.UUID | str) -> str:
    return _cache_prefix(company_id) + "service-types"


async def invalidate_catalog_cache(company_id: uuid.UUID | str) -> None:
    """Drop every cached catalog list of the company (call after any catalog write)."""
    await cache.delete_prefix(_cache_prefix(company_id))


def _scope_of(staff: StaffPrincipal) -> uuid.UUID | None:
    """Tenant filter for id-addressed endpoints: superadmin sees every company."""
    return None if staff.is_super_admin else staff.company_id


def _parse_uuid(value: str | None, message: str) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError as exc:
        raise NotFoundError(message) from exc


# ----------------------------------------------------------------------------- mapping


def category_out(c: Category) -> CategoryOut:
    """Category ORM row → DTO."""
    return CategoryOut(
        id=str(c.id),
        company_id=str(c.company_id),
        parent_id=str(c.parent_id) if c.parent_id else None,
        name=c.name,
        code=c.code,
        icon=c.icon,
        color=c.color,
        order=c.order,
        is_active=c.is_active,
        phone=c.phone,
        workflow=c.workflow,  # type: ignore[arg-type]
        created_at=c.created_at,
        updated_at=c.updated_at,
    )


def service_type_out(s: ServiceType, ordered_30d: int | None = None) -> ServiceTypeOut:
    """ServiceType ORM row → DTO (`stats` only when a count is supplied)."""
    return ServiceTypeOut(
        id=str(s.id),
        company_id=str(s.company_id),
        category_id=str(s.category_id),
        name=s.name,
        code=s.code,
        description=s.description,
        price=int(s.price),
        branch_prices={str(k): int(v) for k, v in (s.branch_prices or {}).items()},
        turnaround_days=int(s.turnaround_days),
        order=int(s.order),
        is_active=s.is_active,
        schema_id=str(s.schema_id) if s.schema_id else None,
        document_scope=s.document_scope,  # type: ignore[arg-type]
        default_template_id=str(s.default_template_id) if s.default_template_id else None,
        stats=ServiceStats(ordered30d=ordered_30d) if ordered_30d is not None else None,
        created_at=s.created_at,
        updated_at=s.updated_at,
    )


def schema_out(s: AttributeSchema, used_by: int) -> AttributeSchemaOut:
    """AttributeSchema ORM row → DTO; `fields` JSON is returned as stored."""
    return AttributeSchemaOut(
        id=str(s.id),
        company_id=str(s.company_id),
        name=s.name,
        description=s.description,
        version=int(s.version),
        status=s.status,  # type: ignore[arg-type]
        fields=list(s.fields or []),
        used_by=used_by,
        created_at=s.created_at,
        updated_at=s.updated_at,
    )


def _category_snapshot(c: Category) -> dict[str, Any]:
    return {
        "name": c.name,
        "code": c.code,
        "parentId": str(c.parent_id) if c.parent_id else None,
        "order": c.order,
        "isActive": c.is_active,
        "workflow": c.workflow,
        "phone": c.phone,
    }


def _service_snapshot(s: ServiceType) -> dict[str, Any]:
    return {
        "name": s.name,
        "code": s.code,
        "categoryId": str(s.category_id),
        "price": int(s.price),
        "branchPrices": {str(k): int(v) for k, v in (s.branch_prices or {}).items()},
        "turnaroundDays": s.turnaround_days,
        "order": s.order,
        "isActive": s.is_active,
        "schemaId": str(s.schema_id) if s.schema_id else None,
        "documentScope": s.document_scope,
        "defaultTemplateId": str(s.default_template_id) if s.default_template_id else None,
    }


def _schema_snapshot(s: AttributeSchema) -> dict[str, Any]:
    fields = [f for f in (s.fields or []) if isinstance(f, dict)]
    return {"name": s.name, "version": s.version, "status": s.status, "fieldCount": len(fields), "fieldKeys": [f.get("key") for f in fields]}


# ----------------------------------------------------------------------------- shared helpers (other modules)


async def get_service_types_by_ids(session: AsyncSession, company_id: uuid.UUID, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, ServiceType]:
    """Alive service types of the company keyed by id (unknown ids are simply absent)."""
    rows = await repo.get_service_types_by_ids(session, company_id, ids)
    return {row.id: row for row in rows}


async def get_category(session: AsyncSession, company_id: uuid.UUID, category_id: uuid.UUID) -> Category | None:
    """Alive category of the company or None."""
    return await repo.get_category(session, category_id, company_id)


async def get_schemas_by_ids(session: AsyncSession, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, AttributeSchema]:
    """Alive attribute schemas keyed by id (cross-company by design: callers hold ids from their own rows)."""
    rows = await repo.get_schemas_by_ids(session, ids)
    return {row.id: row for row in rows}


async def get_category_or_404(session: AsyncSession, category_id: uuid.UUID, company_id: uuid.UUID | None) -> Category:
    """Alive category or 404 "Kategoriya topilmadi"; `company_id=None` = platform-wide (superadmin)."""
    row = await repo.get_category(session, category_id, company_id)
    if not row:
        raise NotFoundError(MSG_CATEGORY_NOT_FOUND)
    return row


async def get_service_type_or_404(session: AsyncSession, service_type_id: uuid.UUID, company_id: uuid.UUID | None) -> ServiceType:
    """Alive service type or 404 "Xizmat topilmadi"."""
    row = await repo.get_service_type(session, service_type_id, company_id)
    if not row:
        raise NotFoundError(MSG_SERVICE_NOT_FOUND)
    return row


async def get_schema_or_404(session: AsyncSession, schema_id: uuid.UUID, company_id: uuid.UUID | None) -> AttributeSchema:
    """Alive schema or 404 "Sxema topilmadi"."""
    row = await repo.get_schema(session, schema_id, company_id)
    if not row:
        raise NotFoundError(MSG_SCHEMA_NOT_FOUND)
    return row


# ----------------------------------------------------------------------------- categories


async def list_categories(session: AsyncSession, company_id: uuid.UUID) -> list[CategoryOut]:
    """All categories of the company sorted by order (cached 5 min)."""
    key = _categories_key(company_id)
    hit = await cache.get_json(key)
    if hit is not None:
        return [CategoryOut.model_validate(x) for x in hit]
    dtos = [category_out(c) for c in await repo.list_categories(session, company_id)]
    await cache.set_json(key, [d.model_dump(by_alias=True, mode="json") for d in dtos], CATALOG_CACHE_TTL)
    return dtos


async def _resolve_parent(session: AsyncSession, company_id: uuid.UUID, parent_id: str | None, self_id: uuid.UUID | None) -> uuid.UUID | None:
    """Parent must exist in the company; on update it must not be the category itself or a descendant (422)."""
    pid = _parse_uuid(parent_id, MSG_CATEGORY_NOT_FOUND)
    if pid is None:
        return None
    await get_category_or_404(session, pid, company_id)
    if self_id is not None and (pid == self_id or pid in await repo.descendant_category_ids(session, company_id, self_id)):
        raise ValidationError("Kategoriya o‘zining ichki kategoriyasiga ko‘chirilmaydi")
    return pid


async def create_category(session: AsyncSession, company_id: uuid.UUID, body: CategoryCreateIn, staff: StaffPrincipal, meta: RequestMeta) -> CategoryOut:
    """Create a category; `order` defaults to siblings + 1 (same parent)."""
    parent_id = await _resolve_parent(session, company_id, body.parent_id, None)
    order = body.order if body.order is not None else await repo.sibling_count(session, company_id, parent_id) + 1
    row = Category(
        company_id=company_id,
        parent_id=parent_id,
        name=body.name,
        code=body.code or None,
        icon=body.icon or None,
        color=body.color or None,
        order=order,
        is_active=body.is_active,
        phone=body.phone or None,
        workflow=body.workflow,
        created_by=staff.id,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row, ["created_at", "updated_at"])
    await audit(session, actor_type="staff", actor_id=staff.id, company_id=company_id, action="create", entity="category", entity_id=row.id, after=_category_snapshot(row), ip=meta.ip, request_id=meta.request_id)
    await invalidate_catalog_cache(company_id)
    return category_out(row)


async def update_category(session: AsyncSession, category_id: uuid.UUID, body: CategoryUpdateIn, staff: StaffPrincipal, meta: RequestMeta) -> CategoryOut:
    """Partial merge; re-parenting is validated against cycles."""
    row = await get_category_or_404(session, category_id, _scope_of(staff))
    before = _category_snapshot(row)
    data = body.model_dump(exclude_unset=True)
    if "parent_id" in data:
        row.parent_id = await _resolve_parent(session, row.company_id, data["parent_id"], row.id)
    if data.get("name"):
        row.name = data["name"]
    if data.get("workflow"):
        row.workflow = data["workflow"]
    if data.get("order") is not None:
        row.order = data["order"]
    if data.get("is_active") is not None:
        row.is_active = data["is_active"]
    for field in ("code", "icon", "color", "phone"):
        if field in data:
            setattr(row, field, data[field] or None)
    await session.flush()
    await session.refresh(row, ["updated_at"])
    await audit(session, actor_type="staff", actor_id=staff.id, company_id=row.company_id, action="update", entity="category", entity_id=row.id, before=before, after=_category_snapshot(row), ip=meta.ip, request_id=meta.request_id)
    await invalidate_catalog_cache(row.company_id)
    return category_out(row)


async def delete_category(session: AsyncSession, category_id: uuid.UUID, staff: StaffPrincipal, meta: RequestMeta) -> None:
    """Soft delete; 409 has_children when sub-categories exist, 409 in_use when services exist."""
    row = await get_category_or_404(session, category_id, _scope_of(staff))
    if await repo.has_child_categories(session, row.company_id, row.id):
        raise ConflictError("Avval ichki kategoriyalarni o‘chiring", code="has_children")
    if await repo.category_has_services(session, row.company_id, row.id):
        raise ConflictError("Kategoriyada xizmatlar bor", code="in_use")
    row.deleted_at = utcnow()
    row.deleted_by = staff.id
    await session.flush()
    await audit(session, actor_type="staff", actor_id=staff.id, company_id=row.company_id, action="delete", entity="category", entity_id=row.id, before=_category_snapshot(row), ip=meta.ip, request_id=meta.request_id)
    await invalidate_catalog_cache(row.company_id)


# ----------------------------------------------------------------------------- service types


async def _service_types_cached(session: AsyncSession, company_id: uuid.UUID) -> list[ServiceTypeOut]:
    """Base list of the company's service types (no stats), cached 5 min."""
    key = _service_types_key(company_id)
    hit = await cache.get_json(key)
    if hit is not None:
        return [ServiceTypeOut.model_validate(x) for x in hit]
    dtos = [service_type_out(s) for s in await repo.list_service_types(session, company_id)]
    await cache.set_json(key, [d.model_dump(by_alias=True, mode="json") for d in dtos], CATALOG_CACHE_TTL)
    return dtos


async def list_service_types(session: AsyncSession, company_id: uuid.UUID, q: ServiceTypeQuery) -> list[ServiceTypeOut]:
    """Service types filtered by category subtree / activeOnly / search (fold on name+code), order asc,
    each with `stats.ordered30d` computed by one grouped query over the last 30 days."""
    items = await _service_types_cached(session, company_id)
    if q.category_id:
        root = _parse_uuid(q.category_id, MSG_CATEGORY_NOT_FOUND)
        allowed = {str(i) for i in await repo.descendant_category_ids(session, company_id, root)}  # type: ignore[arg-type]
        items = [s for s in items if s.category_id in allowed]
    if q.active_only:
        items = [s for s in items if s.is_active]
    if q.search:
        items = [s for s in items if matches(s.name, q.search) or matches(s.code, q.search)]
    counts = await repo.ordered_30d_by_service(session, company_id, [uuid.UUID(s.id) for s in items])
    for s in items:
        s.stats = ServiceStats(ordered30d=counts.get(uuid.UUID(s.id), 0))
    return items


async def get_service_type_dto(session: AsyncSession, service_type_id: uuid.UUID, staff: StaffPrincipal) -> ServiceTypeOut:
    """Single service type (own company; superadmin any) with `stats`."""
    row = await get_service_type_or_404(session, service_type_id, _scope_of(staff))
    counts = await repo.ordered_30d_by_service(session, row.company_id, [row.id])
    return service_type_out(row, counts.get(row.id, 0))


async def _ensure_service_code_free(session: AsyncSession, company_id: uuid.UUID, code: str | None, exclude_id: uuid.UUID | None) -> None:
    if code and await repo.service_code_taken(session, company_id, code, exclude_id):
        raise ConflictError("Bu kod band")


async def _resolve_schema_id(session: AsyncSession, company_id: uuid.UUID, schema_id: str | None) -> uuid.UUID | None:
    sid = _parse_uuid(schema_id, MSG_SCHEMA_NOT_FOUND)
    if sid is not None:
        await get_schema_or_404(session, sid, company_id)
    return sid


async def create_service_type(session: AsyncSession, company_id: uuid.UUID, body: ServiceTypeCreateIn, staff: StaffPrincipal, meta: RequestMeta) -> ServiceTypeOut:
    """Create a service type (defaults: price 0, turnaround 1, order 99, active, scope item); code unique per company."""
    category_id = _parse_uuid(body.category_id, MSG_CATEGORY_NOT_FOUND)
    await get_category_or_404(session, category_id, company_id)  # type: ignore[arg-type]
    code = (body.code or "").strip() or None
    await _ensure_service_code_free(session, company_id, code, None)
    row = ServiceType(
        company_id=company_id,
        category_id=category_id,
        name=body.name,
        code=code,
        description=body.description or None,
        price=body.price,
        branch_prices=dict(body.branch_prices or {}),
        turnaround_days=body.turnaround_days,
        order=body.order,
        is_active=body.is_active,
        schema_id=await _resolve_schema_id(session, company_id, body.schema_id),
        document_scope=body.document_scope,
        default_template_id=_parse_uuid(body.default_template_id, MSG_TEMPLATE_NOT_FOUND),
        created_by=staff.id,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row, ["created_at", "updated_at"])
    await audit(session, actor_type="staff", actor_id=staff.id, company_id=company_id, action="create", entity="service_type", entity_id=row.id, after=_service_snapshot(row), ip=meta.ip, request_id=meta.request_id)
    await invalidate_catalog_cache(company_id)
    return service_type_out(row, 0)


async def update_service_type(session: AsyncSession, service_type_id: uuid.UUID, body: ServiceTypeUpdateIn, staff: StaffPrincipal, meta: RequestMeta) -> ServiceTypeOut:
    """Partial merge; `branchPrices` replaced wholesale; code stays unique per company."""
    row = await get_service_type_or_404(session, service_type_id, _scope_of(staff))
    before = _service_snapshot(row)
    data = body.model_dump(exclude_unset=True)
    if data.get("category_id"):
        cid = _parse_uuid(data["category_id"], MSG_CATEGORY_NOT_FOUND)
        await get_category_or_404(session, cid, row.company_id)  # type: ignore[arg-type]
        row.category_id = cid  # type: ignore[assignment]
    if "code" in data:
        code = (data["code"] or "").strip() or None
        if code and code.lower() != (row.code or "").lower():
            await _ensure_service_code_free(session, row.company_id, code, row.id)
        row.code = code
    if data.get("name"):
        row.name = data["name"]
    if "description" in data:
        row.description = data["description"] or None
    for field in ("price", "turnaround_days", "order", "is_active", "document_scope"):
        if data.get(field) is not None:
            setattr(row, field, data[field])
    if data.get("branch_prices") is not None:
        row.branch_prices = dict(data["branch_prices"])
    if "schema_id" in data:
        row.schema_id = await _resolve_schema_id(session, row.company_id, data["schema_id"])
    if "default_template_id" in data:
        row.default_template_id = _parse_uuid(data["default_template_id"], MSG_TEMPLATE_NOT_FOUND)
    await session.flush()
    await session.refresh(row, ["updated_at"])
    await audit(session, actor_type="staff", actor_id=staff.id, company_id=row.company_id, action="update", entity="service_type", entity_id=row.id, before=before, after=_service_snapshot(row), ip=meta.ip, request_id=meta.request_id)
    await invalidate_catalog_cache(row.company_id)
    counts = await repo.ordered_30d_by_service(session, row.company_id, [row.id])
    return service_type_out(row, counts.get(row.id, 0))


async def delete_service_type(session: AsyncSession, service_type_id: uuid.UUID, staff: StaffPrincipal, meta: RequestMeta) -> None:
    """Soft delete; 409 in_use when any order item references the service."""
    row = await get_service_type_or_404(session, service_type_id, _scope_of(staff))
    if await repo.service_type_has_order_items(session, row.company_id, row.id):
        raise ConflictError("Bu xizmat bo‘yicha buyurtmalar bor — o‘chirish o‘rniga nofaol qiling", code="in_use")
    row.deleted_at = utcnow()
    row.deleted_by = staff.id
    await session.flush()
    await audit(session, actor_type="staff", actor_id=staff.id, company_id=row.company_id, action="delete", entity="service_type", entity_id=row.id, before=_service_snapshot(row), ip=meta.ip, request_id=meta.request_id)
    await invalidate_catalog_cache(row.company_id)


# ----------------------------------------------------------------------------- attribute schemas


def _fields_payload(fields: list[FieldDefIn]) -> list[dict[str, Any]]:
    """Validated field defs → JSON as sent (extras preserved); keys must be unique (422)."""
    seen: set[str] = set()
    for f in fields:
        if f.key in seen:
            raise ValidationError(f"Maydon kaliti takrorlangan: {f.key}")
        seen.add(f.key)
    return [f.model_dump(mode="json") for f in fields]


async def list_schemas(session: AsyncSession, company_id: uuid.UUID) -> list[AttributeSchemaOut]:
    """All schemas of the company with `usedBy` (one grouped query)."""
    rows = await repo.list_schemas(session, company_id)
    usage = await repo.schema_usage(session, company_id, [r.id for r in rows])
    return [schema_out(r, usage.get(r.id, 0)) for r in rows]


async def get_schema_dto(session: AsyncSession, schema_id: uuid.UUID, staff: StaffPrincipal) -> AttributeSchemaOut:
    """Single schema (own company; superadmin any) with `usedBy`."""
    row = await get_schema_or_404(session, schema_id, _scope_of(staff))
    usage = await repo.schema_usage(session, row.company_id, [row.id])
    return schema_out(row, usage.get(row.id, 0))


async def create_schema(session: AsyncSession, company_id: uuid.UUID, body: SchemaCreateIn, staff: StaffPrincipal, meta: RequestMeta) -> AttributeSchemaOut:
    """Create a schema: version 1, status draft, fields as given (default [])."""
    row = AttributeSchema(
        company_id=company_id,
        name=body.name,
        description=body.description or None,
        version=1,
        status="draft",
        fields=_fields_payload(body.fields),
        created_by=staff.id,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row, ["created_at", "updated_at"])
    await audit(session, actor_type="staff", actor_id=staff.id, company_id=company_id, action="create", entity="attribute_schema", entity_id=row.id, after=_schema_snapshot(row), ip=meta.ip, request_id=meta.request_id)
    await invalidate_catalog_cache(company_id)
    return schema_out(row, 0)


async def update_schema(session: AsyncSession, schema_id: uuid.UUID, body: SchemaUpdateIn, staff: StaffPrincipal, meta: RequestMeta) -> AttributeSchemaOut:
    """Partial merge; `version += 1` iff the schema is published AND the payload contains `fields`; status unchanged."""
    row = await get_schema_or_404(session, schema_id, _scope_of(staff))
    before = _schema_snapshot(row)
    data = body.model_dump(exclude_unset=True)
    if data.get("name"):
        row.name = data["name"]
    if "description" in data:
        row.description = data["description"] or None
    if body.fields is not None:
        row.fields = _fields_payload(body.fields)
        if row.status == "published":
            row.version = int(row.version) + 1
    await session.flush()
    await session.refresh(row, ["updated_at"])
    await audit(session, actor_type="staff", actor_id=staff.id, company_id=row.company_id, action="update", entity="attribute_schema", entity_id=row.id, before=before, after=_schema_snapshot(row), ip=meta.ip, request_id=meta.request_id)
    await invalidate_catalog_cache(row.company_id)
    usage = await repo.schema_usage(session, row.company_id, [row.id])
    return schema_out(row, usage.get(row.id, 0))


async def publish_schema(session: AsyncSession, schema_id: uuid.UUID, staff: StaffPrincipal, meta: RequestMeta) -> AttributeSchemaOut:
    """Set status published (no version bump); 422 empty when the schema has no fields."""
    row = await get_schema_or_404(session, schema_id, _scope_of(staff))
    if not row.fields:
        raise ValidationError("Kamida bitta maydon kerak", code="empty")
    before = _schema_snapshot(row)
    row.status = "published"
    await session.flush()
    await session.refresh(row, ["updated_at"])
    await audit(session, actor_type="staff", actor_id=staff.id, company_id=row.company_id, action="publish", entity="attribute_schema", entity_id=row.id, before=before, after=_schema_snapshot(row), ip=meta.ip, request_id=meta.request_id)
    await invalidate_catalog_cache(row.company_id)
    usage = await repo.schema_usage(session, row.company_id, [row.id])
    return schema_out(row, usage.get(row.id, 0))
