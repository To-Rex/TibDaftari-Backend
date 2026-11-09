"""Catalog SQL helpers: categories, service types (+ per-unit settings), attribute schemas. No rules, no HTTP.

Lists take the set of owner units to show (`scope.lineage_ids` for management pages, `ancestor_ids` of a
facility for what is usable there); id lookups take no tenant filter — the service checks visibility.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timeutil import utcnow
from app.infrastructure.db.base import alive
from app.infrastructure.db.models import AttributeSchema, Category, OrderItem, ServiceType, ServiceUnitSetting


def _ids(values: Iterable[uuid.UUID]) -> list[uuid.UUID]:
    return list(dict.fromkeys(values))


# ----------------------------------------------------------------------------- categories


async def list_categories(session: AsyncSession, owner_unit_ids: Sequence[uuid.UUID]) -> Sequence[Category]:
    """Alive categories (active + inactive) owned by any of the units, sort order asc then name."""
    if not owner_unit_ids:
        return []
    stmt = select(Category).where(Category.owner_unit_id.in_(owner_unit_ids), alive(Category)).order_by(Category.sort_order.asc(), Category.name.asc())
    return (await session.execute(stmt)).scalars().all()


async def get_category(session: AsyncSession, category_id: uuid.UUID) -> Category | None:
    stmt = select(Category).where(Category.id == category_id, alive(Category))
    return (await session.execute(stmt)).scalar_one_or_none()


async def sibling_count(session: AsyncSession, owner_unit_ids: Sequence[uuid.UUID], parent_id: uuid.UUID | None) -> int:
    """Alive categories visible from the given owners that share `parent_id` (for the default sort order)."""
    if not owner_unit_ids:
        return 0
    parent_clause = Category.parent_id.is_(None) if parent_id is None else Category.parent_id == parent_id
    stmt = select(func.count()).where(Category.owner_unit_id.in_(owner_unit_ids), alive(Category), parent_clause)
    return int((await session.execute(stmt)).scalar_one())


async def descendant_category_ids(session: AsyncSession, root_id: uuid.UUID) -> set[uuid.UUID]:
    """`root_id` plus every transitive alive descendant, whoever owns them (recursive CTE, one round trip)."""
    base = select(Category.id).where(Category.id == root_id, alive(Category)).cte("tree", recursive=True)
    child = select(Category.id).join(base, Category.parent_id == base.c.id).where(alive(Category))
    tree = base.union(child)
    rows = (await session.execute(select(tree.c.id))).scalars().all()
    return set(rows)


async def has_child_categories(session: AsyncSession, category_id: uuid.UUID) -> bool:
    stmt = select(Category.id).where(Category.parent_id == category_id, alive(Category)).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def category_has_services(session: AsyncSession, category_id: uuid.UUID) -> bool:
    stmt = select(ServiceType.id).where(ServiceType.category_id == category_id, alive(ServiceType)).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none() is not None


# ----------------------------------------------------------------------------- service types


async def list_service_types(session: AsyncSession, owner_unit_ids: Sequence[uuid.UUID]) -> Sequence[ServiceType]:
    """Alive service types owned by any of the units, sort order asc then name (filters are applied in Python
    on the cached list)."""
    if not owner_unit_ids:
        return []
    stmt = select(ServiceType).where(ServiceType.owner_unit_id.in_(owner_unit_ids), alive(ServiceType)).order_by(ServiceType.sort_order.asc(), ServiceType.name.asc())
    return (await session.execute(stmt)).scalars().all()


async def get_service_type(session: AsyncSession, service_type_id: uuid.UUID) -> ServiceType | None:
    stmt = select(ServiceType).where(ServiceType.id == service_type_id, alive(ServiceType))
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_service_types_by_ids(session: AsyncSession, ids: Iterable[uuid.UUID]) -> Sequence[ServiceType]:
    id_list = _ids(ids)
    if not id_list:
        return []
    stmt = select(ServiceType).where(ServiceType.id.in_(id_list), alive(ServiceType))
    return (await session.execute(stmt)).scalars().all()


async def service_code_taken(session: AsyncSession, owner_unit_id: uuid.UUID, code: str, exclude_id: uuid.UUID | None = None) -> bool:
    """Case-insensitive uniqueness of `code` among the owner's alive service types (`uq_service_types_owner_code_alive`)."""
    stmt = select(func.count()).where(ServiceType.owner_unit_id == owner_unit_id, alive(ServiceType), func.lower(ServiceType.code) == code.lower())
    if exclude_id is not None:
        stmt = stmt.where(ServiceType.id != exclude_id)
    return int((await session.execute(stmt)).scalar_one()) > 0


