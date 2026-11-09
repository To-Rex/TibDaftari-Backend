"""Regression tests for the security / concurrency review fixes (test database, rows prefixed T-fix-)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator

import pytest
from app.core.config import settings
from app.core.crypto import encrypt
from app.core.permissions import UNIT_ADMIN_PERMISSIONS
from app.infrastructure.db.models import Employee, MessagingAccount, OtpChallenge, OutboxMessage, Patient
from app.modules.messaging import service as messaging
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.helpers import (
    API,
    COUNTRY_ID,
    app_instance,
    bearer,
    make_employee,
    make_role,
    make_tree,
    own_engine,
    phone,
    seed,
    staff_token,
)

SFX = uuid.uuid4().hex[:8]
PASSWORD = "T-fix-pass-1"


@pytest.fixture(scope="module")
def ctx() -> Iterator[dict]:
    async def rows(s):
        a = await make_tree(s)
        s.add(MessagingAccount(unit_id=a.facility.id, channel="sms", provider="xabarchi", api_key_enc=encrypt("t-fix-key"), api_key_masked="t-fi••••-key"))
        sup = await make_employee(s, None, super_admin=True, login=f"t-fix-super-{SFX}", password=PASSWORD)
        adm = await make_employee(s, a.region, "admin", login=f"t-fix-admin-{SFX}", password=PASSWORD)
        victim = await make_employee(s, a.region, "admin", login=f"t-fix-victim-{SFX}", password=PASSWORD)
        custom = await make_role(s, a.region, "custom", "T-fix custom", ["reception.patient.read"])
        p = phone("99893")
        patient = Patient(country_id=COUNTRY_ID, registered_unit_id=a.facility.id, full_name=f"T-fix Bemor {SFX}", phone=p, tags=[])
        s.add(patient)
        await s.flush()
        return {
            "a": {k: str(getattr(a, k).id) for k in ("country", "region", "district", "facility")},
            "admin_role": "01a00000-0000-7000-8000-000000000002",
            "custom_role": str(custom.id),
            "super": str(sup.id),
            "admin": str(adm.id),
            "victim": str(victim.id),
            "phone": p,
            "patient": str(patient.id),
        }

    data = seed(rows)
    with TestClient(app_instance()) as client:
        data["client"] = client
        data["tokens"] = {who: staff_token(client, f"t-fix-{who}-{SFX}", PASSWORD) for who in ("super", "admin")}
        yield data


def h(ctx: dict, who: str) -> dict[str, str]:
    return bearer(ctx["tokens"][who])


async def _query(stmt: object) -> list:
    """Read rows on a private engine (the app engine belongs to the TestClient event loop)."""
    own = own_engine()
    async with AsyncSession(own, expire_on_commit=False) as s:
        rows = list((await s.execute(stmt)).scalars().all())  # type: ignore[arg-type]
    await own.dispose()
    return rows


def _employee(emp_id: str) -> Employee:
    return asyncio.run(_query(select(Employee).where(Employee.id == uuid.UUID(emp_id))))[0]


def test_failed_login_counter_persists(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    login = f"t-fix-victim-{SFX}"
    for _ in range(2):
        assert c.post(f"{API}/auth/staff/login", json={"login": login, "password": "wrong"}).status_code == 401
    emp = _employee(ctx["victim"])
    assert emp.failed_logins == 2
    # a good login resets the counter
    assert c.post(f"{API}/auth/staff/login", json={"login": login, "password": PASSWORD}).status_code == 200
    assert _employee(ctx["victim"]).failed_logins == 0


def test_otp_attempts_persist_and_lock_out(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    phone_ = ctx["phone"]
    r = c.post(f"{API}/auth/patient/otp/request", json={"phone": phone_})
    assert r.status_code == 200, r.text
    cid, code = r.json()["challengeId"], r.json()["devCode"]
    assert code, "dev mode required for this test"
    wrong = "0000" if code != "0000" else "0001"
    for _ in range(settings.otp_max_attempts):
        r = c.post(f"{API}/auth/patient/otp/verify", json={"challengeId": cid, "code": wrong, "phone": phone_})
        assert r.status_code == 401 and r.json()["error"]["code"] == "otp_invalid", r.text

    assert asyncio.run(_query(select(OtpChallenge).where(OtpChallenge.id == uuid.UUID(cid))))[0].attempts == settings.otp_max_attempts
    # even the right code is refused now (attempts exhausted → expired), either via the DB counter or the Redis limiter
    r = c.post(f"{API}/auth/patient/otp/verify", json={"challengeId": cid, "code": code, "phone": phone_})
    assert r.status_code in (401, 429), r.text
    if r.status_code == 401:
        assert r.json()["error"]["code"] == "otp_expired"


def test_otp_code_is_not_readable_from_outbox(ctx: dict) -> None:
    # the code went out through the patient's facility (its own SMS account)
    rows = asyncio.run(_query(select(OutboxMessage).where(OutboxMessage.unit_id == uuid.UUID(ctx["a"]["facility"]), OutboxMessage.kind == "otp")))
    assert rows, "the OTP request above must have enqueued an SMS (provider configured)"
    for row in rows:
        assert "****" in row.text and not any(ch.isdigit() for ch in row.text.split(":")[-1])
        assert messaging.outgoing_text(row) != row.text and messaging.outgoing_text(row).split(":")[-1].strip().isdigit()
        assert row.account_id is not None and row.status == "queued"
    c: TestClient = ctx["client"]
    listed = c.get(f"{API}/units/{ctx['a']['facility']}/outbox", params={"kind": "otp"}, headers=h(ctx, "admin")).json()["items"]
    assert listed and all("****" in m["text"] for m in listed)


def test_unit_admin_cannot_touch_superadmin_or_grant_platform(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    assert c.put(f"{API}/employees/{ctx['super']}", json={"password": "hacked-1"}, headers=h(ctx, "admin")).status_code == 403
    assert c.put(f"{API}/employees/{ctx['super']}/overrides", json={"allow": [], "deny": []}, headers=h(ctx, "admin")).status_code == 403
    assert c.put(f"{API}/employees/{ctx['admin']}/overrides", json={"allow": ["platform.unit.manage"], "deny": []}, headers=h(ctx, "admin")).status_code == 403
    r = c.put(f"{API}/roles/{ctx['custom_role']}", json={"permissions": ["reception.patient.read", "platform.unit.manage"]}, headers=h(ctx, "admin"))
    assert r.status_code == 403
    # old key names are still understood (roles saved before the port) — and the platform one is still refused
    r = c.put(f"{API}/roles/{ctx['custom_role']}", json={"permissions": ["admin.company.read", "platform.company.manage"]}, headers=h(ctx, "admin"))
    assert r.status_code == 403
    r = c.put(f"{API}/roles/{ctx['custom_role']}", json={"permissions": ["admin.company.read", "admin.unit.read"]}, headers=h(ctx, "admin"))
    assert r.status_code == 200 and r.json()["permissions"] == ["admin.unit.read"]  # stored under the v2 name, once
    # superadmin may still do it
    r = c.put(f"{API}/employees/{ctx['admin']}/overrides", json={"allow": ["platform.unit.manage"], "deny": []}, headers=h(ctx, "super"))
    assert r.status_code == 200
    # ...but the platform permission is still stripped for a non-superadmin principal
    r = c.get(f"{API}/auth/staff/me", headers=h(ctx, "admin"))
    assert r.status_code == 200 and "platform.unit.manage" not in r.json()["permissions"]
    assert c.get(f"{API}/platform/regions", params={"dateFrom": "2026-11-01", "dateTo": "2026-11-09"}, headers=h(ctx, "admin")).status_code == 403
    assert c.get(f"{API}/platform/regions", params={"dateFrom": "2026-11-01", "dateTo": "2026-11-09"}, headers=h(ctx, "super")).status_code == 200
    # ...their own subtree they may fold
    r = c.get(f"{API}/platform/regions", params={"dateFrom": "2026-11-01", "dateTo": "2026-11-09", "unitId": ctx["a"]["region"]}, headers=h(ctx, "admin"))
    assert r.status_code == 200 and r.json()["groupBy"] == "district"
    # self-read of the employee card needs no admin permission
    assert c.get(f"{API}/employees/{ctx['admin']}", headers=h(ctx, "admin")).status_code == 200


def test_template_create_active_requires_publish_and_content(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    region = ctx["a"]["region"]
    # admin has publish → empty doc still cannot be created active
    r = c.post(f"{API}/units/{region}/templates", json={"name": "T-fix tpl", "status": "active"}, headers=h(ctx, "admin"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "empty", r.text
    r = c.post(f"{API}/units/{region}/templates", json={"name": "T-fix tpl"}, headers=h(ctx, "admin"))
    assert r.status_code == 201 and r.json()["status"] == "draft"


def test_sms_settings_need_key_and_accept_settings_permission(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    region = ctx["a"]["region"]
    r = c.post(f"{API}/units/{region}/employees", json={"fullName": "T-fix Settings", "login": f"t-fix-settings-{SFX}", "roleId": ctx["admin_role"], "overrides": {"allow": [], "deny": ["admin.unit.write"]}}, headers=h(ctx, "admin"))
    assert r.status_code == 201, r.text
    hh = bearer(staff_token(c, f"t-fix-settings-{SFX}"))
    assert "admin.unit.write" not in c.get(f"{API}/auth/staff/me", headers=hh).json()["permissions"]
    # the SMS account and the settings fields are allowed with admin.settings.write; identity fields are not
    r = c.put(f"{API}/units/{region}/sms", json={"mode": "own", "apiKey": "xab_live_fix1234", "defaultPriority": "bulk"}, headers=hh)
    assert r.status_code == 200 and r.json()["defaultPriority"] == "bulk", r.text
    assert c.put(f"{API}/units/{region}", json={"documentFreeViewDays": 2}, headers=hh).status_code == 200
    assert c.put(f"{API}/units/{region}", json={"name": "X", "documentFreeViewDays": 2}, headers=hh).status_code == 403
    # own mode without any key → 422 (after the account was switched off, the saved key is gone)
    r = c.put(f"{API}/units/{region}/sms", json={"mode": "off"}, headers=hh)
    assert r.status_code == 200 and r.json()["provider"] == "none"
    r = c.put(f"{API}/units/{region}/sms", json={"mode": "own", "defaultPriority": "transactional"}, headers=hh)
    assert r.status_code == 422 and r.json()["error"]["code"] == "sms_api_key_required"
    assert c.put(f"{API}/units/{region}/sms", json={"mode": "inherited"}, headers=hh).status_code == 200
    assert UNIT_ADMIN_PERMISSIONS  # the admin role carries every unit permission; platform ones stay out of reach
