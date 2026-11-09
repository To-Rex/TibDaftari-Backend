"""Organisation-tree SQL helpers: units, closure-based counts/paths, inherited SMS accounts and texts. No rules, no HTTP."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.base import alive
from app.infrastructure.db.models import (
    Country,
    District,
    Employee,
    MessagingAccount,
    OrgUnit,
    OrgUnitClosure,
    Region,
    SmsTemplate,
)


async def get_unit(session: AsyncSession, unit_id: uuid.UUID) -> OrgUnit | None:
    return (await session.execute(select(OrgUnit).where(OrgUnit.id == unit_id, alive(OrgUnit)))).scalar_one_or_none()


async def get_unit_any(session: AsyncSession, unit_id: uuid.UUID) -> OrgUnit | None:
    """Deleted units included (history of a closed facility still needs its name)."""
    return await session.get(OrgUnit, unit_id)


async def units_by_ids(session: AsyncSession, ids: Iterable[uuid.UUID | None]) -> dict[uuid.UUID, OrgUnit]:
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    rows = (await session.execute(select(OrgUnit).where(OrgUnit.id.in_(wanted)))).scalars().all()
    return {u.id: u for u in rows}


async def list_units(session: AsyncSession, ids: Sequence[uuid.UUID] | None = None, *, country_id: uuid.UUID | None = None, include_inactive: bool = True) -> list[OrgUnit]:
    """Alive units — all of them, or only `ids` — in tree order (sort_order, name)."""
    stmt = select(OrgUnit).where(alive(OrgUnit))
    if ids is not None:
        if not ids:
            return []
        stmt = stmt.where(OrgUnit.id.in_(ids))
    if country_id is not None:
        stmt = stmt.where(OrgUnit.country_id == country_id)
    if not include_inactive:
        stmt = stmt.where(OrgUnit.is_active.is_(True))
    rows = (await session.execute(stmt.order_by(OrgUnit.sort_order.asc(), OrgUnit.name.asc(), OrgUnit.id.asc()))).scalars().all()
    return list(rows)


async def children(session: AsyncSession, parent_id: uuid.UUID) -> list[OrgUnit]:
    stmt = select(OrgUnit).where(OrgUnit.parent_id == parent_id, alive(OrgUnit)).order_by(OrgUnit.sort_order.asc(), OrgUnit.name.asc())
    return list((await session.execute(stmt)).scalars().all())


async def depths(session: AsyncSession, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
    """unit → depth in the tree (0 = country node)."""
    if not ids:
        return {}
    stmt = select(OrgUnitClosure.descendant_id, func.max(OrgUnitClosure.depth)).where(OrgUnitClosure.descendant_id.in_(ids)).group_by(OrgUnitClosure.descendant_id)
    return {d: int(n) for d, n in (await session.execute(stmt)).all()}


async def paths(session: AsyncSession, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, str]:
    """unit → "Country › Region › District › Facility" (names of the ancestors incl. itself, root first)."""
    if not ids:
        return {}
    anc = OrgUnit.__table__.alias("anc")
    stmt = (
        select(OrgUnitClosure.descendant_id, OrgUnitClosure.depth, anc.c.name)
        .select_from(OrgUnitClosure.__table__.join(anc, anc.c.id == OrgUnitClosure.ancestor_id))
        .where(OrgUnitClosure.descendant_id.in_(ids))
        .order_by(OrgUnitClosure.descendant_id, OrgUnitClosure.depth.desc())
    )
    rows = (await session.execute(stmt)).all()
    out: dict[uuid.UUID, list[str]] = {}
    for did, _depth, name in rows:
        out.setdefault(did, []).append(name)
    return {k: " › ".join(v) for k, v in out.items()}


async def unit_path(session: AsyncSession, unit_id: uuid.UUID) -> str:
    return (await paths(session, [unit_id])).get(unit_id, "")


async def counts(session: AsyncSession, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, dict[str, int]]:
    """unit → {regions, districts, facilities, employees} counted over its subtree (alive rows only)."""
    if not ids:
        return {}
    out: dict[uuid.UUID, dict[str, int]] = {i: {"regions": 0, "districts": 0, "facilities": 0, "employees": 0} for i in ids}
    desc = OrgUnit.__table__.alias("d")
    stmt = (
        select(OrgUnitClosure.ancestor_id, desc.c.level, func.count())
        .select_from(OrgUnitClosure.__table__.join(desc, desc.c.id == OrgUnitClosure.descendant_id))
        .where(OrgUnitClosure.ancestor_id.in_(ids), OrgUnitClosure.depth > 0, desc.c.deleted_at.is_(None))
        .group_by(OrgUnitClosure.ancestor_id, desc.c.level)
    )
    for aid, level, n in (await session.execute(stmt)).all():
        key = {"region": "regions", "district": "districts", "facility": "facilities"}.get(level)
        if key:
            out[aid][key] = int(n)
    emp = (
        select(OrgUnitClosure.ancestor_id, func.count())
        .select_from(OrgUnitClosure.__table__.join(Employee.__table__, Employee.unit_id == OrgUnitClosure.descendant_id))
        .where(OrgUnitClosure.ancestor_id.in_(ids), Employee.deleted_at.is_(None))
        .group_by(OrgUnitClosure.ancestor_id)
    )
    for aid, n in (await session.execute(emp)).all():
        out[aid]["employees"] = int(n)
    return out


async def geo_names(session: AsyncSession, rows: Sequence[OrgUnit]) -> dict[str, str]:
    """`{id: name}` for every country/region/district referenced by `rows` (three small IN queries)."""
    out: dict[str, str] = {}
    for model, attr in ((Country, "country_id"), (Region, "region_id"), (District, "district_id")):
        ids = {getattr(u, attr) for u in rows if getattr(u, attr) is not None}
        if not ids:
            continue
        for row_id, name in (await session.execute(select(model.id, model.name).where(model.id.in_(ids)))).all():
            out[str(row_id)] = name
    return out


async def code_taken(session: AsyncSession, code: str, exclude_id: uuid.UUID | None = None) -> bool:
    """Facility codes (cheque prefixes) are unique among alive units, case-insensitive."""
    stmt = select(OrgUnit.id).where(func.lower(OrgUnit.code) == code.lower(), alive(OrgUnit))
    if exclude_id is not None:
        stmt = stmt.where(OrgUnit.id != exclude_id)
    return (await session.execute(stmt.limit(1))).scalar_one_or_none() is not None


async def geo_node_exists(session: AsyncSession, level: str, geo_id: uuid.UUID) -> bool:
    """Is there already an alive node of `level` for this region / district (one node per territory)?"""
    column = OrgUnit.region_id if level == "region" else OrgUnit.district_id
    stmt = select(OrgUnit.id).where(OrgUnit.level == level, column == geo_id, alive(OrgUnit)).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def get_country(session: AsyncSession, country_id: uuid.UUID) -> Country | None:
    return await session.get(Country, country_id)


async def get_region(session: AsyncSession, region_id: uuid.UUID) -> Region | None:
    return await session.get(Region, region_id)


async def get_district(session: AsyncSession, district_id: uuid.UUID) -> District | None:
    return await session.get(District, district_id)


# ----------------------------------------------------------------------------- inherited settings / messaging


async def effective_settings(session: AsyncSession, unit_id: uuid.UUID) -> dict[str, Any]:
    """Root → leaf shallow merge of `settings` (DB function `effective_settings`)."""
    row = (await session.execute(select(func.effective_settings(unit_id)))).scalar_one_or_none()
    return dict(row or {})


async def accounts_on_path(session: AsyncSession, ancestor_ids: Sequence[uuid.UUID], channel: str) -> dict[uuid.UUID, MessagingAccount]:
    """unit → its alive account of `channel`, for the units on a root→leaf path."""
    if not ancestor_ids:
        return {}
    stmt = select(MessagingAccount).where(MessagingAccount.unit_id.in_(ancestor_ids), MessagingAccount.channel == channel, alive(MessagingAccount))
    return {a.unit_id: a for a in (await session.execute(stmt)).scalars().all()}


def nearest_account(ancestor_ids: Sequence[uuid.UUID], accounts: dict[uuid.UUID, MessagingAccount]) -> MessagingAccount | None:
    """The deepest active account on the path (ancestor_ids is root first)."""
    for uid in reversed(list(ancestor_ids)):
        acc = accounts.get(uid)
        if acc is not None and acc.is_active:
            return acc
    return None


async def resolve_account(session: AsyncSession, unit_id: uuid.UUID, channel: str) -> MessagingAccount | None:
    """Nearest active account of `channel` for the unit (DB function `resolve_messaging_account`)."""
    stmt = (
        select(MessagingAccount)
        .join(OrgUnitClosure, OrgUnitClosure.ancestor_id == MessagingAccount.unit_id)
        .where(OrgUnitClosure.descendant_id == unit_id, MessagingAccount.channel == channel, alive(MessagingAccount), MessagingAccount.is_active.is_(True))
        .order_by(OrgUnitClosure.depth.asc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def own_account(session: AsyncSession, unit_id: uuid.UUID, channel: str) -> MessagingAccount | None:
    stmt = select(MessagingAccount).where(MessagingAccount.unit_id == unit_id, MessagingAccount.channel == channel, alive(MessagingAccount))
    return (await session.execute(stmt)).scalar_one_or_none()


async def sms_texts_on_path(session: AsyncSession, ancestor_ids: Sequence[uuid.UUID], lang: str = "uz") -> dict[uuid.UUID, dict[str, str]]:
    """unit → {key: body} of its own alive, active texts, for the units on a root→leaf path."""
    if not ancestor_ids:
        return {}
    stmt = select(SmsTemplate).where(SmsTemplate.unit_id.in_(ancestor_ids), SmsTemplate.lang == lang, alive(SmsTemplate), SmsTemplate.is_active.is_(True))
    out: dict[uuid.UUID, dict[str, str]] = {}
    for t in (await session.execute(stmt)).scalars().all():
        out.setdefault(t.unit_id, {})[t.key] = t.body
    return out


def merge_texts(ancestor_ids: Sequence[uuid.UUID], texts: dict[uuid.UUID, dict[str, str]]) -> dict[str, str]:
    """Root → leaf merge: the nearest unit's text wins per key."""
    merged: dict[str, str] = {}
    for uid in ancestor_ids:
        merged.update(texts.get(uid, {}))
    return merged


