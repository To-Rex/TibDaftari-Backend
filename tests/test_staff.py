"""API tests for the staff module (employees, roles) against the test database (fixture rows prefixed T-staff-)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from app.core.permissions import UNIT_ADMIN_PERMISSIONS
from fastapi.testclient import TestClient

from tests.helpers import API, app_instance, bearer, make_employee, make_tree, seed, staff_token

SFX = uuid.uuid4().hex[:8]
PASSWORD = "T-staff-pass-1"
SYSTEM_ROLE_IDS = {
    "superadmin": "01a00000-0000-7000-8000-000000000001",
    "admin": "01a00000-0000-7000-8000-000000000002",
    "registrator": "01a00000-0000-7000-8000-000000000003",
    "laborant": "01a00000-0000-7000-8000-000000000004",
    "vrach": "01a00000-0000-7000-8000-000000000005",
    "rahbar": "01a00000-0000-7000-8000-000000000006",
}


@pytest.fixture(scope="module")
def ctx() -> Iterator[dict]:
    async def rows(s):
        a = await make_tree(s)
        b = await make_tree(s)
        sup = await make_employee(s, None, super_admin=True, login=f"t-staff-super-{SFX}", password=PASSWORD)
        await make_employee(s, a.region, "admin", login=f"t-staff-admin-a-{SFX}", password=PASSWORD, full_name="T-staff Admin A")
        await make_employee(s, b.region, "admin", login=f"t-staff-admin-b-{SFX}", password=PASSWORD, full_name="T-staff Admin B")
        return {
            "a": {k: str(getattr(a, k).id) for k in ("country", "region", "district", "facility")},
            "b": {k: str(getattr(b, k).id) for k in ("country", "region", "district", "facility")},
            "tag": a.tag,
            "super_id": str(sup.id),
        }

    data = seed(rows)
    with TestClient(app_instance()) as client:
        data["client"] = client
        data["tokens"] = {who: staff_token(client, f"t-staff-{who}-{SFX}", PASSWORD) for who in ("super", "admin-a", "admin-b")}
        data["state"] = {}
        yield data


def h(ctx: dict, who: str) -> dict[str, str]:
    return bearer(ctx["tokens"][who])


def test_roles_list_create_update_delete(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    a, b = ctx["a"], ctx["b"]
    roles = c.get(f"{API}/units/{a['region']}/roles", headers=h(ctx, "admin-a")).json()
    by_key = {r["key"]: r for r in roles}
    # the six system roles are available everywhere (scopeUnitId null), with the number of employees under the unit
    assert {"superadmin", "installer", "admin", "registrator", "laborant", "vrach", "rahbar"} <= set(by_key)
    assert all(by_key[k]["scopeUnitId"] is None and by_key[k]["isSystem"] for k in SYSTEM_ROLE_IDS)
    assert by_key["admin"]["id"] == SYSTEM_ROLE_IDS["admin"] and by_key["admin"]["employeeCount"] == 1 and by_key["superadmin"]["employeeCount"] == 0
    # create a role scoped to the region: usable in its subtree, invisible to the other tree
    r = c.post(f"{API}/units/{a['region']}/roles", json={"name": "T-staff Laborant T", "permissions": ["lab.worklist.read", "lab.result.write"], "description": "d"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 201, r.text
    role = r.json()
    assert role == {"id": role["id"], "scopeUnitId": a["region"], "key": "t-staff-laborant-t", "name": "T-staff Laborant T", "description": "d", "permissions": ["lab.worklist.read", "lab.result.write"], "isSystem": False, "employeeCount": 0}
    ctx["state"]["role_id"] = role["id"]
    assert c.post(f"{API}/units/{a['region']}/roles", json={"name": "Other", "key": "t-staff-laborant-t"}, headers=h(ctx, "admin-a")).status_code == 409
    assert c.post(f"{API}/units/{a['region']}/roles", json={"name": "Root", "key": "superadmin"}, headers=h(ctx, "admin-a")).status_code == 422
    assert c.post(f"{API}/units/{a['region']}/roles", json={"name": "Bad", "permissions": ["nope.x"]}, headers=h(ctx, "admin-a")).status_code == 422
    assert c.post(f"{API}/units/{a['region']}/roles", json={"name": "Platform", "permissions": ["platform.unit.manage"]}, headers=h(ctx, "admin-a")).status_code == 403
    assert c.post(f"{API}/units/{a['region']}/roles", json={"name": "x"}, headers=h(ctx, "admin-b")).status_code == 403
    # cached lists reflect the write: visible from the region and from the facility below it, not from tree B
    assert any(x["id"] == role["id"] for x in c.get(f"{API}/units/{a['region']}/roles", headers=h(ctx, "admin-a")).json())
    assert any(x["id"] == role["id"] for x in c.get(f"{API}/units/{a['facility']}/roles", headers=h(ctx, "admin-a")).json())
    assert all(x["id"] != role["id"] for x in c.get(f"{API}/units/{b['region']}/roles", headers=h(ctx, "admin-b")).json())
    # update: a unit role by its administrator; system roles only by the superadmin; unknown → 404; other tree → 404
    r = c.put(f"{API}/roles/{role['id']}", json={"key": "boss", "name": "T-staff Boss", "permissions": ["lab.worklist.read"]}, headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and r.json()["key"] == "boss" and r.json()["name"] == "T-staff Boss" and r.json()["permissions"] == ["lab.worklist.read"]
    r = c.put(f"{API}/roles/{SYSTEM_ROLE_IDS['admin']}", json={"key": "chief", "permissions": list(UNIT_ADMIN_PERMISSIONS)}, headers=h(ctx, "admin-a"))
    assert r.status_code == 403
    r = c.put(f"{API}/roles/{SYSTEM_ROLE_IDS['admin']}", json={"key": "chief", "description": "Birlik ma'muri"}, headers=h(ctx, "super"))
    assert r.status_code == 200 and r.json()["key"] == "admin" and r.json()["description"] == "Birlik ma'muri"  # system roles keep their key
    r = c.put(f"{API}/roles/{uuid.uuid4()}", json={"name": "x"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 404 and r.json()["error"]["message"] == "Rol topilmadi"
    assert c.put(f"{API}/roles/{role['id']}", json={"name": "x"}, headers=h(ctx, "admin-b")).status_code == 404
    # delete system → 409 in_use
    r = c.delete(f"{API}/roles/{SYSTEM_ROLE_IDS['registrator']}", headers=h(ctx, "super"))
    assert r.status_code == 409 and r.json()["error"] == {"code": "in_use", "message": "Tizim rolini o‘chirib bo‘lmaydi"}
    assert c.delete(f"{API}/roles/{SYSTEM_ROLE_IDS['registrator']}", headers=h(ctx, "admin-a")).status_code == 403


def test_employees_crud_and_sessions(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    a, b = ctx["a"], ctx["b"]
    role_id = ctx["state"]["role_id"]
    login = f"T-staff-Lab-{SFX}"
    body = {"fullName": "T-staff Yusupova Ҳилола", "login": login, "roleId": role_id, "unitId": a["facility"], "unitIds": [a["district"]], "phone": "+998 91 222-33-44", "overrides": {"allow": ["reports.export"], "deny": []}}
    r = c.post(f"{API}/units/{a['region']}/employees", json=body, headers=h(ctx, "admin-a"))
    assert r.status_code == 201, r.text
    e = r.json()
    assert e["unitId"] == a["facility"] and e["unitIds"] == [a["district"]] and e["unitName"] == f"T-{ctx['tag']} SES" and e["unitLevel"] == "facility"
    assert e["roleId"] == role_id and e["status"] == "active" and e["isSuperAdmin"] is False
    assert e["overrides"] == {"allow": ["reports.export"], "deny": []} and e["categoryIds"] == [] and 0 <= e["avatarHue"] <= 359
    assert e["lastLoginAt"] is None and "password" not in r.text.lower().replace("passwordhash", "")
    ctx["state"]["emp_id"] = e["id"]
    # default password 123456 works; the session carries the home facility and both scope roots
    r = c.post(f"{API}/auth/staff/login", json={"login": login, "password": "123456"})
    assert r.status_code == 200, r.text
    lab = r.json()
    lab_token = lab["accessToken"]
    assert "reports.export" in lab["permissions"] and lab["facilityId"] == a["facility"] and set(lab["unitIds"]) == {a["facility"], a["district"]}
    # the home unit defaults to the path unit
    r = c.post(f"{API}/units/{a['district']}/employees", json={"fullName": "T-staff Default", "login": f"t-staff-def-{SFX}", "roleId": SYSTEM_ROLE_IDS["registrator"]}, headers=h(ctx, "admin-a"))
    assert r.status_code == 201 and r.json()["unitId"] == a["district"] and r.json()["unitLevel"] == "district"
    # login uniqueness is case-insensitive and global
    r = c.post(f"{API}/units/{b['region']}/employees", json={"fullName": "X", "login": login.lower(), "roleId": SYSTEM_ROLE_IDS["admin"]}, headers=h(ctx, "admin-b"))
    assert r.status_code == 409 and r.json()["error"]["message"] == "Bu login band"
    # a role of another tree / a unit outside the scope / the superadmin flag by a non-superadmin
    r = c.post(f"{API}/units/{b['region']}/employees", json={"fullName": "X", "login": f"t-staff-x1-{SFX}", "roleId": role_id}, headers=h(ctx, "admin-b"))
    assert r.status_code == 404 and r.json()["error"]["message"] == "Rol topilmadi"
    r = c.post(f"{API}/units/{a['region']}/employees", json={"fullName": "X", "login": f"t-staff-x2-{SFX}", "roleId": role_id, "unitIds": [str(uuid.uuid4())]}, headers=h(ctx, "admin-a"))
    assert r.status_code == 404 and r.json()["error"]["message"] == "Birlik topilmadi"
    r = c.post(f"{API}/units/{a['region']}/employees", json={"fullName": "X", "login": f"t-staff-x2-{SFX}", "roleId": role_id, "unitId": b["facility"]}, headers=h(ctx, "admin-a"))
    assert r.status_code == 404 and r.json()["error"]["message"] == "Birlik topilmadi"
    assert c.post(f"{API}/units/{a['region']}/employees", json={"fullName": "X", "login": f"t-staff-x3-{SFX}", "roleId": role_id, "isSuperAdmin": True}, headers=h(ctx, "admin-a")).status_code == 403
    assert c.post(f"{API}/units/{a['region']}/employees", json={"fullName": "X", "login": f"t-staff-x4-{SFX}", "roleId": SYSTEM_ROLE_IDS["superadmin"]}, headers=h(ctx, "admin-a")).status_code == 403
    # list: search (Cyrillic + fold), filters, unitId narrowing, default sort; superadmins are listed for superadmins only
    r = c.get(f"{API}/units/{a['region']}/employees", params={"search": "hilola"}, headers=h(ctx, "admin-a"))
    assert [x["id"] for x in r.json()["items"]] == [e["id"]]
    r = c.get(f"{API}/units/{a['region']}/employees", params={"search": "912223344"}, headers=h(ctx, "admin-a"))
    assert [x["id"] for x in r.json()["items"]] == [e["id"]]
    r = c.get(f"{API}/units/{a['region']}/employees", params={"unitId": a["facility"], "roleId": role_id, "status": "active"}, headers=h(ctx, "admin-a"))
    assert [x["id"] for x in r.json()["items"]] == [e["id"]]
    assert c.get(f"{API}/units/{a['region']}/employees", params={"unitId": b["facility"]}, headers=h(ctx, "admin-a")).json()["total"] == 0
    r = c.get(f"{API}/units/{a['region']}/employees", params={"pageSize": 50}, headers=h(ctx, "admin-a"))
    names = [x["fullName"] for x in r.json()["items"]]
    assert names == sorted(names) and r.json()["total"] == 3 and all(x["isSuperAdmin"] is False for x in r.json()["items"])
    ids_super = {x["id"] for x in c.get(f"{API}/units/{a['region']}/employees", params={"pageSize": 50}, headers=h(ctx, "super")).json()["items"]}
    assert ctx["super_id"] in ids_super and e["id"] in ids_super
    assert c.get(f"{API}/units/{a['facility']}/employees", headers=h(ctx, "admin-a")).json()["total"] == 1
    # get + unit isolation (403 outside the caller's units; the superadmin sees everyone)
    assert c.get(f"{API}/employees/{e['id']}", headers=h(ctx, "admin-a")).status_code == 200
    assert c.get(f"{API}/employees/{e['id']}", headers=h(ctx, "admin-b")).status_code == 403
    assert c.get(f"{API}/employees/{e['id']}", headers=h(ctx, "super")).status_code == 200
    assert c.get(f"{API}/employees/{ctx['super_id']}", headers=h(ctx, "admin-a")).status_code == 403
    assert c.get(f"{API}/units/{a['region']}/employees", headers=h(ctx, "admin-b")).status_code == 403
    # overrides: invalid key → 422; valid → replaced wholesale
    r = c.put(f"{API}/employees/{e['id']}/overrides", json={"allow": ["bogus.key"], "deny": []}, headers=h(ctx, "admin-a"))
    assert r.status_code == 422
    r = c.put(f"{API}/employees/{e['id']}/overrides", json={"allow": [], "deny": ["lab.result.write"]}, headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and r.json()["overrides"] == {"allow": [], "deny": ["lab.result.write"]}
    # update: password change revokes the lab session
    assert c.get(f"{API}/auth/staff/me", headers=bearer(lab_token)).status_code == 200
    r = c.put(f"{API}/employees/{e['id']}", json={"password": "newpass1", "email": "lab@example.com", "categoryIds": []}, headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and r.json()["email"] == "lab@example.com"
    assert c.get(f"{API}/auth/staff/me", headers=bearer(lab_token)).status_code == 401
    assert c.post(f"{API}/auth/staff/login", json={"login": login, "password": "newpass1"}).status_code == 200
    # moving the home unit: inside the caller's scope only; the extra units are replaced wholesale
    r = c.put(f"{API}/employees/{e['id']}", json={"unitId": a["district"], "unitIds": []}, headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and r.json()["unitId"] == a["district"] and r.json()["unitIds"] == [] and r.json()["unitLevel"] == "district"
    assert c.put(f"{API}/employees/{e['id']}", json={"unitId": b["facility"]}, headers=h(ctx, "admin-a")).status_code == 403
    # login change conflict / status inactive blocks login
    r = c.put(f"{API}/employees/{e['id']}", json={"login": f"t-staff-admin-a-{SFX}"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 409
    r = c.put(f"{API}/employees/{e['id']}", json={"status": "inactive"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and r.json()["status"] == "inactive"
    assert c.post(f"{API}/auth/staff/login", json={"login": login, "password": "newpass1"}).status_code == 403
    assert c.put(f"{API}/employees/{e['id']}", json={"fullName": "hack"}, headers=h(ctx, "admin-b")).status_code == 403
    r = c.put(f"{API}/employees/{uuid.uuid4()}", json={"fullName": "x"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 404 and r.json()["error"]["message"] == "Xodim topilmadi"


def test_delete_role_in_use_then_free(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    a = ctx["a"]
    role_id = ctx["state"]["role_id"]
    r = c.delete(f"{API}/roles/{role_id}", headers=h(ctx, "admin-a"))
    assert r.status_code == 409 and r.json()["error"] == {"code": "in_use", "message": "Bu rol xodimlarga biriktirilgan"}
    assert c.delete(f"{API}/roles/{role_id}", headers=h(ctx, "admin-b")).status_code == 404
    assert c.put(f"{API}/employees/{ctx['state']['emp_id']}", json={"roleId": SYSTEM_ROLE_IDS["laborant"]}, headers=h(ctx, "admin-a")).status_code == 200
    assert c.delete(f"{API}/roles/{role_id}", headers=h(ctx, "admin-a")).status_code == 204
    assert all(x["id"] != role_id for x in c.get(f"{API}/units/{a['region']}/roles", headers=h(ctx, "admin-a")).json())
    assert c.delete(f"{API}/roles/{role_id}", headers=h(ctx, "admin-a")).status_code == 404
