"""Reports service — dashboard summary + breakdown, cached briefly per (company, branch, range)."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import StaffPrincipal
from app.core.exceptions import ValidationError
from app.core.pagination import page_of
from app.core.schemas import Page, dump
from app.core.timeutil import day_range, today_local, utcnow
from app.infrastructure.db.models import Category
from app.infrastructure.redis import cache
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


async def _build_dashboard(session: AsyncSession, company_id: uuid.UUID, d_from: date, d_to: date, branches: list[uuid.UUID] | None) -> DashboardSummaryOut:
    today = today_local()
    t_start, t_end = day_range(today, today)
    start, end = day_range(d_from, d_to)
    counters = await repo.dashboard_counters(session, company_id, branches, t_start, t_end)
    trend_map = await repo.order_trend(session, company_id, branches, start, end)
    trend: list[TrendPoint] = []
    d = d_from
    while d <= d_to:
        orders, revenue = trend_map.get(d.isoformat(), (0, 0))
        trend.append(TrendPoint(date=d.isoformat(), orders=orders, revenue=revenue))
        d += timedelta(days=1)
    rows = await repo.items_by_category(session, company_id, branches, start, end)
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


def _branches(staff: StaffPrincipal | None, branch_id: uuid.UUID | None) -> list[uuid.UUID] | None:
    """The caller's branch scope for a report: pinned employees are confined to their branches."""
    return staff.branch_scope(branch_id) if staff else ([branch_id] if branch_id else None)


async def dashboard(session: AsyncSession, company_id: uuid.UUID, q: RangeQuery, staff: StaffPrincipal | None = None) -> DashboardSummaryOut:
    """§9 `dashboard`: today's counters + dense daily trend + top-level category split (Redis 30s)."""
    d_from, d_to, branch_id = _parse_range(q)
    branches = _branches(staff, branch_id)
    scope_key = ",".join(sorted(str(b) for b in branches)) if branches is not None else "all"
    key = f"co:{company_id}:reports:dashboard:{scope_key}:{d_from}:{d_to}"
    hit = await cache.get_json(key)
    if hit is not None:
        return DashboardSummaryOut.model_validate(hit)
    out = await _build_dashboard(session, company_id, d_from, d_to, branches)
    await cache.set_json(key, dump(out), DASHBOARD_TTL_SECONDS)
    return out


async def breakdown(session: AsyncSession, company_id: uuid.UUID, q: BreakdownQuery, staff: StaffPrincipal | None = None) -> list[BreakdownRowOut]:
    """§9 `breakdown`: non-cancelled items in range grouped by category/service/branch/employee, revenue desc."""
    d_from, d_to, branch_id = _parse_range(q)
    start, end = day_range(d_from, d_to)
    rows = await repo.breakdown(session, company_id, q.by, _branches(staff, branch_id), start, end)
    return [BreakdownRowOut(name=r.name or "—", count=int(r.count), revenue=int(r.revenue)) for r in rows]


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


def _scope_key(branches: list[uuid.UUID] | None) -> str:
    return ",".join(sorted(str(b) for b in branches)) if branches is not None else "all"


async def _cached(key: str, model: type, build):  # type: ignore[no-untyped-def]
    hit = await cache.get_json(key)
    if hit is not None:
        return model.model_validate(hit)
    out = await build()
    await cache.set_json(key, dump(out), REPORT_TTL_SECONDS)
    return out


async def patients_report(session: AsyncSession, company_id: uuid.UUID, q: RangeQuery, staff: StaffPrincipal | None = None) -> PatientReportOut:
    """Who came in the period: new vs returning, demographics, districts, most frequent, debts (finance only)."""
    d_from, d_to, branch_id = _parse_range(q)
    branches = _branches(staff, branch_id)
    finance = staff is None or staff.has("reports.finance.read")
    start, end = day_range(d_from, d_to)

    async def build() -> PatientReportOut:
        s = await repo.patient_summary(session, company_id, branches, start, end)
        gender: dict[str, int] = {"male": 0, "female": 0, "unknown": 0}
        ages: dict[str, int] = {k: 0 for k in (*AGE_GROUPS, "unknown")}
        for r in await repo.patient_demographics(session, company_id, branches, start, end):
            gender[gender_key(r.gender)] += int(r.count)
            ages[age_group(r.age)] += int(r.count)
        by_day = {r.day.isoformat(): r for r in await repo.patient_trend(session, company_id, branches, start, end)}
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
            districts=[NamedCount(name=r.name or "", count=int(r.count)) for r in await repo.patient_districts(session, company_id, branches, start, end)],
            trend=trend,
            top_patients=[
                TopPatientOut(patient_id=str(r.pid), name=r.name or "—", phone=r.phone or "", orders=int(r.orders), paid=int(r.paid) if finance else None, last_visit=r.last)
                for r in await repo.top_patients(session, company_id, branches, start, end)
            ],
        )

    return await _cached(f"co:{company_id}:reports:patients:{_scope_key(branches)}:{d_from}:{d_to}:{int(finance)}", PatientReportOut, build)