async def resolve_sms_texts(session: AsyncSession, unit_id: uuid.UUID, lang: str = "uz") -> dict[str, str]:
    """The SMS texts a message of `unit_id` uses (own, else inherited; keys missing → platform default)."""
    anc = (await session.execute(select(OrgUnitClosure.ancestor_id).where(OrgUnitClosure.descendant_id == unit_id).order_by(OrgUnitClosure.depth.desc()))).scalars().all()
    return merge_texts(list(anc), await sms_texts_on_path(session, list(anc), lang))


async def own_sms_texts(session: AsyncSession, unit_id: uuid.UUID, lang: str = "uz") -> list[SmsTemplate]:
    stmt = select(SmsTemplate).where(SmsTemplate.unit_id == unit_id, SmsTemplate.lang == lang, alive(SmsTemplate))
    return list((await session.execute(stmt)).scalars().all())


async def subtree_ids(session: AsyncSession, unit_id: uuid.UUID) -> list[uuid.UUID]:
    rows = (await session.execute(select(OrgUnitClosure.descendant_id).where(OrgUnitClosure.ancestor_id == unit_id))).scalars().all()
    return list(rows)


async def ancestors_of(session: AsyncSession, unit_ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
    """Every ancestor (incl. themselves) of the given units — the path context a scoped employee may read."""
    if not unit_ids:
        return []
    rows = (await session.execute(select(OrgUnitClosure.ancestor_id).where(OrgUnitClosure.descendant_id.in_(unit_ids)).distinct())).scalars().all()
    return list(rows)