async def service_type_has_order_items(session: AsyncSession, service_type_id: uuid.UUID) -> bool:
    stmt = select(OrderItem.id).where(OrderItem.service_type_id == service_type_id, alive(OrderItem)).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def ordered_30d_by_service(session: AsyncSession, unit_ids: Sequence[uuid.UUID], service_type_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, int]:
    """`{serviceTypeId: count}` of alive order items of the units created in the last 30 days — one grouped query."""
    id_list = _ids(service_type_ids)
    if not id_list or not unit_ids:
        return {}
    since = utcnow() - timedelta(days=30)
    stmt = (
        select(OrderItem.service_type_id, func.count())
        .where(OrderItem.unit_id.in_(unit_ids), OrderItem.service_type_id.in_(id_list), OrderItem.created_at >= since, alive(OrderItem))
        .group_by(OrderItem.service_type_id)
    )
    return {row[0]: int(row[1]) for row in (await session.execute(stmt)).all()}


# ----------------------------------------------------------------------------- per-unit settings


async def unit_settings_for(session: AsyncSession, service_type_ids: Iterable[uuid.UUID], unit_ids: Sequence[uuid.UUID] | None = None) -> Sequence[ServiceUnitSetting]:
    """`service_unit_settings` rows of the services (optionally only those set on `unit_ids`)."""
    id_list = _ids(service_type_ids)
    if not id_list or (unit_ids is not None and not unit_ids):
        return []
    stmt = select(ServiceUnitSetting).where(ServiceUnitSetting.service_type_id.in_(id_list))
    if unit_ids is not None:
        stmt = stmt.where(ServiceUnitSetting.unit_id.in_(unit_ids))
    return (await session.execute(stmt)).scalars().all()


async def replace_unit_settings(session: AsyncSession, service_type_id: uuid.UUID, wanted: dict[uuid.UUID, tuple[bool, int | None]], updated_by: uuid.UUID | None) -> None:
    """Make the service's per-unit rows exactly `wanted` = {unit: (is_enabled, price | None)}: rows for units not
    listed are deleted, listed ones are inserted or updated in place."""
    existing = {r.unit_id: r for r in await unit_settings_for(session, [service_type_id])}
    for unit_id, row in existing.items():
        if unit_id not in wanted:
            await session.delete(row)
    now = utcnow()
    for unit_id, (is_enabled, price) in wanted.items():
        row = existing.get(unit_id)
        if row is None:
            session.add(ServiceUnitSetting(service_type_id=service_type_id, unit_id=unit_id, is_enabled=is_enabled, price=price, updated_at=now, updated_by=updated_by))
        elif row.is_enabled != is_enabled or row.price != price:
            row.is_enabled = is_enabled
            row.price = price
            row.updated_at = now
            row.updated_by = updated_by
    await session.flush()


# ----------------------------------------------------------------------------- attribute schemas


async def list_schemas(session: AsyncSession, owner_unit_ids: Sequence[uuid.UUID]) -> Sequence[AttributeSchema]:
    if not owner_unit_ids:
        return []
    stmt = select(AttributeSchema).where(AttributeSchema.owner_unit_id.in_(owner_unit_ids), alive(AttributeSchema)).order_by(AttributeSchema.name.asc(), AttributeSchema.id.asc())
    return (await session.execute(stmt)).scalars().all()


async def get_schema(session: AsyncSession, schema_id: uuid.UUID) -> AttributeSchema | None:
    stmt = select(AttributeSchema).where(AttributeSchema.id == schema_id, alive(AttributeSchema))
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_schemas_by_ids(session: AsyncSession, ids: Iterable[uuid.UUID]) -> Sequence[AttributeSchema]:
    id_list = _ids(ids)
    if not id_list:
        return []
    stmt = select(AttributeSchema).where(AttributeSchema.id.in_(id_list), alive(AttributeSchema))
    return (await session.execute(stmt)).scalars().all()


async def schema_usage(session: AsyncSession, schema_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, int]:
    """`{schemaId: number of alive service types using it}` (whoever owns them) — one grouped query."""
    id_list = _ids(schema_ids)
    if not id_list:
        return {}
    stmt = select(ServiceType.schema_id, func.count()).where(ServiceType.schema_id.in_(id_list), alive(ServiceType)).group_by(ServiceType.schema_id)
    return {row[0]: int(row[1]) for row in (await session.execute(stmt)).all()}
