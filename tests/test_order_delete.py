"""API tests for cheque deletion (`DELETE /orders/{id}`, permission `reception.order.delete`)
against the real dev DB (fixture rows prefixed `T-delete-`)."""
from __future__ import annotations

import asyncio
import random
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from app.core.config import settings
from app.core.permissions import PERMISSIONS
from app.core.security import hash_password
from app.infrastructure.db.models import Branch, Category, Company, Employee, Patient, Payment, Role, ServiceType
from app.infrastructure.db.session import dispose_engine, session_scope
from app.infrastructure.redis.client import close_redis
from fastapi.testclient import TestClient
from sqlalchemy import select

RUN = uuid.uuid4().hex[:8]
API = "/api/v1"


def _phone() -> str:
    return "99897" + "".join(random.choice("0123456789") for _ in range(7))


def _fixture_rows() -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        async with session_scope() as s:
            co_a = Company(name=f"T-delete-A-{RUN}", slug=f"t-delete-a-{RUN}")
            co_b = Company(name=f"T-delete-B-{RUN}", slug=f"t-delete-b-{RUN}")
            s.add_all([co_a, co_b])
            await s.flush()
            admin_perms = [p for p in PERMISSIONS if not p.startswith("platform.")]
            role_admin = Role(company_id=co_a.id, key="admin", name="T-delete", permissions=admin_perms)
            role_limited = Role(company_id=co_a.id, key="limited", name="T-delete", permissions=["reception.patient.read"])
            role_b = Role(company_id=co_b.id, key="admin", name="T-delete", permissions=admin_perms)
            s.add_all([role_admin, role_limited, role_b])
            await s.flush()
            pw = hash_password("secret123")
            emp_a = Employee(company_id=co_a.id, full_name="T-delete Doctor", login=f"t-delete-a-{RUN}", password_hash=pw, role_id=role_admin.id)
            emp_limited = Employee(
                company_id=co_a.id,
                full_name="T-delete Limited",
                login=f"t-delete-l-{RUN}",
                password_hash=pw,
                role_id=role_limited.id,
            )
            emp_b = Employee(company_id=co_b.id, full_name="T-delete B", login=f"t-delete-b-{RUN}", password_hash=pw, role_id=role_b.id)
            branch = Branch(company_id=co_a.id, name=f"T-delete filial {RUN}", code=f"T{RUN[:4].upper()}", order_seq=0)
            patient = Patient(company_id=co_a.id, full_name=f"T-delete Bemor {RUN}", phone=_phone(), gender="male", discount_percent=0, tags=[])
            cat = Category(company_id=co_a.id, name=f"T-delete Yo‘nalish {RUN}", order=1)
            st1 = ServiceType(company_id=co_a.id, category_id=cat.id, name=f"T-delete Xizmat {RUN}", code=f"TD{RUN[:5]}", price=100000, branch_prices={})
            s.add_all([emp_a, emp_limited, emp_b, branch, patient, cat, st1])
            await s.flush()
            return {
                "co_a": str(co_a.id),
                "co_b": str(co_b.id),
                "emp_a": str(emp_a.id),
                "login_a": emp_a.login,
                "login_limited": emp_limited.login,
                "login_b": emp_b.login,
                "branch": str(branch.id),
                "branch_code": branch.code,
                "patient": str(patient.id),
                "st1": str(st1.id),
            }

    async def _main() -> dict[str, Any]:
        try:
            return await _run()
        finally:
            await close_redis()
            await dispose_engine()

    return asyncio.run(_main())


@pytest.fixture(scope="module")
def env() -> Iterator[dict[str, Any]]:
    settings.workers_enabled = False
    settings.telegram_enabled = False
    data = _fixture_rows()
    from app.main import app

    with TestClient(app) as client:
        data["client"] = client
        for key, login in (("h_a", data["login_a"]), ("h_limited", data["login_limited"]), ("h_b", data["login_b"])):
            tok = client.post(f"{API}/auth/staff/login", json={"login": login, "password": "secret123"}).json()["accessToken"]
            data[key] = {"Authorization": f"Bearer {tok}"}
        yield data


