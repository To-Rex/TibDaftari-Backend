"""Regression tests for the security / concurrency review fixes (real dev DB, rows prefixed T-fix-)."""

from __future__ import annotations

import asyncio
import random
import uuid
from collections.abc import Iterator

import pytest
from app.core.config import settings
from app.core.crypto import encrypt
from app.core.permissions import COMPANY_ADMIN_PERMISSIONS
from app.core.security import hash_password
from app.infrastructure.db.models import Company, Employee, OtpChallenge, OutboxMessage, Patient, Role
from app.infrastructure.db.session import engine, session_scope
from app.modules.messaging import service as messaging
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

settings.workers_enabled = False
settings.telegram_enabled = False

SFX = uuid.uuid4().hex[:8]
PASSWORD = "T-fix-pass-1"
API = "/api/v1"


def _phone() -> str:
    return "99893" + "".join(random.choice("0123456789") for _ in range(7))


async def _seed() -> dict[str, str]:
    async with session_scope() as s:
        a = Company(name=f"T-fix-A-{SFX}", slug=f"t-fix-a-{SFX}", sms_provider="xabarchi", sms_api_key_enc=encrypt("t-fix-key"), sms_api_key_masked="t-fi••••-key")
        s.add(a)
        await s.flush()
        platform = Role(company_id=None, key=f"t-fix-platform-{SFX}", name="T-fix platform", permissions=[], is_system=True)
        admin = Role(company_id=a.id, key="admin", name="Admin", permissions=list(COMPANY_ADMIN_PERMISSIONS), is_system=True)
        s.add_all([platform, admin])
        await s.flush()
        pw = hash_password(PASSWORD)
        sup = Employee(company_id=a.id, full_name="T-fix Super", login=f"t-fix-super-{SFX}", password_hash=pw, role_id=platform.id, is_super_admin=True)
        adm = Employee(company_id=a.id, full_name="T-fix Admin", login=f"t-fix-admin-{SFX}", password_hash=pw, role_id=admin.id)
        victim = Employee(company_id=a.id, full_name="T-fix Victim", login=f"t-fix-victim-{SFX}", password_hash=pw, role_id=admin.id)
        phone = _phone()
        patient = Patient(company_id=a.id, full_name=f"T-fix Bemor {SFX}", phone=phone, tags=[])
        s.add_all([sup, adm, victim, patient])
        await s.flush()
        ids = {"a": str(a.id), "admin_role": str(admin.id), "super": str(sup.id), "admin": str(adm.id), "victim": str(victim.id), "phone": phone, "patient": str(patient.id)}
    await engine.dispose()
    return ids


@pytest.fixture(scope="module")
def ctx() -> Iterator[dict[str, object]]:
    ids = asyncio.run(_seed())
    from app.main import app

    with TestClient(app) as client:
        tokens = {}
        for who in ("super", "admin"):
            r = client.post(f"{API}/auth/staff/login", json={"login": f"t-fix-{who}-{SFX}", "password": PASSWORD})
            assert r.status_code == 200, r.text
            tokens[who] = r.json()["accessToken"]
        yield {"client": client, "ids": ids, "tokens": tokens}


