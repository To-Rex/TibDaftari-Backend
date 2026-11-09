"""API tests for the catalog module against the test database (fixture rows prefixed T-catalog-).

Catalogue rows are owned by a unit of the tree and visible to its whole subtree: created at the region node here,
listed from the facility below it, invisible to the other fixture tree."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import timedelta

import pytest
from app.core.timeutil import utcnow
from app.infrastructure.db.models import Order, OrderItem, Patient, ServiceType
from fastapi.testclient import TestClient
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
PASSWORD = "T-catalog-pass-1"


@pytest.fixture(scope="module")
def ctx() -> Iterator[dict]:
    async def rows(s):
        a = await make_tree(s)
        b = await make_tree(s)
        viewer = await make_role(s, a.region, "viewer", "T-catalog Viewer", ["reception.patient.read"])
        admin_a = await make_employee(s, a.region, "admin", login=f"t-catalog-admin-a-{SFX}", password=PASSWORD)
        await make_employee(s, b.region, "admin", login=f"t-catalog-admin-b-{SFX}", password=PASSWORD)
        await make_employee(s, a.facility, role=viewer, login=f"t-catalog-viewer-a-{SFX}", password=PASSWORD)
        patient = Patient(country_id=COUNTRY_ID, registered_unit_id=a.facility.id, full_name=f"T-catalog Bemor {SFX}", phone=phone(), tags=[])
        s.add(patient)
        await s.flush()
        order = Order(unit_id=a.facility.id, number=f"{a.facility.code}-000900", patient_id=patient.id, patient_name=patient.full_name, patient_phone=patient.phone, created_by_employee_id=admin_a.id)
        s.add(order)
        await s.flush()
        return {
            "a": {k: str(getattr(a, k).id) for k in ("country", "region", "district", "facility")},
            "b": {k: str(getattr(b, k).id) for k in ("country", "region", "district", "facility")},
            "order": str(order.id),
        }

    data = seed(rows)
    with TestClient(app_instance()) as client:
        data["client"] = client
        data["tokens"] = {who: staff_token(client, f"t-catalog-{who}-{SFX}", PASSWORD) for who in ("admin-a", "admin-b", "viewer-a")}
        data["made"] = {}
        yield data


async def _add_order_items(order_id: str, unit_id: str, service_type_id: str, recent: int, old: int) -> None:
    """Direct rows: `recent` inside the 30-day window, `old` outside (created_at is client-set).
    Uses a private engine: the app engine is bound to the TestClient's event loop."""
    own = own_engine()
    async with AsyncSession(own, expire_on_commit=False) as s, s.begin():
        st = await s.get(ServiceType, uuid.UUID(service_type_id))
        assert st is not None
        now = utcnow()
        for i in range(recent + old):
            created = now - timedelta(days=1 if i < recent else 45)
            s.add(
                OrderItem(
                    order_id=uuid.UUID(order_id),
                    unit_id=uuid.UUID(unit_id),
                    service_type_id=st.id,
                    service_name=st.name,
                    category_id=st.category_id,
                    category_name="T-catalog cat",
                    price=st.price,
                    final_price=st.price,
                    created_at=created,
                    updated_at=created,
                )
            )
    await own.dispose()


def h(ctx: dict, who: str) -> dict[str, str]:
    return bearer(ctx["tokens"][who])


