"""Reports service — dashboard summary + breakdown, cached briefly per (company, branch, range)."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationError
from app.core.schemas import dump
from app.core.timeutil import day_range, today_local
from app.infrastructure.db.models import Category
from app.infrastructure.redis import cache
from app.modules.reports import repository as repo
from app.modules.reports.schemas import (
    BreakdownQuery,
    BreakdownRowOut,
    CategorySlice,
    DashboardSummaryOut,
    RangeQuery,
    TrendPoint,
)

DASHBOARD_TTL_SECONDS = 30
MAX_RANGE_DAYS = 366 * 3


def _parse_range(q: RangeQuery) -> tuple[date, date, uuid.UUID | None]:
    try:
        d_from = date.fromisoformat(q.date_from)
        d_to = date.fromisoformat(q.date_to)
    except ValueError as exc:
        raise ValidationError("Sana noto‘g‘ri") from exc
    if d_to < d_from:
        d_from, d_to = d_to, d_from
    if (d_to - d_from).days > MAX_RANGE_DAYS:
        raise ValidationError("Sana oralig‘i juda katta")
    branch_id: uuid.UUID | None = None
    if q.branch_id:
        try:
            branch_id = uuid.UUID(q.branch_id)
        except ValueError as exc:
            raise ValidationError("Filial noto‘g‘ri") from exc
    return d_from, d_to, branch_id


def top_level_lookup(cats: list[Category]) -> dict[uuid.UUID, Category]:
    """category id → its root ancestor (walks parent_id in memory; cycles/unknown parents stop the walk)."""
    by_id = {c.id: c for c in cats}
    out: dict[uuid.UUID, Category] = {}
    for c in cats:
        cur = c
        seen = {c.id}
        while cur.parent_id and cur.parent_id in by_id and cur.parent_id not in seen:
            seen.add(cur.parent_id)
            cur = by_id[cur.parent_id]
        out[c.id] = cur
    return out


async def _build_dashboard(session: AsyncSession, company_id: uuid.UUID, d_from: date, d_to: date, branch_id: uuid.UUID | None) -> DashboardSummaryOut:
    today = today_local()
    t_start, t_end = day_range(today, today)
    start, end = day_range(d_from, d_to)
    counters = await repo.dashboard_counters(session, company_id, branch_id, t_start, t_end)
    trend_map = await repo.order_trend(session, company_id, branch_id, start, end)
    trend: list[TrendPoint] = []
    d = d_from
    while d <= d_to:
        orders, revenue = trend_map.get(d.isoformat(), (0, 0))
        trend.append(TrendPoint(date=d.isoformat(), orders=orders, revenue=revenue))
        d += timedelta(days=1)
    rows = await repo.items_by_category(session, company_id, branch_id, start, end)
    roots = top_level_lookup(await repo.categories(session, company_id)) if rows else {}
    agg: dict[uuid.UUID, CategorySlice] = {}
    for r in rows:
        root = roots.get(r.category_id)
        key = root.id if root else r.category_id
        slice_ = agg.get(key)
        if slice_ is None:
            slice_ = CategorySlice(name=root.name if root else (r.name or "—"), count=0, revenue=0, color=root.color if root else None)
            agg[key] = slice_
        slice_.count += int(r.count)
        slice_.revenue += int(r.revenue)
    by_category = sorted(agg.values(), key=lambda s: s.revenue, reverse=True)
    return DashboardSummaryOut(**counters, trend=trend, by_category=by_category)


async def dashboard(session: AsyncSession, company_id: uuid.UUID, q: RangeQuery) -> DashboardSummaryOut:
    """§9 `dashboard`: today's counters + dense daily trend + top-level category split (Redis 30s)."""
    d_from, d_to, branch_id = _parse_range(q)
    key = f"co:{company_id}:reports:dashboard:{branch_id or 'all'}:{d_from}:{d_to}"
    hit = await cache.get_json(key)
    if hit is not None:
        return DashboardSummaryOut.model_validate(hit)
    out = await _build_dashboard(session, company_id, d_from, d_to, branch_id)
    await cache.set_json(key, dump(out), DASHBOARD_TTL_SECONDS)
    return out


async def breakdown(session: AsyncSession, company_id: uuid.UUID, q: BreakdownQuery) -> list[BreakdownRowOut]:
    """§9 `breakdown`: non-cancelled items in range grouped by category/service/branch/employee, revenue desc."""
    d_from, d_to, branch_id = _parse_range(q)
    start, end = day_range(d_from, d_to)
    rows = await repo.breakdown(session, company_id, q.by, branch_id, start, end)
    return [BreakdownRowOut(name=r.name or "—", count=int(r.count), revenue=int(r.revenue)) for r in rows]
