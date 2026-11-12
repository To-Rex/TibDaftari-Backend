"""The four read-only leader roles — «Davlat / Viloyat / Tuman / Muassasa rahbari»: they look at everything inside
their units (reports, statistics, lists, cards, documents) and change nothing; each is pinned to one level of the
tree (422 `leader_scope` otherwise). Fixture rows are prefixed `T-`."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from app.core.permissions import LEADER_PERMISSIONS, LEADER_ROLES, is_read_permission
from fastapi.testclient import TestClient

from tests.helpers import (
    API,
    app_instance,
    bearer,
    make_employee,
    make_tree,
    seed,
    staff_session,
    staff_token,
    system_role,
)

RANGE = "dateFrom=2026-11-01&dateTo=2026-11-12"


@pytest.fixture(scope="module")
def ctx() -> Iterator[dict]:
    async def rows(s):
        a = await make_tree(s)
        b = await make_tree(s)
        sup = await make_employee(s, None, super_admin=True)
        admin_r = await make_employee(s, a.region, "admin")
        admin_f = await make_employee(s, a.facility, "admin")
        lead_r = await make_employee(s, a.region, "leader_region")
        lead_f = await make_employee(s, a.facility, "leader_facility")
        lead_d = await make_employee(s, a.district, "leader_district")
        roles = {k: str((await system_role(s, k)).id) for k in (*LEADER_ROLES, "admin")}
        return {
            "a": {k: str(getattr(a, k).id) for k in ("country", "region", "district", "facility")},
            "b": {k: str(getattr(b, k).id) for k in ("country", "region", "district", "facility")},
            "tag": a.tag,
            "roles": roles,
            "ids": {"lead_r": str(lead_r.id), "lead_d": str(lead_d.id), "admin_f": str(admin_f.id)},
            "logins": {"super": sup.login, "admin-r": admin_r.login, "admin-f": admin_f.login, "lead-r": lead_r.login, "lead-f": lead_f.login},
        }

    data = seed(rows)
    with TestClient(app_instance()) as client:
        data["client"] = client
        data["tokens"] = {who: staff_token(client, login) for who, login in data["logins"].items()}
        yield data


def h(ctx: dict, who: str) -> dict[str, str]:
    return bearer(ctx["tokens"][who])


def emp(ctx: dict, *, role: str, unit_id: str, extra: list[str] | None = None, tag: str = "x") -> dict:
    return {"fullName": f"T-{ctx['tag']} {tag}", "login": f"t-{tag}-{uuid.uuid4().hex[:8]}", "roleId": role, "unitId": unit_id, "unitIds": extra or [], "password": "123456"}


def test_the_leader_set_only_looks() -> None:
    assert all(is_read_permission(p) for p in LEADER_PERMISSIONS)
    assert set(LEADER_ROLES.values()) == {"country", "region", "district", "facility"}


def test_leader_session_sees_but_never_writes(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    s = staff_session(c, ctx["logins"]["lead-r"])
    assert s["roleKey"] == "leader_region" and s["roleName"] == "Viloyat rahbari" and s["isSuperAdmin"] is False
    assert s["unitId"] == ctx["a"]["region"] and s["unit"]["level"] == "region" and s["facilityId"] is None
    assert set(s["permissions"]) == set(LEADER_PERMISSIONS)
    assert all(is_read_permission(p) for p in s["permissions"])
    f = staff_session(c, ctx["logins"]["lead-f"])
    assert f["roleKey"] == "leader_facility" and f["roleName"] == "Muassasa rahbari" and f["facilityId"] == ctx["a"]["facility"]


def test_region_leader_reads_the_whole_region(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    hl = h(ctx, "lead-r")
    a, b = ctx["a"], ctx["b"]
    tree = {u["id"] for u in c.get(f"{API}/units/tree", headers=hl).json()}
    assert {a["region"], a["district"], a["facility"]} <= tree and b["region"] not in tree
    assert c.get(f"{API}/units/{a['facility']}", headers=hl).status_code == 200
    assert c.get(f"{API}/units/{b['region']}", headers=hl).status_code == 403
    reads = [
        f"/units/{a['region']}/reports/dashboard?{RANGE}",
        f"/units/{a['region']}/reports/breakdown?by=unit&{RANGE}",
        f"/units/{a['region']}/reports/patients?{RANGE}",
        f"/units/{a['region']}/reports/results?{RANGE}",
        f"/units/{a['region']}/reports/services?{RANGE}",
        f"/platform/regions?unitId={a['region']}&{RANGE}",
        f"/units/{a['region']}/employees",
        f"/units/{a['region']}/patients",
        f"/units/{a['region']}/orders",
        f"/units/{a['facility']}/worklist",
        f"/units/{a['region']}/documents",
        f"/units/{a['region']}/templates",
        f"/units/{a['region']}/categories",
        f"/units/{a['region']}/sms",
        f"/employees/{ctx['ids']['admin_f']}",
    ]
    for path in reads:
        r = c.get(f"{API}{path}", headers=hl)
        assert r.status_code == 200, (path, r.status_code, r.text)
    r = c.get(f"{API}/units/{a['region']}/reports/export.xlsx?{RANGE}", headers=hl)
    assert r.status_code == 200 and r.content[:2] == b"PK"
    # the facility leader stays inside the facility
    hf = h(ctx, "lead-f")
    assert c.get(f"{API}/units/{a['facility']}/reports/dashboard?{RANGE}", headers=hf).status_code == 200
    assert c.get(f"{API}/units/{a['region']}/reports/dashboard?{RANGE}", headers=hf).status_code == 403


def test_leader_changes_nothing(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    hl = h(ctx, "lead-r")
    a, tag = ctx["a"], ctx["tag"]
    attempts = [
        ("post", "/units", {"parentId": a["district"], "kind": "ses", "name": f"T-{tag} Rahbar SES", "code": f"TL{tag}"}),
        ("put", f"/units/{a['facility']}", {"phone": "+998 62 000-00-01"}),
        ("put", f"/units/{a['facility']}", {"documentFreeViewDays": 3}),
        ("post", f"/units/{a['facility']}/employees", emp(ctx, role=ctx["roles"]["admin"], unit_id=a["facility"], tag="by-leader")),
        ("put", f"/employees/{ctx['ids']['admin_f']}", {"fullName": "T-x"}),
        ("put", f"/employees/{ctx['ids']['lead_r']}", {"fullName": "T-me"}),
        ("put", f"/employees/{ctx['ids']['lead_r']}/overrides", {"allow": ["admin.unit.write"], "deny": []}),
        ("post", f"/units/{a['facility']}/categories", {"name": f"T-{tag} Lab"}),
        ("post", f"/units/{a['region']}/roles", {"name": "T-x", "key": "t-x", "permissions": ["admin.unit.read"]}),
        ("get", f"/units/{a['facility']}/reset", None),
    ]
    for method, path, body in attempts:
        r = getattr(c, method)(f"{API}{path}", json=body, headers=hl) if body is not None else c.get(f"{API}{path}", headers=hl)
        assert r.status_code == 403, (method, path, r.status_code, r.text)


def test_each_leader_role_is_pinned_to_its_level(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    hs = h(ctx, "super")
    a, b, roles = ctx["a"], ctx["b"], ctx["roles"]

    def post(role: str, unit: str, extra: list[str] | None = None, *, scope: str | None = None):
        # the path unit must cover the home unit AND the extra units (a cross-region extra → post on the country node)
        return c.post(f"{API}/units/{scope or unit}/employees", json=emp(ctx, role=roles[role], unit_id=unit, extra=extra, tag=role), headers=hs)

    # the wrong level → 422 leader_scope
    for role, unit in (("leader_region", a["facility"]), ("leader_facility", a["region"]), ("leader_district", a["region"]), ("leader_country", a["region"])):
        r = post(role, unit)
        assert r.status_code == 422 and r.json()["error"]["code"] == "leader_scope", (role, r.text)
    # the right level → 201; extra units of the same level in the same country are fine, another level is not
    assert post("leader_district", a["district"]).status_code == 201
    assert post("leader_country", a["country"]).status_code == 201
    r = post("leader_region", a["region"], [b["region"]], scope=a["country"])
    assert r.status_code == 201 and r.json()["unitIds"] == [b["region"]], r.text
    r = post("leader_region", a["region"], [a["facility"]])
    assert r.status_code == 422 and r.json()["error"]["code"] == "leader_scope"
    # moving or re-roling keeps the rule
    r = c.put(f"{API}/employees/{ctx['ids']['lead_d']}", json={"unitId": a["facility"]}, headers=hs)
    assert r.status_code == 422 and r.json()["error"]["code"] == "leader_scope"
    r = c.put(f"{API}/employees/{ctx['ids']['admin_f']}", json={"roleId": roles["leader_facility"]}, headers=hs)
    assert r.status_code == 200 and r.json()["roleId"] == roles["leader_facility"], r.text
    r = c.put(f"{API}/employees/{ctx['ids']['admin_f']}", json={"roleId": roles["leader_region"]}, headers=hs)
    assert r.status_code == 422 and r.json()["error"]["code"] == "leader_scope"
    r = c.put(f"{API}/employees/{ctx['ids']['admin_f']}", json={"roleId": roles["admin"]}, headers=hs)
    assert r.status_code == 200


def test_unit_administrators_grant_leaders_inside_their_scope(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    a, roles = ctx["a"], ctx["roles"]
    # the facility administrator: a «Muassasa rahbari» of the facility — yes; a «Viloyat rahbari» — the region is not theirs
    r = c.post(f"{API}/units/{a['facility']}/employees", json=emp(ctx, role=roles["leader_facility"], unit_id=a["facility"], tag="f-lead"), headers=h(ctx, "admin-f"))
    assert r.status_code == 201, r.text
    r = c.post(f"{API}/units/{a['region']}/employees", json=emp(ctx, role=roles["leader_region"], unit_id=a["region"], tag="f-bad"), headers=h(ctx, "admin-f"))
    assert r.status_code in (403, 404), r.text
    # the region administrator homes a «Viloyat rahbari» on the region (no superadmin needed)
    r = c.post(f"{API}/units/{a['region']}/employees", json=emp(ctx, role=roles["leader_region"], unit_id=a["region"], tag="r-lead"), headers=h(ctx, "admin-r"))
    assert r.status_code == 201, r.text
    # the leader keys are reserved for the system roles
    r = c.post(f"{API}/units/{a['region']}/roles", json={"name": "T-fake", "key": "leader_region", "permissions": ["admin.unit.read"]}, headers=h(ctx, "admin-r"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "reserved"