def test_categories_crud_and_rules(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    a, b = ctx["a"], ctx["b"]
    r = c.post(f"{API}/units/{a['region']}/categories", json={"name": "T-catalog Lab", "code": "LAB", "workflow": "lab", "color": "#0ea5e9"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 201, r.text
    root = r.json()
    assert set(root) == {"id", "ownerUnitId", "parentId", "name", "code", "icon", "color", "order", "isActive", "phone", "workflow", "createdAt", "updatedAt"}
    assert root["ownerUnitId"] == a["region"] and root["parentId"] is None and root["order"] == 1 and root["isActive"] is True and root["createdAt"].endswith("Z")
    r = c.post(f"{API}/units/{a['region']}/categories", json={"name": "T-catalog Gematologiya", "parentId": root["id"]}, headers=h(ctx, "admin-a"))
    assert r.status_code == 201, r.text
    child = r.json()
    assert child["parentId"] == root["id"] and child["order"] == 1
    # a facility may add its own sub-category under an inherited one; the region cannot hang a category under a
    # facility's (it would be invisible where its parent is not)
    r = c.post(f"{API}/units/{a['facility']}/categories", json={"name": "T-catalog Biokimyo", "parentId": child["id"]}, headers=h(ctx, "admin-a"))
    assert r.status_code == 201, r.text
    grandchild = r.json()
    assert grandchild["ownerUnitId"] == a["facility"]
    r = c.post(f"{API}/units/{a['region']}/categories", json={"name": "T-catalog Wrong", "parentId": grandchild["id"]}, headers=h(ctx, "admin-a"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "parent_not_visible"
    # list: any staff of the subtree (the viewer at the facility) sees the inherited tree, sorted by order; other tree forbidden
    lst = c.get(f"{API}/units/{a['facility']}/categories", headers=h(ctx, "viewer-a"))
    assert lst.status_code == 200 and {x["id"] for x in lst.json()} >= {root["id"], child["id"], grandchild["id"]}
    orders = [x["order"] for x in lst.json()]
    assert orders == sorted(orders)
    region_list = {x["id"] for x in c.get(f"{API}/units/{a['region']}/categories", headers=h(ctx, "admin-a")).json()}
    assert {root["id"], child["id"], grandchild["id"]} <= region_list  # the region manages what its facilities own too
    assert root["id"] not in {x["id"] for x in c.get(f"{API}/units/{b['region']}/categories", headers=h(ctx, "admin-b")).json()}
    assert c.get(f"{API}/units/{a['region']}/categories", headers=h(ctx, "admin-b")).status_code == 403
    assert c.post(f"{API}/units/{a['region']}/categories", json={"name": "x"}, headers=h(ctx, "viewer-a")).status_code == 403
    # cycle: root under its grandchild → 422; unknown parent → 404
    r = c.put(f"{API}/categories/{root['id']}", json={"parentId": grandchild["id"]}, headers=h(ctx, "admin-a"))
    assert r.status_code == 422
    assert c.put(f"{API}/categories/{root['id']}", json={"parentId": root["id"]}, headers=h(ctx, "admin-a")).status_code == 422
    r = c.put(f"{API}/categories/{root['id']}", json={"parentId": str(uuid.uuid4())}, headers=h(ctx, "admin-a"))
    assert r.status_code == 404 and r.json()["error"]["message"] == "Kategoriya topilmadi"
    # partial update + isolation on id-addressed routes (the owner is outside admin B's view → 403)
    r = c.put(f"{API}/categories/{child['id']}", json={"name": "T-catalog Gematologiya 2", "phone": "998712000000", "isActive": False}, headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and r.json()["name"] == "T-catalog Gematologiya 2" and r.json()["phone"] == "998712000000" and r.json()["isActive"] is False
    assert r.json()["parentId"] == root["id"] and r.json()["code"] is None
    r = c.put(f"{API}/categories/{child['id']}", json={"name": "hack"}, headers=h(ctx, "admin-b"))
    assert r.status_code == 403 and r.json()["error"]["message"] == "Ruxsat yo‘q"
    # delete: has children → 409
    r = c.delete(f"{API}/categories/{root['id']}", headers=h(ctx, "admin-a"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "has_children" and r.json()["error"]["message"] == "Avval ichki kategoriyalarni o‘chiring"
    ctx["made"].update({"root": root, "child": child, "grandchild": grandchild})


def test_service_types_crud_filters_stats(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    a, b = ctx["a"], ctx["b"]
    root, child, grandchild = ctx["made"]["root"], ctx["made"]["child"], ctx["made"]["grandchild"]
    fac = a["facility"]
    # the region cannot bind a service to a category only its facility sees; the facility can
    r = c.post(f"{API}/units/{a['region']}/service-types", json={"categoryId": grandchild["id"], "name": "T-catalog x"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 404 and r.json()["error"]["message"] == "Kategoriya topilmadi"
    r = c.post(f"{API}/units/{a['region']}/service-types", json={"categoryId": child["id"], "name": "T-catalog Umumiy qon tahlili", "code": "UQT", "price": 50000, "order": 2}, headers=h(ctx, "admin-a"))
    assert r.status_code == 201, r.text
    st1 = r.json()
    assert set(st1) == {"id", "ownerUnitId", "categoryId", "name", "code", "description", "price", "unitPrices", "disabledUnitIds", "effectivePrice", "available", "turnaroundDays", "order", "isActive", "schemaId", "documentScope", "defaultTemplateId", "stats", "createdAt", "updatedAt"}
    assert st1["ownerUnitId"] == a["region"] and st1["unitPrices"] == {} and st1["disabledUnitIds"] == [] and st1["effectivePrice"] is None and st1["available"] is None
    assert st1["turnaroundDays"] == 1 and st1["schemaId"] is None and st1["documentScope"] == "item" and st1["defaultTemplateId"] is None
    assert st1["stats"] == {"ordered30d": 0}
    r = c.post(f"{API}/units/{a['region']}/service-types", json={"categoryId": root["id"], "name": "T-catalog Siydik tahlili", "code": "uqt"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 409 and r.json()["error"]["message"] == "Bu kod band"
    r = c.post(f"{API}/units/{a['region']}/service-types", json={"categoryId": root["id"], "name": "T-catalog Siydik tahlili", "code": "ST", "order": 1, "isActive": False}, headers=h(ctx, "admin-a"))
    assert r.status_code == 201, r.text
    st2 = r.json()
    r = c.post(f"{API}/units/{a['region']}/service-types", json={"categoryId": str(uuid.uuid4()), "name": "x"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 404 and r.json()["error"]["message"] == "Kategoriya topilmadi"
    # order items: 3 recent + 2 old for st1 → ordered30d = 3
    asyncio.run(_add_order_items(ctx["order"], fac, st1["id"], recent=3, old=2))
    # list (the viewer at the facility may read): sorted by order asc; stats present
    lst = c.get(f"{API}/units/{fac}/service-types", headers=h(ctx, "viewer-a"))
    assert lst.status_code == 200, lst.text
    rows = {x["id"]: x for x in lst.json()}
    assert [x["id"] for x in lst.json() if x["id"] in (st1["id"], st2["id"])] == [st2["id"], st1["id"]]
    assert rows[st1["id"]]["stats"] == {"ordered30d": 3} and rows[st2["id"]]["stats"] == {"ordered30d": 0}
    # categoryId expands to descendants: root → both; child → st1 only; activeOnly drops st2; search folds
    ids = lambda r: {x["id"] for x in r.json()}  # noqa: E731
    assert ids(c.get(f"{API}/units/{a['region']}/service-types", params={"categoryId": root["id"]}, headers=h(ctx, "admin-a"))) == {st1["id"], st2["id"]}
    assert ids(c.get(f"{API}/units/{a['region']}/service-types", params={"categoryId": child["id"]}, headers=h(ctx, "admin-a"))) == {st1["id"]}
    assert ids(c.get(f"{API}/units/{a['region']}/service-types", params={"categoryId": root["id"], "activeOnly": "true"}, headers=h(ctx, "admin-a"))) == {st1["id"]}
    assert ids(c.get(f"{API}/units/{a['region']}/service-types", params={"search": "Умумий қон таҳлили"}, headers=h(ctx, "admin-a"))) == {st1["id"]}
    assert ids(c.get(f"{API}/units/{a['region']}/service-types", params={"search": "st"}, headers=h(ctx, "admin-a"))) >= {st2["id"]}
    assert not ({st1["id"], st2["id"]} & ids(c.get(f"{API}/units/{b['region']}/service-types", headers=h(ctx, "admin-b"))))
    # get + isolation
    r = c.get(f"{API}/service-types/{st1['id']}", headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and r.json()["stats"] == {"ordered30d": 3}
    assert c.get(f"{API}/service-types/{st1['id']}", headers=h(ctx, "viewer-a")).status_code == 200  # inherited at the facility
    r = c.get(f"{API}/service-types/{st1['id']}", headers=h(ctx, "admin-b"))
    assert r.status_code == 403 and r.json()["error"]["message"] == "Ruxsat yo‘q"
    r = c.get(f"{API}/service-types/{uuid.uuid4()}", headers=h(ctx, "admin-a"))
    assert r.status_code == 404 and r.json()["error"]["message"] == "Xizmat topilmadi"
    # update: unit prices wholesale (units of the owner's subtree only), code conflict, category move
    r = c.put(f"{API}/service-types/{st2['id']}", json={"unitPrices": {str(uuid.uuid4()): 70000}}, headers=h(ctx, "admin-a"))
    assert r.status_code == 404 and r.json()["error"]["message"] == "Birlik topilmadi"
    r = c.put(f"{API}/service-types/{st2['id']}", json={"unitPrices": {fac: 70000}, "price": 60000, "isActive": True, "categoryId": child["id"]}, headers=h(ctx, "admin-a"))
    assert r.status_code == 200, r.text
    assert r.json()["unitPrices"] == {fac: 70000} and r.json()["price"] == 60000 and r.json()["isActive"] is True and r.json()["categoryId"] == child["id"]
    # listed for the facility: the effective price and availability are resolved; the district keeps the base price
    at_fac = {x["id"]: x for x in c.get(f"{API}/units/{a['region']}/service-types", params={"unitId": fac}, headers=h(ctx, "admin-a")).json()}
    assert (at_fac[st2["id"]]["effectivePrice"], at_fac[st2["id"]]["available"]) == (70000, True) and (at_fac[st1["id"]]["effectivePrice"], at_fac[st1["id"]]["available"]) == (50000, True)
    at_district = {x["id"]: x for x in c.get(f"{API}/units/{a['region']}/service-types", params={"unitId": a["district"]}, headers=h(ctx, "admin-a")).json()}
    assert at_district[st2["id"]]["effectivePrice"] == 60000
    r = c.put(f"{API}/service-types/{st2['id']}", json={"unitPrices": {}}, headers=h(ctx, "admin-a"))
    assert r.json()["unitPrices"] == {}
    # per-unit availability: unknown unit → 404; a real one sticks (deduplicated); `unitId` lists flag it off; reset
    r = c.put(f"{API}/service-types/{st2['id']}", json={"disabledUnitIds": [str(uuid.uuid4())]}, headers=h(ctx, "admin-a"))
    assert r.status_code == 404 and r.json()["error"]["message"] == "Birlik topilmadi"
    r = c.put(f"{API}/service-types/{st2['id']}", json={"disabledUnitIds": [fac, fac]}, headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and r.json()["disabledUnitIds"] == [fac], r.text
    assert c.get(f"{API}/service-types/{st2['id']}", headers=h(ctx, "admin-a")).json()["disabledUnitIds"] == [fac]
    at_fac = {x["id"]: x for x in c.get(f"{API}/units/{a['region']}/service-types", params={"unitId": fac}, headers=h(ctx, "admin-a")).json()}
    assert at_fac[st2["id"]]["available"] is False and at_fac[st1["id"]]["available"] is True
    assert c.get(f"{API}/units/{a['region']}/service-types", params={"unitId": str(uuid.uuid4())}, headers=h(ctx, "admin-a")).status_code == 404
    assert c.get(f"{API}/units/{a['region']}/service-types", params={"unitId": b["facility"]}, headers=h(ctx, "admin-a")).status_code == 404
    assert st2["id"] in ids(c.get(f"{API}/units/{a['region']}/service-types", headers=h(ctx, "admin-a")))
    # switched off at the district → off at the facility too (nearest level wins); switched on again at the facility
    r = c.put(f"{API}/service-types/{st2['id']}", json={"disabledUnitIds": [a["district"]]}, headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and r.json()["disabledUnitIds"] == [a["district"]]
    assert {x["id"]: x for x in c.get(f"{API}/units/{a['region']}/service-types", params={"unitId": fac}, headers=h(ctx, "admin-a")).json()}[st2["id"]]["available"] is False
    r = c.put(f"{API}/service-types/{st2['id']}", json={"disabledUnitIds": []}, headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and r.json()["disabledUnitIds"] == []
    r = c.put(f"{API}/service-types/{st2['id']}", json={"code": "UQT"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 409 and r.json()["error"]["message"] == "Bu kod band"
    assert c.put(f"{API}/service-types/{st2['id']}", json={"name": "hack"}, headers=h(ctx, "admin-b")).status_code == 403
    # cache invalidated after write: list reflects the new price
    lst = c.get(f"{API}/units/{a['region']}/service-types", headers=h(ctx, "admin-a")).json()
    assert {x["id"]: x for x in lst}[st2["id"]]["price"] == 60000
    # delete: st1 has order items → 409 in_use; st2 soft-deleted and gone from list; then category with services → 409
    r = c.delete(f"{API}/service-types/{st1['id']}", headers=h(ctx, "admin-a"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "in_use"
    r = c.delete(f"{API}/categories/{child['id']}", headers=h(ctx, "admin-a"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "has_children"
    r = c.delete(f"{API}/categories/{grandchild['id']}", headers=h(ctx, "admin-a"))
    assert r.status_code == 204
    r = c.delete(f"{API}/categories/{child['id']}", headers=h(ctx, "admin-a"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "in_use" and r.json()["error"]["message"] == "Kategoriyada xizmatlar bor"
    assert c.delete(f"{API}/service-types/{st2['id']}", headers=h(ctx, "admin-b")).status_code == 403
    assert c.delete(f"{API}/service-types/{st2['id']}", headers=h(ctx, "admin-a")).status_code == 204
    assert c.get(f"{API}/service-types/{st2['id']}", headers=h(ctx, "admin-a")).status_code == 404
    assert st2["id"] not in ids(c.get(f"{API}/units/{a['region']}/service-types", headers=h(ctx, "admin-a")))
    ctx["made"]["st1"] = st1


def test_schemas_lifecycle(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    a = ctx["a"]
    st1 = ctx["made"]["st1"]
    r = c.post(f"{API}/units/{a['region']}/schemas", json={"name": "T-catalog UQT sxema", "description": "d"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 201, r.text
    sch = r.json()
    assert set(sch) == {"id", "ownerUnitId", "name", "description", "version", "status", "fields", "usedBy", "createdAt", "updatedAt"}
    assert sch["ownerUnitId"] == a["region"] and sch["version"] == 1 and sch["status"] == "draft" and sch["fields"] == [] and sch["usedBy"] == 0
    # publish empty → 422 empty
    r = c.post(f"{API}/schemas/{sch['id']}/publish", headers=h(ctx, "admin-a"))
    assert r.status_code == 422 and r.json()["error"] == {"code": "empty", "message": "Kamida bitta maydon kerak"}
    fields = [
        {"key": "hb", "label": "Gemoglobin", "type": "number", "required": True, "order": 1, "unit": "g/l", "references": [{"gender": "male", "min": 130, "max": 160}]},
        {"key": "note", "label": "Izoh", "type": "longtext", "required": False, "order": 2, "placeholder": "..."},
    ]
    # duplicate keys → 422; bad type → 422 (request validation)
    r = c.put(f"{API}/schemas/{sch['id']}", json={"fields": [fields[0], {**fields[1], "key": "hb"}]}, headers=h(ctx, "admin-a"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    r = c.put(f"{API}/schemas/{sch['id']}", json={"fields": [{**fields[0], "type": "weird"}]}, headers=h(ctx, "admin-a"))
    assert r.status_code == 422
    # draft: fields saved as-is (extras kept), no version bump
    r = c.put(f"{API}/schemas/{sch['id']}", json={"fields": fields}, headers=h(ctx, "admin-a"))
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 1 and r.json()["fields"] == fields and r.json()["status"] == "draft"
    # publish → published, version stays 1; then edit fields → version 2, status unchanged; name-only edit → still 2
    r = c.post(f"{API}/schemas/{sch['id']}/publish", headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and r.json()["status"] == "published" and r.json()["version"] == 1
    r = c.put(f"{API}/schemas/{sch['id']}", json={"fields": fields[:1]}, headers=h(ctx, "admin-a"))
    assert r.json()["version"] == 2 and r.json()["status"] == "published" and len(r.json()["fields"]) == 1
    r = c.put(f"{API}/schemas/{sch['id']}", json={"name": "T-catalog UQT sxema v2"}, headers=h(ctx, "admin-a"))
    assert r.json()["version"] == 2 and r.json()["name"] == "T-catalog UQT sxema v2"
    # bind to service type → usedBy 1 in list and get; unknown schema → 404
    r = c.put(f"{API}/service-types/{st1['id']}", json={"schemaId": sch["id"]}, headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and r.json()["schemaId"] == sch["id"]
    r = c.put(f"{API}/service-types/{st1['id']}", json={"schemaId": str(uuid.uuid4())}, headers=h(ctx, "admin-a"))
    assert r.status_code == 404 and r.json()["error"]["message"] == "Sxema topilmadi"
    lst = c.get(f"{API}/units/{a['facility']}/schemas", headers=h(ctx, "viewer-a"))
    assert lst.status_code == 200 and {x["id"]: x["usedBy"] for x in lst.json()}[sch["id"]] == 1
    assert c.get(f"{API}/schemas/{sch['id']}", headers=h(ctx, "admin-a")).json()["usedBy"] == 1
    # isolation + permissions
    assert c.get(f"{API}/schemas/{sch['id']}", headers=h(ctx, "admin-b")).status_code == 403
    assert c.get(f"{API}/units/{a['region']}/schemas", headers=h(ctx, "admin-b")).status_code == 403
    assert c.post(f"{API}/schemas/{sch['id']}/publish", headers=h(ctx, "viewer-a")).status_code == 403
    assert c.post(f"{API}/units/{a['region']}/schemas", json={"name": "x"}, headers=h(ctx, "viewer-a")).status_code == 403
