"""Employee ↔ category binding: restricted staff only see / act on items of their categories (test database)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from app.infrastructure.db.models import AttributeSchema, Category, Patient, ServiceType
from fastapi.testclient import TestClient

from tests.helpers import API, COUNTRY_ID, app_instance, bearer, make_employee, make_tree, phone, seed, staff_session

RUN = uuid.uuid4().hex[:8]


@pytest.fixture(scope="module")
def env() -> Iterator[dict[str, Any]]:
    async def rows(s):
        a = await make_tree(s)
        admin = await make_employee(s, a.region, "admin", login=f"t-catres-admin-{RUN}")
        cat_a = Category(owner_unit_id=a.region.id, name=f"T-catres A {RUN}", sort_order=1)
        cat_b = Category(owner_unit_id=a.region.id, name=f"T-catres B {RUN}", sort_order=2)
        schema = AttributeSchema(owner_unit_id=a.region.id, name=f"T-catres sxema {RUN}", status="published", fields=[{"key": "hb", "label": "Hb", "type": "number", "required": True, "order": 1}])
        patient = Patient(country_id=COUNTRY_ID, registered_unit_id=a.facility.id, full_name=f"T-catres Bemor {RUN}", phone=phone(), tags=[])
        s.add_all([cat_a, cat_b, schema, patient])
        await s.flush()
        st_a = ServiceType(owner_unit_id=a.region.id, category_id=cat_a.id, name=f"T-catres xizmat A {RUN}", price=10000, schema_id=schema.id)
        st_b = ServiceType(owner_unit_id=a.region.id, category_id=cat_b.id, name=f"T-catres xizmat B {RUN}", price=20000, schema_id=schema.id)
        s.add_all([st_a, st_b])
        await s.flush()
        return {"facility": str(a.facility.id), "region": str(a.region.id), "admin": admin.login, "cat_a": str(cat_a.id), "cat_b": str(cat_b.id), "st_a": str(st_a.id), "st_b": str(st_b.id), "patient": str(patient.id)}

    data = seed(rows)
    with TestClient(app_instance()) as c:
        data["client"] = c
        h = bearer(staff_session(c, data["admin"])["accessToken"])
        data["h"] = h
        # two paid cheques → four items in the worklist (two per category)
        for services in ([data["st_a"], data["st_b"]], [data["st_a"], data["st_b"]]):
            r = c.post(f"{API}/units/{data['facility']}/orders", json={"patientId": data["patient"], "serviceTypeIds": services}, headers=h)
            assert r.status_code == 201, r.text
            r = c.post(f"{API}/orders/{r.json()['order']['id']}/pay", json={"amount": r.json()["order"]["total"], "method": "cash"}, headers=h)
            assert r.status_code == 200, r.text
        yield data


def test_restricted_employee_sees_only_own_categories(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    h = env["h"]
    fac = env["facility"]
    roles = c.get(f"{API}/units/{fac}/roles", headers=h).json()
    lab_role = next(r for r in roles if r["key"] == "laborant")
    login = f"t-catres-{uuid.uuid4().hex[:8]}"
    r = c.post(f"{API}/units/{fac}/employees", headers=h, json={"fullName": "T-catres laborant", "login": login, "password": "123456", "roleId": lab_role["id"], "categoryIds": [env["cat_a"]]})
    assert r.status_code == 201, r.text
    assert r.json()["categoryIds"] == [env["cat_a"]] and r.json()["unitId"] == fac
    hl = bearer(staff_session(c, login)["accessToken"])

    # worklist without a filter → only category A
    wl = c.get(f"{API}/units/{fac}/worklist", headers=hl, params={"pageSize": 50}).json()
    assert wl["total"] == 2 and {row["categoryId"] for row in wl["items"]} == {env["cat_a"]}
    # explicit filter on a foreign category → empty, not a leak
    wl_b = c.get(f"{API}/units/{fac}/worklist", headers=hl, params={"pageSize": 5, "categoryIds": env["cat_b"]}).json()
    assert wl_b["total"] == 0 and wl_b["items"] == []
    assert c.get(f"{API}/units/{fac}/worklist/counts", headers=hl, params={"categoryIds": env["cat_b"]}).json()["all"] == 0

    # admin (unrestricted) sees category B rows; the restricted user may not touch them
    wl_admin_b = c.get(f"{API}/units/{fac}/worklist", headers=h, params={"pageSize": 1, "categoryIds": env["cat_b"], "status": ["pending", "entered"]}).json()
    assert wl_admin_b["total"] == 2
    foreign = wl_admin_b["items"][0]
    r = c.put(f"{API}/items/{foreign['id']}/values", headers=hl, json={"values": foreign["values"], "labNote": "x"})
    assert r.status_code == 403, r.text
    assert r.json()["error"]["message"] == "Bu kategoriya sizga biriktirilmagan"
    assert c.get(f"{API}/items/{foreign['id']}", headers=hl).status_code == 200  # reading the row is fine, acting on it is not

    # own-category item is editable
    own = next(row for row in wl["items"] if row["status"] in ("pending", "entered"))
    r = c.put(f"{API}/items/{own['id']}/values", headers=hl, json={"values": {"hb": 12}, "labNote": "ok"})
    assert r.status_code == 200 and r.json()["status"] == "entered", r.text


def test_worklist_counts_match_list_totals(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    h = env["h"]
    fac = env["facility"]
    counts = c.get(f"{API}/units/{fac}/worklist/counts", headers=h).json()
    assert set(counts) == {"all", "pending", "entered", "submitted", "approved", "rejected", "cancelled"}
    assert counts["all"] == sum(v for k, v in counts.items() if k != "all") == 4
    entered = c.get(f"{API}/units/{fac}/worklist", headers=h, params={"pageSize": 1, "status": "entered"}).json()
    assert entered["total"] == counts["entered"] == 1
    # the region sees the same worklist (everything under it); another facility of the tree narrows to nothing
    assert c.get(f"{API}/units/{env['region']}/worklist/counts", headers=h).json()["all"] == 4
    assert c.get(f"{API}/units/{env['region']}/worklist/counts", headers=h, params={"unitId": str(uuid.uuid4())}).json()["all"] == 0
