"""API tests for the patient portal login against the test database (fixture rows prefixed `T-plogin-`).

Fixture: two relatives sharing one phone registered at facility A (told apart by passport), one order each; a staff
member of A with reception.patient.write and one of facility B (unit isolation).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from app.core.config import settings
from app.infrastructure.db.models import Order, OrgUnit, Patient
from fastapi.testclient import TestClient

from tests.helpers import API, COUNTRY_ID, app_instance, bearer, make_employee, make_tree, phone, seed, staff_token

RUN = uuid.uuid4().hex[:8]


def _order(facility: OrgUnit, patient: Patient, number: str, employee_id: uuid.UUID) -> Order:
    return Order(
        unit_id=facility.id,
        number=number,
        patient_id=patient.id,
        patient_name=patient.full_name,
        patient_phone=patient.phone,
        created_by_employee_id=employee_id,
    )


async def _fixture_rows(s) -> dict[str, Any]:
    shared = phone("99894")
    a = await make_tree(s)
    b = await make_tree(s)
    emp_a = await make_employee(s, a.facility, "registrator", login=f"t-plogin-a-{RUN}", password="secret123")
    emp_b = await make_employee(s, b.facility, "registrator", login=f"t-plogin-b-{RUN}", password="secret123")
    mother = Patient(country_id=COUNTRY_ID, registered_unit_id=a.facility.id, full_name=f"Karimova T-plogin {RUN}", phone=shared, passport_number=f"AA{RUN[:7]}", tags=[])
    son = Patient(country_id=COUNTRY_ID, registered_unit_id=a.facility.id, full_name=f"Karimov T-plogin {RUN}", phone=shared, passport_number=f"AB{RUN[:7]}", tags=[])
    s.add_all([mother, son])
    await s.flush()
    o_mother = _order(a.facility, mother, f"{a.facility.code}-000001", emp_a.id)
    o_son = _order(a.facility, son, f"{a.facility.code}-000002", emp_a.id)
    s.add_all([o_mother, o_son])
    await s.flush()
    return {
        "phone": shared,
        "login_a": emp_a.login,
        "login_b": emp_b.login,
        "mother": str(mother.id),
        "son": str(son.id),
        "o_mother": str(o_mother.id),
        "o_son": str(o_son.id),
    }


@pytest.fixture(scope="module")
def env() -> Iterator[dict[str, Any]]:
    mp = pytest.MonkeyPatch()
    mp.setattr(settings, "login_max_attempts", 3)
    mp.setattr(settings, "auth_rate_limit_per_minute", 1000)
    data = seed(_fixture_rows)
    with TestClient(app_instance()) as client:
        for key in ("a", "b"):
            data[f"h_{key}"] = bearer(staff_token(client, data[f"login_{key}"], "secret123"))
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
    assert r.json()["sms"] == "not_configured"  # the fixture facility has no SMS provider — nothing is stored
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


def test_other_facilities_cannot_touch_the_login(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    assert c.put(f"{API}/patients/{env['son']}/portal-access", json={}, headers=env["h_b"]).status_code == 404
    assert c.delete(f"{API}/patients/{env['son']}/portal-access", headers=env["h_b"]).status_code == 404
