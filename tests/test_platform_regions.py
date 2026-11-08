"""Platform regional view: grouping level, folding of per-branch counters into territory rows, route guard (pure — no
database)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from app.api.deps import StaffPrincipal
from app.core.exceptions import ForbiddenError, ValidationError
from app.modules.platform import router as platform_router
from app.modules.platform import service
from app.modules.platform.schemas import RegionStatsQuery, UnitMetrics
from app.modules.tenant.schemas import CompanyListQuery
from pydantic import ValidationError as PydanticValidationError

A, B, C = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def principal(*perms: str, super_admin: bool = False) -> StaffPrincipal:
    emp = SimpleNamespace(id=uuid.uuid4(), company_id=uuid.uuid4(), is_super_admin=super_admin, branch_ids=[])
    return StaffPrincipal(employee=emp, role=SimpleNamespace(key="admin"), permissions=list(perms), jti="t", token_exp=0)  # type: ignore[arg-type]


def test_default_level_is_one_below_the_filter() -> None:
    assert service.default_group_by(None, None, None) == "region"
    assert service.default_group_by(A, None, None) == "region"
    assert service.default_group_by(A, B, None) == "district"
    assert service.default_group_by(A, B, C) == "company"


def test_parse_query() -> None:
    q = RegionStatsQuery(date_from="2026-11-09", date_to="2026-11-01", region_id=str(B))
    d_from, d_to, country, region, district, group_by = service.parse_query(q)
    assert (d_from.isoformat(), d_to.isoformat()) == ("2026-11-01", "2026-11-09")  # swapped into order
    assert (country, region, district, group_by) == (None, B, None, "district")
    assert service.parse_query(RegionStatsQuery(date_from="2026-11-01", date_to="2026-11-01", group_by="company"))[5] == "company"
    with pytest.raises(ValidationError):
        service.parse_query(RegionStatsQuery(date_from="2026-11-01", date_to="2026-11-01", region_id="nope"))
    with pytest.raises(ValidationError):
        service.parse_query(RegionStatsQuery(date_from="2020-01-01", date_to="2026-11-01"))
    with pytest.raises(PydanticValidationError):  # not a calendar day
        RegionStatsQuery(date_from="2026-1-1", date_to="2026-11-01")


def test_fold_rows_sorts_sums_and_buckets_the_unassigned() -> None:
    structure = ({A: 2, B: 1, None: 1}, {A: 1, B: 1, None: 1})
    metrics = {
        "orders": {A: 10, B: 30, None: 2},
        "revenue": {A: 500_000, B: 900_000},
        "patients_new": {A: 4},
        "results_ready": {B: 7, C: 1},  # C: only old results of a branch deleted since — still a row
        "sms_sent": {None: 5},
    }
    names = {A: {"name": "Yunusobod", "parent_name": "Toshkent sh."}, B: {"name": "Chilonzor", "parent_name": "Toshkent sh."}}
    rows, unassigned, total = service.fold_rows("district", structure, metrics, names)
    assert [r.name for r in rows] == ["Chilonzor", "Yunusobod", "—"]  # revenue desc; C has no name yet
    chil = rows[0]
    assert (chil.id, chil.kind, chil.parent_name, chil.companies, chil.branches, chil.orders, chil.revenue, chil.results_ready) == (str(B), "district", "Toshkent sh.", 1, 1, 30, 900_000, 7)
    assert rows[2].id == str(C) and rows[2].results_ready == 1 and rows[2].branches == 0
    assert unassigned == UnitMetrics(companies=1, branches=1, orders=2, sms_sent=5)
    # the total includes the unassigned bucket
    assert (total.orders, total.revenue, total.patients_new, total.results_ready, total.sms_sent, total.branches) == (42, 1_400_000, 4, 8, 5, 4)


def test_fold_rows_company_level_carries_slug_and_state() -> None:
    names = {A: {"name": "Shifo", "parent_name": "Samarqand, Urgut", "slug": "shifo", "is_active": False}}
    rows, unassigned, total = service.fold_rows("company", ({A: 3}, {A: 1}), {m: {} for m in service.METRICS}, names)
    assert unassigned is None and total.companies == 1
    assert (rows[0].slug, rows[0].is_active, rows[0].parent_name, rows[0].branches) == ("shifo", False, "Samarqand, Urgut", 3)


def test_company_list_query_keeps_the_page_fields() -> None:
    q = CompanyListQuery.model_validate({"page": 2, "pageSize": 50, "search": "shifo", "regionId": str(B), "isActive": True, "unlocated": False})
    assert (q.page, q.page_size, q.search, q.region_id, q.is_active, q.unlocated, q.district_id) == (2, 50, "shifo", str(B), True, False, None)


class _ReachedServiceError(Exception):
    pass


@pytest.mark.asyncio
async def test_route_is_platform_only(monkeypatch) -> None:
    async def stop(*a, **k):
        raise _ReachedServiceError

    monkeypatch.setattr(platform_router.service, "region_stats", stop)
    q = RegionStatsQuery(date_from="2026-11-01", date_to="2026-11-09")
    with pytest.raises(_ReachedServiceError):
        await platform_router.region_stats(q, principal("platform.company.manage"), None)  # type: ignore[arg-type]
    with pytest.raises(_ReachedServiceError):
        await platform_router.region_stats(q, principal(super_admin=True), None)  # type: ignore[arg-type]
    # a clinic admin has every company permission but not the platform one
    with pytest.raises(ForbiddenError):
        await platform_router.region_stats(q, principal("admin.company.read", "reports.finance.read"), None)  # type: ignore[arg-type]


class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return self._rows

    def one(self):
        return self._rows[0]

    def scalar_one(self):
        return self._rows[0][0]


class _CompilingSession:
    """Compiles every statement for PostgreSQL (catches broken SQL construction) and answers with empty data."""

    def __init__(self) -> None:
        self.sql: list[str] = []

    async def execute(self, stmt):
        from sqlalchemy.dialects import postgresql

        self.sql.append(str(stmt.compile(dialect=postgresql.dialect())))
        text = self.sql[-1].lower()
        if "group by" in text or "count(" not in text:
            return _FakeResult([])  # grouped counters / name lookups: nothing
        if "count(distinct" in text:
            return _FakeResult([(0,)])  # distinct clinics of the scope
        return _FakeResult([(0, 0)])  # coverage pairs


@pytest.mark.asyncio
async def test_build_compiles_every_statement_and_fills_the_trend() -> None:
    session = _CompilingSession()
    out = await service._build(session, service.date(2026, 11, 1), service.date(2026, 11, 3), None, B, None, "district")  # type: ignore[arg-type]
    assert out.group_by == "district" and out.rows == [] and out.unassigned is None
    assert [p.date for p in out.trend] == ["2026-11-01", "2026-11-02", "2026-11-03"]
    assert out.coverage.companies == 0 and out.total == UnitMetrics()
    # structure, 5 metrics, distinct clinics, 2 trend queries, 2 coverage queries — names skipped (no keys)
    assert len(session.sql) == 11
    joined = "\n".join(session.sql)
    assert "branches.region_id = " in joined and "companies.deleted_at IS NULL" in joined
    assert "payments.refunded_at IS NULL" in joined and "order_items.status = " in joined and "outbox_messages.sent_at >= " in joined


@pytest.mark.asyncio
@pytest.mark.parametrize("group_by", ["country", "region", "district", "company"])
async def test_names_compile_for_every_level(group_by: str) -> None:
    from app.modules.platform import repository as repo

    session = _CompilingSession()
    assert await repo.names(session, group_by, {A}) == {}  # type: ignore[arg-type]
    assert len(session.sql) == 1
