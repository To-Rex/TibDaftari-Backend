"""The "O‘rnatuvchi" (installer) role: superadmin-like inside the units a superadmin gave the employee — their
region, more regions of the same country, or the whole country — and nothing beyond. Granted, re-scoped and edited
by superadmins only; may reset units inside the scope. Fixture rows are prefixed `T-`."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from app.infrastructure.db.models import OrgUnit
from app.modules.units.reset import confirm_word
from fastapi.testclient import TestClient

from tests.helpers import (
    API,
    COUNTRY_UNIT_ID,
    app_instance,
    bearer,
    db,
    make_employee,
    make_tree,
    seed,
    staff_session,
    staff_token,
    system_role,
)


@pytest.fixture(scope="module")
def ctx() -> Iterator[dict]:
    async def rows(s):
        a = await make_tree(s)
        b = await make_tree(s)
        sup = await make_employee(s, None, super_admin=True)
        installer = await make_employee(s, a.region, "installer")
        admin_a = await make_employee(s, a.region, "admin")
        return {
            "a": {k: str(getattr(a, k).id) for k in ("region", "district", "facility")},
            "b": {k: str(getattr(b, k).id) for k in ("region", "district", "facility")},
            "tag": a.tag,
            "installer_id": str(installer.id),
            "installer_role": str((await system_role(s, "installer")).id),
            "admin_role": str((await system_role(s, "admin")).id),
            "logins": {"super": sup.login, "inst": installer.login, "admin-a": admin_a.login},
        }

    data = seed(rows)
    with TestClient(app_instance()) as client:
        data["client"] = client
        data["tokens"] = {who: staff_token(client, login) for who, login in data["logins"].items()}
        yield data


def h(ctx: dict, who: str) -> dict[str, str]:
    return bearer(ctx["tokens"][who])


def emp(ctx: dict, *, role: str, unit_id: str, extra: list[str] | None = None, tag: str = "x") -> dict:
    import uuid

    return {"fullName": f"T-{ctx['tag']} {tag}", "login": f"t-{tag}-{uuid.uuid4().hex[:8]}", "roleId": role, "unitId": unit_id, "unitIds": extra or [], "password": "123456"}


def test_installer_session_is_bounded_to_its_region(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    s = staff_session(c, ctx["logins"]["inst"])
    assert s["roleKey"] == "installer" and s["isSuperAdmin"] is False
    assert s["unitId"] == ctx["a"]["region"] and s["unitIds"] == [ctx["a"]["region"]]
    assert "admin.unit.reset" in s["permissions"] and "admin.unit.manage" in s["permissions"]
    assert "platform.unit.manage" not in s["permissions"]
    # the own subtree is visible, the neighbouring region is not
    assert c.get(f"{API}/units/{ctx['a']['facility']}", headers=h(ctx, "inst")).status_code == 200
    assert c.get(f"{API}/units/{ctx['b']['region']}", headers=h(ctx, "inst")).status_code == 403
    tree = {u["id"] for u in c.get(f"{API}/units/tree", headers=h(ctx, "inst")).json()}
    assert set(ctx["a"].values()) <= tree and ctx["b"]["region"] not in tree


def test_installer_administers_everything_inside(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    hi = h(ctx, "inst")
    # a new facility under the own district …
    r = c.post(f"{API}/units", json={"parentId": ctx["a"]["district"], "kind": "ses", "name": f"T-{ctx['tag']} Ikkinchi SES", "code": f"T2{ctx['tag']}"}, headers=hi)
    assert r.status_code == 201, r.text
    fac2 = r.json()["id"]
    # … staff, catalogue and settings there
    r = c.post(f"{API}/units/{fac2}/employees", json=emp(ctx, role=ctx["admin_role"], unit_id=fac2, tag="fac-admin"), headers=hi)
    assert r.status_code == 201, r.text
    r = c.post(f"{API}/units/{fac2}/categories", json={"name": f"T-{ctx['tag']} Lab"}, headers=hi)
    assert r.status_code == 201, r.text
    r = c.put(f"{API}/units/{fac2}", json={"phone": "+998 62 000-00-00"}, headers=hi)
    assert r.status_code == 200, r.text
    # … but nothing under the other region
    r = c.post(f"{API}/units", json={"parentId": ctx["b"]["district"], "kind": "ses", "name": f"T-{ctx['tag']} Begona", "code": f"T3{ctx['tag']}"}, headers=hi)
    assert r.status_code in (403, 404), r.text
    r = c.post(f"{API}/units/{ctx['b']['facility']}/employees", json=emp(ctx, role=ctx["admin_role"], unit_id=ctx["b"]["facility"], tag="b-admin"), headers=hi)
    assert r.status_code in (403, 404), r.text


def test_only_a_superadmin_grants_the_role(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    region = ctx["a"]["region"]
    body = emp(ctx, role=ctx["installer_role"], unit_id=region, tag="inst2")
    assert c.post(f"{API}/units/{region}/employees", json=body, headers=h(ctx, "admin-a")).status_code == 403
    assert c.post(f"{API}/units/{region}/employees", json=body, headers=h(ctx, "inst")).status_code == 403
    r = c.post(f"{API}/units/{region}/employees", json=body, headers=h(ctx, "super"))
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["unitId"] == region and created["roleId"] == ctx["installer_role"]
    # the role key is reserved for the system
    r = c.post(f"{API}/units/{region}/roles", json={"name": "Installer", "key": "installer", "permissions": ["admin.unit.read"]}, headers=h(ctx, "super"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "reserved"


def test_installer_scope_is_a_region_or_a_country_of_one_country(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    hs = h(ctx, "super")
    a, b = ctx["a"], ctx["b"]
    # a facility is no home for an installer
    r = c.post(f"{API}/units/{a['facility']}/employees", json=emp(ctx, role=ctx["installer_role"], unit_id=a["facility"], tag="bad1"), headers=hs)
    assert r.status_code == 422 and r.json()["error"]["code"] == "installer_scope"
    # extra units must be regions of the same country — a facility is refused …
    r = c.post(f"{API}/units/{a['region']}/employees", json=emp(ctx, role=ctx["installer_role"], unit_id=a["region"], extra=[a["facility"]], tag="bad2"), headers=hs)
    assert r.status_code == 422 and r.json()["error"]["code"] == "installer_scope"
    # … another region of the country is fine: the installer then works in both subtrees
    r = c.post(f"{API}/units/{COUNTRY_UNIT_ID!s}/employees", json=emp(ctx, role=ctx["installer_role"], unit_id=a["region"], extra=[b["region"]], tag="two"), headers=hs)
    assert r.status_code == 201, r.text
    two = r.json()
    assert set(two["unitIds"]) == {b["region"]}
    s = staff_session(c, two["login"])
    assert set(s["unitIds"]) == {a["region"], b["region"]}
    assert c.get(f"{API}/units/{b['facility']}", headers=bearer(s["accessToken"])).status_code == 200
    # the whole country: home = the country node (everything under it, never another country)
    r = c.post(f"{API}/units/{COUNTRY_UNIT_ID!s}/employees", json=emp(ctx, role=ctx["installer_role"], unit_id=str(COUNTRY_UNIT_ID), tag="country"), headers=hs)
    assert r.status_code == 201, r.text
    s = staff_session(c, r.json()["login"])
    assert s["unitIds"] == [str(COUNTRY_UNIT_ID)] and s["unit"]["level"] == "country"
    # widening an existing installer is also checked (a facility as extra unit → 422)
    r = c.put(f"{API}/employees/{two['id']}", json={"unitIds": [b["region"], a["facility"]]}, headers=hs)
    assert r.status_code == 422 and r.json()["error"]["code"] == "installer_scope"


def test_only_a_superadmin_edits_an_installer(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    iid = ctx["installer_id"]
    # a region administrator sees the installer (same subtree) but may not touch the account
    assert c.get(f"{API}/employees/{iid}", headers=h(ctx, "admin-a")).status_code == 200
    assert c.put(f"{API}/employees/{iid}", json={"fullName": f"T-{ctx['tag']} hijack"}, headers=h(ctx, "admin-a")).status_code == 403
    assert c.put(f"{API}/employees/{iid}", json={"password": "hijacked1"}, headers=h(ctx, "admin-a")).status_code == 403
    assert c.put(f"{API}/employees/{iid}/overrides", json={"allow": [], "deny": ["admin.unit.reset"]}, headers=h(ctx, "admin-a")).status_code == 403
    # nor may the installer swap their own role for a wider one or grant the superadmin flag
    assert c.put(f"{API}/employees/{iid}", json={"isSuperAdmin": True}, headers=h(ctx, "inst")).status_code == 403
    # a superadmin may
    r = c.put(f"{API}/employees/{iid}", json={"fullName": f"T-{ctx['tag']} O‘rnatuvchi"}, headers=h(ctx, "super"))
    assert r.status_code == 200 and r.json()["fullName"].endswith("O‘rnatuvchi")


# last: the staff reset of region A removes its ordinary administrator (admin-a), whom the tests above still use
def test_installer_resets_inside_its_scope_only(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    fac = ctx["a"]["facility"]
    # a unit administrator never resets; the installer previews and resets inside the scope
    r = c.get(f"{API}/units/{fac}/reset", headers=h(ctx, "admin-a"))
    assert r.status_code == 403 and r.json()["error"]["message"] == "Faqat superadmin yoki o‘rnatuvchi uchun"
    r = c.get(f"{API}/units/{fac}/reset", headers=h(ctx, "inst"))
    assert r.status_code == 200, r.text
    assert r.json()["id"] == fac
    r = c.get(f"{API}/units/{ctx['b']['facility']}/reset", headers=h(ctx, "inst"))
    assert r.status_code == 403 and r.json()["error"]["message"] == "Ruxsat yo‘q"
    # resetting the staff of the own region keeps the installer account (a superadmin handed it out) and the caller
    region = db(c, lambda s: s.get(OrgUnit, ctx["a"]["region"]))
    r = c.post(f"{API}/units/{ctx['a']['region']}/reset", json={"parts": ["orders", "staff"], "confirm": confirm_word(region)}, headers=h(ctx, "inst"))
    assert r.status_code == 200, r.text
    assert c.get(f"{API}/employees/{ctx['installer_id']}", headers=h(ctx, "super")).status_code == 200
    # the installer still logs in afterwards
    assert staff_session(c, ctx["logins"]["inst"])["roleKey"] == "installer"
