"""API tests for the reports module against the real dev DB (fixture rows prefixed T-reports-)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, time, timedelta

import pytest
from app.core.config import settings
from app.core.permissions import COMPANY_ADMIN_PERMISSIONS
from app.core.security import hash_password
from app.core.timeutil import DEFAULT_TZ, today_local
from app.infrastructure.db.models import (
    Branch,
    Category,
    Company,
    Employee,
    Order,
    OrderItem,
    OutboxMessage,
    Patient,
    Payment,
    Role,
)
from app.infrastructure.db.session import engine, session_scope
from fastapi.testclient import TestClient

settings.workers_enabled = False
settings.telegram_enabled = False

SFX = uuid.uuid4().hex[:8]
PASSWORD = "T-reports-pass-1"
TODAY = today_local()
YESTERDAY = TODAY - timedelta(days=1)


def _at(day, hour: int = 10) -> datetime:
    """A moment on `day` in Asia/Tashkent (stored as UTC)."""
    return datetime.combine(day, time(hour=hour), tzinfo=DEFAULT_TZ).astimezone(UTC)


async def _seed() -> dict[str, str]:
    async with session_scope() as s:
        a = Company(name=f"T-reports-A-{SFX}", slug=f"t-reports-a-{SFX}")
        b = Company(name=f"T-reports-B-{SFX}", slug=f"t-reports-b-{SFX}")
        s.add_all([a, b])
        await s.flush()
        admin_a = Role(company_id=a.id, key="admin", name="Admin", permissions=list(COMPANY_ADMIN_PERMISSIONS), is_system=True)
        admin_b = Role(company_id=b.id, key="admin", name="Admin", permissions=list(COMPANY_ADMIN_PERMISSIONS), is_system=True)
        viewer_a = Role(company_id=a.id, key="viewer", name="Viewer", permissions=["reception.patient.read"], is_system=False)
        s.add_all([admin_a, admin_b, viewer_a])
        await s.flush()
        pw = hash_password(PASSWORD)
        emp = Employee(company_id=a.id, full_name="T-reports Admin A", login=f"t-reports-admin-a-{SFX}", password_hash=pw, role_id=admin_a.id)
        s.add_all(
            [
                emp,
                Employee(company_id=b.id, full_name="T-reports Admin B", login=f"t-reports-admin-b-{SFX}", password_hash=pw, role_id=admin_b.id),
                Employee(company_id=a.id, full_name="T-reports Viewer A", login=f"t-reports-viewer-a-{SFX}", password_hash=pw, role_id=viewer_a.id),
            ]
        )
        br1 = Branch(company_id=a.id, name="T-reports Markaz", code=f"TR{SFX[:4]}")
        br2 = Branch(company_id=a.id, name="T-reports Filial", code=f"TS{SFX[:4]}")
        root = Category(company_id=a.id, name="T-reports Laboratoriya", color="#0ea5e9", order=1)
        s.add_all([br1, br2, root])
        await s.flush()
        child = Category(company_id=a.id, parent_id=root.id, name="T-reports Gematologiya", order=1)
        other = Category(company_id=a.id, name="T-reports UZI", color="#f59e0b", order=2)
        s.add_all([child, other])
        await s.flush()
        p1 = Patient(company_id=a.id, full_name="T-reports Bemor 1", phone=f"9989{SFX[:8]}"[:12].ljust(12, "0"), gender="male")
        p2 = Patient(company_id=a.id, full_name="T-reports Bemor 2", phone=f"9989{SFX[::-1][:8]}"[:12].ljust(12, "1"), gender="female")
        s.add_all([p1, p2])
        await s.flush()

        def order(branch: Branch, num: str, created: datetime, *, paid: int, status: str = "open") -> Order:
            return Order(
                company_id=a.id, branch_id=branch.id, number=f"{branch.code}-{num}", patient_id=p1.id, patient_name=p1.full_name, patient_phone=p1.phone,
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
                company_id=a.id, order_id=o.id, branch_id=o.branch_id, service_type_id=uuid.uuid4(), service_name=name, category_id=cat.id, category_name=cat.name,
                price=price, final_price=price, status=status, technician_name=tech, created_at=o.created_at, updated_at=o.created_at,
            )

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
                Payment(company_id=a.id, order_id=o_today.id, branch_id=br1.id, amount=50_000, method="cash", employee_id=emp.id, created_at=_at(TODAY), updated_at=_at(TODAY)),
                Payment(company_id=a.id, order_id=o_today.id, branch_id=br1.id, amount=7_000, method="cash", employee_id=emp.id, refunded_at=_at(TODAY, 12), created_at=_at(TODAY), updated_at=_at(TODAY)),
                Payment(company_id=a.id, order_id=o_yday.id, branch_id=br2.id, amount=30_000, method="card", employee_id=emp.id, created_at=_at(YESTERDAY), updated_at=_at(YESTERDAY)),
                OutboxMessage(company_id=a.id, channel="sms", kind="broadcast", to="998900000000", text="T-reports queued", status="queued", next_attempt_at=datetime.now(UTC) + timedelta(days=30)),
                OutboxMessage(company_id=a.id, channel="sms", kind="broadcast", to="998900000000", text="T-reports sent", status="sent"),
            ]
        )
        ids = {"a": str(a.id), "b": str(b.id), "br1": str(br1.id), "br2": str(br2.id)}
    await engine.dispose()
    return ids


@pytest.fixture(scope="module")
def ctx() -> Iterator[dict[str, object]]:
    ids = asyncio.run(_seed())
    with TestClient(app_instance()) as client:
        tokens = {}
        for who in ("admin-a", "admin-b", "viewer-a"):
            r = client.post("/api/v1/auth/staff/login", json={"login": f"t-reports-{who}-{SFX}", "password": PASSWORD})
            assert r.status_code == 200, r.text
            tokens[who] = r.json()["accessToken"]
        yield {"client": client, "ids": ids, "tokens": tokens}


def app_instance():
    from app.main import app

    return app


def h(ctx: dict, who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {ctx['tokens'][who]}"}


def test_dashboard(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    cid = ctx["ids"]["a"]
    rng = {"dateFrom": (TODAY - timedelta(days=6)).isoformat(), "dateTo": TODAY.isoformat()}
    r = c.get(f"/api/v1/companies/{cid}/reports/dashboard", params=rng, headers=h(ctx, "admin-a"))
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
    # branch filter (br2 = only yesterday's order)
    r = c.get(f"/api/v1/companies/{cid}/reports/dashboard", params={**rng, "branchId": ctx["ids"]["br2"]}, headers=h(ctx, "admin-a"))
    d2 = r.json()
    assert d2["todayOrders"] == 0 and d2["todayRevenue"] == 0 and d2["pendingLab"] == 1 and d2["pendingApproval"] == 0
    assert d2["byCategory"] == [{"name": "T-reports UZI", "count": 1, "revenue": 30_000, "color": "#f59e0b"}]
    # permissions + tenant isolation + validation
    assert c.get(f"/api/v1/companies/{cid}/reports/dashboard", params=rng, headers=h(ctx, "viewer-a")).status_code == 403
    assert c.get(f"/api/v1/companies/{cid}/reports/dashboard", params=rng, headers=h(ctx, "admin-b")).status_code == 403
    assert c.get(f"/api/v1/companies/{cid}/reports/dashboard", params={"dateFrom": "2026-1-1", "dateTo": "2026-01-02"}, headers=h(ctx, "admin-a")).status_code == 422
    assert c.get(f"/api/v1/companies/{cid}/reports/dashboard", headers=h(ctx, "admin-a")).status_code == 422


def test_breakdown(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    cid = ctx["ids"]["a"]
    rng = {"dateFrom": (TODAY - timedelta(days=6)).isoformat(), "dateTo": TODAY.isoformat()}
    r = c.get(f"/api/v1/companies/{cid}/reports/breakdown", params={**rng, "by": "category"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 200, r.text
    assert r.json() == [
        {"name": "T-reports Gematologiya", "count": 2, "revenue": 50_000},
        {"name": "T-reports UZI", "count": 1, "revenue": 30_000},
    ]
    r = c.get(f"/api/v1/companies/{cid}/reports/breakdown", params={**rng, "by": "service"}, headers=h(ctx, "admin-a"))
    assert [x["name"] for x in r.json()] == ["T-reports UAK", "T-reports UZI qorin", "T-reports Glukoza"]
    r = c.get(f"/api/v1/companies/{cid}/reports/breakdown", params={**rng, "by": "branch"}, headers=h(ctx, "admin-a"))
    assert r.json() == [{"name": "T-reports Markaz", "count": 2, "revenue": 50_000}, {"name": "T-reports Filial", "count": 1, "revenue": 30_000}]
    r = c.get(f"/api/v1/companies/{cid}/reports/breakdown", params={**rng, "by": "employee"}, headers=h(ctx, "admin-a"))
    assert r.json() == [{"name": "T-reports Laborant", "count": 2, "revenue": 40_000}, {"name": "—", "count": 1, "revenue": 40_000}] or r.json() == [
        {"name": "—", "count": 1, "revenue": 40_000},
        {"name": "T-reports Laborant", "count": 2, "revenue": 40_000},
    ]
    # whole-history range picks up the 40-day-old approved item; branch filter narrows
    wide = {"dateFrom": (TODAY - timedelta(days=60)).isoformat(), "dateTo": TODAY.isoformat()}
    r = c.get(f"/api/v1/companies/{cid}/reports/breakdown", params={**wide, "by": "service", "branchId": ctx["ids"]["br1"]}, headers=h(ctx, "admin-a"))
    assert r.json() == [{"name": "T-reports UZI qorin", "count": 1, "revenue": 99_000}, {"name": "T-reports UAK", "count": 1, "revenue": 40_000}, {"name": "T-reports Glukoza", "count": 1, "revenue": 10_000}]
    assert c.get(f"/api/v1/companies/{cid}/reports/breakdown", params={**rng, "by": "nope"}, headers=h(ctx, "admin-a")).status_code == 422
    assert c.get(f"/api/v1/companies/{cid}/reports/breakdown", params={**rng, "by": "service"}, headers=h(ctx, "admin-b")).status_code == 403
