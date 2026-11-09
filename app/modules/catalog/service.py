"""Catalog business rules: category tree, service types (+ per-unit price / on-off), attribute schemas.

Ownership: every row has an `owner_unit_id` (a unit of any level) and is visible to the owner's whole subtree.
Management lists (`/units/{unit_id}/...`) show the lineage of the selected unit — what it inherits from above
plus everything owned below; creating under a unit makes that unit the owner. A row addressed by id may be
read / changed by anyone whose scope contains the owner or whose own units lie under it (superadmin: any).

Read-mostly lists are cached per REQUESTING unit in Redis (`catalog:{unit_id}:categories|services`, 5 min);
visibility is inherited, so any write drops the whole `catalog:` namespace.

Public helpers reused by other modules: `get_service_types_by_ids`, `get_category`, `get_schemas_by_ids`,
`effective_for_unit`, `available_at`, `list_service_types`, `invalidate_catalog_cache`,
`get_category_or_404`, `get_service_type_or_404`, `get_schema_or_404`, `category_out`, `service_type_out`,
`schema_out`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import RequestMeta, StaffPrincipal
from app.core.audit import audit
from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.core.scope import UnitScope, ancestor_ids, descendant_ids
from app.core.textutil import matches
from app.core.timeutil import utcnow
from app.infrastructure.db.models import AttributeSchema, Category, ServiceType, ServiceUnitSetting
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
from app.modules.units import repository as units_repo

CATALOG_CACHE_TTL = 300
CACHE_PREFIX = "catalog:"

MSG_CATEGORY_NOT_FOUND = "Kategoriya topilmadi"
MSG_SERVICE_NOT_FOUND = "Xizmat topilmadi"
MSG_SCHEMA_NOT_FOUND = "Sxema topilmadi"
MSG_TEMPLATE_NOT_FOUND = "Shablon topilmadi"
MSG_UNIT_NOT_FOUND = "Birlik topilmadi"
MSG_FORBIDDEN = "Ruxsat yo‘q"
MSG_PARENT_NOT_VISIBLE = "Ota kategoriya bu birlikda ko‘rinmaydi"

#: (enabled, price) of a service at a unit — see `effective_for_unit`
Effective = tuple[bool, int]


def _categories_key(unit_id: uuid.UUID | str) -> str:
    return f"{CACHE_PREFIX}{unit_id}:categories"


def _services_key(unit_id: uuid.UUID | str) -> str:
    return f"{CACHE_PREFIX}{unit_id}:services"


async def invalidate_catalog_cache(owner_unit_id: uuid.UUID | str | None = None) -> None:
    """Drop every cached catalog list (call after any catalog write). Rows are inherited down the tree, so a
    write under any owner may change what every other unit sees — the whole namespace goes."""
    await cache.delete_prefix(CACHE_PREFIX)


def _parse_uuid(value: str | None, message: str) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError as exc:
        raise NotFoundError(message) from exc


def _uuid_or_404(value: str | uuid.UUID, message: str) -> uuid.UUID:
    parsed = _parse_uuid(str(value), message)
    if parsed is None:
        raise NotFoundError(message)
    return parsed


# ----------------------------------------------------------------------------- visibility


async def require_owner_visible(session: AsyncSession, staff: StaffPrincipal, owner_unit_id: uuid.UUID) -> None:
    """A catalogue row may be read / changed when its owner lies inside the caller's scope (own units and below)
    or on the path above one of the caller's units (rows they inherit). Superadmin: anything. 403 otherwise."""
    if staff.can_see(owner_unit_id):
        return
    if owner_unit_id in await units_repo.ancestors_of(session, staff.unit_ids):
        return
    raise ForbiddenError(MSG_FORBIDDEN)


async def _owner_path(session: AsyncSession, owner_unit_id: uuid.UUID) -> set[uuid.UUID]:
    """Root → owner itself: the owners whose catalogue is visible AT `owner_unit_id` (what a row owned there may
    reference — its parent category, its category, its schema)."""
    return set(await ancestor_ids(session, owner_unit_id))


