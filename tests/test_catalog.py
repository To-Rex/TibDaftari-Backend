"""API tests for the catalog module against the real dev DB (fixture rows prefixed T-catalog-)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import timedelta

import pytest
from app.core.config import settings
from app.core.permissions import COMPANY_ADMIN_PERMISSIONS
from app.core.security import hash_password
from app.core.timeutil import utcnow
from app.infrastructure.db.models import Company, Employee, OrderItem, Role, ServiceType
from app.infrastructure.db.session import engine, session_scope
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

settings.workers_enabled = False
settings.telegram_enabled = False

SFX = uuid.uuid4().hex[:8]
PASSWORD = "T-catalog-pass-1"


async def _seed() -> dict[str, str]:
    async with session_scope() as s:
        a = Company(name=f"T-catalog-A-{SFX}", slug=f"t-catalog-a-{SFX}")
        b = Company(name=f"T-catalog-B-{SFX}", slug=f"t-catalog-b-{SFX}")
        s.add_all([a, b])
        await s.flush()
        admin_a = Role(company_id=a.id, key="admin", name="Admin", permissions=list(COMPANY_ADMIN_PERMISSIONS), is_system=True)
        admin_b = Role(company_id=b.id, key="admin", name="Admin", permissions=list(COMPANY_ADMIN_PERMISSIONS), is_system=True)
        viewer_a = Role(company_id=a.id, key="viewer", name="Viewer", permissions=["reception.patient.read"], is_system=False)
        s.add_all([admin_a, admin_b, viewer_a])
        await s.flush()
        pw = hash_password(PASSWORD)
        s.add_all(
            [
                Employee(company_id=a.id, full_name="T-catalog Admin A", login=f"t-catalog-admin-a-{SFX}", password_hash=pw, role_id=admin_a.id),
                Employee(company_id=b.id, full_name="T-catalog Admin B", login=f"t-catalog-admin-b-{SFX}", password_hash=pw, role_id=admin_b.id),
                Employee(company_id=a.id, full_name="T-catalog Viewer A", login=f"t-catalog-viewer-a-{SFX}", password_hash=pw, role_id=viewer_a.id),
            ]
        )
        ids = {"a": str(a.id), "b": str(b.id)}
    await engine.dispose()
    return ids


async def _add_order_items(company_id: str, service_type_id: str, recent: int, old: int) -> None:
    """Direct rows: `recent` inside the 30-day window, `old` outside (created_at is client-set).
    Uses a private engine: the app engine is bound to the TestClient's event loop."""
    own = create_async_engine(settings.sqlalchemy_url)
    async with AsyncSession(own, expire_on_commit=False) as s, s.begin():
        st = await s.get(ServiceType, uuid.UUID(service_type_id))
        assert st is not None
        now = utcnow()
        for i in range(recent + old):
            created = now - timedelta(days=1 if i < recent else 45)
            s.add(
                OrderItem(
                    company_id=uuid.UUID(company_id),
                    order_id=uuid.uuid4(),
                    branch_id=uuid.uuid4(),
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


@pytest.fixture(scope="module")
def ctx() -> Iterator[dict[str, object]]:
    ids = asyncio.run(_seed())
    with TestClient(app_instance()) as client:
        tokens = {}
        for who in ("admin-a", "admin-b", "viewer-a"):
            r = client.post("/api/v1/auth/staff/login", json={"login": f"t-catalog-{who}-{SFX}", "password": PASSWORD})
            assert r.status_code == 200, r.text
            tokens[who] = r.json()["accessToken"]
        yield {"client": client, "ids": ids, "tokens": tokens, "made": {}}


def app_instance():
    from app.main import app

    return app


def h(ctx: dict, who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {ctx['tokens'][who]}"}


def test_categories_crud_and_rules(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    cid = ctx["ids"]["a"]
    r = c.post(f"/api/v1/companies/{cid}/categories", json={"name": "T-catalog Lab", "code": "LAB", "workflow": "lab", "color": "#0ea5e9"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 201, r.text
    root = r.json()
    assert set(root) == {"id", "companyId", "parentId", "name", "code", "icon", "color", "order", "isActive", "phone", "workflow", "createdAt", "updatedAt"}
    assert root["companyId"] == cid and root["parentId"] is None and root["order"] == 1 and root["isActive"] is True and root["createdAt"].endswith("Z")
    r = c.post(f"/api/v1/companies/{cid}/categories", json={"name": "T-catalog Gematologiya", "parentId": root["id"]}, headers=h(ctx, "admin-a"))
    assert r.status_code == 201, r.text
    child = r.json()
    assert child["parentId"] == root["id"] and child["order"] == 1
    r = c.post(f"/api/v1/companies/{cid}/categories", json={"name": "T-catalog Biokimyo", "parentId": child["id"]}, headers=h(ctx, "admin-a"))
    grandchild = r.json()
    # list: any staff of the company (viewer) sees all, sorted by order; other company forbidden
    lst = c.get(f"/api/v1/companies/{cid}/categories", headers=h(ctx, "viewer-a"))
    assert lst.status_code == 200 and {x["id"] for x in lst.json()} >= {root["id"], child["id"], grandchild["id"]}
    orders = [x["order"] for x in lst.json()]
    assert orders == sorted(orders)
    assert c.get(f"/api/v1/companies/{cid}/categories", headers=h(ctx, "admin-b")).status_code == 403
    assert c.post(f"/api/v1/companies/{cid}/categories", json={"name": "x"}, headers=h(ctx, "viewer-a")).status_code == 403
    # cycle: root under its grandchild → 422; unknown parent → 404
    r = c.put(f"/api/v1/categories/{root['id']}", json={"parentId": grandchild["id"]}, headers=h(ctx, "admin-a"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    assert c.put(f"/api/v1/categories/{root['id']}", json={"parentId": root["id"]}, headers=h(ctx, "admin-a")).status_code == 422
    r = c.put(f"/api/v1/categories/{root['id']}", json={"parentId": str(uuid.uuid4())}, headers=h(ctx, "admin-a"))
    assert r.status_code == 404 and r.json()["error"]["message"] == "Kategoriya topilmadi"
    # partial update + tenant isolation on id-addressed routes
    r = c.put(f"/api/v1/categories/{child['id']}", json={"name": "T-catalog Gematologiya 2", "phone": "998712000000", "isActive": False}, headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and r.json()["name"] == "T-catalog Gematologiya 2" and r.json()["phone"] == "998712000000" and r.json()["isActive"] is False
    assert r.json()["parentId"] == root["id"] and r.json()["code"] is None
    assert c.put(f"/api/v1/categories/{child['id']}", json={"name": "hack"}, headers=h(ctx, "admin-b")).status_code == 404
    # delete: has children → 409
    r = c.delete(f"/api/v1/categories/{root['id']}", headers=h(ctx, "admin-a"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "has_children" and r.json()["error"]["message"] == "Avval ichki kategoriyalarni o‘chiring"
    ctx["made"].update({"root": root, "child": child, "grandchild": grandchild})


def test_service_types_crud_filters_stats(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    cid = ctx["ids"]["a"]
    root, child, grandchild = ctx["made"]["root"], ctx["made"]["child"], ctx["made"]["grandchild"]
    r = c.post(f"/api/v1/companies/{cid}/service-types", json={"categoryId": grandchild["id"], "name": "T-catalog Umumiy qon tahlili", "code": "UQT", "price": 50000, "order": 2}, headers=h(ctx, "admin-a"))
    assert r.status_code == 201, r.text
    st1 = r.json()
    assert set(st1) == {"id", "companyId", "categoryId", "name", "code", "description", "price", "branchPrices", "turnaroundDays", "order", "isActive", "schemaId", "documentScope", "defaultTemplateId", "stats", "createdAt", "updatedAt"}
    assert st1["branchPrices"] == {} and st1["turnaroundDays"] == 1 and st1["schemaId"] is None and st1["documentScope"] == "item" and st1["defaultTemplateId"] is None
    assert st1["stats"] == {"ordered30d": 0}
    r = c.post(f"/api/v1/companies/{cid}/service-types", json={"categoryId": root["id"], "name": "T-catalog Siydik tahlili", "code": "uqt"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 409 and r.json()["error"]["message"] == "Bu kod band"
    r = c.post(f"/api/v1/companies/{cid}/service-types", json={"categoryId": root["id"], "name": "T-catalog Siydik tahlili", "code": "ST", "order": 1, "isActive": False}, headers=h(ctx, "admin-a"))
    assert r.status_code == 201, r.text
    st2 = r.json()
    r = c.post(f"/api/v1/companies/{cid}/service-types", json={"categoryId": str(uuid.uuid4()), "name": "x"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 404 and r.json()["error"]["message"] == "Kategoriya topilmadi"
    # order items: 3 recent + 2 old for st1 → ordered30d = 3
    asyncio.run(_add_order_items(cid, st1["id"], recent=3, old=2))
    # list (viewer may read): sorted by order asc; stats present
    lst = c.get(f"/api/v1/companies/{cid}/service-types", headers=h(ctx, "viewer-a"))
    assert lst.status_code == 200, lst.text
    rows = {x["id"]: x for x in lst.json()}
    assert [x["id"] for x in lst.json() if x["id"] in (st1["id"], st2["id"])] == [st2["id"], st1["id"]]
    assert rows[st1["id"]]["stats"] == {"ordered30d": 3} and rows[st2["id"]]["stats"] == {"ordered30d": 0}
    # categoryId expands to descendants: root → both; child → st1 only; activeOnly drops st2; search folds
    ids = lambda r: {x["id"] for x in r.json()}  # noqa: E731
    assert ids(c.get(f"/api/v1/companies/{cid}/service-types", params={"categoryId": root["id"]}, headers=h(ctx, "admin-a"))) == {st1["id"], st2["id"]}
    assert ids(c.get(f"/api/v1/companies/{cid}/service-types", params={"categoryId": child["id"]}, headers=h(ctx, "admin-a"))) == {st1["id"]}
    assert ids(c.get(f"/api/v1/companies/{cid}/service-types", params={"categoryId": root["id"], "activeOnly": "true"}, headers=h(ctx, "admin-a"))) == {st1["id"]}
    assert ids(c.get(f"/api/v1/companies/{cid}/service-types", params={"search": "Умумий қон таҳлили"}, headers=h(ctx, "admin-a"))) == {st1["id"]}
    assert ids(c.get(f"/api/v1/companies/{cid}/service-types", params={"search": "st"}, headers=h(ctx, "admin-a"))) >= {st2["id"]}
    # get + tenant isolation
    r = c.get(f"/api/v1/service-types/{st1['id']}", headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and r.json()["stats"] == {"ordered30d": 3}
    r = c.get(f"/api/v1/service-types/{st1['id']}", headers=h(ctx, "admin-b"))
    assert r.status_code == 404 and r.json()["error"]["message"] == "Xizmat topilmadi"
    # update: branchPrices wholesale, code conflict, category move
    bid = str(uuid.uuid4())
    r = c.put(f"/api/v1/service-types/{st2['id']}", json={"branchPrices": {bid: 70000}, "price": 60000, "isActive": True, "categoryId": child["id"]}, headers=h(ctx, "admin-a"))
    assert r.status_code == 200, r.text
    assert r.json()["branchPrices"] == {bid: 70000} and r.json()["price"] == 60000 and r.json()["isActive"] is True and r.json()["categoryId"] == child["id"]
    r = c.put(f"/api/v1/service-types/{st2['id']}", json={"branchPrices": {}}, headers=h(ctx, "admin-a"))
    assert r.json()["branchPrices"] == {}
    r = c.put(f"/api/v1/service-types/{st2['id']}", json={"code": "UQT"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 409 and r.json()["error"]["message"] == "Bu kod band"
    assert c.put(f"/api/v1/service-types/{st2['id']}", json={"name": "hack"}, headers=h(ctx, "admin-b")).status_code == 404
    # cache invalidated after write: list reflects the new price
    lst = c.get(f"/api/v1/companies/{cid}/service-types", headers=h(ctx, "admin-a")).json()
    assert {x["id"]: x for x in lst}[st2["id"]]["price"] == 60000
    # delete: st1 has order items → 409 in_use; st2 soft-deleted and gone from list; then category with services → 409
    r = c.delete(f"/api/v1/service-types/{st1['id']}", headers=h(ctx, "admin-a"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "in_use"
    r = c.delete(f"/api/v1/categories/{grandchild['id']}", headers=h(ctx, "admin-a"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "in_use" and r.json()["error"]["message"] == "Kategoriyada xizmatlar bor"
    assert c.delete(f"/api/v1/service-types/{st2['id']}", headers=h(ctx, "admin-b")).status_code == 404
    assert c.delete(f"/api/v1/service-types/{st2['id']}", headers=h(ctx, "admin-a")).status_code == 204
    assert c.get(f"/api/v1/service-types/{st2['id']}", headers=h(ctx, "admin-a")).status_code == 404
    assert st2["id"] not in ids(c.get(f"/api/v1/companies/{cid}/service-types", headers=h(ctx, "admin-a")))
    ctx["made"]["st1"] = st1


def test_schemas_lifecycle(ctx: dict) -> None:
    c: TestClient = ctx["client"]
    cid = ctx["ids"]["a"]
    st1 = ctx["made"]["st1"]
    r = c.post(f"/api/v1/companies/{cid}/schemas", json={"name": "T-catalog UQT sxema", "description": "d"}, headers=h(ctx, "admin-a"))
    assert r.status_code == 201, r.text
    sch = r.json()
    assert set(sch) == {"id", "companyId", "name", "description", "version", "status", "fields", "usedBy", "createdAt", "updatedAt"}
    assert sch["version"] == 1 and sch["status"] == "draft" and sch["fields"] == [] and sch["usedBy"] == 0
    # publish empty → 422 empty
    r = c.post(f"/api/v1/schemas/{sch['id']}/publish", headers=h(ctx, "admin-a"))
    assert r.status_code == 422 and r.json()["error"] == {"code": "empty", "message": "Kamida bitta maydon kerak"}
    fields = [
        {"key": "hb", "label": "Gemoglobin", "type": "number", "required": True, "order": 1, "unit": "g/l", "references": [{"gender": "male", "min": 130, "max": 160}]},
        {"key": "note", "label": "Izoh", "type": "longtext", "required": False, "order": 2, "placeholder": "..."},
    ]
    # duplicate keys → 422; bad type → 422 (request validation)
    r = c.put(f"/api/v1/schemas/{sch['id']}", json={"fields": [fields[0], {**fields[1], "key": "hb"}]}, headers=h(ctx, "admin-a"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    r = c.put(f"/api/v1/schemas/{sch['id']}", json={"fields": [{**fields[0], "type": "weird"}]}, headers=h(ctx, "admin-a"))
    assert r.status_code == 422
    # draft: fields saved as-is (extras kept), no version bump
    r = c.put(f"/api/v1/schemas/{sch['id']}", json={"fields": fields}, headers=h(ctx, "admin-a"))
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 1 and r.json()["fields"] == fields and r.json()["status"] == "draft"
    # publish → published, version stays 1; then edit fields → version 2, status unchanged; name-only edit → still 2
    r = c.post(f"/api/v1/schemas/{sch['id']}/publish", headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and r.json()["status"] == "published" and r.json()["version"] == 1
    r = c.put(f"/api/v1/schemas/{sch['id']}", json={"fields": fields[:1]}, headers=h(ctx, "admin-a"))
    assert r.json()["version"] == 2 and r.json()["status"] == "published" and len(r.json()["fields"]) == 1
    r = c.put(f"/api/v1/schemas/{sch['id']}", json={"name": "T-catalog UQT sxema v2"}, headers=h(ctx, "admin-a"))
    assert r.json()["version"] == 2 and r.json()["name"] == "T-catalog UQT sxema v2"
    # bind to service type → usedBy 1 in list and get; unknown schema → 404
    r = c.put(f"/api/v1/service-types/{st1['id']}", json={"schemaId": sch["id"]}, headers=h(ctx, "admin-a"))
    assert r.status_code == 200 and r.json()["schemaId"] == sch["id"]
    r = c.put(f"/api/v1/service-types/{st1['id']}", json={"schemaId": str(uuid.uuid4())}, headers=h(ctx, "admin-a"))
    assert r.status_code == 404 and r.json()["error"]["message"] == "Sxema topilmadi"
    lst = c.get(f"/api/v1/companies/{cid}/schemas", headers=h(ctx, "viewer-a"))
    assert lst.status_code == 200 and {x["id"]: x["usedBy"] for x in lst.json()}[sch["id"]] == 1
    assert c.get(f"/api/v1/schemas/{sch['id']}", headers=h(ctx, "admin-a")).json()["usedBy"] == 1
    # tenant isolation + permissions
    assert c.get(f"/api/v1/schemas/{sch['id']}", headers=h(ctx, "admin-b")).status_code == 404
    assert c.get(f"/api/v1/companies/{cid}/schemas", headers=h(ctx, "admin-b")).status_code == 403
    assert c.post(f"/api/v1/schemas/{sch['id']}/publish", headers=h(ctx, "viewer-a")).status_code == 403
    assert c.post(f"/api/v1/companies/{cid}/schemas", json={"name": "x"}, headers=h(ctx, "viewer-a")).status_code == 403
