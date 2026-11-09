"""API tests for the reports module against the test database (fixture rows prefixed T-reports-).

The report of a unit covers its whole subtree (two facilities of one district here) and narrows by `unitId`."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, time, timedelta

import pytest
from app.core.timeutil import DEFAULT_TZ, today_local
from app.infrastructure.db.models import Category, Order, OrderItem, OrgUnit, OutboxMessage, Patient, Payment
from fastapi.testclient import TestClient

from tests.helpers import (
    API,
    COUNTRY_ID,
    app_instance,
    bearer,
    make_employee,
    make_facility,
    make_role,
    make_tree,
    phone,
    seed,
    staff_token,
)

SFX = uuid.uuid4().hex[:8]
PASSWORD = "T-reports-pass-1"
TODAY = today_local()
YESTERDAY = TODAY - timedelta(days=1)


class _StubStaff:
    """Minimal StaffPrincipal stand-in for the pure range-clamp test."""

    def __init__(self, *perms: str) -> None:
        self.perms = set(perms)

    def has(self, *perms: str) -> bool:
        return any(p in self.perms for p in perms)


def test_clamp_recent_window() -> None:
    """`reports.finance.recent` (registrar) sees revenue for the last 7 days only."""
    from app.modules.reports.service import _clamp_recent

    today = TODAY
    wide = (today - timedelta(days=30), today)
    recent = _StubStaff("reports.operations.read", "reports.finance.recent")
    full = _StubStaff("reports.finance.read")
    plain = _StubStaff("reports.operations.read")

    f, t = _clamp_recent(recent, *wide)
    assert f == today - timedelta(days=6) and t == today
    inside = (today - timedelta(days=3), today - timedelta(days=1))
    assert _clamp_recent(recent, *inside) == inside
    assert _clamp_recent(recent, *wide)[1] == today  # future dates stay within today
    assert _clamp_recent(full, *wide) == wide
    assert _clamp_recent(plain, *wide) == wide
    assert _clamp_recent(None, *wide) == wide


def test_reports_export_workbook_builds() -> None:
    """The Excel builder emits a valid workbook for every sheet-label language."""
    from datetime import UTC, date, datetime

    from app.modules.reports import export

    detail = [export.DetailRow(date=datetime(2026, 10, 20, 9, 0, tzinfo=UTC), number="T1-000001", patient="Bemor A", phone="+998 90 123-45-67", service="Qon tahlili", laboratory="Gematologiya", unit="T-main", price=50000, status="approved")]
    labs = [export.AggRow(name="Gematologiya", count=3, revenue=150000)]
    svc = [export.AggRow(name="Qon tahlili", count=2, revenue=100000)]
    br = [export.AggRow(name="T-main", count=3, revenue=150000)]
    emp = [export.AggRow(name="Xodim X", count=2, revenue=100000)]
    daily = [export.DayRow(date="2026-10-20", orders=2, revenue=100000)]
    for lang in ("uz", "ru", "en"):
        data = export.build_xlsx(detail, labs, svc, br, emp, daily, lang=lang, d_from=date(2026, 10, 14), d_to=date(2026, 10, 20), generated_at=datetime.now(UTC))
        assert data[:2] == b"PK" and len(data) > 4000


def _at(day, hour: int = 10) -> datetime:
    """A moment on `day` in Asia/Tashkent (stored as UTC)."""
    return datetime.combine(day, time(hour=hour), tzinfo=DEFAULT_TZ).astimezone(UTC)


@pytest.fixture(scope="module")
def ctx() -> Iterator[dict]:
    async def rows(s):
        a = await make_tree(s)
        b = await make_tree(s)
        br1 = a.facility
        br1.name = f"T-{a.tag} Markaz"
        br2 = await make_facility(s, a.district, name=f"T-{a.tag} Filial")
        viewer = await make_role(s, a.region, "viewer", "T-reports Viewer", ["reception.patient.read"])
        emp = await make_employee(s, a.region, "admin", login=f"t-reports-admin-a-{SFX}", password=PASSWORD)
        await make_employee(s, b.region, "admin", login=f"t-reports-admin-b-{SFX}", password=PASSWORD)
        await make_employee(s, a.facility, role=viewer, login=f"t-reports-viewer-a-{SFX}", password=PASSWORD)
        root = Category(owner_unit_id=a.region.id, name="T-reports Laboratoriya", color="#0ea5e9", sort_order=1)
        s.add(root)
        await s.flush()
        child = Category(owner_unit_id=a.region.id, parent_id=root.id, name="T-reports Gematologiya", sort_order=1)
        other = Category(owner_unit_id=a.region.id, name="T-reports UZI", color="#f59e0b", sort_order=2)
        s.add_all([child, other])
        await s.flush()
        p1 = Patient(country_id=COUNTRY_ID, registered_unit_id=br1.id, full_name="T-reports Bemor 1", phone=phone(), gender="male")
        p2 = Patient(country_id=COUNTRY_ID, registered_unit_id=br1.id, full_name="T-reports Bemor 2", phone=phone(), gender="female")
        s.add_all([p1, p2])
        await s.flush()

        def order(unit: OrgUnit, num: str, created: datetime, *, paid: int, status: str = "open") -> Order:
            return Order(
                unit_id=unit.id, number=f"{unit.code}-{num}", patient_id=p1.id, patient_name=p1.full_name, patient_phone=p1.phone,
                created_by_employee_id=emp.id, status=status, payment="paid" if paid else "unpaid", paid_amount=paid, created_at=created, updated_at=created,
            )

        o_today = order(br1, "000001", _at(TODAY), paid=50_000)
        o_yday = order(br2, "000002", _at(YESTERDAY), paid=30_000)
        o_cancel = order(br1, "000003", _at(TODAY, 11), paid=0, status="cancelled")
        o_old = order(br1, "000004", _at(TODAY - timedelta(days=40)), paid=99_000)
        s.add_all([o_today, o_yday, o_cancel, o_old])
        await s.flush()

        def item(o: Order, cat: Category, name: str, price: int, status: str, tech: str | None = None) -> OrderItem:
            return OrderItem(
                order_id=o.id, unit_id=o.unit_id, service_type_id=st.id, service_name=name, category_id=cat.id, category_name=cat.name,
                price=price, final_price=price, status=status, technician_name=tech, created_at=o.created_at, updated_at=o.created_at,
            )

        from app.infrastructure.db.models import ServiceType

        st = ServiceType(owner_unit_id=a.region.id, category_id=child.id, name="T-reports xizmat", price=10_000)
        s.add(st)
        await s.flush()
        s.add_all(
            [
                item(o_today, child, "T-reports UAK", 40_000, "pending"),
                item(o_today, child, "T-reports Glukoza", 10_000, "submitted", "T-reports Laborant"),
                item(o_yday, other, "T-reports UZI qorin", 30_000, "entered", "T-reports Laborant"),
                item(o_cancel, child, "T-reports UAK", 40_000, "cancelled"),
                item(o_old, other, "T-reports UZI qorin", 99_000, "approved"),
                # cancelled item inside a live order must not count anywhere
                item(o_today, other, "T-reports UZI buyrak", 5_000, "cancelled"),
            ]
        )
        s.add_all(
            [
                Payment(order_id=o_today.id, unit_id=br1.id, amount=50_000, method="cash", employee_id=emp.id, created_at=_at(TODAY), updated_at=_at(TODAY)),
                Payment(order_id=o_today.id, unit_id=br1.id, amount=7_000, method="cash", employee_id=emp.id, refunded_at=_at(TODAY, 12), created_at=_at(TODAY), updated_at=_at(TODAY)),
                Payment(order_id=o_yday.id, unit_id=br2.id, amount=30_000, method="card", employee_id=emp.id, created_at=_at(YESTERDAY), updated_at=_at(YESTERDAY)),
                OutboxMessage(unit_id=br1.id, channel="sms", kind="broadcast", to="998900000000", text="T-reports queued", status="queued", next_attempt_at=datetime.now(UTC) + timedelta(days=30)),
                OutboxMessage(unit_id=br1.id, channel="sms", kind="broadcast", to="998900000000", text="T-reports sent", status="sent"),
            ]
        )
        await s.flush()
        return {"a": str(a.region.id), "b": str(b.region.id), "district": str(a.district.id), "br1": str(br1.id), "br2": str(br2.id), "tag": a.tag}

    data = seed(rows)
    with TestClient(app_instance()) as client:
        data["client"] = client
        data["tokens"] = {who: staff_token(client, f"t-reports-{who}-{SFX}", PASSWORD) for who in ("admin-a", "admin-b", "viewer-a")}
        yield data


def h(ctx: dict, who: str) -> dict[str, str]:
    return bearer(ctx["tokens"][who])


def test_dashboard(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    cid = ctx["a"]
    rng = {"dateFrom": (TODAY - timedelta(days=6)).isoformat(), "dateTo": TODAY.isoformat()}
    r = c.get(f"{API}/units/{cid}/reports/dashboard", params=rng, headers=h(ctx, "admin-a"))
    assert r.status_code == 200, r.text
    d = r.json()
    assert set(d) == {"todayOrders", "todayRevenue", "pendingLab", "pendingApproval", "patients", "smsQueued", "trend", "byCategory"}
    assert d["todayOrders"] == 1  # cancelled order excluded
    assert d["todayRevenue"] == 50_000  # refunded payment excluded, yesterday excluded
    assert d["pendingLab"] == 2 and d["pendingApproval"] == 1
    assert d["patients"] == 2 and d["smsQueued"] == 1
    # dense trend: 7 days, today = 1 order / 50k, yesterday = 1 order / 30k, others zero
    assert len(d["trend"]) == 7 and [t["date"] for t in d["trend"]] == [(TODAY - timedelta(days=6 - i)).isoformat() for i in range(7)]
    by_date = {t["date"]: t for t in d["trend"]}
    assert by_date[TODAY.isoformat()] == {"date": TODAY.isoformat(), "orders": 1, "revenue": 50_000}
    assert by_date[YESTERDAY.isoformat()] == {"date": YESTERDAY.isoformat(), "orders": 1, "revenue": 30_000}
    assert all(t["orders"] == 0 and t["revenue"] == 0 for t in d["trend"] if t["date"] not in (TODAY.isoformat(), YESTERDAY.isoformat()))
    # byCategory rolled up to the ROOT category (child Gematologiya → Laboratoriya), cancelled items excluded, revenue desc
    assert d["byCategory"] == [
        {"name": "T-reports Laboratoriya", "count": 2, "revenue": 50_000, "color": "#0ea5e9"},
        {"name": "T-reports UZI", "count": 1, "revenue": 30_000, "color": "#f59e0b"},
    ]
    # unit filter (br2 = only yesterday's order); the district folds both facilities like the region does
    r = c.get(f"{API}/units/{cid}/reports/dashboard", params={**rng, "unitId": ctx["br2"]}, headers=h(ctx, "admin-a"))
    d2 = r.json()
    assert d2["todayOrders"] == 0 and d2["todayRevenue"] == 0 and d2["pendingLab"] == 1 and d2["pendingApproval"] == 0
    assert d2["byCategory"] == [{"name": "T-reports UZI", "count": 1, "revenue": 30_000, "color": "#f59e0b"}]
    assert c.get(f"{API}/units/{ctx['district']}/reports/dashboard", params=rng, headers=h(ctx, "admin-a")).json()["byCategory"] == d["byCategory"]
    assert c.get(f"{API}/units/{cid}/reports/dashboard", params={**rng, "unitId": ctx["b"]}, headers=h(ctx, "admin-a")).json()["todayOrders"] == 0
    # permissions + unit isolation + validation
    assert c.get(f"{API}/units/{cid}/reports/dashboard", params=rng, headers=h(ctx, "viewer-a")).status_code == 403
    assert c.get(f"{API}/units/{cid}/reports/dashboard", params=rng, headers=h(ctx, "admin-b")).status_code == 403
    assert c.get(f"{API}/units/{cid}/reports/dashboard", params={"dateFrom": "2026-1-1", "dateTo": "2026-01-02"}, headers=h(ctx, "admin-a")).status_code == 422
    assert c.get(f"{API}/units/{cid}/reports/dashboard", headers=h(ctx, "admin-a")).status_code == 422


def test_breakdown(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    cid = ctx["a"]
    rng = {"dateFrom": (TODAY - timedelta(days=6)).isoformat(), "dateTo": TODAY.isoformat()}
    r = c.get(f"{API}/units/{cid}/reports/breakdown", params={**rng, "by": "category"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 200, r.text
    assert r.json() == [
        {"name": "T-reports Gematologiya", "count": 2, "revenue": 50_000},
        {"name": "T-reports UZI", "count": 1, "revenue": 30_000},
    ]
    r = c.get(f"{API}/units/{cid}/reports/breakdown", params={**rng, "by": "service"}, headers=h(ctx, "admin-a"))
    assert [x["name"] for x in r.json()] == ["T-reports UAK", "T-reports UZI qorin", "T-reports Glukoza"]
    r = c.get(f"{API}/units/{cid}/reports/breakdown", params={**rng, "by": "unit"}, headers=h(ctx, "admin-a"))
    assert r.json() == [{"name": f"T-{ctx['tag']} Markaz", "count": 2, "revenue": 50_000}, {"name": f"T-{ctx['tag']} Filial", "count": 1, "revenue": 30_000}]
    assert c.get(f"{API}/units/{cid}/reports/breakdown", params={**rng, "by": "branch"}, headers=h(ctx, "admin-a")).status_code == 422
    r = c.get(f"{API}/units/{cid}/reports/breakdown", params={**rng, "by": "employee"}, headers=h(ctx, "admin-a"))
    assert r.json() == [{"name": "T-reports Laborant", "count": 2, "revenue": 40_000}, {"name": "—", "count": 1, "revenue": 40_000}] or r.json() == [
        {"name": "—", "count": 1, "revenue": 40_000},
        {"name": "T-reports Laborant", "count": 2, "revenue": 40_000},
    ]
    # whole-history range picks up the 40-day-old approved item; the unit filter narrows
    wide = {"dateFrom": (TODAY - timedelta(days=60)).isoformat(), "dateTo": TODAY.isoformat()}
    r = c.get(f"{API}/units/{cid}/reports/breakdown", params={**wide, "by": "service", "unitId": ctx["br1"]}, headers=h(ctx, "admin-a"))
    assert r.json() == [{"name": "T-reports UZI qorin", "count": 1, "revenue": 99_000}, {"name": "T-reports UAK", "count": 1, "revenue": 40_000}, {"name": "T-reports Glukoza", "count": 1, "revenue": 10_000}]
    assert c.get(f"{API}/units/{cid}/reports/breakdown", params={**rng, "by": "nope"}, headers=h(ctx, "admin-a")).status_code == 422
    assert c.get(f"{API}/units/{cid}/reports/breakdown", params={**rng, "by": "service"}, headers=h(ctx, "admin-b")).status_code == 403
    # the reports export builds from the same cuts
    x = c.get(f"{API}/units/{cid}/reports/export.xlsx", params={**rng, "lang": "ru"}, headers=h(ctx, "admin-a"))
    assert x.status_code == 200 and x.content[:2] == b"PK"
    assert c.get(f"{API}/units/{cid}/reports/export.xlsx", params=rng, headers=h(ctx, "viewer-a")).status_code == 403
