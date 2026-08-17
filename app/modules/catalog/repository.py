"""Catalog SQL helpers: categories, service types, attribute schemas. No rules, no HTTP."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timeutil import utcnow
from app.infrastructure.db.base import alive
from app.infrastructure.db.models import AttributeSchema, Category, OrderItem, ServiceType

# ----------------------------------------------------------------------------- categories


async def list_categories(session: AsyncSession, company_id: uuid.UUID) -> Sequence[Category]:
    """All alive categories of a company (active + inactive), order asc then name."""
    stmt = select(Category).where(Category.company_id == company_id, alive(Category)).order_by(Category.order.asc(), Category.name.asc())
    return (await session.execute(stmt)).scalars().all()


async def get_category(session: AsyncSession, category_id: uuid.UUID, company_id: uuid.UUID | None) -> Category | None:
    stmt = select(Category).where(Category.id == category_id, alive(Category))
    if company_id is not None:
        stmt = stmt.where(Category.company_id == company_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def sibling_count(session: AsyncSession, company_id: uuid.UUID, parent_id: uuid.UUID | None) -> int:
    parent_clause = Category.parent_id.is_(None) if parent_id is None else Category.parent_id == parent_id
    stmt = select(func.count()).where(Category.company_id == company_id, alive(Category), parent_clause)
    return int((await session.execute(stmt)).scalar_one())


async def descendant_category_ids(session: AsyncSession, company_id: uuid.UUID, root_id: uuid.UUID) -> set[uuid.UUID]:
    """`root_id` plus every transitive alive descendant (recursive CTE, one round trip)."""
    base = select(Category.id).where(Category.id == root_id, Category.company_id == company_id, alive(Category)).cte("tree", recursive=True)
    child = select(Category.id).join(base, Category.parent_id == base.c.id).where(Category.company_id == company_id, alive(Category))
    tree = base.union(child)
    rows = (await session.execute(select(tree.c.id))).scalars().all()
    return set(rows)


async def has_child_categories(session: AsyncSession, company_id: uuid.UUID, category_id: uuid.UUID) -> bool:
    stmt = select(Category.id).where(Category.company_id == company_id, Category.parent_id == category_id, alive(Category)).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def category_has_services(session: AsyncSession, company_id: uuid.UUID, category_id: uuid.UUID) -> bool:
    stmt = select(ServiceType.id).where(ServiceType.company_id == company_id, ServiceType.category_id == category_id, alive(ServiceType)).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none() is not None


# ----------------------------------------------------------------------------- service types


async def list_service_types(session: AsyncSession, company_id: uuid.UUID) -> Sequence[ServiceType]:
    """All alive service types of a company, order asc then name (filters are applied in Python on the cached list)."""
    stmt = select(ServiceType).where(ServiceType.company_id == company_id, alive(ServiceType)).order_by(ServiceType.order.asc(), ServiceType.name.asc())
    return (await session.execute(stmt)).scalars().all()


async def get_service_type(session: AsyncSession, service_type_id: uuid.UUID, company_id: uuid.UUID | None) -> ServiceType | None:
    stmt = select(ServiceType).where(ServiceType.id == service_type_id, alive(ServiceType))
    if company_id is not None:
        stmt = stmt.where(ServiceType.company_id == company_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_service_types_by_ids(session: AsyncSession, company_id: uuid.UUID, ids: Iterable[uuid.UUID]) -> Sequence[ServiceType]:
    id_list = list({i for i in ids})
    if not id_list:
        return []
    stmt = select(ServiceType).where(ServiceType.company_id == company_id, ServiceType.id.in_(id_list), alive(ServiceType))
    return (await session.execute(stmt)).scalars().all()


async def service_code_taken(session: AsyncSession, company_id: uuid.UUID, code: str, exclude_id: uuid.UUID | None = None) -> bool:
    """Case-insensitive uniqueness of `code` among alive service types of the company."""
    stmt = select(func.count()).where(ServiceType.company_id == company_id, alive(ServiceType), func.lower(ServiceType.code) == code.lower())
    if exclude_id is not None:
        stmt = stmt.where(ServiceType.id != exclude_id)
    return int((await session.execute(stmt)).scalar_one()) > 0


async def service_type_has_order_items(session: AsyncSession, company_id: uuid.UUID, service_type_id: uuid.UUID) -> bool:
    stmt = select(OrderItem.id).where(OrderItem.company_id == company_id, OrderItem.service_type_id == service_type_id, alive(OrderItem)).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def ordered_30d_by_service(session: AsyncSession, company_id: uuid.UUID, service_type_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, int]:
    """`{serviceTypeId: count}` of alive order items created in the last 30 days — one grouped query."""
    id_list = list(service_type_ids)
    if not id_list:
        return {}
    since = utcnow() - timedelta(days=30)
    stmt = (
        select(OrderItem.service_type_id, func.count())
        .where(OrderItem.company_id == company_id, OrderItem.service_type_id.in_(id_list), OrderItem.created_at >= since, alive(OrderItem))
        .group_by(OrderItem.service_type_id)
    )
    return {row[0]: int(row[1]) for row in (await session.execute(stmt)).all()}


# ----------------------------------------------------------------------------- attribute schemas


async def list_schemas(session: AsyncSession, company_id: uuid.UUID) -> Sequence[AttributeSchema]:
    stmt = select(AttributeSchema).where(AttributeSchema.company_id == company_id, alive(AttributeSchema)).order_by(AttributeSchema.name.asc(), AttributeSchema.id.asc())
    return (await session.execute(stmt)).scalars().all()


async def get_schema(session: AsyncSession, schema_id: uuid.UUID, company_id: uuid.UUID | None) -> AttributeSchema | None:
    stmt = select(AttributeSchema).where(AttributeSchema.id == schema_id, alive(AttributeSchema))
    if company_id is not None:
        stmt = stmt.where(AttributeSchema.company_id == company_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_schemas_by_ids(session: AsyncSession, ids: Iterable[uuid.UUID]) -> Sequence[AttributeSchema]:
    id_list = list({i for i in ids})
    if not id_list:
        return []
    stmt = select(AttributeSchema).where(AttributeSchema.id.in_(id_list), alive(AttributeSchema))
    return (await session.execute(stmt)).scalars().all()


async def schema_usage(session: AsyncSession, company_id: uuid.UUID, schema_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, int]:
    """`{schemaId: number of alive service types using it}` — one grouped query."""
    id_list = list(schema_ids)
    if not id_list:
        return {}
    stmt = (
        select(ServiceType.schema_id, func.count())
        .where(ServiceType.company_id == company_id, ServiceType.schema_id.in_(id_list), alive(ServiceType))
        .group_by(ServiceType.schema_id)
    )
    return {row[0]: int(row[1]) for row in (await session.execute(stmt)).all()}
