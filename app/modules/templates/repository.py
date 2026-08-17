"""Templates SQL helpers: result templates, template assets and the small catalog/tenant look-ups the
preview needs (service types, schemas, branch, category). No rules, no HTTP."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.base import alive
from app.infrastructure.db.models import AttributeSchema, Branch, Category, ResultTemplate, ServiceType, TemplateAsset

# ----------------------------------------------------------------------------- templates


async def list_templates(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    status: str | None = None,
    service_type_id: uuid.UUID | None = None,
) -> Sequence[ResultTemplate]:
    """Alive templates of a company, updated_at desc; `service_type_id` → bound to it OR generic (empty)."""
    stmt = select(ResultTemplate).where(ResultTemplate.company_id == company_id, alive(ResultTemplate))
    if status:
        stmt = stmt.where(ResultTemplate.status == status)
    if service_type_id is not None:
        stmt = stmt.where(
            ResultTemplate.service_type_ids.any(service_type_id) | (func.cardinality(ResultTemplate.service_type_ids) == 0)
        )
    stmt = stmt.order_by(ResultTemplate.updated_at.desc(), ResultTemplate.id.desc())
    return (await session.execute(stmt)).scalars().all()


async def get_template(session: AsyncSession, template_id: uuid.UUID, company_id: uuid.UUID | None) -> ResultTemplate | None:
    stmt = select(ResultTemplate).where(ResultTemplate.id == template_id, alive(ResultTemplate))
    if company_id is not None:
        stmt = stmt.where(ResultTemplate.company_id == company_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def increment_usage(session: AsyncSession, template_id: uuid.UUID, n: int) -> None:
    await session.execute(update(ResultTemplate).where(ResultTemplate.id == template_id).values(usage=ResultTemplate.usage + n))


# ----------------------------------------------------------------------------- assets


async def list_assets(session: AsyncSession, company_id: uuid.UUID) -> Sequence[TemplateAsset]:
    stmt = select(TemplateAsset).where(TemplateAsset.company_id == company_id, alive(TemplateAsset)).order_by(TemplateAsset.created_at.asc())
    return (await session.execute(stmt)).scalars().all()


async def get_assets_by_ids(session: AsyncSession, company_id: uuid.UUID, ids: Iterable[uuid.UUID]) -> Sequence[TemplateAsset]:
    ids = list(ids)
    if not ids:
        return []
    stmt = select(TemplateAsset).where(TemplateAsset.company_id == company_id, TemplateAsset.id.in_(ids), alive(TemplateAsset))
    return (await session.execute(stmt)).scalars().all()


# ----------------------------------------------------------------------------- preview look-ups


async def get_service_types(session: AsyncSession, company_id: uuid.UUID, ids: Iterable[uuid.UUID]) -> Sequence[ServiceType]:
    ids = list(ids)
    if not ids:
        return []
    stmt = select(ServiceType).where(ServiceType.company_id == company_id, ServiceType.id.in_(ids), alive(ServiceType))
    return (await session.execute(stmt)).scalars().all()


async def get_schemas(session: AsyncSession, company_id: uuid.UUID, ids: Iterable[uuid.UUID]) -> Sequence[AttributeSchema]:
    ids = list(ids)
    if not ids:
        return []
    stmt = select(AttributeSchema).where(AttributeSchema.company_id == company_id, AttributeSchema.id.in_(ids), alive(AttributeSchema))
    return (await session.execute(stmt)).scalars().all()


async def first_branch(session: AsyncSession, company_id: uuid.UUID) -> Branch | None:
    stmt = select(Branch).where(Branch.company_id == company_id, alive(Branch)).order_by(Branch.created_at.asc()).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_category(session: AsyncSession, company_id: uuid.UUID, category_id: uuid.UUID) -> Category | None:
    stmt = select(Category).where(Category.company_id == company_id, Category.id == category_id, alive(Category))
    return (await session.execute(stmt)).scalar_one_or_none()