def h(ctx: dict, who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {ctx['tokens'][who]}"}


async def _query(stmt: object) -> list:
    """Read rows on a private engine (the app engine belongs to the TestClient event loop)."""
    own = create_async_engine(settings.sqlalchemy_url)
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
    emp = _employee(ctx["ids"]["victim"])
    assert emp.failed_logins == 2
    # a good login resets the counter
    assert c.post(f"{API}/auth/staff/login", json={"login": login, "password": PASSWORD}).status_code == 200
    assert _employee(ctx["ids"]["victim"]).failed_logins == 0


def test_otp_attempts_persist_and_lock_out(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    phone = ctx["ids"]["phone"]
    r = c.post(f"{API}/auth/patient/otp/request", json={"phone": phone})
    assert r.status_code == 200, r.text
    cid, code = r.json()["challengeId"], r.json()["devCode"]
    assert code, "dev mode required for this test"
    wrong = "0000" if code != "0000" else "0001"
    for _ in range(settings.otp_max_attempts):
        r = c.post(f"{API}/auth/patient/otp/verify", json={"challengeId": cid, "code": wrong, "phone": phone})
        assert r.status_code == 401 and r.json()["error"]["code"] == "otp_invalid", r.text

    assert asyncio.run(_query(select(OtpChallenge).where(OtpChallenge.id == uuid.UUID(cid))))[0].attempts == settings.otp_max_attempts
    # even the right code is refused now (attempts exhausted → expired), either via the DB counter or the Redis limiter
    r = c.post(f"{API}/auth/patient/otp/verify", json={"challengeId": cid, "code": code, "phone": phone})
    assert r.status_code in (401, 429), r.text
    if r.status_code == 401:
        assert r.json()["error"]["code"] == "otp_expired"


def test_otp_code_is_not_readable_from_outbox(ctx: dict) -> None:
    rows = asyncio.run(_query(select(OutboxMessage).where(OutboxMessage.company_id == uuid.UUID(ctx["ids"]["a"]), OutboxMessage.kind == "otp")))
    assert rows, "the OTP request above must have enqueued an SMS (provider configured)"
    for row in rows:
        assert "****" in row.text and not any(ch.isdigit() for ch in row.text.split(":")[-1])
        assert messaging.outgoing_text(row) != row.text and messaging.outgoing_text(row).split(":")[-1].strip().isdigit()
    c: TestClient = ctx["client"]
    listed = c.get(f"{API}/companies/{ctx['ids']['a']}/outbox", params={"kind": "otp"}, headers=h(ctx, "admin")).json()["items"]
    assert listed and all("****" in m["text"] for m in listed)


def test_company_admin_cannot_touch_superadmin_or_grant_platform(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    ids = ctx["ids"]
    assert c.put(f"{API}/employees/{ids['super']}", json={"password": "hacked-1"}, headers=h(ctx, "admin")).status_code == 403
    assert c.put(f"{API}/employees/{ids['super']}/overrides", json={"allow": [], "deny": []}, headers=h(ctx, "admin")).status_code == 403
    assert c.put(f"{API}/employees/{ids['admin']}/overrides", json={"allow": ["platform.company.manage"], "deny": []}, headers=h(ctx, "admin")).status_code == 403
    r = c.put(f"{API}/roles/{ids['admin_role']}", json={"permissions": [*COMPANY_ADMIN_PERMISSIONS, "platform.company.manage"]}, headers=h(ctx, "admin"))
    assert r.status_code == 403
    # superadmin may still do it
    r = c.put(f"{API}/employees/{ids['admin']}/overrides", json={"allow": ["platform.company.manage"], "deny": []}, headers=h(ctx, "super"))
    assert r.status_code == 200
    # ...but the platform permission is still stripped for a non-superadmin principal
    r = c.get(f"{API}/auth/staff/me", headers=h(ctx, "admin"))
    assert r.status_code == 200 and "platform.company.manage" not in r.json()["permissions"]
    assert c.get(f"{API}/companies", headers=h(ctx, "admin")).status_code == 403
    # self-read of the employee card needs no admin permission
    assert c.get(f"{API}/employees/{ids['admin']}", headers=h(ctx, "admin")).status_code == 200


def test_template_create_active_requires_publish_and_content(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    cid = ctx["ids"]["a"]
    # admin has publish → empty doc still cannot be created active
    r = c.post(f"{API}/companies/{cid}/templates", json={"name": "T-fix tpl", "status": "active"}, headers=h(ctx, "admin"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "empty", r.text
    r = c.post(f"{API}/companies/{cid}/templates", json={"name": "T-fix tpl"}, headers=h(ctx, "admin"))
    assert r.status_code == 201 and r.json()["status"] == "draft"


def test_sms_settings_need_key_and_accept_settings_permission(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    ids = ctx["ids"]
    r = c.post(f"{API}/companies/{ids['a']}/employees", json={"fullName": "T-fix Settings", "login": f"t-fix-settings-{SFX}", "roleId": ids["admin_role"], "overrides": {"allow": [], "deny": ["admin.company.write"]}}, headers=h(ctx, "admin"))
    assert r.status_code == 201, r.text
    tok = c.post(f"{API}/auth/staff/login", json={"login": f"t-fix-settings-{SFX}", "password": "123456"}).json()["accessToken"]
    hh = {"Authorization": f"Bearer {tok}"}
    # sms-only body is allowed with admin.settings.write; identity fields are not
    r = c.put(f"{API}/companies/{ids['a']}", json={"sms": {"provider": "xabarchi", "defaultPriority": "bulk"}}, headers=hh)
    assert r.status_code == 200 and r.json()["sms"]["defaultPriority"] == "bulk", r.text
    assert c.put(f"{API}/companies/{ids['a']}", json={"name": "X", "sms": {"provider": "xabarchi"}}, headers=hh).status_code == 403
    # provider without any key → 422
    r = c.put(f"{API}/companies/{ids['a']}", json={"sms": {"provider": "none", "defaultPriority": "transactional"}}, headers=hh)
    assert r.status_code == 200
    r = c.put(f"{API}/companies/{ids['a']}", json={"sms": {"provider": "xabarchi", "defaultPriority": "transactional"}}, headers=hh)
    assert r.status_code == 422 and r.json()["error"]["code"] == "sms_api_key_required"
