"""API tests for the patient portal login against the real dev DB (fixture rows prefixed `T-plogin-`).

Fixture: two relatives sharing one phone in clinic A (told apart by passport), one order each; a staff member of A
with reception.patient.write and one of clinic B (tenant isolation).
"""

from __future__ import annotations

import asyncio
import random
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from app.core.config import settings
from app.core.security import hash_password
from app.infrastructure.db.models import Company, Employee, Order, Patient, Role
from app.infrastructure.db.session import dispose_engine, session_scope
from app.infrastructure.redis.client import close_redis
from fastapi.testclient import TestClient

RUN = uuid.uuid4().hex[:8]
API = "/api/v1"
PERMS = ["reception.patient.read", "reception.patient.write"]


def _phone() -> str:
    return "99894" + "".join(random.choice("0123456789") for _ in range(7))


def _order(co: uuid.UUID, patient: Patient, number: str) -> Order:
    return Order(
        company_id=co,
        branch_id=uuid.uuid4(),
        number=number,
        patient_id=patient.id,
        patient_name=patient.full_name,
        patient_phone=patient.phone,
        created_by_employee_id=uuid.uuid4(),
    )


def _fixture_rows() -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        async with session_scope() as s:
            phone = _phone()
            co_a = Company(name=f"T-plogin-A-{RUN}", slug=f"t-plogin-a-{RUN}")
            co_b = Company(name=f"T-plogin-B-{RUN}", slug=f"t-plogin-b-{RUN}")
            s.add_all([co_a, co_b])
            await s.flush()
            role_a = Role(company_id=co_a.id, key="registrator", name="T-plogin", permissions=PERMS)
            role_b = Role(company_id=co_b.id, key="registrator", name="T-plogin", permissions=PERMS)
            s.add_all([role_a, role_b])
            await s.flush()
            pw = hash_password("secret123")
            emp_a = Employee(company_id=co_a.id, full_name="T-plogin A", login=f"t-plogin-a-{RUN}", password_hash=pw, role_id=role_a.id)
            emp_b = Employee(company_id=co_b.id, full_name="T-plogin B", login=f"t-plogin-b-{RUN}", password_hash=pw, role_id=role_b.id)
            mother = Patient(company_id=co_a.id, full_name=f"Karimova T-plogin {RUN}", phone=phone, passport_number=f"AA{RUN[:7]}", tags=[])
            son = Patient(company_id=co_a.id, full_name=f"Karimov T-plogin {RUN}", phone=phone, passport_number=f"AB{RUN[:7]}", tags=[])
            s.add_all([emp_a, emp_b, mother, son])
            await s.flush()
            o_mother = _order(co_a.id, mother, f"T-PL-{RUN}-1")
            o_son = _order(co_a.id, son, f"T-PL-{RUN}-2")
            s.add_all([o_mother, o_son])
            await s.flush()
            return {
                "phone": phone,
                "login_a": emp_a.login,
                "login_b": emp_b.login,
                "mother": str(mother.id),
                "son": str(son.id),
                "o_mother": str(o_mother.id),
                "o_son": str(o_son.id),
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
    settings.otp_dev_mode = True
    mp = pytest.MonkeyPatch()
    mp.setattr(settings, "login_max_attempts", 3)
    mp.setattr(settings, "auth_rate_limit_per_minute", 1000)
    data = _fixture_rows()
    from app.main import app

    with TestClient(app) as client:
        for key in ("a", "b"):
            r = client.post(f"{API}/auth/staff/login", json={"login": data[f"login_{key}"], "password": "secret123"})
            assert r.status_code == 200, r.text
            data[f"h_{key}"] = {"Authorization": f"Bearer {r.json()['accessToken']}"}
        data["client"] = client
        yield data
    mp.undo()


def _login(c: TestClient, login: str, password: str) -> Any:
    return c.post(f"{API}/auth/patient/login", json={"login": login, "password": password})


def _bearer(r: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {r.json()['accessToken']}"}


def _overview_orders(c: TestClient, h: dict[str, str]) -> set[str]:
    r = c.get(f"{API}/portal/overview", headers=h)
    assert r.status_code == 200, r.text
    return {o["id"] for o in r.json()["orders"]}


def test_relatives_on_one_phone_each_see_their_own_results(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    r = c.put(f"{API}/patients/{env['mother']}/portal-access", json={}, headers=env["h_a"])
    assert r.status_code == 200, r.text
    mother = r.json()
    assert mother["login"].startswith("karimova") and len(mother["password"]) == 8 and mother["sms"] is None
    assert mother["patient"]["portal"]["login"] == mother["login"] and mother["patient"]["portal"]["loginSetAt"]
    env["mother_login"], env["mother_pw"] = mother["login"], mother["password"]

    taken = c.put(f"{API}/patients/{env['son']}/portal-access", json={"login": mother["login"].upper()}, headers=env["h_a"])
    assert taken.status_code == 409 and taken.json()["error"]["code"] == "login_taken"
    bad = c.put(f"{API}/patients/{env['son']}/portal-access", json={"login": "a b"}, headers=env["h_a"])
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_login"

    son_login = f"t-plogin-{RUN}"
    r = c.put(f"{API}/patients/{env['son']}/portal-access", json={"login": son_login.upper(), "password": "family-2026", "sendSms": True}, headers=env["h_a"])
    assert r.status_code == 200, r.text
    assert r.json()["login"] == son_login and r.json()["password"] == "family-2026"
    assert r.json()["sms"] == "not_configured"  # the fixture clinic has no SMS provider — nothing is stored
    env["son_login"] = son_login

    as_mother = _login(c, mother["login"].upper(), mother["password"])
    assert as_mother.status_code == 200, as_mother.text
    assert as_mother.json()["via"] == "login" and as_mother.json()["login"] == mother["login"]
    assert as_mother.json()["patientId"] == env["mother"]
    assert _overview_orders(c, _bearer(as_mother)) == {env["o_mother"]}
    assert c.get(f"{API}/portal/orders/{env['o_son']}", headers=_bearer(as_mother)).status_code == 404
    me = c.get(f"{API}/auth/patient/me", headers=_bearer(as_mother))
    assert me.status_code == 200 and me.json()["via"] == "login"

    as_son = _login(c, son_login, "family-2026")
    assert as_son.status_code == 200 and _overview_orders(c, _bearer(as_son)) == {env["o_son"]}

    # phone + SMS code works as before and still sees every patient of the phone
    req = c.post(f"{API}/auth/patient/otp/request", json={"phone": env["phone"]})
    assert req.status_code == 200, req.text
    ver = c.post(f"{API}/auth/patient/otp/verify", json={"phone": env["phone"], "code": req.json()["devCode"], "challengeId": req.json()["challengeId"]})
    assert ver.status_code == 200 and ver.json()["via"] == "otp" and ver.json()["login"] is None
    env["h_otp"] = _bearer(ver)
    assert {env["o_mother"], env["o_son"]} <= _overview_orders(c, env["h_otp"])


def test_wrong_password_and_lockout(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    assert _login(c, "no-such-login-" + RUN, "whatever").status_code == 401
    for _ in range(3):
        assert _login(c, env["son_login"], "wrong-one").status_code == 401
    locked = _login(c, env["son_login"], "family-2026")
    assert locked.status_code == 429
    # renewing at reception clears the lock
    r = c.put(f"{API}/patients/{env['son']}/portal-access", json={"password": "family-2027"}, headers=env["h_a"])
    assert r.status_code == 200 and r.json()["login"] == env["son_login"]
    assert _login(c, env["son_login"], "family-2027").status_code == 200


def test_patient_changes_the_password(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    first = _bearer(_login(c, env["mother_login"], env["mother_pw"]))
    second = _bearer(_login(c, env["mother_login"], env["mother_pw"]))
    url = f"{API}/auth/patient/password"
    assert c.post(url, json={"currentPassword": "x", "newPassword": "newpass-1"}, headers=env["h_otp"]).json()["error"]["code"] == "login_session_only"
    wrong = c.post(url, json={"currentPassword": "nope-nope", "newPassword": "newpass-1"}, headers=first)
    assert wrong.status_code == 422 and wrong.json()["error"]["code"] == "wrong_password"
    same = c.post(url, json={"currentPassword": env["mother_pw"], "newPassword": env["mother_pw"]}, headers=first)
    assert same.status_code == 422 and same.json()["error"]["code"] == "same_password"
    ok = c.post(url, json={"currentPassword": env["mother_pw"], "newPassword": "newpass-1"}, headers=first)
    assert ok.status_code == 200, ok.text
    assert c.get(f"{API}/portal/overview", headers=first).status_code == 200  # this session stays
    assert c.get(f"{API}/portal/overview", headers=second).status_code == 401  # the other one ends
    assert _login(c, env["mother_login"], env["mother_pw"]).status_code == 401
    assert _login(c, env["mother_login"], "newpass-1").status_code == 200
    env["mother_pw"], env["h_mother"] = "newpass-1", first


def test_reception_renews_and_switches_the_login_off(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    renewed = c.put(f"{API}/patients/{env['mother']}/portal-access", json={}, headers=env["h_a"])
    assert renewed.status_code == 200 and renewed.json()["login"] == env["mother_login"]
    assert renewed.json()["password"] != env["mother_pw"]
    assert c.get(f"{API}/portal/overview", headers=env["h_mother"]).status_code == 401  # old login sessions end
    assert c.get(f"{API}/portal/overview", headers=env["h_otp"]).status_code == 200  # SMS-code sessions do not

    off = c.delete(f"{API}/patients/{env['mother']}/portal-access", headers=env["h_a"])
    assert off.status_code == 200 and off.json()["portal"]["login"] is None
    assert _login(c, env["mother_login"], renewed.json()["password"]).status_code == 401
    again = c.delete(f"{API}/patients/{env['mother']}/portal-access", headers=env["h_a"])
    assert again.status_code == 200  # idempotent
    # the login is free again
    reuse = c.put(f"{API}/patients/{env['mother']}/portal-access", json={"login": env["mother_login"]}, headers=env["h_a"])
    assert reuse.status_code == 200


def test_other_clinics_cannot_touch_the_login(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    assert c.put(f"{API}/patients/{env['son']}/portal-access", json={}, headers=env["h_b"]).status_code == 404
    assert c.delete(f"{API}/patients/{env['son']}/portal-access", headers=env["h_b"]).status_code == 404
