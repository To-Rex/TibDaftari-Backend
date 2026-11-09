"""API tests for cheque deletion (`DELETE /orders/{id}`, permission `reception.order.delete`)
against the test database (fixture rows prefixed `T-delete-`)."""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from app.infrastructure.db.models import Category, Patient, Payment, ServiceType
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.helpers import (
    API,
    COUNTRY_ID,
    app_instance,
    bearer,
    db,
    make_employee,
    make_role,
    make_tree,
    phone,
    seed,
    staff_token,
)

RUN = uuid.uuid4().hex[:8]


async def _fixture_rows(s) -> dict[str, Any]:
    a = await make_tree(s)
    b = await make_tree(s)
    limited = await make_role(s, a.facility, "limited", "T-delete Limited", ["reception.patient.read"])
    emp_a = await make_employee(s, a.facility, "admin", login=f"t-delete-a-{RUN}", password="secret123", full_name="T-delete Doctor")
    emp_limited = await make_employee(s, a.facility, role=limited, login=f"t-delete-l-{RUN}", password="secret123", full_name="T-delete Limited")
    emp_b = await make_employee(s, b.facility, "admin", login=f"t-delete-b-{RUN}", password="secret123", full_name="T-delete B")
    patient = Patient(country_id=COUNTRY_ID, registered_unit_id=a.facility.id, full_name=f"T-delete Bemor {RUN}", phone=phone("99897"), gender="male", discount_percent=0, tags=[])
    cat = Category(owner_unit_id=a.facility.id, name=f"T-delete Yo‘nalish {RUN}", sort_order=1)
    s.add_all([patient, cat])
    await s.flush()
    st1 = ServiceType(owner_unit_id=a.facility.id, category_id=cat.id, name=f"T-delete Xizmat {RUN}", code=f"TD{RUN[:5]}", price=100000)
    s.add(st1)
    await s.flush()
    return {
        "facility": str(a.facility.id),
        "emp_a": str(emp_a.id),
        "login_a": emp_a.login,
        "login_limited": emp_limited.login,
        "login_b": emp_b.login,
        "code": a.facility.code,
        "patient": str(patient.id),
        "st1": str(st1.id),
    }


@pytest.fixture(scope="module")
def env() -> Iterator[dict[str, Any]]:
    data = seed(_fixture_rows)
    with TestClient(app_instance()) as client:
        data["client"] = client
        for key, login in (("h_a", data["login_a"]), ("h_limited", data["login_limited"]), ("h_b", data["login_b"])):
            data[key] = bearer(staff_token(client, login, "secret123"))
        yield data


def _create(env: dict[str, Any], headers: dict[str, str], service_type_ids: list[str]) -> dict[str, Any]:
    r = env["client"].post(
        f"{API}/units/{env['facility']}/orders",
        json={"patientId": env["patient"], "serviceTypeIds": service_type_ids},
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


def _patient_stats(env: dict[str, Any]) -> dict[str, Any]:
    r = env["client"].get(f"{API}/patients/{env['patient']}", headers=env["h_a"])
    assert r.status_code == 200, r.text
    return r.json()["stats"]


_RANGES = iter(range(1, 28))


def _today_revenue(env: dict[str, Any]) -> int:
    """`todayRevenue` is independent of the range — a fresh range per call keeps the 30s dashboard cache out of the way."""
    day = next(_RANGES)
    r = env["client"].get(f"{API}/units/{env['facility']}/reports/dashboard?dateFrom=2026-01-{day:02d}&dateTo=2026-12-31", headers=env["h_a"])
    assert r.status_code == 200, r.text
    return int(r.json()["todayRevenue"])


def test_delete_untouched_order(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    before = _patient_stats(env)["orders"]
    created = _create(env, env["h_a"], [env["st1"]])
    oid = created["order"]["id"]
    assert _patient_stats(env)["orders"] == before + 1
    r = c.delete(f"{API}/orders/{oid}", headers=env["h_a"])
    assert r.status_code == 200, r.text
    assert r.json()["id"] == oid
    # gone from every read, patient stats rolled back
    assert c.get(f"{API}/orders/{oid}", headers=env["h_a"]).status_code == 404
    listing = c.get(f"{API}/units/{env['facility']}/orders", headers=env["h_a"]).json()
    assert all(row["id"] != oid for row in listing["items"])
    assert _patient_stats(env)["orders"] == before


def test_delete_requires_permission(env: dict[str, Any]) -> None:
    created = _create(env, env["h_a"], [env["st1"]])
    oid = created["order"]["id"]
    assert env["client"].delete(f"{API}/orders/{oid}", headers=env["h_limited"]).status_code == 403
    # still there
    assert env["client"].get(f"{API}/orders/{oid}", headers=env["h_a"]).status_code == 200


def test_delete_unit_isolation(env: dict[str, Any]) -> None:
    created = _create(env, env["h_a"], [env["st1"]])
    oid = created["order"]["id"]
    assert env["client"].delete(f"{API}/orders/{oid}", headers=env["h_b"]).status_code == 403
    assert env["client"].get(f"{API}/orders/{oid}", headers=env["h_a"]).status_code == 200


def test_delete_paid_order_rolls_back_accounting(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    before, revenue_before = _patient_stats(env), _today_revenue(env)
    created = _create(env, env["h_a"], [env["st1"]])
    oid = created["order"]["id"]
    total = created["order"]["total"]
    _pay(env, oid, env["h_a"], total)
    assert _patient_stats(env)["totalSpent"] == before["totalSpent"] + total
    assert _today_revenue(env) == revenue_before + total
    # paid or not, a cheque without lab work is deletable
    r = c.delete(f"{API}/orders/{oid}", headers=env["h_a"])
    assert r.status_code == 200, r.text
    # dropped from accounting: dashboard revenue and patient stats rolled back
    assert _today_revenue(env) == revenue_before
    stats = _patient_stats(env)
    assert stats["orders"] == before["orders"] and stats["totalSpent"] == before["totalSpent"]

    # the payment row is soft-deleted, not removed
    async def payments(s: Any) -> list:
        return (await s.execute(select(Payment.deleted_at).where(Payment.order_id == uuid.UUID(oid)))).scalars().all()

    deleted = db(c, payments)
    assert len(deleted) == 1 and all(d is not None for d in deleted)


def test_delete_refunded_history_is_allowed(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    spent_before = _patient_stats(env)["totalSpent"]
    created = _create(env, env["h_a"], [env["st1"]])
    oid = created["order"]["id"]
    paid = _pay(env, oid, env["h_a"], created["order"]["total"])
    refund = c.post(f"{API}/payments/{paid['payments'][0]['id']}/refund", json={"reason": "xato"}, headers=env["h_a"])
    assert refund.status_code == 200, refund.text
    r = c.delete(f"{API}/orders/{oid}", headers=env["h_a"])
    assert r.status_code == 200, r.text

    # the refunded payment row stays in the DB (soft-deleted) — history preserved
    async def payments(s: Any) -> list:
        return (await s.execute(select(Payment.refunded_at, Payment.deleted_at).where(Payment.order_id == uuid.UUID(oid)))).all()

    rows = db(c, payments)
    assert len(rows) == 1 and rows[0][0] is not None and rows[0][1] is not None
    assert _patient_stats(env)["totalSpent"] == spent_before


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