async def _audit(
    session: AsyncSession,
    *,
    owner_unit_id: uuid.UUID,
    staff: StaffPrincipal,
    meta: RequestMeta,
    action: str,
    entity: str,
    entity_id: uuid.UUID,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    """Audit rows of catalogue writes carry the owner unit and its country."""
    owner = await units_repo.get_unit_any(session, owner_unit_id)
    await audit(
        session,
        actor_type="staff",
        actor_id=staff.id,
        action=action,
        entity=entity,
        entity_id=entity_id,
        unit_id=owner_unit_id,
        country_id=owner.country_id if owner is not None else None,
        before=before,
        after=after,
        ip=meta.ip,
        request_id=meta.request_id,
    )


# ----------------------------------------------------------------------------- mapping


def category_out(c: Category) -> CategoryOut:
    """Category ORM row → DTO."""
    return CategoryOut(
        id=str(c.id),
        owner_unit_id=str(c.owner_unit_id),
        parent_id=str(c.parent_id) if c.parent_id else None,
        name=c.name,
        code=c.code,
        icon=c.icon,
        color=c.color,
        order=int(c.sort_order),
        is_active=c.is_active,
        phone=c.phone,
        workflow=c.workflow,  # type: ignore[arg-type]
        created_at=c.created_at,
        updated_at=c.updated_at,
    )


def settings_view(rows: Iterable[ServiceUnitSetting]) -> tuple[dict[str, int], list[str]]:
    """`service_unit_settings` rows of one service → (`unitPrices`, `disabledUnitIds`) as the wire shows them."""
    prices: dict[str, int] = {}
    disabled: list[str] = []
    for r in rows:
        if r.price is not None:
            prices[str(r.unit_id)] = int(r.price)
        if not r.is_enabled:
            disabled.append(str(r.unit_id))
    return prices, disabled


def service_type_out(s: ServiceType, settings: Iterable[ServiceUnitSetting] = (), ordered_30d: int | None = None, effective: Effective | None = None) -> ServiceTypeOut:
    """ServiceType ORM row (+ its per-unit rows) → DTO; `stats` only when a count is supplied, `effectivePrice` /
    `available` only when resolved for a unit."""
    prices, disabled = settings_view(settings)
    return ServiceTypeOut(
        id=str(s.id),
        owner_unit_id=str(s.owner_unit_id),
        category_id=str(s.category_id),
        name=s.name,
        code=s.code,
        description=s.description,
        price=int(s.price),
        unit_prices=prices,
        disabled_unit_ids=disabled,
        effective_price=effective[1] if effective is not None else None,
        available=effective[0] if effective is not None else None,
        turnaround_days=int(s.turnaround_days),
        order=int(s.sort_order),
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
        owner_unit_id=str(s.owner_unit_id),
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
        "ownerUnitId": str(c.owner_unit_id),
        "parentId": str(c.parent_id) if c.parent_id else None,
        "order": int(c.sort_order),
        "isActive": c.is_active,
        "workflow": c.workflow,
        "phone": c.phone,
    }


def _service_snapshot(s: ServiceType, settings: Iterable[ServiceUnitSetting]) -> dict[str, Any]:
    prices, disabled = settings_view(settings)
    return {
        "name": s.name,
        "code": s.code,
        "ownerUnitId": str(s.owner_unit_id),
        "categoryId": str(s.category_id),
        "price": int(s.price),
        "unitPrices": prices,
        "disabledUnitIds": disabled,
        "turnaroundDays": s.turnaround_days,
        "order": int(s.sort_order),
        "isActive": s.is_active,
        "schemaId": str(s.schema_id) if s.schema_id else None,
        "documentScope": s.document_scope,
        "defaultTemplateId": str(s.default_template_id) if s.default_template_id else None,
    }


def _schema_snapshot(s: AttributeSchema) -> dict[str, Any]:
    fields = [f for f in (s.fields or []) if isinstance(f, dict)]
    return {"name": s.name, "ownerUnitId": str(s.owner_unit_id), "version": s.version, "status": s.status, "fieldCount": len(fields), "fieldKeys": [f.get("key") for f in fields]}


# ----------------------------------------------------------------------------- shared helpers (other modules)


async def get_service_types_by_ids(session: AsyncSession, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, ServiceType]:
    """Alive service types keyed by id (unknown ids are simply absent; callers hold ids from their own rows)."""
    rows = await repo.get_service_types_by_ids(session, ids)
    return {row.id: row for row in rows}


async def get_category(session: AsyncSession, category_id: uuid.UUID) -> Category | None:
    """Alive category or None."""
    return await repo.get_category(session, category_id)


async def get_schemas_by_ids(session: AsyncSession, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, AttributeSchema]:
    """Alive attribute schemas keyed by id (callers hold ids from their own rows)."""
    rows = await repo.get_schemas_by_ids(session, ids)
    return {row.id: row for row in rows}


async def get_category_or_404(session: AsyncSession, category_id: uuid.UUID, staff: StaffPrincipal | None = None) -> Category:
    """Alive category or 404 "Kategoriya topilmadi"; with `staff`, 403 when its owner is outside their view."""
    row = await repo.get_category(session, category_id)
    if not row:
        raise NotFoundError(MSG_CATEGORY_NOT_FOUND)
    if staff is not None:
        await require_owner_visible(session, staff, row.owner_unit_id)
    return row


async def get_service_type_or_404(session: AsyncSession, service_type_id: uuid.UUID, staff: StaffPrincipal | None = None) -> ServiceType:
    """Alive service type or 404 "Xizmat topilmadi"; with `staff`, 403 when its owner is outside their view."""
    row = await repo.get_service_type(session, service_type_id)
    if not row:
        raise NotFoundError(MSG_SERVICE_NOT_FOUND)
    if staff is not None:
        await require_owner_visible(session, staff, row.owner_unit_id)
    return row


async def get_schema_or_404(session: AsyncSession, schema_id: uuid.UUID, staff: StaffPrincipal | None = None) -> AttributeSchema:
    """Alive schema or 404 "Sxema topilmadi"; with `staff`, 403 when its owner is outside their view."""
    row = await repo.get_schema(session, schema_id)
    if not row:
        raise NotFoundError(MSG_SCHEMA_NOT_FOUND)
    if staff is not None:
        await require_owner_visible(session, staff, row.owner_unit_id)
    return row


# ----------------------------------------------------------------------------- effective price / availability


def _resolve_on_path(path: Sequence[uuid.UUID], rows: Iterable[ServiceUnitSetting], defaults: dict[uuid.UUID, Effective]) -> dict[uuid.UUID, Effective]:
    """Walk each service's rows leaf → root (`path` is root first): the nearest row decides `is_enabled`, the
    nearest row with a price decides the price; `defaults` = (enabled, price) when nothing is set."""
    by_service: dict[uuid.UUID, dict[uuid.UUID, ServiceUnitSetting]] = {}
    for r in rows:
        by_service.setdefault(r.service_type_id, {})[r.unit_id] = r
    out: dict[uuid.UUID, Effective] = {}
    for sid, (default_enabled, base_price) in defaults.items():
        on_path = by_service.get(sid) or {}
        enabled: bool | None = None
        price: int | None = None
        for unit_id in reversed(path):
            r = on_path.get(unit_id)
            if r is None:
                continue
            if enabled is None:
                enabled = bool(r.is_enabled)
            if price is None and r.price is not None:
                price = int(r.price)
            if enabled is not None and price is not None:
                break
        out[sid] = (default_enabled and (enabled if enabled is not None else True), price if price is not None else base_price)
    return out


async def effective_for_unit(session: AsyncSession, service_types: Iterable[ServiceType], unit_id: uuid.UUID) -> dict[uuid.UUID, Effective]:
    """`{service id: (enabled, price)}` AT `unit_id`: the nearest `service_unit_settings` row on the unit's path
    (root → unit) wins for on/off, the nearest row with a price wins for the price; default = (the service is
    active AND its owner lies on the path, base price). A service the unit cannot see is therefore disabled."""
    rows_in = list(service_types)
    if not rows_in:
        return {}
    path = await ancestor_ids(session, unit_id)
    on_path = set(path)
    defaults: dict[uuid.UUID, Effective] = {s.id: (bool(s.is_active) and s.owner_unit_id in on_path, int(s.price)) for s in rows_in}
    settings = await repo.unit_settings_for(session, list(defaults), path)
    return _resolve_on_path(path, settings, defaults)


async def available_at(session: AsyncSession, service_type_id: uuid.UUID, unit_id: uuid.UUID) -> bool:
    """Is the service offered AT `unit_id` (alive, active, visible there and not switched off on its path)?"""
    row = await repo.get_service_type(session, service_type_id)
    if row is None:
        return False
    enabled, _price = (await effective_for_unit(session, [row], unit_id)).get(row.id, (False, 0))
    return enabled


# ----------------------------------------------------------------------------- categories


async def list_categories(session: AsyncSession, scope: UnitScope) -> list[CategoryOut]:
    """Categories managed from the unit — inherited from above plus everything owned in its subtree — sorted by
    order (cached 5 min per requesting unit)."""
    key = _categories_key(scope.id)
    hit = await cache.get_json(key)
    if hit is not None:
        return [CategoryOut.model_validate(x) for x in hit]
    dtos = [category_out(c) for c in await repo.list_categories(session, scope.lineage_ids)]
    await cache.set_json(key, [d.model_dump(by_alias=True, mode="json") for d in dtos], CATALOG_CACHE_TTL)
    return dtos


async def _resolve_parent(session: AsyncSession, owner_unit_id: uuid.UUID, parent_id: str | None, self_id: uuid.UUID | None) -> uuid.UUID | None:
    """The parent must exist (404) and be visible AT the owner — owned by the owner or one of its ancestors —
    so the child is never shown where its parent is not (422); on update it must not be the category itself or
    one of its descendants (422)."""
    pid = _parse_uuid(parent_id, MSG_CATEGORY_NOT_FOUND)
    if pid is None:
        return None
    parent = await repo.get_category(session, pid)
    if parent is None:
        raise NotFoundError(MSG_CATEGORY_NOT_FOUND)
    if parent.owner_unit_id not in await _owner_path(session, owner_unit_id):
        raise ValidationError(MSG_PARENT_NOT_VISIBLE, code="parent_not_visible")
    if self_id is not None and (pid == self_id or pid in await repo.descendant_category_ids(session, self_id)):
        raise ValidationError("Kategoriya o‘zining ichki kategoriyasiga ko‘chirilmaydi")
    return pid


async def create_category(session: AsyncSession, scope: UnitScope, body: CategoryCreateIn, staff: StaffPrincipal, meta: RequestMeta) -> CategoryOut:
    """Create a category owned by the path unit; `order` defaults to visible siblings + 1 (same parent)."""
    owner_unit_id = scope.id
    parent_id = await _resolve_parent(session, owner_unit_id, body.parent_id, None)
    order = body.order if body.order is not None else await repo.sibling_count(session, scope.lineage_ids, parent_id) + 1
    row = Category(
        owner_unit_id=owner_unit_id,
        parent_id=parent_id,
        name=body.name,
        code=body.code or None,
        icon=body.icon or None,
        color=body.color or None,
        sort_order=order,
        is_active=body.is_active,
        phone=body.phone or None,
        workflow=body.workflow,
        created_by=staff.id,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row, ["created_at", "updated_at"])
    await _audit(session, owner_unit_id=owner_unit_id, staff=staff, meta=meta, action="create", entity="category", entity_id=row.id, after=_category_snapshot(row))
    await invalidate_catalog_cache(owner_unit_id)
    return category_out(row)


async def update_category(session: AsyncSession, category_id: uuid.UUID, body: CategoryUpdateIn, staff: StaffPrincipal, meta: RequestMeta) -> CategoryOut:
    """Partial merge; re-parenting is validated against visibility and cycles."""
    row = await get_category_or_404(session, category_id, staff)
    before = _category_snapshot(row)
    data = body.model_dump(exclude_unset=True)
    if "parent_id" in data:
        row.parent_id = await _resolve_parent(session, row.owner_unit_id, data["parent_id"], row.id)
    if data.get("name"):
        row.name = data["name"]
    if data.get("workflow"):
        row.workflow = data["workflow"]
    if data.get("order") is not None:
        row.sort_order = data["order"]
    if data.get("is_active") is not None:
        row.is_active = data["is_active"]
    for field in ("code", "icon", "color", "phone"):
        if field in data:
            setattr(row, field, data[field] or None)
    await session.flush()
    await session.refresh(row, ["updated_at"])
    await _audit(session, owner_unit_id=row.owner_unit_id, staff=staff, meta=meta, action="update", entity="category", entity_id=row.id, before=before, after=_category_snapshot(row))
    await invalidate_catalog_cache(row.owner_unit_id)
    return category_out(row)


async def delete_category(session: AsyncSession, category_id: uuid.UUID, staff: StaffPrincipal, meta: RequestMeta) -> None:
    """Soft delete; 409 has_children when sub-categories exist (whoever owns them), 409 in_use when services exist."""
    row = await get_category_or_404(session, category_id, staff)
    if await repo.has_child_categories(session, row.id):
        raise ConflictError("Avval ichki kategoriyalarni o‘chiring", code="has_children")
    if await repo.category_has_services(session, row.id):
        raise ConflictError("Kategoriyada xizmatlar bor", code="in_use")
    row.deleted_at = utcnow()
    row.deleted_by = staff.id
    await session.flush()
    await _audit(session, owner_unit_id=row.owner_unit_id, staff=staff, meta=meta, action="delete", entity="category", entity_id=row.id, before=_category_snapshot(row))
    await invalidate_catalog_cache(row.owner_unit_id)


# ----------------------------------------------------------------------------- service types


async def _service_types_cached(session: AsyncSession, scope: UnitScope) -> list[ServiceTypeOut]:
    """Base list of the lineage's service types with their per-unit rows (no stats, nothing resolved), cached 5 min
    per requesting unit."""
    key = _services_key(scope.id)
    hit = await cache.get_json(key)
    if hit is not None:
        return [ServiceTypeOut.model_validate(x) for x in hit]
    rows = await repo.list_service_types(session, scope.lineage_ids)
    by_service: dict[uuid.UUID, list[ServiceUnitSetting]] = {}
    for st in await repo.unit_settings_for(session, [r.id for r in rows]):
        by_service.setdefault(st.service_type_id, []).append(st)
    dtos = [service_type_out(r, by_service.get(r.id, ())) for r in rows]
    await cache.set_json(key, [d.model_dump(by_alias=True, mode="json") for d in dtos], CATALOG_CACHE_TTL)
    return dtos


async def list_service_types(session: AsyncSession, scope: UnitScope, q: ServiceTypeQuery) -> list[ServiceTypeOut]:
    """Service types of the lineage filtered by category subtree / activeOnly / search (fold on name+code), order asc,
    each with `stats.ordered30d` over the scope's units (one grouped query, last 30 days).

    `unitId` (a unit inside the scope) switches to the "usable AT that unit" view: only rows owned on the unit's
    path, with `effectivePrice` / `available` resolved for it (switched-off services stay in the list, flagged)."""
    items = await _service_types_cached(session, scope)
    path: list[uuid.UUID] = []
    if q.unit_id:
        unit_id = _uuid_or_404(q.unit_id, MSG_UNIT_NOT_FOUND)
        if not scope.contains(unit_id):
            raise NotFoundError(MSG_UNIT_NOT_FOUND)
        path = await ancestor_ids(session, unit_id)
        on_path = {str(u) for u in path}
        items = [s for s in items if s.owner_unit_id in on_path]
    if q.category_id:
        root = _uuid_or_404(q.category_id, MSG_CATEGORY_NOT_FOUND)
        allowed = {str(i) for i in await repo.descendant_category_ids(session, root)}
        items = [s for s in items if s.category_id in allowed]
    if q.active_only:
        items = [s for s in items if s.is_active]
    if q.search:
        items = [s for s in items if matches(s.name, q.search) or matches(s.code, q.search)]
    ids = [uuid.UUID(s.id) for s in items]
    counts = await repo.ordered_30d_by_service(session, scope.descendant_ids, ids)
    effective: dict[uuid.UUID, Effective] = {}
    if path:
        defaults: dict[uuid.UUID, Effective] = {uuid.UUID(s.id): (s.is_active, s.price) for s in items}
        effective = _resolve_on_path(path, await repo.unit_settings_for(session, ids, path), defaults)
    for s in items:
        sid = uuid.UUID(s.id)
        s.stats = ServiceStats(ordered30d=counts.get(sid, 0))
        if path:
            s.available, s.effective_price = effective[sid]
    return items


async def _stats_units(session: AsyncSession, staff: StaffPrincipal, owner_unit_id: uuid.UUID) -> list[uuid.UUID]:
    """Units whose cheques count towards a single service's 30-day stat: the owner's subtree ∩ the caller's scope."""
    return staff.restrict(await descendant_ids(session, owner_unit_id))


async def get_service_type_dto(session: AsyncSession, service_type_id: uuid.UUID, staff: StaffPrincipal) -> ServiceTypeOut:
    """Single service type (visible owner; superadmin any) with its per-unit rows and `stats`."""
    row = await get_service_type_or_404(session, service_type_id, staff)
    settings = await repo.unit_settings_for(session, [row.id])
    counts = await repo.ordered_30d_by_service(session, await _stats_units(session, staff, row.owner_unit_id), [row.id])
    return service_type_out(row, settings, counts.get(row.id, 0))


async def _ensure_service_code_free(session: AsyncSession, owner_unit_id: uuid.UUID, code: str | None, exclude_id: uuid.UUID | None) -> None:
    if code and await repo.service_code_taken(session, owner_unit_id, code, exclude_id):
        raise ConflictError("Bu kod band")


async def _category_on_path(session: AsyncSession, category_id: uuid.UUID, owner_path: set[uuid.UUID]) -> uuid.UUID:
    """A service's category must be visible AT the service's owner (owned by it or an ancestor) — 404 otherwise."""
    category = await repo.get_category(session, category_id)
    if category is None or category.owner_unit_id not in owner_path:
        raise NotFoundError(MSG_CATEGORY_NOT_FOUND)
    return category.id


async def _resolve_schema_id(session: AsyncSession, owner_path: set[uuid.UUID], schema_id: str | None) -> uuid.UUID | None:
    """A service's schema must be visible AT the service's owner — 404 otherwise."""
    sid = _parse_uuid(schema_id, MSG_SCHEMA_NOT_FOUND)
    if sid is None:
        return None
    schema = await repo.get_schema(session, sid)
    if schema is None or schema.owner_unit_id not in owner_path:
        raise NotFoundError(MSG_SCHEMA_NOT_FOUND)
    return sid


async def _resolve_unit_settings(session: AsyncSession, owner_unit_id: uuid.UUID, unit_prices: dict[str, int], disabled_unit_ids: Iterable[str]) -> dict[uuid.UUID, tuple[bool, int | None]]:
    """`unitPrices` + `disabledUnitIds` → `{unit: (is_enabled, price | None)}`; every unit must lie in the owner's
    subtree (404 "Birlik topilmadi")."""
    wanted: dict[uuid.UUID, tuple[bool, int | None]] = {}
    for raw, price in unit_prices.items():
        wanted[_uuid_or_404(raw, MSG_UNIT_NOT_FOUND)] = (True, int(price))
    for raw in disabled_unit_ids:
        unit_id = _uuid_or_404(raw, MSG_UNIT_NOT_FOUND)
        wanted[unit_id] = (False, wanted.get(unit_id, (True, None))[1])
    if wanted and not set(wanted) <= set(await descendant_ids(session, owner_unit_id)):
        raise NotFoundError(MSG_UNIT_NOT_FOUND)
    return wanted


async def create_service_type(session: AsyncSession, scope: UnitScope, body: ServiceTypeCreateIn, staff: StaffPrincipal, meta: RequestMeta) -> ServiceTypeOut:
    """Create a service type owned by the path unit (defaults: price 0, turnaround 1, order 99, active, scope item);
    code unique per owner; category / schema must be visible at the owner; per-unit rows inside its subtree."""
    owner_unit_id = scope.id
    owner_path = await _owner_path(session, owner_unit_id)
    category_id = await _category_on_path(session, _uuid_or_404(body.category_id, MSG_CATEGORY_NOT_FOUND), owner_path)
    code = (body.code or "").strip() or None
    await _ensure_service_code_free(session, owner_unit_id, code, None)
    schema_id = await _resolve_schema_id(session, owner_path, body.schema_id)
    wanted = await _resolve_unit_settings(session, owner_unit_id, body.unit_prices, body.disabled_unit_ids)
    row = ServiceType(
        owner_unit_id=owner_unit_id,
        category_id=category_id,
        name=body.name,
        code=code,
        description=body.description or None,
        price=body.price,
        turnaround_days=body.turnaround_days,
        sort_order=body.order,
        is_active=body.is_active,
        schema_id=schema_id,
        document_scope=body.document_scope,
        default_template_id=_parse_uuid(body.default_template_id, MSG_TEMPLATE_NOT_FOUND),
        created_by=staff.id,
    )
    session.add(row)
    await session.flush()
    await repo.replace_unit_settings(session, row.id, wanted, staff.id)
    await session.refresh(row, ["created_at", "updated_at"])
    settings = await repo.unit_settings_for(session, [row.id])
    await _audit(session, owner_unit_id=owner_unit_id, staff=staff, meta=meta, action="create", entity="service_type", entity_id=row.id, after=_service_snapshot(row, settings))
    await invalidate_catalog_cache(owner_unit_id)
    return service_type_out(row, settings, 0)


async def update_service_type(session: AsyncSession, service_type_id: uuid.UUID, body: ServiceTypeUpdateIn, staff: StaffPrincipal, meta: RequestMeta) -> ServiceTypeOut:
    """Partial merge; `unitPrices` / `disabledUnitIds` replace the per-unit rows wholesale (a facet left out of
    the payload keeps its current rows); code stays unique per owner."""
    row = await get_service_type_or_404(session, service_type_id, staff)
    current = await repo.unit_settings_for(session, [row.id])
    before = _service_snapshot(row, current)
    owner_path = await _owner_path(session, row.owner_unit_id)
    data = body.model_dump(exclude_unset=True)
    if data.get("category_id"):
        row.category_id = await _category_on_path(session, _uuid_or_404(data["category_id"], MSG_CATEGORY_NOT_FOUND), owner_path)
    if "code" in data:
        code = (data["code"] or "").strip() or None
        if code and code.lower() != (row.code or "").lower():
            await _ensure_service_code_free(session, row.owner_unit_id, code, row.id)
        row.code = code
    if data.get("name"):
        row.name = data["name"]
    if "description" in data:
        row.description = data["description"] or None
    for field in ("price", "turnaround_days", "is_active", "document_scope"):
        if data.get(field) is not None:
            setattr(row, field, data[field])
    if data.get("order") is not None:
        row.sort_order = data["order"]
    if data.get("unit_prices") is not None or data.get("disabled_unit_ids") is not None:
        prices_now, disabled_now = settings_view(current)
        prices = data["unit_prices"] if data.get("unit_prices") is not None else prices_now
        disabled = data["disabled_unit_ids"] if data.get("disabled_unit_ids") is not None else disabled_now
        await repo.replace_unit_settings(session, row.id, await _resolve_unit_settings(session, row.owner_unit_id, prices, disabled), staff.id)
        row.updated_at = utcnow()
    if "schema_id" in data:
        row.schema_id = await _resolve_schema_id(session, owner_path, data["schema_id"])
    if "default_template_id" in data:
        row.default_template_id = _parse_uuid(data["default_template_id"], MSG_TEMPLATE_NOT_FOUND)
    await session.flush()
    await session.refresh(row, ["updated_at"])
    settings = await repo.unit_settings_for(session, [row.id])
    await _audit(session, owner_unit_id=row.owner_unit_id, staff=staff, meta=meta, action="update", entity="service_type", entity_id=row.id, before=before, after=_service_snapshot(row, settings))
    await invalidate_catalog_cache(row.owner_unit_id)
    counts = await repo.ordered_30d_by_service(session, await _stats_units(session, staff, row.owner_unit_id), [row.id])
    return service_type_out(row, settings, counts.get(row.id, 0))


async def delete_service_type(session: AsyncSession, service_type_id: uuid.UUID, staff: StaffPrincipal, meta: RequestMeta) -> None:
    """Soft delete; 409 in_use when any order item (anywhere) references the service."""
    row = await get_service_type_or_404(session, service_type_id, staff)
    if await repo.service_type_has_order_items(session, row.id):
        raise ConflictError("Bu xizmat bo‘yicha buyurtmalar bor — o‘chirish o‘rniga nofaol qiling", code="in_use")
    settings = await repo.unit_settings_for(session, [row.id])
    row.deleted_at = utcnow()
    row.deleted_by = staff.id
    await session.flush()
    await _audit(session, owner_unit_id=row.owner_unit_id, staff=staff, meta=meta, action="delete", entity="service_type", entity_id=row.id, before=_service_snapshot(row, settings))
    await invalidate_catalog_cache(row.owner_unit_id)


# ----------------------------------------------------------------------------- attribute schemas


def _fields_payload(fields: list[FieldDefIn]) -> list[dict[str, Any]]:
    """Validated field defs → JSON as sent (extras preserved); keys must be unique (422)."""
    seen: set[str] = set()
    for f in fields:
        if f.key in seen:
            raise ValidationError(f"Maydon kaliti takrorlangan: {f.key}")
        seen.add(f.key)
    return [f.model_dump(mode="json") for f in fields]


async def list_schemas(session: AsyncSession, scope: UnitScope) -> list[AttributeSchemaOut]:
    """Schemas managed from the unit (inherited + owned below) with `usedBy` (one grouped query)."""
    rows = await repo.list_schemas(session, scope.lineage_ids)
    usage = await repo.schema_usage(session, [r.id for r in rows])
    return [schema_out(r, usage.get(r.id, 0)) for r in rows]


async def get_schema_dto(session: AsyncSession, schema_id: uuid.UUID, staff: StaffPrincipal) -> AttributeSchemaOut:
    """Single schema (visible owner; superadmin any) with `usedBy`."""
    row = await get_schema_or_404(session, schema_id, staff)
    usage = await repo.schema_usage(session, [row.id])
    return schema_out(row, usage.get(row.id, 0))


async def create_schema(session: AsyncSession, scope: UnitScope, body: SchemaCreateIn, staff: StaffPrincipal, meta: RequestMeta) -> AttributeSchemaOut:
    """Create a schema owned by the path unit: version 1, status draft, fields as given (default [])."""
    owner_unit_id = scope.id
    row = AttributeSchema(
        owner_unit_id=owner_unit_id,
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
    await _audit(session, owner_unit_id=owner_unit_id, staff=staff, meta=meta, action="create", entity="attribute_schema", entity_id=row.id, after=_schema_snapshot(row))
    await invalidate_catalog_cache(owner_unit_id)
    return schema_out(row, 0)


async def update_schema(session: AsyncSession, schema_id: uuid.UUID, body: SchemaUpdateIn, staff: StaffPrincipal, meta: RequestMeta) -> AttributeSchemaOut:
    """Partial merge; `version += 1` iff the schema is published AND the payload contains `fields`; status unchanged."""
    row = await get_schema_or_404(session, schema_id, staff)
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
    await _audit(session, owner_unit_id=row.owner_unit_id, staff=staff, meta=meta, action="update", entity="attribute_schema", entity_id=row.id, before=before, after=_schema_snapshot(row))
    await invalidate_catalog_cache(row.owner_unit_id)
    usage = await repo.schema_usage(session, [row.id])
    return schema_out(row, usage.get(row.id, 0))


async def publish_schema(session: AsyncSession, schema_id: uuid.UUID, staff: StaffPrincipal, meta: RequestMeta) -> AttributeSchemaOut:
    """Set status published (no version bump); 422 empty when the schema has no fields."""
    row = await get_schema_or_404(session, schema_id, staff)
    if not row.fields:
        raise ValidationError("Kamida bitta maydon kerak", code="empty")
    before = _schema_snapshot(row)
    row.status = "published"
    await session.flush()
    await session.refresh(row, ["updated_at"])
    await _audit(session, owner_unit_id=row.owner_unit_id, staff=staff, meta=meta, action="publish", entity="attribute_schema", entity_id=row.id, before=before, after=_schema_snapshot(row))
    await invalidate_catalog_cache(row.owner_unit_id)
    usage = await repo.schema_usage(session, [row.id])
    return schema_out(row, usage.get(row.id, 0))
