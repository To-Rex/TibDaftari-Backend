"""Unit scope — the selected organisation unit and the id sets every scoped query needs.

    scope = await load_scope(session, unit_id)
    select(Order).where(Order.unit_id.in_(scope.descendant_ids))          # operational rows under the unit
    select(ServiceType).where(ServiceType.owner_unit_id.in_(scope.ancestor_ids))   # catalogue usable AT the unit
    select(ServiceType).where(ServiceType.owner_unit_id.in_(scope.lineage_ids))    # catalogue managed FROM the unit

The id sets come from `org_unit_closure` (kept by DB triggers) and are cached in Redis for a few minutes;
every org-tree write calls `invalidate_scope_cache()`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.base import alive
from app.infrastructure.db.models import OrgUnit, OrgUnitClosure
from app.infrastructure.db.snapshot import row_restore, row_snapshot
from app.infrastructure.redis import cache

SCOPE_TTL = 300
SCOPE_KEY = "unit:{unit_id}:scope"


@dataclass(slots=True)
class UnitScope:
    unit: OrgUnit
    #: the unit and everything under it (deleted units included — their history still belongs here)
    descendant_ids: list[uuid.UUID]
    #: root → … → the unit itself
    ancestor_ids: list[uuid.UUID]
    #: alive facilities under the unit (the unit itself when it is one) — where cheques can be written
    facility_ids: list[uuid.UUID]
    _desc_set: frozenset[uuid.UUID] = field(default_factory=frozenset, repr=False)

    def __post_init__(self) -> None:
        self._desc_set = frozenset(self.descendant_ids)

    @property
    def id(self) -> uuid.UUID:
        return self.unit.id

    @property
    def country_id(self) -> uuid.UUID:
        return self.unit.country_id

    @property
    def is_facility(self) -> bool:
        return self.unit.level == "facility"

    @property
    def lineage_ids(self) -> list[uuid.UUID]:
        """Ancestors (without self) + the subtree: the catalogue an administrator of this unit manages."""
        return [*self.ancestor_ids[:-1], *self.descendant_ids]

    def contains(self, unit_id: uuid.UUID | None) -> bool:
        return unit_id is not None and unit_id in self._desc_set

    def narrow(self, sub_unit_ids: list[uuid.UUID] | None) -> list[uuid.UUID]:
        """Descendants restricted to the given sub-units (a `unitId` list filter); unknown ids match nothing."""
        if not sub_unit_ids:
            return self.descendant_ids
        return [u for u in sub_unit_ids if u in self._desc_set] or [uuid.UUID(int=0)]


async def narrow_to(session: AsyncSession, scope: UnitScope, sub_unit_ids: list[uuid.UUID] | None) -> list[uuid.UUID]:
    """The scope narrowed to the SUBTREES of the given sub-units (a `unitId` filter on a list): a district id
    covers its facilities. Ids outside the scope match nothing; no ids → the whole scope."""
    if not sub_unit_ids:
        return scope.descendant_ids
    roots = [u for u in sub_unit_ids if scope.contains(u)]
    if not roots:
        return [uuid.UUID(int=0)]
    return await descendant_ids(session, roots)


async def descendant_ids(session: AsyncSession, unit_ids: list[uuid.UUID] | uuid.UUID) -> list[uuid.UUID]:
    roots = unit_ids if isinstance(unit_ids, list) else [unit_ids]
    if not roots:
        return []
    rows = (await session.execute(select(OrgUnitClosure.descendant_id).where(OrgUnitClosure.ancestor_id.in_(roots)).distinct())).scalars().all()
    return list(rows)


async def ancestor_ids(session: AsyncSession, unit_id: uuid.UUID) -> list[uuid.UUID]:
    """Root first, the unit itself last."""
    rows = (await session.execute(select(OrgUnitClosure.ancestor_id).where(OrgUnitClosure.descendant_id == unit_id).order_by(OrgUnitClosure.depth.desc()))).scalars().all()
    return list(rows)


async def facility_ids_under(session: AsyncSession, unit_id: uuid.UUID) -> list[uuid.UUID]:
    stmt = (
        select(OrgUnit.id)
        .join(OrgUnitClosure, OrgUnitClosure.descendant_id == OrgUnit.id)
        .where(OrgUnitClosure.ancestor_id == unit_id, OrgUnit.level == "facility", alive(OrgUnit))
        .order_by(OrgUnit.sort_order, OrgUnit.name)
    )
    return list((await session.execute(stmt)).scalars().all())


async def load_scope(session: AsyncSession, unit_id: uuid.UUID | str) -> UnitScope | None:
    """The scope of an alive unit (None when it does not exist / is deleted). Cached."""
    try:
        uid = uuid.UUID(str(unit_id))
    except ValueError:
        return None
    key = SCOPE_KEY.format(unit_id=uid)
    snap = await cache.get_json(key)
    if isinstance(snap, dict) and snap.get("unit"):
        return UnitScope(
            unit=row_restore(OrgUnit, snap["unit"]),
            descendant_ids=[uuid.UUID(x) for x in snap["desc"]],
            ancestor_ids=[uuid.UUID(x) for x in snap["anc"]],
            facility_ids=[uuid.UUID(x) for x in snap["fac"]],
        )
    unit = (await session.execute(select(OrgUnit).where(OrgUnit.id == uid, alive(OrgUnit)))).scalar_one_or_none()
    if unit is None:
        return None
    desc = await descendant_ids(session, uid)
    anc = await ancestor_ids(session, uid)
    fac = await facility_ids_under(session, uid)
    await cache.set_json(key, {"unit": row_snapshot(unit), "desc": [str(x) for x in desc], "anc": [str(x) for x in anc], "fac": [str(x) for x in fac]}, SCOPE_TTL)
    return UnitScope(unit=unit, descendant_ids=desc, ancestor_ids=anc, facility_ids=fac)


async def invalidate_scope_cache(unit_id: uuid.UUID | str | None = None) -> None:
    """Org-tree writes: a single unit's cached scope, or (default) every unit's — subtree membership may have changed."""
    if unit_id:
        await cache.delete(SCOPE_KEY.format(unit_id=unit_id))
    else:
        await cache.delete_prefix("unit:")


def ids_of(values: Any) -> list[uuid.UUID]:
    """Lenient uuid list parser for query filters (malformed values are dropped)."""
    out: list[uuid.UUID] = []
    for v in values or []:
        try:
            out.append(uuid.UUID(str(v)))
        except ValueError:
            continue
    return out
