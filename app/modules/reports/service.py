"""Reports service — dashboard, breakdowns, Excel export and the patients / results / services reports of one
organisation unit (`scope`), cached briefly per (unit, narrowing, range)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import StaffPrincipal
from app.core.exceptions import ValidationError
from app.core.pagination import page_of
from app.core.schemas import Page, dump
from app.core.scope import UnitScope, descendant_ids, ids_of
from app.core.timeutil import day_range, today_local, utcnow
from app.infrastructure.db.models import Category
from app.infrastructure.redis import cache
from app.modules.messaging import service as messaging_service
from app.modules.reports import export as reports_export
from app.modules.reports import repository as repo
from app.modules.reports.schemas import (
    BreakdownQuery,
    BreakdownRowOut,
    CategorySlice,
    CountSlice,
    DashboardSummaryOut,
    NamedCount,
    PatientReportOut,
    PatientTrendPoint,
    RangeQuery,
    ReportExportQuery,
    ResultListQuery,
    ResultRowOut,
    ResultsReportOut,
    ServicesReportOut,
    ServiceUsageRow,
    TopPatientOut,
    TrendPoint,
    TurnaroundRow,
    UnusedServiceRow,
)
from app.modules.units.repository import units_by_ids

DASHBOARD_TTL_SECONDS = 30
MAX_RANGE_DAYS = 366 * 3
#: staff with `reports.finance.recent` but not `reports.finance.read` see revenue for the last 7 days only
#: (the registrar role has both since 2026-10-31 — the cap applies when an admin takes finance.read away)
RECENT_WINDOW_DAYS = 7
#: a unit list that matches nothing (an unknown / out-of-scope `unitId`)
NOTHING: list[uuid.UUID] = [uuid.UUID(int=0)]


def _recent_only(staff: StaffPrincipal | None) -> bool:
    """Recent-only finance: revenue is visible, but only within the last 7 days."""
    return staff is not None and not staff.has("reports.finance.read") and staff.has("reports.finance.recent")


def _clamp_recent(staff: StaffPrincipal | None, d_from: date, d_to: date) -> tuple[date, date]:
    """Cap the window of a revenue-bearing report to the last 7 days for recent-only staff."""
    if not _recent_only(staff):
        return d_from, d_to
    today = today_local()
    oldest = today - timedelta(days=RECENT_WINDOW_DAYS - 1)
    if d_from < oldest:
        d_from = oldest
    if d_to > today:
        d_to = today
    return d_from, d_to


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
    unit_id: uuid.UUID | None = None
    if q.unit_id:
        parsed = ids_of([q.unit_id])
        if not parsed:
            raise ValidationError("Birlik noto‘g‘ri")
        unit_id = parsed[0]
    return d_from, d_to, unit_id


async def _units(session: AsyncSession, scope: UnitScope, unit_id: uuid.UUID | None) -> tuple[list[uuid.UUID], str]:
    """The units a report covers: the scope's whole subtree, or — with `unitId` — the chosen sub-unit and everything
    under it (a facility is just itself; a unit outside the scope matches nothing). Second value = the cache-key part."""
    if unit_id is None:
        return scope.descendant_ids, "all"
    narrowed = scope.narrow([unit_id])
    return (await descendant_ids(session, narrowed) or NOTHING), str(unit_id)


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


async def _build_dashboard(session: AsyncSession, scope: UnitScope, unit_ids: list[uuid.UUID], d_from: date, d_to: date) -> DashboardSummaryOut:
    today = today_local()
    t_start, t_end = day_range(today, today)
    start, end = day_range(d_from, d_to)
    counters = await repo.dashboard_counters(session, unit_ids, t_start, t_end, country_id=scope.country_id)
    sms_queued = await messaging_service.count_queued(session, unit_ids)
    trend_map = await repo.order_trend(session, unit_ids, start, end)
    trend: list[TrendPoint] = []
    d = d_from
    while d <= d_to:
        orders, revenue = trend_map.get(d.isoformat(), (0, 0))
        trend.append(TrendPoint(date=d.isoformat(), orders=orders, revenue=revenue))
        d += timedelta(days=1)
    rows = await repo.items_by_category(session, unit_ids, start, end)
    roots = top_level_lookup(await repo.categories(session, scope)) if rows else {}
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
    return DashboardSummaryOut(**counters, sms_queued=int(sms_queued), trend=trend, by_category=by_category)


async def dashboard(session: AsyncSession, scope: UnitScope, q: RangeQuery, staff: StaffPrincipal | None = None) -> DashboardSummaryOut:
    """§9 `dashboard`: today's counters + dense daily trend + top-level category split (Redis 30s)."""
    d_from, d_to, unit_id = _parse_range(q)
    d_from, d_to = _clamp_recent(staff, d_from, d_to)
    unit_ids, part = await _units(session, scope, unit_id)
    key = f"unit:{scope.id}:reports:dashboard:{part}:{d_from}:{d_to}"
    hit = await cache.get_json(key)
    if hit is not None:
        return DashboardSummaryOut.model_validate(hit)
    out = await _build_dashboard(session, scope, unit_ids, d_from, d_to)
    await cache.set_json(key, dump(out), DASHBOARD_TTL_SECONDS)
    return out


