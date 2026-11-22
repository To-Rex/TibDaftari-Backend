"""The company — the root of the organisation tree (Kompaniya → Davlat → Viloyat → Tuman → Muassasa).

What must hold: everything that existed hangs under one company and works as before; a second company is a whole
world of its own — its own country nodes (the same country, the same viloyat, the same cheque prefix may live in
both), its own patient registry (the same passport in two companies is two people's rows, and neither company finds
the other's), its own staff scope. Company-level people — an administrator, the read-only «Kompaniya rahbari» —
see the whole company and nothing outside it. Fixture rows are prefixed `T-`."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from app.core.permissions import LEADER_PERMISSIONS
from app.infrastructure.db.models import OrgUnit, Patient
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.helpers import (
    API,
    COMPANY_UNIT_ID,
    COUNTRY_ID,
    app_instance,
    bearer,
    db,
    make_company,
    make_employee,
    make_tree,
    phone,
    rand_tag,
    seed,
    staff_session,
    staff_token,
    system_role,
)

RANGE = "dateFrom=2026-11-01&dateTo=2026-11-22"
#: Xorazm viloyati — a real territory the seeded company already has a node for
XORAZM_GEO_ID = "01a00cdd-5629-7c75-9ba6-16755c8680d8"
LEVELS = ("country", "region", "district", "facility")


@pytest.fixture(scope="module")
def ctx() -> Iterator[dict]:
    async def rows(s):
        a = await make_tree(s)  # in the seeded company
        other = await make_company(s)
        b = await make_tree(s, country=other.country)  # in the company of its own
        sup = await make_employee(s, None, super_admin=True)
        admin_a = await make_employee(s, a.region, "admin")
        reg_a = await make_employee(s, a.facility, "registrator")
        admin_c = await make_employee(s, other.company, "admin")
        lead_c = await make_employee(s, other.company, "leader_company")
        reg_b = await make_employee(s, b.facility, "registrator")
        roles = {k: str((await system_role(s, k)).id) for k in ("leader_company", "leader_country", "installer", "admin")}
        return {
            "a": {k: str(getattr(a, k).id) for k in LEVELS},
            "b": {k: str(getattr(b, k).id) for k in LEVELS},
            "company": str(other.company.id),
            "company_name": other.company.name,
            "code_a": a.facility.code,
            "tag": other.tag,
            "roles": roles,
            "logins": {"super": sup.login, "admin-a": admin_a.login, "reg-a": reg_a.login, "admin-c": admin_c.login, "lead-c": lead_c.login, "reg-b": reg_b.login},
        }

    data = seed(rows)
    with TestClient(app_instance()) as client:
        data["client"] = client
        data["tokens"] = {who: staff_token(client, login) for who, login in data["logins"].items()}
        data["state"] = {}
        yield data


def h(ctx: dict, who: str) -> dict[str, str]:
    return bearer(ctx["tokens"][who])


def emp(ctx: dict, *, role: str, unit_id: str, extra: list[str] | None = None, tag: str = "x") -> dict:
    return {"fullName": f"T-{ctx['tag']} {tag}", "login": f"t-{tag}-{uuid.uuid4().hex[:8]}", "roleId": role, "unitId": unit_id, "unitIds": extra or [], "password": "123456"}


def test_everything_hangs_under_a_company(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    a, b = ctx["a"], ctx["b"]
    tree = {u["id"]: u for u in c.get(f"{API}/units/tree", headers=h(ctx, "super")).json()}
    seeded, own = tree[str(COMPANY_UNIT_ID)], tree[ctx["company"]]
    for company in (seeded, own):
        assert company["level"] == "company" and company["parentId"] is None and company["depth"] == 0
        assert company["countryId"] is None and company["regionId"] is None and company["companyId"] == company["id"]
        assert company["path"] == company["name"] and company["timezone"] == "Asia/Tashkent"
    # every node says which company it lives in; the chain is one level deeper than before the company
    assert {tree[a[k]]["companyId"] for k in LEVELS} == {seeded["id"]} and {tree[b[k]]["companyId"] for k in LEVELS} == {own["id"]}
    assert tree[a["country"]]["parentId"] == seeded["id"] and tree[b["country"]]["parentId"] == own["id"]
    assert [tree[b[k]]["depth"] for k in LEVELS] == [1, 2, 3, 4]
    assert tree[b["facility"]]["path"].startswith(f"{own['name']} › ") and tree[b["facility"]]["companyName"] == own["name"]
    # both companies work in the same country: one country node each
    assert tree[a["country"]]["countryId"] == tree[b["country"]]["countryId"] == str(COUNTRY_ID) and a["country"] != b["country"]
    assert own["counts"] == {"countries": 1, "regions": 1, "districts": 1, "facilities": 1, "employees": 3}
    # one company of the tree only
    only = c.get(f"{API}/units/tree", params={"companyId": ctx["company"]}, headers=h(ctx, "super")).json()
    assert {u["companyId"] for u in only} == {ctx["company"]} and {b[k] for k in LEVELS} <= {u["id"] for u in only}


def test_companies_are_made_by_the_platform(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    name = f"T-{rand_tag()} xolding"
    # a company administrator rules their company — they do not open another one
    assert c.post(f"{API}/units", json={"name": name}, headers=h(ctx, "admin-c")).status_code == 403
    r = c.post(f"{API}/units", json={"name": name, "code": f"th{ctx['tag']}", "phone": "998712000000"}, headers=h(ctx, "super"))
    assert r.status_code == 201, r.text
    comp = r.json()
    assert comp["level"] == "company" and comp["parentId"] is None and comp["companyId"] == comp["id"] and comp["countryId"] is None
    assert comp["depth"] == 0 and comp["path"] == name and comp["code"] == f"TH{ctx['tag']}" and comp["kind"] is None
    assert comp["counts"] == {"countries": 0, "regions": 0, "districts": 0, "facilities": 0, "employees": 0}
    ctx["state"]["new_company"] = comp["id"]
    # one company per name (case-insensitive)
    r = c.post(f"{API}/units", json={"name": name.upper()}, headers=h(ctx, "super"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "company_exists"
    r = c.put(f"{API}/units/{comp['id']}", json={"name": ctx["company_name"]}, headers=h(ctx, "super"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "company_exists"
    r = c.put(f"{API}/units/{comp['id']}", json={"name": f"{name} 2", "shortName": "T-X"}, headers=h(ctx, "super"))
    assert r.status_code == 200 and r.json()["name"] == f"{name} 2" and r.json()["path"] == f"{name} 2"

    # a country node hangs under a company: the country is named, and there is one node per country in a company
    r = c.post(f"{API}/units", json={"parentId": comp["id"], "name": "T-davlat"}, headers=h(ctx, "super"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "country_required"
    r = c.post(f"{API}/units", json={"parentId": comp["id"], "countryId": str(uuid.uuid4()), "name": "T-davlat"}, headers=h(ctx, "super"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "country_required"
    kz = next(x for x in c.get(f"{API}/countries").json() if x["code"] == "KZ")
    r = c.post(f"{API}/units", json={"parentId": comp["id"], "countryId": kz["id"], "name": f"T-{ctx['tag']} Qozog‘iston", "code": "kz"}, headers=h(ctx, "super"))
    assert r.status_code == 201, r.text
    node = r.json()
    assert node["level"] == "country" and node["countryId"] == kz["id"] and node["companyId"] == comp["id"] and node["parentId"] == comp["id"]
    assert node["depth"] == 1 and node["path"] == f"{name} 2 › T-{ctx['tag']} Qozog‘iston" and node["regionId"] is None
    r = c.post(f"{API}/units", json={"parentId": comp["id"], "countryId": kz["id"], "name": "T-dup"}, headers=h(ctx, "super"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "node_exists"
    # the country every other company works in is free here too
    r = c.post(f"{API}/units", json={"parentId": comp["id"], "countryId": str(COUNTRY_ID), "name": f"T-{ctx['tag']} O‘zbekiston 2"}, headers=h(ctx, "super"))
    assert r.status_code == 201 and r.json()["timezone"] == "Asia/Tashkent"
    assert c.get(f"{API}/units/{comp['id']}", headers=h(ctx, "super")).json()["counts"]["countries"] == 2
    assert next(x for x in c.get(f"{API}/countries").json() if x["code"] == "KZ")["isEnabled"] is True


def test_the_same_territory_and_code_live_in_two_companies(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    b, tag = ctx["b"], ctx["tag"]
    # Xorazm already has a node in the seeded company — this company gets its own
    r = c.post(f"{API}/units", json={"parentId": b["country"], "regionId": XORAZM_GEO_ID, "name": f"T-{tag} Xorazm", "code": f"tx{tag}"}, headers=h(ctx, "admin-c"))
    assert r.status_code == 201, r.text
    assert r.json()["companyId"] == ctx["company"] and r.json()["regionId"] == XORAZM_GEO_ID and r.json()["depth"] == 2
    r = c.post(f"{API}/units", json={"parentId": b["country"], "regionId": XORAZM_GEO_ID, "name": "T-dup"}, headers=h(ctx, "admin-c"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "node_exists"
    # a cheque prefix is unique inside a company, not across companies
    body = {"parentId": b["district"], "name": f"T-{tag} klinika", "kind": "clinic", "code": ctx["code_a"]}
    r = c.post(f"{API}/units", json=body, headers=h(ctx, "admin-c"))
    assert r.status_code == 201, r.text
    assert r.json()["code"] == ctx["code_a"] and r.json()["companyId"] == ctx["company"]
    r = c.post(f"{API}/units", json={**body, "name": f"T-{tag} klinika 2"}, headers=h(ctx, "admin-c"))
    assert r.status_code == 409 and r.json()["error"]["message"] == "Bu kod band"
    # the database says the same: a node cannot be handed to another company
    async def move(s):
        unit = await s.get(OrgUnit, uuid.UUID(b["region"]))
        unit.parent_id = uuid.UUID(ctx["a"]["country"])
        try:
            await s.flush()
        except Exception as exc:
            await s.rollback()
            return type(exc).__name__
        await s.rollback()
        return None

    assert db(c, move) is not None


def test_company_people_see_their_company_only(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    a, b = ctx["a"], ctx["b"]
    s = staff_session(c, ctx["logins"]["admin-c"])
    assert s["unitId"] == ctx["company"] and s["unit"]["level"] == "company" and s["unit"]["countryId"] is None
    assert s["unit"]["companyId"] == ctx["company"] and s["unit"]["path"] == ctx["company_name"] and s["facilityId"] is None
    mine = {u["id"] for u in c.get(f"{API}/units/tree", headers=h(ctx, "admin-c")).json()}
    assert {ctx["company"], *b.values()} <= mine and not ({str(COMPANY_UNIT_ID), *a.values()} & mine)
    assert c.get(f"{API}/units/{a['region']}", headers=h(ctx, "admin-c")).status_code == 403
    assert c.get(f"{API}/units/{COMPANY_UNIT_ID}", headers=h(ctx, "admin-c")).status_code == 403
    # a region administrator of the seeded company: their company above them as context, nothing of the other one
    theirs = {u["id"] for u in c.get(f"{API}/units/tree", headers=h(ctx, "admin-a")).json()}
    assert str(COMPANY_UNIT_ID) in theirs and not ({ctx["company"], *b.values()} & theirs)
    assert c.get(f"{API}/units/{ctx['company']}", headers=h(ctx, "admin-a")).status_code == 403
    assert c.post(f"{API}/units", json={"parentId": b["district"], "name": "T-hack", "kind": "ses", "code": "THACK"}, headers=h(ctx, "admin-a")).status_code == 403
    # the company's own card is the administrator's to edit; lists work from the company node
    r = c.put(f"{API}/units/{ctx['company']}", json={"phone": "998712223344", "documentFreeViewDays": 7}, headers=h(ctx, "admin-c"))
    assert r.status_code == 200 and r.json()["phone"] == "998712223344" and r.json()["settings"]["documentFreeViewDays"] == 7
    # … and the setting is inherited all the way down
    assert c.get(f"{API}/units/{b['facility']}", headers=h(ctx, "admin-c")).json()["settings"]["documentFreeViewDays"] == 7
    assert c.get(f"{API}/units/{a['facility']}", headers=h(ctx, "admin-a")).json()["settings"]["documentFreeViewDays"] == 0
    employees = c.get(f"{API}/units/{ctx['company']}/employees", headers=h(ctx, "admin-c"))
    assert employees.status_code == 200 and {e["login"] for e in employees.json()["items"]} >= {ctx["logins"]["admin-c"], ctx["logins"]["reg-b"]}
    assert ctx["logins"]["reg-a"] not in {e["login"] for e in employees.json()["items"]}
    for path in ("reports/dashboard", "reports/patients", "reports/sms"):
        assert c.get(f"{API}/units/{ctx['company']}/{path}?{RANGE}", headers=h(ctx, "admin-c")).status_code == 200, path


def test_patients_are_never_shared_between_companies(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    a, b = ctx["a"], ctx["b"]
    passport, number = f"TC{uuid.uuid4().hex[:7].upper()}", phone("99895")
    body = {"fullName": f"T-{ctx['tag']} Bemor", "phone": number, "passportNumber": passport}
    r = c.post(f"{API}/units/{a['facility']}/patients", json=body, headers=h(ctx, "reg-a"))
    assert r.status_code == 201, r.text
    pa = r.json()
    # the same passport and number in the other company: a different registry, a different row
    r = c.post(f"{API}/units/{b['facility']}/patients", json=body, headers=h(ctx, "reg-b"))
    assert r.status_code == 201, r.text
    pb = r.json()
    assert pb["id"] != pa["id"] and pb["phoneShared"] == 0 and pa["countryId"] == pb["countryId"] == str(COUNTRY_ID)
    # … while inside one company the passport is still one person's
    r = c.post(f"{API}/units/{b['facility']}/patients", json={**body, "phone": phone()}, headers=h(ctx, "reg-b"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "duplicate_passport"
    # nobody finds the other company's patient: search, duplicates, the card, the family
    for who, unit, own, foreign in (("reg-b", b["facility"], pb, pa), ("reg-a", a["facility"], pa, pb)):
        found = {p["id"] for p in c.get(f"{API}/units/{unit}/patients/search", params={"q": passport}, headers=h(ctx, who)).json()}
        assert own["id"] in found and foreign["id"] not in found, who
        dup = {p["id"] for p in c.post(f"{API}/units/{unit}/patients/duplicates", json={"phone": number, "passportNumber": passport}, headers=h(ctx, who)).json()}
        assert dup == {own["id"]}, who
        assert c.get(f"{API}/patients/{foreign['id']}", headers=h(ctx, who)).status_code == 404
        assert c.get(f"{API}/patients/{own['id']}/family", headers=h(ctx, who)).json()["members"] == []
    # the company node lists the company's patients — every country of it, nobody else's
    listed = c.get(f"{API}/units/{ctx['company']}/patients", params={"search": passport}, headers=h(ctx, "admin-c"))
    assert listed.status_code == 200 and [p["id"] for p in listed.json()["items"]] == [pb["id"]]
    # registering from the company node needs a facility — and files the patient in that facility's country
    r = c.post(f"{API}/units/{ctx['company']}/patients", json={"fullName": f"T-{ctx['tag']} Markaz", "phone": phone()}, headers=h(ctx, "admin-c"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "facility_required"
    r = c.post(f"{API}/units/{ctx['company']}/patients", json={"fullName": f"T-{ctx['tag']} Markaz", "phone": phone(), "registeredUnitId": b["facility"]}, headers=h(ctx, "admin-c"))
    assert r.status_code == 201 and r.json()["countryId"] == str(COUNTRY_ID) and r.json()["registeredUnitId"] == b["facility"]

    async def companies(s):
        rows = (await s.execute(select(Patient.id, Patient.company_id).where(Patient.id.in_([uuid.UUID(pa["id"]), uuid.UUID(pb["id"])])))).all()
        return {str(i): str(co) for i, co in rows}

    assert db(c, companies) == {pa["id"]: str(COMPANY_UNIT_ID), pb["id"]: ctx["company"]}


def test_the_company_leader_looks_and_changes_nothing(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    b, roles = ctx["b"], ctx["roles"]
    s = staff_session(c, ctx["logins"]["lead-c"])
    assert s["roleKey"] == "leader_company" and s["roleName"] == "Kompaniya rahbari" and s["unit"]["level"] == "company"
    assert set(s["permissions"]) == set(LEADER_PERMISSIONS)
    for path in ("reports/dashboard", "reports/patients", "reports/sms", "patients", "employees"):
        sep = "&" if "?" in path else "?"
        assert c.get(f"{API}/units/{ctx['company']}/{path}{sep}{RANGE}", headers=h(ctx, "lead-c")).status_code == 200, path
    assert c.get(f"{API}/platform/regions?{RANGE}&unitId={ctx['company']}", headers=h(ctx, "lead-c")).status_code == 200
    assert c.put(f"{API}/units/{ctx['company']}", json={"name": "T-hack"}, headers=h(ctx, "lead-c")).status_code == 403
    assert c.post(f"{API}/units", json={"parentId": ctx["company"], "countryId": str(COUNTRY_ID), "name": "T-hack"}, headers=h(ctx, "lead-c")).status_code == 403
    assert c.post(f"{API}/units/{b['facility']}/patients", json={"fullName": "T-hack", "phone": phone()}, headers=h(ctx, "lead-c")).status_code == 403
    # the role is pinned to the company level; an installer may now be given a whole company
    r = c.post(f"{API}/units/{ctx['company']}/employees", json=emp(ctx, role=roles["leader_company"], unit_id=b["country"], tag="lc1"), headers=h(ctx, "super"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "leader_scope" and r.json()["error"]["details"]["level"] == "company"
    r = c.post(f"{API}/units/{ctx['company']}/employees", json=emp(ctx, role=roles["leader_country"], unit_id=ctx["company"], tag="lc2"), headers=h(ctx, "super"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "leader_scope" and r.json()["error"]["details"]["level"] == "country"
    r = c.post(f"{API}/units/{ctx['company']}/employees", json=emp(ctx, role=roles["leader_company"], unit_id=ctx["company"], tag="lc3"), headers=h(ctx, "super"))
    assert r.status_code == 201 and r.json()["unitLevel"] == "company"
    r = c.post(f"{API}/units/{ctx['company']}/employees", json=emp(ctx, role=roles["installer"], unit_id=ctx["company"], tag="inst"), headers=h(ctx, "super"))
    assert r.status_code == 201, r.text
    # nobody is handed units of two companies: no unit of the tree holds both, so the request cannot even be made
    r = c.post(f"{API}/units/{ctx['company']}/employees", json=emp(ctx, role=roles["installer"], unit_id=b["region"], extra=[ctx["a"]["region"]], tag="inst2"), headers=h(ctx, "super"))
    assert r.status_code == 404 and r.json()["error"]["message"] == "Birlik topilmadi"
    r = c.post(f"{API}/units/{ctx['company']}/employees", json=emp(ctx, role=roles["admin"], unit_id=b["facility"], extra=[ctx["a"]["facility"]], tag="adm2"), headers=h(ctx, "super"))
    assert r.status_code == 404


def test_activity_by_company(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    r = c.get(f"{API}/platform/regions?{RANGE}&groupBy=company", headers=h(ctx, "super"))
    assert r.status_code == 200, r.text
    out = r.json()
    rows = {x["id"]: x for x in out["rows"]}
    assert out["groupBy"] == "company" and {str(COMPANY_UNIT_ID), ctx["company"]} <= set(rows)
    own = rows[ctx["company"]]
    assert own["kind"] == "company" and own["unitId"] == ctx["company"] and own["name"] == ctx["company_name"] and own["units"] == 2
    # one company: its territories only, each with the company's own node
    r = c.get(f"{API}/platform/regions?{RANGE}&companyId={ctx['company']}", headers=h(ctx, "super"))
    out = r.json()
    assert r.status_code == 200 and out["groupBy"] == "region" and out["total"]["units"] == 2
    nodes = {x["unitId"] for x in out["rows"]}
    assert ctx["b"]["region"] in nodes and ctx["a"]["region"] not in nodes
    r = c.get(f"{API}/platform/regions?{RANGE}&companyId={ctx['company']}&groupBy=country", headers=h(ctx, "super"))
    assert [(x["id"], x["unitId"], x["units"]) for x in r.json()["rows"]] == [(str(COUNTRY_ID), ctx["b"]["country"], 2)]
    # a company administrator asks for their own company; the whole platform stays closed
    r = c.get(f"{API}/platform/regions?{RANGE}&unitId={ctx['company']}", headers=h(ctx, "admin-c"))
    assert r.status_code == 200 and r.json()["groupBy"] == "region" and r.json()["total"]["units"] == 2
    assert c.get(f"{API}/platform/regions?{RANGE}&groupBy=company", headers=h(ctx, "admin-c")).status_code == 403
    assert c.get(f"{API}/platform/regions?{RANGE}&unitId={COMPANY_UNIT_ID}", headers=h(ctx, "admin-c")).status_code == 403


def test_quick_start_opens_under_a_country_not_a_company(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    r = c.get(f"{API}/units/{ctx['company']}/quick-start", headers=h(ctx, "admin-c"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "country_required"
    r = c.get(f"{API}/units/{ctx['b']['country']}/quick-start", headers=h(ctx, "admin-c"))
    assert r.status_code == 200, r.text
