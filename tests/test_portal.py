"""API tests for the patient portal against the test database (fixture rows prefixed `T-portal-`).

Fixture graph: one phone registered at two facilities (trees A, B); orders in both, a cancelled order,
an approved item with a document, a pending item with unpublished values, and a foreign patient's order.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from app.core.timeutil import utcnow
from app.infrastructure.db.models import (
    AttributeSchema,
    Category,
    Order,
    OrderItem,
    OrgUnit,
    Patient,
    ResultDocument,
    ResultTemplate,
    ServiceType,
)
from app.modules.templates import service as templates_svc
from fastapi.testclient import TestClient

from tests.helpers import API, COUNTRY_ID, app_instance, bearer, make_employee, make_tree, phone, seed

RUN = uuid.uuid4().hex[:8]
FAKE_PDF = b"%PDF-1.4 portal test"


def _order(facility: OrgUnit, patient: Patient, number: str, employee_id: uuid.UUID, **kw: Any) -> Order:
    return Order(
        unit_id=facility.id,
        number=number,
        patient_id=patient.id,
        patient_name=patient.full_name,
        patient_phone=patient.phone,
        created_by_employee_id=employee_id,
        **kw,
    )


def _item(order: Order, st: ServiceType, cat: Category, **kw: Any) -> OrderItem:
    return OrderItem(
        order_id=order.id,
        unit_id=order.unit_id,
        service_type_id=st.id,
        service_name=st.name,
        category_id=cat.id,
        category_name=cat.name,
        price=st.price,
        final_price=st.price,
        **kw,
    )


async def _fixture_rows(s) -> dict[str, Any]:
    shared_phone = phone("99893")
    a = await make_tree(s)
    b = await make_tree(s)
    emp_a = await make_employee(s, a.facility, "registrator", login=f"t-portal-a-{RUN}")
    emp_b = await make_employee(s, b.facility, "registrator", login=f"t-portal-b-{RUN}")
    p_a = Patient(
        country_id=COUNTRY_ID,
        registered_unit_id=a.facility.id,
        full_name=f"T-portal Bemor {RUN}",
        phone=shared_phone,
        gender="female",
        pinfl="1234567890" + RUN[:4].translate(str.maketrans("abcdef", "012345")),
        passport_number=f"AA{RUN[:7]}",
        discount_percent=15,
        note="maxfiy",
        tags=["vip"],
        stats_orders=2,
        stats_total_spent=90000,
    )
    p_b = Patient(country_id=COUNTRY_ID, registered_unit_id=b.facility.id, full_name=f"T-portal Bemor B {RUN}", phone=shared_phone, tags=[])
    foreign = Patient(country_id=COUNTRY_ID, registered_unit_id=a.facility.id, full_name=f"T-portal Boshqa {RUN}", phone=phone("99893"), tags=[])
    cat = Category(owner_unit_id=a.region.id, name=f"T-portal Kat {RUN}", sort_order=1)
    schema = AttributeSchema(
        owner_unit_id=a.region.id,
        name=f"T-portal sxema {RUN}",
        status="published",
        fields=[{"key": "hb", "label": "Gemoglobin", "type": "number", "required": True, "order": 1}],
    )
    s.add_all([p_a, p_b, foreign, cat, schema])
    await s.flush()
    st = ServiceType(owner_unit_id=a.region.id, category_id=cat.id, name=f"T-portal Qon {RUN}", code=f"TP{RUN[:5]}", price=50000, schema_id=schema.id)
    st2 = ServiceType(owner_unit_id=a.region.id, category_id=cat.id, name=f"T-portal Konsult {RUN}", price=40000)
    tpl = ResultTemplate(owner_unit_id=a.region.id, name=f"T-portal tpl {RUN}", status="active", service_type_ids=[], category_ids=[], doc={"elements": []})
    s.add_all([st, st2, tpl])
    await s.flush()
    o1 = _order(a.facility, p_a, f"{a.facility.code}-000001", emp_a.id, status="in_progress", payment="paid", subtotal=90000, total=90000, paid_amount=90000, item_count=2)
    o_cancel = _order(a.facility, p_a, f"{a.facility.code}-000002", emp_a.id, status="cancelled")
    o_b = _order(b.facility, p_b, f"{b.facility.code}-000001", emp_b.id, status="open")
    o_foreign = _order(a.facility, foreign, f"{a.facility.code}-000003", emp_a.id, status="open")
    s.add_all([o1, o_cancel, o_b, o_foreign])
    await s.flush()
    now = utcnow()
    it_appr = _item(o1, st, cat, status="approved", schema_id=schema.id, schema_version=1, values={"hb": 130}, lab_note="normada", approved_at=now)
    it_pend = _item(o1, st2, cat, status="entered", values={"secret": 1}, lab_note="hali tayyor emas")
    s.add_all([it_appr, it_pend])
    await s.flush()
    doc = ResultDocument(
        unit_id=a.facility.id,
        order_id=o1.id,
        patient_id=p_a.id,
        order_item_id=it_appr.id,
        order_item_ids=[it_appr.id],
        template_id=tpl.id,
        template_version=1,
        title=f"{st.name} — natija",
        deliveries=[{"channel": "portal", "status": "delivered", "at": now.isoformat()}],
        snapshot={"version": 1, "doc": {}, "context": {}, "assets": {}, "language": "uz"},
    )
    s.add(doc)
    await s.flush()
    it_appr.document_id = doc.id
    await s.flush()
    return {
        "phone": shared_phone,
        "a": {k: str(getattr(a, k).id) for k in ("country", "region", "district", "facility")},
        "b": {k: str(getattr(b, k).id) for k in ("country", "region", "district", "facility")},
        "tag_a": a.tag,
        "tag_b": b.tag,
        "p_a": str(p_a.id),
        "p_b": str(p_b.id),
        "o1": str(o1.id),
        "o_cancel": str(o_cancel.id),
        "o_b": str(o_b.id),
        "o_foreign": str(o_foreign.id),
        "it_appr": str(it_appr.id),
        "it_pend": str(it_pend.id),
        "doc": str(doc.id),
        "tpl": str(tpl.id),
        "schema": str(schema.id),
        "st": str(st.id),
        "st_code": st.code,
        "st2": str(st2.id),
        "cat": str(cat.id),
    }


@pytest.fixture(scope="module")
def env() -> Iterator[dict[str, Any]]:
    data = seed(_fixture_rows)
    mp = pytest.MonkeyPatch()

    async def _pdf(*_: Any, **__: Any) -> bytes:
        return FAKE_PDF

    mp.setattr(templates_svc, "render_snapshot_pdf", _pdf)
    with TestClient(app_instance()) as client:
        req = client.post(f"{API}/auth/patient/otp/request", json={"phone": data["phone"]})
        assert req.status_code == 200, req.text
        ver = client.post(
            f"{API}/auth/patient/otp/verify",
            json={"phone": data["phone"], "code": req.json()["devCode"], "challengeId": req.json()["challengeId"]},
        )
        assert ver.status_code == 200, ver.text
        data["client"] = client
        data["h"] = bearer(ver.json()["accessToken"])
        yield data
    mp.undo()


def test_overview_merges_facilities_and_redacts_patient(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    r = c.get(f"{API}/portal/overview", headers=env["h"])
    assert r.status_code == 200, r.text
    body = r.json()
    p = body["patient"]
    assert p["id"] == env["p_a"] and p["phone"] == env["phone"] and p["gender"] == "female" and p["registeredUnitId"] == env["a"]["facility"]
    assert p["discountPercent"] == 0 and p["tags"] == []
    for hidden in ("note", "pinfl", "passportNumber", "contractNumber", "workplace"):
        assert hidden not in p
    assert p["stats"] == {"orders": 2, "lastVisitAt": None, "totalSpent": 90000}
    assert p["portal"]["linked"] is True and "address" in p and p["createdAt"].endswith("Z")

    order_ids = [o["id"] for o in body["orders"]]
    assert env["o1"] in order_ids and env["o_b"] in order_ids
    assert env["o_cancel"] not in order_ids and env["o_foreign"] not in order_ids
    assert body["orders"][0]["createdAt"] >= body["orders"][-1]["createdAt"]

    docs = body["documents"]
    assert [d["id"] for d in docs] == [env["doc"]]
    assert docs[0]["pdfUrl"] == f"/api/v1/portal/documents/{env['doc']}/pdf"
    # the organisations behind the cheques (the district nodes above the facilities) and the facilities themselves
    orgs = {x["id"]: x["name"] for x in body["orgs"]}
    assert orgs == {env["a"]["district"]: f"T-{env['tag_a']} tumani", env["b"]["district"]: f"T-{env['tag_b']} tumani"}
    units = {x["id"]: x for x in body["units"]}
    assert set(units) == {env["a"]["facility"], env["b"]["facility"]}
    assert units[env["a"]["facility"]] == {"id": env["a"]["facility"], "orgId": env["a"]["district"], "name": f"T-{env['tag_a']} SES", "address": None, "phone": None}


def test_order_redacts_unapproved_results_and_hides_foreign(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    r = c.get(f"{API}/portal/orders/{env['o1']}", headers=env["h"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["order"]["id"] == env["o1"] and body["order"]["payment"] == "paid" and body["order"]["unitId"] == env["a"]["facility"]
    items = {i["id"]: i for i in body["items"]}
    assert set(items) == {env["it_appr"], env["it_pend"]}
    assert items[env["it_appr"]]["values"] == {"hb": 130} and items[env["it_appr"]]["labNote"] == "normada"
    assert items[env["it_pend"]]["values"] == {} and items[env["it_pend"]]["labNote"] is None
    assert items[env["it_pend"]]["status"] == "entered" and items[env["it_appr"]]["history"] == []
    assert [d["id"] for d in body["documents"]] == [env["doc"]]
    assert body["org"]["id"] == env["a"]["district"] and body["unit"]["id"] == env["a"]["facility"]

    # other facility, same phone → owned
    r = c.get(f"{API}/portal/orders/{env['o_b']}", headers=env["h"])
    assert r.status_code == 200 and r.json()["unit"]["id"] == env["b"]["facility"]
    # cancelled order is still owned (detail allowed), foreign patient's order → 404 with the exact message
    assert c.get(f"{API}/portal/orders/{env['o_cancel']}", headers=env["h"]).status_code == 200
    r = c.get(f"{API}/portal/orders/{env['o_foreign']}", headers=env["h"])
    assert r.status_code == 404 and r.json()["error"] == {"code": "not_found", "message": "Chek topilmadi"}
    assert c.get(f"{API}/portal/orders/{uuid.uuid4()}", headers=env["h"]).status_code == 404
    # no token → 401
    assert c.get(f"{API}/portal/orders/{env['o1']}").status_code == 401


def test_document_bundle_and_pdf(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    r = c.get(f"{API}/portal/documents/{env['doc']}", headers=env["h"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["document"]["id"] == env["doc"] and body["document"]["orderItemIds"] == [env["it_appr"]] and body["document"]["unitId"] == env["a"]["facility"]
    assert body["template"]["id"] == env["tpl"] and "doc" in body["template"] and body["template"]["ownerUnitId"] == env["a"]["region"]
    assert body["item"]["id"] == env["it_appr"] and body["item"]["values"] == {"hb": 130}
    assert body["order"]["id"] == env["o1"]
    assert [i["id"] for i in body["items"]] == [env["it_appr"]]
    assert [s["id"] for s in body["schemas"]] == [env["schema"]] and body["schemas"][0]["usedBy"] == 1
    assert body["serviceCodes"] == {env["st"]: env["st_code"]}
    assert body["category"]["id"] == env["cat"]
    assert body["org"] == {"id": env["a"]["district"], "name": f"T-{env['tag_a']} tumani", "logoUrl": None, "phone": None, "address": None}
    assert body["unit"] == {"id": env["a"]["facility"], "orgId": env["a"]["district"], "name": f"T-{env['tag_a']} SES", "address": None, "phone": None}
    assert body["assets"] == []

    r = c.get(f"{API}/portal/documents/{env['doc']}/pdf", headers=env["h"])
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/pdf")
    assert r.content == FAKE_PDF
    # second call served from the stored file
    assert c.get(f"{API}/portal/documents/{env['doc']}/pdf", headers=env["h"]).content == FAKE_PDF

    r = c.get(f"{API}/portal/documents/{uuid.uuid4()}", headers=env["h"])
    assert r.status_code == 404 and r.json()["error"]["message"] == "Hujjat topilmadi"
    assert c.get(f"{API}/portal/documents/{uuid.uuid4()}/pdf", headers=env["h"]).status_code == 404