async def breakdown(session: AsyncSession, scope: UnitScope, q: BreakdownQuery, staff: StaffPrincipal | None = None) -> list[BreakdownRowOut]:
    """§9 `breakdown`: non-cancelled items in range grouped by category/service/unit/employee, revenue desc."""
    d_from, d_to, unit_id = _parse_range(q)
    d_from, d_to = _clamp_recent(staff, d_from, d_to)
    unit_ids, _ = await _units(session, scope, unit_id)
    start, end = day_range(d_from, d_to)
    rows = await repo.breakdown(session, unit_ids, q.by, start, end)
    return [BreakdownRowOut(name=r.name or "—", count=int(r.count), revenue=int(r.revenue)) for r in rows]


async def export_workbook(session: AsyncSession, scope: UnitScope, q: ReportExportQuery, staff: StaffPrincipal | None = None) -> bytes:
    """The period as an Excel workbook: patients × analyses, labs and the other cuts (service / facility / employee / daily)."""
    d_from, d_to, unit_id = _parse_range(q)
    d_from, d_to = _clamp_recent(staff, d_from, d_to)
    unit_ids, _ = await _units(session, scope, unit_id)
    start, end = day_range(d_from, d_to)

    detail = await repo.export_items(session, unit_ids, start, end)
    roots = top_level_lookup(await repo.categories(session, scope)) if detail else {}
    units = await units_by_ids(session, {r.unit_id for r in detail})

    def _lab(category_id: uuid.UUID | None, fallback: str | None) -> str:
        root = roots.get(category_id) if category_id else None
        return root.name if root else (fallback or "—")

    def _unit_name(unit_id: uuid.UUID | None) -> str:
        u = units.get(unit_id) if unit_id else None
        return u.name if u else "—"

    detail_rows = [
        reports_export.DetailRow(
            date=r.created_at,
            number=r.order_number or "",
            patient=r.patient_name or "",
            phone=r.patient_phone or "",
            service=r.service_name or "",
            laboratory=_lab(r.category_id, r.category_name),
            unit=_unit_name(r.unit_id),
            price=int(r.final_price or 0),
            status=r.status,
        )
        for r in detail
    ]

    def _agg(rows: Sequence[Any]) -> list[reports_export.AggRow]:
        return [reports_export.AggRow(name=r.name or "—", count=int(r.count), revenue=int(r.revenue)) for r in rows]

    cat_rows = await repo.items_by_category(session, unit_ids, start, end)
    labs = sorted(
        (reports_export.AggRow(name=_lab(r.category_id, r.name), count=int(r.count), revenue=int(r.revenue)) for r in cat_rows),
        key=lambda x: x.revenue,
        reverse=True,
    )
    services = _agg(await repo.breakdown(session, unit_ids, "service", start, end))
    unit_rows = _agg(await repo.breakdown(session, unit_ids, "unit", start, end))
    employee_rows = _agg(await repo.breakdown(session, unit_ids, "employee", start, end))

    trend_map = await repo.order_trend(session, unit_ids, start, end)
    daily: list[reports_export.DayRow] = []
    d = d_from
    while d <= d_to:
        orders, revenue = trend_map.get(d.isoformat(), (0, 0))
        daily.append(reports_export.DayRow(date=d.isoformat(), orders=orders, revenue=revenue))
        d += timedelta(days=1)

    return reports_export.build_xlsx(
        detail_rows, labs, services, unit_rows, employee_rows, daily,
        lang=q.lang, d_from=d_from, d_to=d_to, generated_at=utcnow(),
    )


