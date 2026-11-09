"""Platform territory view: grouping level, folding of per-facility counters into territory rows, access rules and
the SQL it builds (pure — no database)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from app.api.deps import StaffPrincipal
from app.core.exceptions import ForbiddenError, ValidationError
from app.modules.platform import service
from app.modules.platform.schemas import RegionStatsQuery, UnitMetrics
from pydantic import ValidationError as PydanticValidationError

A, B, C = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def principal(*perms: str, super_admin: bool = False, units: tuple[uuid.UUID, ...] = ()) -> StaffPrincipal:
    emp = SimpleNamespace(id=uuid.uuid4(), unit_id=units[0] if units else None, is_super_admin=super_admin)
    return StaffPrincipal(employee=emp, role=SimpleNamespace(key="admin"), permissions=list(perms), jti="t", token_exp=0, unit_ids=list(units), allowed_unit_ids=frozenset(units))  # type: ignore[arg-type]


def test_default_level_is_one_below_the_filter() -> None:
    assert service.default_group_by(None, None, None) == "region"
    assert service.default_group_by(A, None, None) == "region"
    assert service.default_group_by(A, B, None) == "district"
    assert service.default_group_by(A, B, C) == "unit"
    # a root unit of the tree counts with its own level: a region node folds by district, a district node by facility
    assert service.default_group_by(None, None, None, "country") == "region"
    assert service.default_group_by(None, None, None, "region") == "district"
    assert service.default_group_by(None, None, None, "district") == "unit"
    assert service.default_group_by(None, None, None, "facility") == "unit"
    # the narrower of the two wins
    assert service.default_group_by(A, B, None, "country") == "district"


def test_parse_query() -> None:
    q = RegionStatsQuery(date_from="2026-11-09", date_to="2026-11-01", region_id=str(B))
    parsed = service.parse_query(q)
    assert (parsed.date_from.isoformat(), parsed.date_to.isoformat()) == ("2026-11-01", "2026-11-09")  # swapped into order
    assert (parsed.country_id, parsed.region_id, parsed.district_id, parsed.unit_id, parsed.group_by) == (None, B, None, None, None)
    assert parsed.resolve_group_by() == "district" and parsed.resolve_group_by("district") == "unit"
    explicit = service.parse_query(RegionStatsQuery(date_from="2026-11-01", date_to="2026-11-01", group_by="unit", unit_id=str(C)))
    assert explicit.group_by == "unit" and explicit.unit_id == C and explicit.resolve_group_by("country") == "unit"
    with pytest.raises(ValidationError):
        service.parse_query(RegionStatsQuery(date_from="2026-11-01", date_to="2026-11-01", region_id="nope"))
    with pytest.raises(ValidationError):
        service.parse_query(RegionStatsQuery(date_from="2026-11-01", date_to="2026-11-01", unit_id="nope"))
    with pytest.raises(ValidationError):
        service.parse_query(RegionStatsQuery(date_from="2020-01-01", date_to="2026-11-01"))
    with pytest.raises(PydanticValidationError):  # not a calendar day
        RegionStatsQuery(date_from="2026-1-1", date_to="2026-11-01")
    with pytest.raises(PydanticValidationError):  # `company` is no level of the tree any more
        RegionStatsQuery(date_from="2026-11-01", date_to="2026-11-01", group_by="company")


def test_fold_rows_sorts_and_sums() -> None:
    structure = {A: 2, B: 1}
    metrics = {
        "orders": {A: 10, B: 30},
        "revenue": {A: 500_000, B: 900_000},
        "patients_new": {A: 4},
        "results_ready": {B: 7, C: 1},  # C: only old results of a facility closed since — still a row
        "sms_sent": {A: 5},
    }
    names = {A: {"name": "Yunusobod", "parent_name": "Toshkent sh.", "unit_id": uuid.uuid4()}, B: {"name": "Chilonzor", "parent_name": "Toshkent sh.", "unit_id": None}}
    rows, total = service.fold_rows("district", structure, metrics, names)
    assert [r.name for r in rows] == ["Chilonzor", "Yunusobod", "—"]  # revenue desc; C has no name yet
    chil = rows[0]
    assert (chil.id, chil.kind, chil.parent_name, chil.units, chil.orders, chil.revenue, chil.results_ready, chil.unit_id) == (str(B), "district", "Toshkent sh.", 1, 30, 900_000, 7, None)
    assert rows[1].unit_id == str(names[A]["unit_id"]) and rows[1].sms_sent == 5
    assert rows[2].id == str(C) and rows[2].results_ready == 1 and rows[2].units == 0
    assert total == UnitMetrics(units=3, orders=40, revenue=1_400_000, patients_new=4, results_ready=8, sms_sent=5)
    # every facility carries every geo key: there is no "unassigned" bucket (a None key only adds to the total)
    rows2, total2 = service.fold_rows("region", {None: 1}, {m: {} for m in service.METRICS}, {})
    assert rows2 == [] and total2.units == 1


def test_fold_rows_facility_level_carries_code_kind_and_state() -> None:
    names = {A: {"name": "Shifo", "parent_name": "Samarqand, Urgut", "unit_id": A, "code": "SH", "facility_kind": "clinic", "is_active": False}}
    rows, total = service.fold_rows("unit", {A: 1}, {m: {} for m in service.METRICS}, names)
    assert total.units == 1
    assert (rows[0].code, rows[0].facility_kind, rows[0].is_active, rows[0].parent_name, rows[0].unit_id, rows[0].kind) == ("SH", "clinic", False, "Samarqand, Urgut", str(A), "unit")


class _ReachedBuildError(Exception):
    pass


@pytest.mark.asyncio
async def test_whole_tree_is_platform_only_and_a_unit_needs_the_scope(monkeypatch) -> None:
    async def stop(*a, **k):
        raise _ReachedBuildError

    async def no_cache(*a, **k):
        return None

    monkeypatch.setattr(service, "_build", stop)
    monkeypatch.setattr(service.cache, "get_json", no_cache)
    q = RegionStatsQuery(date_from="2026-11-01", date_to="2026-11-09")
    with pytest.raises(_ReachedBuildError):
        await service.region_stats(None, q, principal("platform.unit.manage"))  # type: ignore[arg-type]
    with pytest.raises(_ReachedBuildError):
        await service.region_stats(None, q, principal(super_admin=True))  # type: ignore[arg-type]
    # a unit administrator has every unit permission but not the platform one
    with pytest.raises(ForbiddenError):
        await service.region_stats(None, q, principal("admin.unit.read", "reports.finance.read"))  # type: ignore[arg-type]

    # with a `unitId` inside their scope they fold their own subtree
    unit = SimpleNamespace(id=A, level="region")

    async def get_unit(_s, uid):
        return unit if uid == A else None

    monkeypatch.setattr(service.units_repo, "get_unit", get_unit)
    mine = RegionStatsQuery(date_from="2026-11-01", date_to="2026-11-09", unit_id=str(A))
    with pytest.raises(_ReachedBuildError):
        await service.region_stats(None, mine, principal("reports.finance.read", units=(A,)))  # type: ignore[arg-type]
    with pytest.raises(ForbiddenError):
        await service.region_stats(None, mine, principal("reports.finance.read", units=(B,)))  # type: ignore[arg-type]


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
        return _FakeResult([])


@pytest.mark.asyncio
async def test_build_compiles_every_statement_and_fills_the_trend() -> None:
    session = _CompilingSession()
    parsed = service.parse_query(RegionStatsQuery(date_from="2026-11-01", date_to="2026-11-03", region_id=str(B), unit_id=str(A)))
    out = await service._build(session, parsed, "district")  # type: ignore[arg-type]
    assert out.group_by == "district" and out.rows == [] and out.total == UnitMetrics()
    assert [p.date for p in out.trend] == ["2026-11-01", "2026-11-02", "2026-11-03"]
    # structure, 5 metrics, 2 trend queries — names skipped (no keys)
    assert len(session.sql) == 8
    joined = "\n".join(session.sql)
    assert "org_units.level = " in joined and "org_units.region_id = " in joined and "org_unit_closure.ancestor_id = " in joined
    assert "payments.refunded_at IS NULL" in joined and "result_documents.status = " in joined and "outbox_messages.sent_at >= " in joined
    assert "patients.registered_unit_id" in joined and "orders.deleted_at IS NULL" in joined


@pytest.mark.asyncio
@pytest.mark.parametrize("group_by", ["country", "region", "district", "unit"])
async def test_names_compile_for_every_level(group_by: str) -> None:
    from app.modules.platform import repository as repo

    session = _CompilingSession()
    assert await repo.names(session, group_by, {A}) == {}  # type: ignore[arg-type]
    assert len(session.sql) == 1
    assert ("org_units.code" in session.sql[0]) == (group_by == "unit")