def _create(env: dict[str, Any], headers: dict[str, str], service_type_ids: list[str]) -> dict[str, Any]:
    r = env["client"].post(
        f"{API}/companies/{env['co_a']}/orders",
        json={"patientId": env["patient"], "branchId": env["branch"], "serviceTypeIds": service_type_ids},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


def _pay(env: dict[str, Any], order_id: str, headers: dict[str, str], amount: int) -> dict[str, Any]:
    r = env["client"].post(
        f"{API}/orders/{order_id}/pay",
        json={"orderId": order_id, "amount": amount, "method": "cash", "sendSms": False},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()


def _db(client: TestClient, fn: Any) -> Any:
    """Run an async DB read on the app's own event loop (shares the engine pool with the app)."""

    async def _main() -> Any:
        from app.infrastructure.db.session import session_scope as _ss

        async with _ss() as s:
            return await fn(s)

    return client.portal.call(_main)


def _patient_stats(env: dict[str, Any]) -> dict[str, Any]:
    r = env["client"].get(f"{API}/patients/{env['patient']}", headers=env["h_a"])
    assert r.status_code == 200, r.text
    return r.json()["stats"]


def _today_revenue(env: dict[str, Any]) -> int:
    r = env["client"].get(f"{API}/companies/{env['co_a']}/reports/dashboard?dateFrom=2026-01-01&dateTo=2026-12-31", headers=env["h_a"])
    assert r.status_code == 200, r.text
    return int(r.json()["todayRevenue"])


def test_delete_untouched_order(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    created = _create(env, env["h_a"], [env["st1"]])
    oid = created["order"]["id"]
    assert _patient_stats(env)["orders"] == 1
    r = c.delete(f"{API}/orders/{oid}", headers=env["h_a"])
    assert r.status_code == 200, r.text
    assert r.json()["id"] == oid
    # gone from every read, patient stats rolled back
    assert c.get(f"{API}/orders/{oid}", headers=env["h_a"]).status_code == 404
    listing = c.get(f"{API}/companies/{env['co_a']}/orders", headers=env["h_a"]).json()
    assert all(row["id"] != oid for row in listing["items"])
    assert _patient_stats(env)["orders"] == 0


def test_delete_requires_permission(env: dict[str, Any]) -> None:
    created = _create(env, env["h_a"], [env["st1"]])
    oid = created["order"]["id"]
    assert env["client"].delete(f"{API}/orders/{oid}", headers=env["h_limited"]).status_code == 403
    # still there
    assert env["client"].get(f"{API}/orders/{oid}", headers=env["h_a"]).status_code == 200


def test_delete_tenant_isolation(env: dict[str, Any]) -> None:
    created = _create(env, env["h_a"], [env["st1"]])
    oid = created["order"]["id"]
    assert env["client"].delete(f"{API}/orders/{oid}", headers=env["h_b"]).status_code == 404


def test_delete_paid_order_rolls_back_accounting(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    created = _create(env, env["h_a"], [env["st1"]])
    oid = created["order"]["id"]
    _pay(env, oid, env["h_a"], created["order"]["total"])
    assert _patient_stats(env)["totalSpent"] == created["order"]["total"]
    assert _today_revenue(env) == created["order"]["total"]
    # paid or not, a cheque without lab work is deletable
    r = c.delete(f"{API}/orders/{oid}", headers=env["h_a"])
    assert r.status_code == 200, r.text
    # dropped from accounting: dashboard revenue and patient stats rolled back
    assert _today_revenue(env) == 0
    stats = _patient_stats(env)
    assert stats["orders"] == 0 and stats["totalSpent"] == 0
    # the payment row is soft-deleted, not removed
    deleted = _db(
        c,
        lambda s: (
            s.execute(select(Payment.deleted_at).where(Payment.order_id == uuid.UUID(oid))).scalars().all()
        ),
    )
    assert len(deleted) == 1 and all(d is not None for d in deleted)


def test_delete_refunded_history_is_allowed(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    created = _create(env, env["h_a"], [env["st1"]])
    oid = created["order"]["id"]
    paid = _pay(env, oid, env["h_a"], created["order"]["total"])
    refund = c.post(f"{API}/payments/{paid['payments'][0]['id']}/refund", json={"reason": "xato"}, headers=env["h_a"])
    assert refund.status_code == 200, refund.text
    r = c.delete(f"{API}/orders/{oid}", headers=env["h_a"])
    assert r.status_code == 200, r.text
    # the refunded payment row stays in the DB (soft-deleted) — history preserved
    rows = _db(
        c,
        lambda s: (
            s.execute(
                select(Payment.refunded_at, Payment.deleted_at).where(Payment.order_id == uuid.UUID(oid))
            ).all()
        ),
    )
    assert len(rows) == 1 and rows[0][0] is not None and rows[0][1] is not None
    assert _patient_stats(env)["totalSpent"] == 0


def test_delete_blocked_once_lab_started(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    created = _create(env, env["h_a"], [env["st1"]])
    oid = created["order"]["id"]
    item_id = created["items"][0]["id"]
    r = c.put(f"{API}/items/{item_id}/values", json={"values": {"hb": 120}}, headers=env["h_a"])
    assert r.status_code == 200, r.text
    d = c.delete(f"{API}/orders/{oid}", headers=env["h_a"])
    assert d.status_code == 409
    assert d.json()["error"]["code"] == "in_progress"
