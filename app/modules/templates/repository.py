"""Templates SQL helpers: result templates (+ their `template_units` bindings), template assets and the small
catalogue look-ups the preview needs (service types, schemas, category). No rules, no HTTP.

Ownership: `owner_unit_id` (any level) — callers pass the owner id sets they may see (`scope.lineage_ids` for
management, the unit's path for use). "Usable at a unit" = owner on the unit's root → unit path AND either no
`template_units` rows or one whose unit lies on that path.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.base import alive
from app.infrastructure.db.models import (
    AttributeSchema,
    Category,
    ResultTemplate,
    ServiceType,
    TemplateAsset,
    TemplateUnit,
)

# ----------------------------------------------------------------------------- templates


def _bindings_allow(unit_ids: Sequence[uuid.UUID]):
    """SQL: the template has no unit bindings, or one of them is in `unit_ids`."""
    bound = select(TemplateUnit.template_id).where(TemplateUnit.template_id == ResultTemplate.id)
    return ~bound.exists() | bound.where(TemplateUnit.unit_id.in_(unit_ids)).exists()


async def list_templates(
    session: AsyncSession,
    owner_ids: Sequence[uuid.UUID],
    *,
    status: str | None = None,
    service_type_id: uuid.UUID | None = None,
    path_ids: Sequence[uuid.UUID] | None = None,
) -> Sequence[ResultTemplate]:
    """Alive templates owned by one of `owner_ids`, updated_at desc; `service_type_id` → bound to it OR generic
    (empty); `path_ids` (root → unit) → only those USABLE at that unit (owner on the path, bindings allow it)."""
    if not owner_ids:
        return []
    stmt = select(ResultTemplate).where(ResultTemplate.owner_unit_id.in_(owner_ids), alive(ResultTemplate))
    if status:
        stmt = stmt.where(ResultTemplate.status == status)
    if service_type_id is not None:
        stmt = stmt.where(
            ResultTemplate.service_type_ids.any(service_type_id) | (func.cardinality(ResultTemplate.service_type_ids) == 0)
        )
    if path_ids is not None:
        if not path_ids:
            return []
        stmt = stmt.where(ResultTemplate.owner_unit_id.in_(path_ids), _bindings_allow(path_ids))
    stmt = stmt.order_by(ResultTemplate.updated_at.desc(), ResultTemplate.id.desc())
    return (await session.execute(stmt)).scalars().all()


async def get_template(session: AsyncSession, template_id: uuid.UUID) -> ResultTemplate | None:
    """Alive template by id (visibility is the service's job)."""
    stmt = select(ResultTemplate).where(ResultTemplate.id == template_id, alive(ResultTemplate))
    return (await session.execute(stmt)).scalar_one_or_none()


async def template_unit_ids(session: AsyncSession, template_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, list[uuid.UUID]]:
    """template → its `template_units` bindings (templates without rows are absent)."""
    ids = list(template_ids)
    if not ids:
        return {}
    stmt = select(TemplateUnit.template_id, TemplateUnit.unit_id).where(TemplateUnit.template_id.in_(ids)).order_by(TemplateUnit.unit_id)
    out: dict[uuid.UUID, list[uuid.UUID]] = {}
    for tid, uid in (await session.execute(stmt)).all():
        out.setdefault(tid, []).append(uid)
    return out


async def set_template_units(session: AsyncSession, template_id: uuid.UUID, unit_ids: Sequence[uuid.UUID]) -> None:
    """Replace a template's bindings (empty = the owner's whole subtree)."""
    await session.execute(delete(TemplateUnit).where(TemplateUnit.template_id == template_id))
    if unit_ids:
        await session.execute(insert(TemplateUnit).values([{"template_id": template_id, "unit_id": u} for u in unit_ids]))


async def receipt_template_exists(session: AsyncSession, path_ids: Sequence[uuid.UUID], lineage_ids: Sequence[uuid.UUID]) -> bool:
    """Whether an alive receipt template (any status) is usable somewhere in a unit's subtree: owned on the unit's
    root → unit path (`path_ids`) and bound to nothing or to a unit of its ancestors ∪ subtree (`lineage_ids`)."""
    if not path_ids:
        return False
    stmt = (
        select(func.count())
        .select_from(ResultTemplate)
        .where(ResultTemplate.owner_unit_id.in_(path_ids), alive(ResultTemplate), ResultTemplate.scope == "receipt", _bindings_allow(lineage_ids))
    )
    return bool((await session.execute(stmt)).scalar_one())


async def increment_usage(session: AsyncSession, template_id: uuid.UUID, n: int) -> None:
    await session.execute(update(ResultTemplate).where(ResultTemplate.id == template_id).values(usage=ResultTemplate.usage + n))


# ----------------------------------------------------------------------------- assets


async def list_assets(session: AsyncSession, owner_ids: Sequence[uuid.UUID]) -> Sequence[TemplateAsset]:
    if not owner_ids:
        return []
    stmt = select(TemplateAsset).where(TemplateAsset.owner_unit_id.in_(owner_ids), alive(TemplateAsset)).order_by(TemplateAsset.created_at.asc())
    return (await session.execute(stmt)).scalars().all()


async def get_asset(session: AsyncSession, asset_id: uuid.UUID) -> TemplateAsset | None:
    stmt = select(TemplateAsset).where(TemplateAsset.id == asset_id, alive(TemplateAsset))
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_assets_by_ids(session: AsyncSession, ids: Iterable[uuid.UUID]) -> Sequence[TemplateAsset]:
    """Alive assets by id — the ones a template doc references (chosen from the editor's visible list)."""
    ids = list(ids)
    if not ids:
        return []
    stmt = select(TemplateAsset).where(TemplateAsset.id.in_(ids), alive(TemplateAsset))
    return (await session.execute(stmt)).scalars().all()


# ----------------------------------------------------------------------------- preview look-ups


async def get_service_types(session: AsyncSession, ids: Iterable[uuid.UUID]) -> Sequence[ServiceType]:
    ids = list(ids)
    if not ids:
        return []
    stmt = select(ServiceType).where(ServiceType.id.in_(ids), alive(ServiceType))
    return (await session.execute(stmt)).scalars().all()


async def get_schemas(session: AsyncSession, ids: Iterable[uuid.UUID]) -> Sequence[AttributeSchema]:
    ids = list(ids)
    if not ids:
        return []
    stmt = select(AttributeSchema).where(AttributeSchema.id.in_(ids), alive(AttributeSchema))
    return (await session.execute(stmt)).scalars().all()


async def get_category(session: AsyncSession, category_id: uuid.UUID) -> Category | None:
    stmt = select(Category).where(Category.id == category_id, alive(Category))
    return (await session.execute(stmt)).scalar_one_or_none()
