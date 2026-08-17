"""API tests for the staff module (employees, roles) against the real dev DB (fixture rows prefixed T-staff-)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator

import pytest
from app.core.config import settings
from app.core.permissions import COMPANY_ADMIN_PERMISSIONS
from app.core.security import hash_password
from app.infrastructure.db.models import Branch, Company, Employee, Role
from app.infrastructure.db.session import engine, session_scope
from fastapi.testclient import TestClient

settings.workers_enabled = False
settings.telegram_enabled = False

SFX = uuid.uuid4().hex[:8]
PASSWORD = "T-staff-pass-1"


async def _seed() -> dict[str, str]:
    async with session_scope() as s:
        a = Company(name=f"T-staff-A-{SFX}", slug=f"t-staff-a-{SFX}")
        b = Company(name=f"T-staff-B-{SFX}", slug=f"t-staff-b-{SFX}")
        s.add_all([a, b])
        await s.flush()
        branch = Branch(company_id=a.id, name="T-staff branch", code="TS")
        platform = Role(company_id=None, key=f"t-staff-platform-{SFX}", name="T-staff platform", permissions=[], is_system=True)
        admin_a = Role(company_id=a.id, key="admin", name="Admin", permissions=list(COMPANY_ADMIN_PERMISSIONS), is_system=True)
        admin_b = Role(company_id=b.id, key="admin", name="Admin", permissions=list(COMPANY_ADMIN_PERMISSIONS), is_system=True)
        s.add_all([branch, platform, admin_a, admin_b])
        await s.flush()
        pw = hash_password(PASSWORD)
        s.add_all(
            [
                Employee(company_id=a.id, full_name="T-staff Super", login=f"t-staff-super-{SFX}", password_hash=pw, role_id=platform.id, is_super_admin=True),
                Employee(company_id=a.id, full_name="T-staff Admin A", login=f"t-staff-admin-a-{SFX}", password_hash=pw, role_id=admin_a.id),
                Employee(company_id=b.id, full_name="T-staff Admin B", login=f"t-staff-admin-b-{SFX}", password_hash=pw, role_id=admin_b.id),
            ]
        )
        ids = {"a": str(a.id), "b": str(b.id), "branch": str(branch.id), "platform_role": str(platform.id), "admin_role_a": str(admin_a.id), "admin_role_b": str(admin_b.id)}
    await engine.dispose()
    return ids


@pytest.fixture(scope="module")
def ctx() -> Iterator[dict[str, object]]:
    ids = asyncio.run(_seed())
    from app.main import app

    with TestClient(app) as client:
        tokens = {}
        for who in ("super", "admin-a", "admin-b"):
            r = client.post("/api/v1/auth/staff/login", json={"login": f"t-staff-{who}-{SFX}", "password": PASSWORD})
            assert r.status_code == 200, r.text
            tokens[who] = r.json()["accessToken"]
        yield {"client": client, "ids": ids, "tokens": tokens, "state": {}}


def h(ctx: dict, who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {ctx['tokens'][who]}"}


def test_roles_list_create_update_delete(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    ids = ctx["ids"]
    roles = c.get(f"/api/v1/companies/{ids['a']}/roles", headers=h(ctx, "admin-a")).json()
    keys = {(r["companyId"], r["id"]) for r in roles}
    assert (None, ids["platform_role"]) in keys and (ids["a"], ids["admin_role_a"]) in keys and (ids["b"], ids["admin_role_b"]) not in keys
    # create
    r = c.post(f"/api/v1/companies/{ids['a']}/roles", json={"name": "Laborant T", "permissions": ["lab.worklist.read", "lab.result.write"], "description": "d"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 201, r.text
    role = r.json()
    assert role == {"id": role["id"], "companyId": ids["a"], "key": "laborant-t", "name": "Laborant T", "description": "d", "permissions": ["lab.worklist.read", "lab.result.write"], "isSystem": False}
    ctx["state"]["role_id"] = role["id"]
    assert c.post(f"/api/v1/companies/{ids['a']}/roles", json={"name": "Other", "key": "laborant-t"}, headers=h(ctx, "admin-a")).status_code == 409
    assert c.post(f"/api/v1/companies/{ids['a']}/roles", json={"name": "Root", "key": "superadmin"}, headers=h(ctx, "admin-a")).status_code == 422
    assert c.post(f"/api/v1/companies/{ids['a']}/roles", json={"name": "Bad", "permissions": ["nope.x"]}, headers=h(ctx, "admin-a")).status_code == 422
    # cached list reflects the write
    assert any(x["id"] == role["id"] for x in c.get(f"/api/v1/companies/{ids['a']}/roles", headers=h(ctx, "admin-a")).json())
    # update: system role keeps its key; unknown → 404; platform role → forbidden for company admin
    perms = [p for p in COMPANY_ADMIN_PERMISSIONS if p != "reports.export"]
    r = c.put(f"/api/v1/roles/{ids['admin_role_a']}", json={"key": "boss", "name": "Administrator", "permissions": perms}, headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and r.json()["key"] == "admin" and r.json()["name"] == "Administrator" and r.json()["permissions"] == perms
    r = c.put(f"/api/v1/roles/{uuid.uuid4()}", json={"name": "x"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 404 and r.json()["error"]["message"] == "Rol topilmadi"
    assert c.put(f"/api/v1/roles/{ids['platform_role']}", json={"name": "x"}, headers=h(ctx, "admin-a")).status_code == 403
    assert c.put(f"/api/v1/roles/{ids['admin_role_a']}", json={"name": "x"}, headers=h(ctx, "admin-b")).status_code == 404
    # delete system → 409 in_use
    r = c.delete(f"/api/v1/roles/{ids['admin_role_a']}", headers=h(ctx, "admin-a"))
    assert r.status_code == 409 and r.json()["error"] == {"code": "in_use", "message": "Tizim rolini o‘chirib bo‘lmaydi"}


def test_employees_crud_and_sessions(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    ids = ctx["ids"]
    role_id = ctx["state"]["role_id"]
    login = f"T-staff-Lab-{SFX}"
    body = {"fullName": "Yusupova Ҳилола", "login": login, "roleId": role_id, "branchIds": [ids["branch"]], "phone": "+998 91 222-33-44", "overrides": {"allow": ["reports.export"], "deny": []}}
    r = c.post(f"/api/v1/companies/{ids['a']}/employees", json=body, headers=h(ctx, "admin-a"))
    assert r.status_code == 201, r.text
    e = r.json()
    assert e["companyId"] == ids["a"] and e["branchIds"] == [ids["branch"]] and e["roleId"] == role_id and e["status"] == "active"
    assert e["overrides"] == {"allow": ["reports.export"], "deny": []} and e["categoryIds"] == [] and 0 <= e["avatarHue"] <= 359
    assert e["lastLoginAt"] is None and "password" not in r.text.lower().replace("passwordhash", "")
    ctx["state"]["emp_id"] = e["id"]
    # default password 123456 works
    r = c.post("/api/v1/auth/staff/login", json={"login": login, "password": "123456"})
    assert r.status_code == 200, r.text
    lab_token = r.json()["accessToken"]
    assert "reports.export" in r.json()["permissions"]
    # login uniqueness is case-insensitive and global
    r = c.post(f"/api/v1/companies/{ids['b']}/employees", json={"fullName": "X", "login": login.lower(), "roleId": ids["admin_role_b"]}, headers=h(ctx, "admin-b"))
    assert r.status_code == 409 and r.json()["error"]["message"] == "Bu login band"
    # role of another company / unknown branch / superadmin flag by non-superadmin
    assert c.post(f"/api/v1/companies/{ids['a']}/employees", json={"fullName": "X", "login": f"t-staff-x1-{SFX}", "roleId": ids["admin_role_b"]}, headers=h(ctx, "admin-a")).status_code == 404
    assert c.post(f"/api/v1/companies/{ids['a']}/employees", json={"fullName": "X", "login": f"t-staff-x2-{SFX}", "roleId": role_id, "branchIds": [str(uuid.uuid4())]}, headers=h(ctx, "admin-a")).status_code == 404
    assert c.post(f"/api/v1/companies/{ids['a']}/employees", json={"fullName": "X", "login": f"t-staff-x3-{SFX}", "roleId": role_id, "isSuperAdmin": True}, headers=h(ctx, "admin-a")).status_code == 403
    # list: search (Cyrillic + fold), filters, default sort
    r = c.get(f"/api/v1/companies/{ids['a']}/employees", params={"search": "hilola"}, headers=h(ctx, "admin-a"))
    assert [x["id"] for x in r.json()["items"]] == [e["id"]]
    r = c.get(f"/api/v1/companies/{ids['a']}/employees", params={"search": "912223344"}, headers=h(ctx, "admin-a"))
    assert [x["id"] for x in r.json()["items"]] == [e["id"]]
    r = c.get(f"/api/v1/companies/{ids['a']}/employees", params={"branchId": ids["branch"], "roleId": role_id, "status": "active"}, headers=h(ctx, "admin-a"))
    assert [x["id"] for x in r.json()["items"]] == [e["id"]]
    r = c.get(f"/api/v1/companies/{ids['a']}/employees", params={"pageSize": 50}, headers=h(ctx, "admin-a"))
    names = [x["fullName"] for x in r.json()["items"]]
    assert names == sorted(names) and r.json()["total"] >= 3
    # get + tenant isolation
    assert c.get(f"/api/v1/employees/{e['id']}", headers=h(ctx, "admin-a")).status_code == 200
    assert c.get(f"/api/v1/employees/{e['id']}", headers=h(ctx, "admin-b")).status_code == 404
    assert c.get(f"/api/v1/employees/{e['id']}", headers=h(ctx, "super")).status_code == 200
    assert c.get(f"/api/v1/companies/{ids['a']}/employees", headers=h(ctx, "admin-b")).status_code == 403
    # overrides: invalid key → 422; valid → replaced wholesale
    r = c.put(f"/api/v1/employees/{e['id']}/overrides", json={"allow": ["bogus.key"], "deny": []}, headers=h(ctx, "admin-a"))
    assert r.status_code == 422
    r = c.put(f"/api/v1/employees/{e['id']}/overrides", json={"allow": [], "deny": ["lab.result.write"]}, headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and r.json()["overrides"] == {"allow": [], "deny": ["lab.result.write"]}
    # update: password change revokes the lab session
    assert c.get("/api/v1/auth/staff/me", headers={"Authorization": f"Bearer {lab_token}"}).status_code == 200
    r = c.put(f"/api/v1/employees/{e['id']}", json={"password": "newpass1", "email": "lab@example.com", "categoryIds": []}, headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and r.json()["email"] == "lab@example.com"
    r = c.get("/api/v1/auth/staff/me", headers={"Authorization": f"Bearer {lab_token}"})
    assert r.status_code == 401
    assert c.post("/api/v1/auth/staff/login", json={"login": login, "password": "newpass1"}).status_code == 200
    # login change conflict / status inactive blocks login
    r = c.put(f"/api/v1/employees/{e['id']}", json={"login": f"t-staff-admin-a-{SFX}"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 409
    r = c.put(f"/api/v1/employees/{e['id']}", json={"status": "inactive"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and r.json()["status"] == "inactive"
    assert c.post("/api/v1/auth/staff/login", json={"login": login, "password": "newpass1"}).status_code == 403
    assert c.put(f"/api/v1/employees/{e['id']}", json={"fullName": "hack"}, headers=h(ctx, "admin-b")).status_code == 404
    r = c.put(f"/api/v1/employees/{uuid.uuid4()}", json={"fullName": "x"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 404 and r.json()["error"]["message"] == "Xodim topilmadi"


def test_delete_role_in_use_then_free(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    ids = ctx["ids"]
    role_id = ctx["state"]["role_id"]
    r = c.delete(f"/api/v1/roles/{role_id}", headers=h(ctx, "admin-a"))
    assert r.status_code == 409 and r.json()["error"] == {"code": "in_use", "message": "Bu rol xodimlarga biriktirilgan"}
    assert c.delete(f"/api/v1/roles/{role_id}", headers=h(ctx, "admin-b")).status_code == 404
    c.put(f"/api/v1/employees/{ctx['state']['emp_id']}", json={"roleId": ids["admin_role_a"]}, headers=h(ctx, "admin-a"))
    assert c.delete(f"/api/v1/roles/{role_id}", headers=h(ctx, "admin-a")).status_code == 204
    assert all(x["id"] != role_id for x in c.get(f"/api/v1/companies/{ids['a']}/roles", headers=h(ctx, "admin-a")).json())
    assert c.delete(f"/api/v1/roles/{role_id}", headers=h(ctx, "admin-a")).status_code == 404