# ----------------------------------------------------------------------------- patients / results / services

REPORT_TTL_SECONDS = 30
#: opens (link / portal) and prints of result documents are recorded from this day on
TRACKING_SINCE = "2026-10-07"
AGE_GROUPS = ("0-17", "18-29", "30-44", "45-59", "60+")


def age_group(age: float | int | None) -> str:
    """Whole years → report bucket; unknown / implausible → "unknown"."""
    if age is None or age < 0 or age > 130:
        return "unknown"
    a = int(age)
    if a < 18:
        return "0-17"
    if a < 30:
        return "18-29"
    if a < 45:
        return "30-44"
    if a < 60:
        return "45-59"
    return "60+"


def gender_key(value: str | None) -> str:
    return value if value in ("male", "female") else "unknown"


def _hours(value: object) -> float | None:
    return round(float(value), 1) if value is not None else None  # type: ignore[arg-type]


def _sms_status(deliveries: list[dict] | None) -> str | None:
    return next((str(d.get("status")) for d in (deliveries or []) if d.get("channel") == "sms"), None)


def _telegram_ok(deliveries: list[dict] | None) -> bool:
    return any(d.get("channel") == "telegram" and d.get("status") == "sent" for d in (deliveries or []))


def previous_period(d_from: date, d_to: date) -> tuple[date, date]:
    """The same number of days right before [d_from, d_to]."""
    days = (d_to - d_from).days + 1
    return d_from - timedelta(days=days), d_from - timedelta(days=1)


async def _cached(key: str, model: type, build):  # type: ignore[no-untyped-def]
    hit = await cache.get_json(key)
    if hit is not None:
        return model.model_validate(hit)
    out = await build()
    await cache.set_json(key, dump(out), REPORT_TTL_SECONDS)
    return out


async def patients_report(session: AsyncSession, scope: UnitScope, q: RangeQuery, staff: StaffPrincipal | None = None) -> PatientReportOut:
    """Who came in the period: new vs returning, demographics, districts, most frequent, debts (finance only)."""
    d_from, d_to, unit_id = _parse_range(q)
    unit_ids, part = await _units(session, scope, unit_id)
    finance = staff is None or staff.has("reports.finance.read")
    start, end = day_range(d_from, d_to)

    async def build() -> PatientReportOut:
        s = await repo.patient_summary(session, unit_ids, start, end)
        gender: dict[str, int] = {"male": 0, "female": 0, "unknown": 0}
        ages: dict[str, int] = {k: 0 for k in (*AGE_GROUPS, "unknown")}
        for r in await repo.patient_demographics(session, unit_ids, start, end):
            gender[gender_key(r.gender)] += int(r.count)
            ages[age_group(r.age)] += int(r.count)
        by_day = {r.day.isoformat(): r for r in await repo.patient_trend(session, unit_ids, start, end)}
        trend: list[PatientTrendPoint] = []
        d = d_from
        while d <= d_to:
            r = by_day.get(d.isoformat())
            trend.append(PatientTrendPoint(date=d.isoformat(), patients=int(r.patients) if r else 0, new=int(r.new) if r else 0))
            d += timedelta(days=1)
        patients, orders = int(s.patients), int(s.orders)
        return PatientReportOut(
            patients=patients,
            new_patients=int(s.new),
            returning_patients=patients - int(s.new),
            orders=orders,
            avg_orders=round(orders / patients, 2) if patients else 0.0,
            avg_check=round(int(s.total) / orders) if finance and orders else (0 if finance else None),
            debtors=int(s.debtors) if finance else None,
            debt=int(s.debt) if finance else None,
            telegram_linked=int(s.telegram),
            portal_linked=int(s.portal),
            gender=[CountSlice(key=k, count=v) for k, v in gender.items()],
            age_groups=[CountSlice(key=k, count=v) for k, v in ages.items()],
            districts=[NamedCount(name=r.name or "", count=int(r.count)) for r in await repo.patient_districts(session, unit_ids, start, end)],
            trend=trend,
            top_patients=[
                TopPatientOut(patient_id=str(r.pid), name=r.name or "—", phone=r.phone or "", orders=int(r.orders), paid=int(r.paid) if finance else None, last_visit=r.last)
                for r in await repo.top_patients(session, unit_ids, start, end)
            ],
        )

    return await _cached(f"unit:{scope.id}:reports:patients:{part}:{d_from}:{d_to}:{int(finance)}", PatientReportOut, build)


