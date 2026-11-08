"""Platform rules — the regional activity view (superadmin): territory scope, grouping level, folding, cache."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationError
from app.core.schemas import dump
from app.core.timeutil import day_range
from app.infrastructure.redis import cache
from app.modules.platform import repository as repo
from app.modules.platform.schemas import (
    CoverageOut,
    RegionGroupBy,
    RegionStatsOut,
    RegionStatsQuery,
    RegionTrendPoint,
    UnitMetrics,
    UnitRow,
)

CACHE_TTL_SECONDS = 60
MAX_RANGE_DAYS = 366 * 3
METRICS = ("orders", "revenue", "patients_new", "results_ready", "sms_sent")


def default_group_by(country_id: uuid.UUID | None, region_id: uuid.UUID | None, district_id: uuid.UUID | None) -> RegionGroupBy:
    """One level below the narrowest filter: nothing / a country → regions, a region → districts, a district → clinics."""
    if district_id:
        return "company"
    if region_id:
        return "district"
    return "region"


def _parse_uuid(value: str | None, label: str) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise ValidationError(f"{label} noto‘g‘ri") from exc


def parse_query(q: RegionStatsQuery) -> tuple[date, date, uuid.UUID | None, uuid.UUID | None, uuid.UUID | None, RegionGroupBy]:
    try:
        d_from = date.fromisoformat(q.date_from)
        d_to = date.fromisoformat(q.date_to)
    except ValueError as exc:
        raise ValidationError("Sana noto‘g‘ri") from exc
    if d_to < d_from:
        d_from, d_to = d_to, d_from
    if (d_to - d_from).days > MAX_RANGE_DAYS:
        raise ValidationError("Sana oralig‘i juda katta")
    country_id = _parse_uuid(q.country_id, "Davlat")
    region_id = _parse_uuid(q.region_id, "Viloyat")
    district_id = _parse_uuid(q.district_id, "Tuman")
    return d_from, d_to, country_id, region_id, district_id, q.group_by or default_group_by(country_id, region_id, district_id)


def fold_rows(
    group_by: RegionGroupBy,
    structure: tuple[dict[uuid.UUID | None, int], dict[uuid.UUID | None, int]],
    metrics: dict[str, dict[uuid.UUID | None, int]],
    names: dict[uuid.UUID, dict[str, Any]],
) -> tuple[list[UnitRow], UnitMetrics | None, UnitMetrics]:
    """Per-key counters → rows (money first, then cheques, then name), the `None` key as the unassigned bucket, and the
    scope total. A key without a live branch (only old cheques of a deleted branch) still gets a row."""
    branches, companies = structure
    keys: set[uuid.UUID | None] = set(branches) | set(companies)
    for m in metrics.values():
        keys |= set(m)
    rows: list[UnitRow] = []
    unassigned: UnitMetrics | None = None
    total = UnitMetrics()
    for key in keys:
        values = {"companies": companies.get(key, 0), "branches": branches.get(key, 0), **{m: metrics[m].get(key, 0) for m in METRICS}}
        for m, v in values.items():
            setattr(total, m, getattr(total, m) + v)
        if key is None:
            unassigned = UnitMetrics(**values)
            continue
        meta = names.get(key) or {}
        rows.append(UnitRow(id=str(key), kind=group_by, name=meta.get("name") or "—", parent_name=meta.get("parent_name"), slug=meta.get("slug"), is_active=meta.get("is_active"), **values))
    rows.sort(key=lambda r: (-r.revenue, -r.orders, r.name.lower()))
    return rows, unassigned, total


async def _build(session: AsyncSession, d_from: date, d_to: date, country_id: uuid.UUID | None, region_id: uuid.UUID | None, district_id: uuid.UUID | None, group_by: RegionGroupBy) -> RegionStatsOut:
    start, end = day_range(d_from, d_to)
    assert start is not None and end is not None
    b = repo.scoped_branches(group_by, country_id, region_id, district_id)
    structure = await repo.structure(session, b)
    metrics = {
        "orders": await repo.orders(session, b, start, end),
        "revenue": await repo.revenue(session, b, start, end),
        "patients_new": await repo.patients_new(session, b, start, end),
        "results_ready": await repo.results_ready(session, b, start, end),
        "sms_sent": await repo.sms_sent(session, b, start, end),
    }
    keys = {k for k in (set(structure[0]) | set(structure[1]) | {k for m in metrics.values() for k in m}) if k is not None}
    rows, unassigned, total = fold_rows(group_by, structure, metrics, await repo.names(session, group_by, keys))
    # the total counts each clinic once, however many territories it spans
    if group_by != "company":
        total.companies = await repo.distinct_companies(session, b)
    trend_map = await repo.trend(session, b, start, end)
    trend: list[RegionTrendPoint] = []
    d = d_from
    while d <= d_to:
        orders, revenue = trend_map.get(d.isoformat(), (0, 0))
        trend.append(RegionTrendPoint(date=d.isoformat(), orders=orders, revenue=revenue))
        d += timedelta(days=1)
    return RegionStatsOut(
        group_by=group_by, date_from=d_from.isoformat(), date_to=d_to.isoformat(), total=total, rows=rows, unassigned=unassigned, trend=trend,
        coverage=CoverageOut(**await repo.coverage(session)),
    )


async def region_stats(session: AsyncSession, q: RegionStatsQuery) -> RegionStatsOut:
    """Activity by territory for the superadmin (Redis 60 s per scope and range)."""
    d_from, d_to, country_id, region_id, district_id, group_by = parse_query(q)
    key = f"platform:regions:{group_by}:{country_id}:{region_id}:{district_id}:{d_from}:{d_to}"
    hit = await cache.get_json(key)
    if hit is not None:
        return RegionStatsOut.model_validate(hit)
    out = await _build(session, d_from, d_to, country_id, region_id, district_id, group_by)
    await cache.set_json(key, dump(out), CACHE_TTL_SECONDS)
    return out
