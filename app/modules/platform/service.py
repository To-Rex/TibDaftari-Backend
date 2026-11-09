"""Platform rules — the territory activity view: who may see what, grouping level, folding, cache.

Superadmins (`platform.unit.manage`) see every country; any other staff member may ask for the subtree of a unit
inside their scope (`unitId`) — a region administrator sees their region folded by district or facility.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import StaffPrincipal
from app.core.exceptions import NotFoundError, ValidationError
from app.core.schemas import dump
from app.core.timeutil import day_range
from app.infrastructure.redis import cache
from app.modules.platform import repository as repo
from app.modules.platform.schemas import (
    RegionGroupBy,
    RegionStatsOut,
    RegionStatsQuery,
    RegionTrendPoint,
    UnitMetrics,
    UnitRow,
)
from app.modules.units import repository as units_repo

CACHE_TTL_SECONDS = 60
MAX_RANGE_DAYS = 366 * 3
METRICS = ("orders", "revenue", "patients_new", "results_ready", "sms_sent")
PLATFORM_MANAGE = "platform.unit.manage"

_LEVELS: tuple[RegionGroupBy, ...] = ("country", "region", "district", "unit")
_UNIT_DEPTH = {"country": 0, "region": 1, "district": 2, "facility": 3}


def default_group_by(country_id: uuid.UUID | None, region_id: uuid.UUID | None, district_id: uuid.UUID | None, unit_level: str | None = None) -> RegionGroupBy:
    """One level below the narrowest filter: nothing / a country → regions, a region → districts, a district →
    facilities; a root unit counts with its own level (a region node → districts, a district node → facilities)."""
    depth = 2 if district_id else 1 if region_id else 0
    if unit_level:
        depth = max(depth, _UNIT_DEPTH.get(unit_level, 0))
    return _LEVELS[min(depth + 1, len(_LEVELS) - 1)]


def _parse_uuid(value: str | None, label: str) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise ValidationError(f"{label} noto‘g‘ri") from exc


@dataclass(frozen=True, slots=True)
class ParsedQuery:
    date_from: date
    date_to: date
    country_id: uuid.UUID | None
    region_id: uuid.UUID | None
    district_id: uuid.UUID | None
    unit_id: uuid.UUID | None
    #: the requested level; None = decide once the root unit is known (`default_group_by`)
    group_by: RegionGroupBy | None

    def resolve_group_by(self, unit_level: str | None = None) -> RegionGroupBy:
        return self.group_by or default_group_by(self.country_id, self.region_id, self.district_id, unit_level)


def parse_query(q: RegionStatsQuery) -> ParsedQuery:
    try:
        d_from = date.fromisoformat(q.date_from)
        d_to = date.fromisoformat(q.date_to)
    except ValueError as exc:
        raise ValidationError("Sana noto‘g‘ri") from exc
    if d_to < d_from:
        d_from, d_to = d_to, d_from
    if (d_to - d_from).days > MAX_RANGE_DAYS:
        raise ValidationError("Sana oralig‘i juda katta")
    return ParsedQuery(
        date_from=d_from,
        date_to=d_to,
        country_id=_parse_uuid(q.country_id, "Davlat"),
        region_id=_parse_uuid(q.region_id, "Viloyat"),
        district_id=_parse_uuid(q.district_id, "Tuman"),
        unit_id=_parse_uuid(q.unit_id, "Birlik"),
        group_by=q.group_by,
    )


def fold_rows(
    group_by: RegionGroupBy,
    structure: dict[uuid.UUID | None, int],
    metrics: dict[str, dict[uuid.UUID | None, int]],
    names: dict[uuid.UUID, dict[str, Any]],
) -> tuple[list[UnitRow], UnitMetrics]:
    """Per-key counters → rows (money first, then cheques, then name) and the scope total. A key without an alive
    facility (only old cheques of a closed one) still gets a row; every facility has every geo key, so there is
    no unassigned bucket."""
    keys: set[uuid.UUID | None] = set(structure)
    for m in metrics.values():
        keys |= set(m)
    rows: list[UnitRow] = []
    total = UnitMetrics()
    for key in keys:
        values = {"units": structure.get(key, 0), **{m: metrics[m].get(key, 0) for m in METRICS}}
        for m, v in values.items():
            setattr(total, m, getattr(total, m) + v)
        if key is None:
            continue
        meta = names.get(key) or {}
        rows.append(
            UnitRow(
                id=str(key),
                kind=group_by,
                name=meta.get("name") or "—",
                parent_name=meta.get("parent_name"),
                unit_id=str(meta["unit_id"]) if meta.get("unit_id") else None,
                code=meta.get("code"),
                facility_kind=meta.get("facility_kind"),
                is_active=meta.get("is_active"),
                **values,
            )
        )
    rows.sort(key=lambda r: (-r.revenue, -r.orders, r.name.lower()))
    return rows, total


async def _build(session: AsyncSession, parsed: ParsedQuery, group_by: RegionGroupBy) -> RegionStatsOut:
    start, end = day_range(parsed.date_from, parsed.date_to)
    assert start is not None and end is not None
    u = repo.scoped_facilities(group_by, parsed.country_id, parsed.region_id, parsed.district_id, parsed.unit_id)
    structure = await repo.structure(session, u)
    metrics = {
        "orders": await repo.orders(session, u, start, end),
        "revenue": await repo.revenue(session, u, start, end),
        "patients_new": await repo.patients_new(session, u, start, end),
        "results_ready": await repo.results_ready(session, u, start, end),
        "sms_sent": await repo.sms_sent(session, u, start, end),
    }
    keys = {k for k in (set(structure) | {k for m in metrics.values() for k in m}) if k is not None}
    rows, total = fold_rows(group_by, structure, metrics, await repo.names(session, group_by, keys))
    trend_map = await repo.trend(session, u, start, end)
    trend: list[RegionTrendPoint] = []
    d = parsed.date_from
    while d <= parsed.date_to:
        orders, revenue = trend_map.get(d.isoformat(), (0, 0))
        trend.append(RegionTrendPoint(date=d.isoformat(), orders=orders, revenue=revenue))
        d += timedelta(days=1)
    return RegionStatsOut(group_by=group_by, date_from=parsed.date_from.isoformat(), date_to=parsed.date_to.isoformat(), total=total, rows=rows, trend=trend)


async def region_stats(session: AsyncSession, q: RegionStatsQuery, staff: StaffPrincipal) -> RegionStatsOut:
    """Activity by territory. Platform permission for the whole tree; otherwise the caller must pass a `unitId`
    inside their own scope. Cached in Redis for 60 s per full parameter set."""
    parsed = parse_query(q)
    unit_level: str | None = None
    if parsed.unit_id is not None:
        unit = await units_repo.get_unit(session, parsed.unit_id)
        if unit is None:
            raise NotFoundError("Birlik topilmadi")
        if not staff.has(PLATFORM_MANAGE):
            staff.require_unit(unit.id)
        unit_level = unit.level
    else:
        staff.require(PLATFORM_MANAGE)
    group_by = parsed.resolve_group_by(unit_level)
    key = f"platform:regions:{group_by}:{parsed.country_id}:{parsed.region_id}:{parsed.district_id}:{parsed.unit_id}:{parsed.date_from}:{parsed.date_to}"
    hit = await cache.get_json(key)
    if hit is not None:
        return RegionStatsOut.model_validate(hit)
    out = await _build(session, parsed, group_by)
    await cache.set_json(key, dump(out), CACHE_TTL_SECONDS)
    return out