async def results_report(session: AsyncSession, scope: UnitScope, q: RangeQuery, staff: StaffPrincipal | None = None) -> ResultsReportOut:
    """Results of the period's cheques: ready or not, overdue, turnaround, and whether patients got them."""
    d_from, d_to, unit_id = _parse_range(q)
    unit_ids, part = await _units(session, scope, unit_id)
    start, end = day_range(d_from, d_to)

    async def build() -> ResultsReportOut:
        it = await repo.item_readiness(session, unit_ids, start, end, utcnow())
        dc = await repo.document_receipt(session, unit_ids, start, end)
        documents, got = int(dc.documents), int(dc.received)
        return ResultsReportOut(
            items=int(it.items),
            items_waiting=int(it.waiting),
            items_submitted=int(it.submitted),
            items_ready=int(it.ready),
            items_overdue=int(it.overdue),
            patients_waiting=int(it.patients_waiting),
            documents=documents,
            received=got,
            received_patients=int(dc.received_patients),
            not_received=documents - got,
            not_received_patients=int(dc.not_received_patients),
            viewed=int(dc.viewed),
            printed=int(dc.printed),
            telegram=int(dc.telegram),
            sms_sent=int(dc.sms_sent),
            avg_hours=_hours(it.avg_hours),
            on_time=int(it.on_time),
            turnaround=[
                TurnaroundRow(name=r.name or "—", approved=int(r.approved), avg_hours=_hours(r.avg_hours), on_time=int(r.on_time))
                for r in await repo.turnaround_by_category(session, unit_ids, start, end)
            ],
            tracking_since=TRACKING_SINCE,
        )

    return await _cached(f"unit:{scope.id}:reports:results:{part}:{d_from}:{d_to}", ResultsReportOut, build)


async def results_list(session: AsyncSession, scope: UnitScope, q: ResultListQuery, staff: StaffPrincipal | None = None) -> Page[ResultRowOut]:
    """Paged rows behind the results report: not received / received results, or cheques still waiting."""
    d_from, d_to, unit_id = _parse_range(q)
    unit_ids, _ = await _units(session, scope, unit_id)
    start, end = day_range(d_from, d_to)
    if q.status == "waiting":
        rows, total = await repo.waiting_orders_page(session, unit_ids, start, end, utcnow(), search=q.search, page=q.page, page_size=q.page_size)
        items = [
            ResultRowOut(
                kind="order", order_id=str(r.order_id), order_number=r.number, patient_id=str(r.patient_id), patient_name=r.patient_name, patient_phone=r.patient_phone,
                title=r.title or "", ordered_at=r.ordered_at, waiting=int(r.waiting), submitted=int(r.submitted), overdue=bool(r.overdue),
            )
            for r in rows
        ]
    else:
        rows, total = await repo.result_documents_page(session, unit_ids, start, end, got=q.status == "received", search=q.search, page=q.page, page_size=q.page_size)
        items = [
            ResultRowOut(
                kind="document", order_id=str(r.order_id), order_number=r.number, patient_id=str(r.patient_id), patient_name=r.patient_name, patient_phone=r.patient_phone,
                title=r.title, ordered_at=r.ordered_at, document_id=str(r.document_id), ready_at=r.ready_at, viewed_at=r.viewed_at, view_count=int(r.view_count or 0),
                printed_at=r.printed_at, print_count=int(r.print_count or 0), telegram=_telegram_ok(r.deliveries), sms=_sms_status(r.deliveries),
            )
            for r in rows
        ]
    return page_of(items, q, total)


