"""API tests for the tenant module against the real dev DB (fixture rows prefixed T-tenant-)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator

import pytest
from app.core.config import settings
from app.core.permissions import COMPANY_ADMIN_PERMISSIONS
from app.core.security import hash_password
from app.infrastructure.db.models import Company, Employee, Role
from app.infrastructure.db.session import engine, session_scope
from app.modules.messaging.xabarchi import ProviderResult, XabarchiError
from app.modules.tenant import service as tenant_service
from fastapi.testclient import TestClient

settings.workers_enabled = False
settings.telegram_enabled = False

SFX = uuid.uuid4().hex[:8]
PASSWORD = "T-tenant-pass-1"


async def _seed() -> dict[str, str]:
    async with session_scope() as s:
        a = Company(name=f"T-tenant-A-{SFX}", slug=f"t-tenant-a-{SFX}", phone="998901112233")
        b = Company(name=f"T-tenant-B-{SFX}", slug=f"t-tenant-b-{SFX}")
        s.add_all([a, b])
        await s.flush()
        platform = Role(company_id=None, key=f"t-tenant-platform-{SFX}", name="T-tenant platform", permissions=[], is_system=True)
        admin_a = Role(company_id=a.id, key="admin", name="Admin", permissions=list(COMPANY_ADMIN_PERMISSIONS), is_system=True)
        admin_b = Role(company_id=b.id, key="admin", name="Admin", permissions=list(COMPANY_ADMIN_PERMISSIONS), is_system=True)
        s.add_all([platform, admin_a, admin_b])
        await s.flush()
        pw = hash_password(PASSWORD)
        s.add_all(
            [
                Employee(company_id=a.id, full_name="T-tenant Super", login=f"t-tenant-super-{SFX}", password_hash=pw, role_id=platform.id, is_super_admin=True),
                Employee(company_id=a.id, full_name="T-tenant Admin A", login=f"t-tenant-admin-a-{SFX}", password_hash=pw, role_id=admin_a.id),
                Employee(company_id=b.id, full_name="T-tenant Admin B", login=f"t-tenant-admin-b-{SFX}", password_hash=pw, role_id=admin_b.id),
            ]
        )
        ids = {"a": str(a.id), "b": str(b.id)}
    await engine.dispose()
    return ids


@pytest.fixture(scope="module")
def ctx() -> Iterator[dict[str, object]]:
    ids = asyncio.run(_seed())
    with TestClient(app_instance()) as client:
        tokens = {}
        for who in ("super", "admin-a", "admin-b"):
            r = client.post("/api/v1/auth/staff/login", json={"login": f"t-tenant-{who}-{SFX}", "password": PASSWORD})
            assert r.status_code == 200, r.text
            tokens[who] = r.json()["accessToken"]
        yield {"client": client, "ids": ids, "tokens": tokens}


def app_instance():
    from app.main import app

    return app


def h(ctx: dict, who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {ctx['tokens'][who]}"}


def test_list_companies_superadmin_only(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    r = c.get("/api/v1/companies", params={"search": f"t-tenant-a-{SFX}".replace("t-tenant-a", "T-tenant-A"), "pageSize": 5}, headers=h(ctx, "super"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1 and body["totalPages"] == 1
    item = body["items"][0]
    assert item["id"] == ctx["ids"]["a"]
    assert item["sms"] == {"provider": "none", "apiKeyMasked": None, "defaultPriority": "transactional", "senderNote": None}
    assert item["telegram"] == {"botUsername": None, "connected": False}
    assert item["branchCount"] == 0 and item["employeeCount"] == 2
    assert "settings" not in item
    assert c.get("/api/v1/companies", headers=h(ctx, "admin-a")).status_code == 403


def test_get_company_tenant_isolation(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    assert c.get(f"/api/v1/companies/{ctx['ids']['a']}", headers=h(ctx, "admin-a")).status_code == 200
    r = c.get(f"/api/v1/companies/{ctx['ids']['b']}", headers=h(ctx, "admin-a"))
    assert r.status_code == 403 and r.json()["error"]["message"] == "Ruxsat yo‘q"
    r = c.get(f"/api/v1/companies/{uuid.uuid4()}", headers=h(ctx, "super"))
    assert r.status_code == 404 and r.json()["error"]["message"] == "Kompaniya topilmadi"


def test_update_company_sms_and_templates(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    cid = ctx["ids"]["a"]
    body = {"legalName": "T-tenant A MChJ", "sms": {"provider": "xabarchi", "apiKey": "xab_live_abcdef7f2a", "defaultPriority": "urgent", "senderNote": "note"}, "smsTemplates": {"payment_receipt": "Chek {order} — {amount}", "reminder": ""}}
    r = c.put(f"/api/v1/companies/{cid}", json=body, headers=h(ctx, "admin-a"))
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["legalName"] == "T-tenant A MChJ" and d["name"] == f"T-tenant-A-{SFX}"
    assert d["sms"] == {"provider": "xabarchi", "apiKeyMasked": "xab_live_••••••••7f2a", "defaultPriority": "urgent", "senderNote": "note"}
    assert d["smsTemplates"] == {"payment_receipt": "Chek {order} — {amount}", "result_ready": None, "reminder": None}
    assert "abcdef" not in r.text
    # keeps the key when apiKey omitted, clears when provider none
    r = c.put(f"/api/v1/companies/{cid}", json={"sms": {"provider": "xabarchi", "defaultPriority": "bulk", "apiKeyMasked": "xab_live_••••••••7f2a"}}, headers=h(ctx, "admin-a"))
    assert r.json()["sms"]["apiKeyMasked"] == "xab_live_••••••••7f2a" and r.json()["sms"]["defaultPriority"] == "bulk"
    r = c.get(f"/api/v1/companies/{cid}", headers=h(ctx, "admin-a"))
    assert r.json()["sms"]["apiKeyMasked"] == "xab_live_••••••••7f2a"
    r = c.put(f"/api/v1/companies/{cid}", json={"sms": {"provider": "none", "defaultPriority": "transactional"}}, headers=h(ctx, "admin-a"))
    assert r.json()["sms"] == {"provider": "none", "apiKeyMasked": None, "defaultPriority": "transactional", "senderNote": None}
    # slug conflict + admin B cannot touch A
    r = c.put(f"/api/v1/companies/{cid}", json={"slug": f"t-tenant-b-{SFX}"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 409
    assert c.put(f"/api/v1/companies/{cid}", json={"name": "x"}, headers=h(ctx, "admin-b")).status_code == 403


def test_create_company_superadmin(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    r = c.post("/api/v1/companies", json={"name": f"T-tenant-C-{SFX}"}, headers=h(ctx, "super"))
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["slug"] == f"t-tenant-c-{SFX}" and d["locale"] == "uz" and d["isActive"] is True
    assert d["sms"]["provider"] == "none" and d["branchCount"] == 0
    assert c.post("/api/v1/companies", json={"name": "T-tenant-C2", "slug": f"t-tenant-c-{SFX}"}, headers=h(ctx, "super")).status_code == 409
    assert c.post("/api/v1/companies", json={"name": "T-tenant-C3"}, headers=h(ctx, "admin-a")).status_code == 403


def test_branches_crud(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    cid = ctx["ids"]["a"]
    r = c.post(f"/api/v1/companies/{cid}/branches", json={"name": "Markaz", "code": "tm", "orderSeq": 99}, headers=h(ctx, "admin-a"))
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["code"] == "TM" and b["orderSeq"] == 0 and b["timezone"] == "Asia/Tashkent" and b["companyId"] == cid
    r = c.post(f"/api/v1/companies/{cid}/branches", json={"name": "Dup", "code": "TM"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 409 and r.json()["error"]["message"] == "Bu filial kodi band"
    r = c.put(f"/api/v1/branches/{b['id']}", json={"name": "Markaz 2", "phone": "998901234567", "isActive": False}, headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and r.json()["name"] == "Markaz 2" and r.json()["isActive"] is False
    r = c.put(f"/api/v1/branches/{uuid.uuid4()}", json={"name": "x"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 404 and r.json()["error"]["message"] == "Filial topilmadi"
    # tenant isolation: admin B sees 404, not the branch
    assert c.put(f"/api/v1/branches/{b['id']}", json={"name": "hack"}, headers=h(ctx, "admin-b")).status_code == 404
    lst = c.get(f"/api/v1/companies/{cid}/branches", headers=h(ctx, "admin-a")).json()
    assert [x["id"] for x in lst] == [b["id"]]
    assert c.get(f"/api/v1/companies/{cid}", headers=h(ctx, "admin-a")).json()["branchCount"] == 1


def test_sms_test(ctx: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    c: TestClient = ctx["client"]
    cid = ctx["ids"]["a"]
    r = c.post(f"/api/v1/companies/{cid}/sms/test", json={}, headers=h(ctx, "admin-a"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "sms_not_configured"
    c.put(f"/api/v1/companies/{cid}", json={"sms": {"provider": "xabarchi", "apiKey": "xab_live_testkey1234", "defaultPriority": "transactional"}}, headers=h(ctx, "admin-a"))
    seen: dict[str, object] = {}

    async def fake_send(api_key: str, to: list[str], text: str, priority: str = "transactional") -> list[ProviderResult]:
        seen.update({"key": api_key, "to": to, "priority": priority})
        return [ProviderResult(to=to[0], provider_id="777", status="queued", raw={})]

    monkeypatch.setattr(tenant_service.xabarchi, "send_sms", fake_send)
    r = c.post(f"/api/v1/companies/{cid}/sms/test", json={"to": "+998 90 765-43-21"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and r.json() == {"ok": True, "providerMessageId": "777"}
    assert seen == {"key": "xab_live_testkey1234", "to": ["998907654321"], "priority": "transactional"}
    r = c.post(f"/api/v1/companies/{cid}/sms/test", json={}, headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and seen["to"] == ["998901112233"]  # default recipient: company phone

    async def failing(*_: object, **__: object) -> list[ProviderResult]:
        raise XabarchiError("Xabarchi API kaliti rad etildi (401/403)", code="sms_auth_error")

    monkeypatch.setattr(tenant_service.xabarchi, "send_sms", failing)
    r = c.post(f"/api/v1/companies/{cid}/sms/test", json={}, headers=h(ctx, "admin-a"))
    assert r.status_code == 502 and r.json()["error"]["message"].startswith("Xabarchi API kaliti rad etildi")


def test_telegram_settings(ctx: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    c: TestClient = ctx["client"]
    cid = ctx["ids"]["a"]

    async def fake_get_me(token: str) -> str:
        assert token == "123456:ABC-DEF-token"
        return "t_tenant_bot"

    monkeypatch.setattr(tenant_service, "_telegram_get_me", fake_get_me)
    r = c.put(f"/api/v1/companies/{cid}/telegram", json={"botToken": "123456:ABC-DEF-token"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 200, r.text
    assert r.json()["telegram"] == {"botUsername": "t_tenant_bot", "connected": True}
    assert "ABC-DEF" not in r.text
    r = c.put(f"/api/v1/companies/{cid}/telegram", json={"botToken": None}, headers=h(ctx, "admin-a"))
    assert r.json()["telegram"] == {"botUsername": None, "connected": False}
    assert c.put(f"/api/v1/companies/{cid}/telegram", json={"botToken": "x"}, headers=h(ctx, "admin-b")).status_code == 403