async def results_report(session: AsyncSession, company_id: uuid.UUID, q: RangeQuery, staff: StaffPrincipal | None = None) -> ResultsReportOut:
    """Results of the period's cheques: ready or not, overdue, turnaround, and whether patients got them."""
    d_from, d_to, branch_id = _parse_range(q)
    branches = _branches(staff, branch_id)
    start, end = day_range(d_from, d_to)

    async def build() -> ResultsReportOut:
        it = await repo.item_readiness(session, company_id, branches, start, end, utcnow())
        dc = await repo.document_receipt(session, company_id, branches, start, end)
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
                for r in await repo.turnaround_by_category(session, company_id, branches, start, end)
            ],
            tracking_since=TRACKING_SINCE,
        )

    return await _cached(f"co:{company_id}:reports:results:{_scope_key(branches)}:{d_from}:{d_to}", ResultsReportOut, build)


async def results_list(session: AsyncSession, company_id: uuid.UUID, q: ResultListQuery, staff: StaffPrincipal | None = None) -> Page[ResultRowOut]:
    """Paged rows behind the results report: not received / received results, or cheques still waiting."""
    d_from, d_to, branch_id = _parse_range(q)
    branches = _branches(staff, branch_id)
    start, end = day_range(d_from, d_to)
    if q.status == "waiting":
        rows, total = await repo.waiting_orders_page(session, company_id, branches, start, end, utcnow(), search=q.search, page=q.page, page_size=q.page_size)
        items = [
            ResultRowOut(
                kind="order", order_id=str(r.order_id), order_number=r.number, patient_id=str(r.patient_id), patient_name=r.patient_name, patient_phone=r.patient_phone,
                title=r.title or "", ordered_at=r.ordered_at, waiting=int(r.waiting), submitted=int(r.submitted), overdue=bool(r.overdue),
            )
            for r in rows
        ]
    else:
        rows, total = await repo.result_documents_page(session, company_id, branches, start, end, got=q.status == "received", search=q.search, page=q.page, page_size=q.page_size)
        items = [
            ResultRowOut(
                kind="document", order_id=str(r.order_id), order_number=r.number, patient_id=str(r.patient_id), patient_name=r.patient_name, patient_phone=r.patient_phone,
                title=r.title, ordered_at=r.ordered_at, document_id=str(r.document_id), ready_at=r.ready_at, viewed_at=r.viewed_at, view_count=int(r.view_count or 0),
                printed_at=r.printed_at, print_count=int(r.print_count or 0), telegram=_telegram_ok(r.deliveries), sms=_sms_status(r.deliveries),
            )
            for r in rows
        ]
    return page_of(items, q, total)


async def services_report(session: AsyncSession, company_id: uuid.UUID, q: RangeQuery, staff: StaffPrincipal | None = None) -> ServicesReportOut:
    """Which services are used most / least in the period (vs the period before), and which nobody ordered."""
    d_from, d_to, branch_id = _parse_range(q)
    branches = _branches(staff, branch_id)
    finance = staff is None or staff.has("reports.finance.read")
    start, end = day_range(d_from, d_to)
    p_from, p_to = previous_period(d_from, d_to)
    p_start, p_end = day_range(p_from, p_to)

    async def build() -> ServicesReportOut:
        rows = await repo.service_usage(session, company_id, branches, start, end)
        prev = await repo.service_counts(session, company_id, branches, p_start, p_end)
        used = {r.sid for r in rows}
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
                for s in await repo.active_services(session, company_id)
                if s.id not in used
            ],
        )

    return await _cached(f"co:{company_id}:reports:services:{_scope_key(branches)}:{d_from}:{d_to}:{int(finance)}", ServicesReportOut, build)