async def _orderable(session: AsyncSession, scope: UnitScope, unit_ids: list[uuid.UUID], services: Sequence[Any]) -> set[uuid.UUID]:
    """Ids of `services` that some facility of the report can order: owned on the facility's path and not switched off
    for it (`service_unit_settings`, the nearest level wins). A service switched off everywhere in the scope cannot be
    ordered there — it is absent, not "unused"."""
    wanted = set(unit_ids)
    facilities = [f for f in scope.facility_ids if f in wanted]
    if not facilities or not services:
        return set()
    paths = await repo.facility_paths(session, facilities)
    facilities_under: dict[uuid.UUID, list[uuid.UUID]] = {}  # owner / setting unit → facilities it applies to
    for f, path in paths.items():
        for u in path:
            facilities_under.setdefault(u, []).append(f)
    overrides: dict[uuid.UUID, dict[uuid.UUID, bool]] = {}
    for sid, uid, enabled in await repo.service_settings(session, [s.id for s in services]):
        overrides.setdefault(sid, {})[uid] = bool(enabled)
    out: set[uuid.UUID] = set()
    for s in services:
        rules = overrides.get(s.id)
        for f in facilities_under.get(s.owner_unit_id, ()):
            if not rules or next((rules[u] for u in reversed(paths[f]) if u in rules), True):
                out.add(s.id)
                break
    return out


async def services_report(session: AsyncSession, scope: UnitScope, q: RangeQuery, staff: StaffPrincipal | None = None) -> ServicesReportOut:
    """Which services are used most / least in the period (vs the period before), and which nobody ordered."""
    d_from, d_to, unit_id = _parse_range(q)
    unit_ids, part = await _units(session, scope, unit_id)
    finance = staff is None or staff.has("reports.finance.read")
    start, end = day_range(d_from, d_to)
    p_from, p_to = previous_period(d_from, d_to)
    p_start, p_end = day_range(p_from, p_to)

    async def build() -> ServicesReportOut:
        rows = await repo.service_usage(session, unit_ids, start, end)
        prev = await repo.service_counts(session, unit_ids, p_start, p_end)
        used = {r.sid for r in rows}
        candidates = [s for s in await repo.active_services(session, scope) if s.id not in used]
        orderable = await _orderable(session, scope, unit_ids, candidates)
        return ServicesReportOut(
            prev_from=p_from.isoformat(),
            prev_to=p_to.isoformat(),
            total=sum(int(r.count) for r in rows),
            prev_total=sum(prev.values()),
            rows=[
                ServiceUsageRow(
                    service_type_id=str(r.sid), name=r.name or "—", category=r.category or "—", count=int(r.count), patients=int(r.patients), prev_count=prev.get(r.sid, 0),
                    revenue=int(r.revenue) if finance else None, pending=int(r.pending), avg_hours=_hours(r.avg_hours),
                )
                for r in rows
            ],
            unused=[
                UnusedServiceRow(service_type_id=str(s.id), name=s.name, category=s.category, prev_count=prev.get(s.id, 0))
                for s in candidates
                if s.id in orderable
            ],
        )

    return await _cached(f"unit:{scope.id}:reports:services:{part}:{d_from}:{d_to}:{int(finance)}", ServicesReportOut, build)
