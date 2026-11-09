"""End-to-end smoke test over a fixture tree against the test database, with the REAL PDF renderer.

Builds the whole catalogue through the API (categories, schemas, services, templates) as a region administrator
and then exercises the staff -> lab -> doctor -> patient portal chain at the facility.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.helpers import API, app_instance, bearer, make_employee, make_tree, phone, seed, staff_session

OUT_DIR = Path(__file__).parent / "out"
RUN = uuid.uuid4().hex[:6]


def _sample_value(field: dict[str, Any]) -> Any:
    """Produce a valid, non-blank value for a schema field."""
    t = field.get("type")
    if t == "number":
        return 12.5
    if t == "select":
        opts = field.get("options") or []
        return opts[0]["value"] if opts else "norma"
    if t == "multiselect":
        opts = field.get("options") or []
        return [opts[0]["value"]] if opts else ["norma"]
    if t == "boolean":
        return True
    if t == "date":
        return "2026-01-15"
    if t == "table":
        cols = field.get("columns") or []
        rows = [dict(r) for r in (field.get("presetRows") or [])] or [{}]
        for row in rows:
            for col in cols:
                if row.get(col["key"]) in (None, ""):
                    row[col["key"]] = _sample_value(col)
        return rows
    return "Aniqlanmadi"


def _fill_values(schema: dict[str, Any]) -> dict[str, Any]:
    return {f["key"]: _sample_value(f) for f in schema["fields"]}


def _text(el_id: str, y: int, text: str) -> dict[str, Any]:
    return {"id": el_id, "type": "text", "x": 40, "y": y, "w": 500, "h": 24, "text": text, "style": {"fontFamily": "sans", "fontSize": 12, "fontWeight": 400, "color": "#14201d", "align": "left"}}


@pytest.fixture(scope="module")
def env() -> Iterator[dict[str, Any]]:
    async def rows(s):
        tree = await make_tree(s)
        admin = await make_employee(s, tree.region, "admin", login=f"t-e2e-admin-{RUN}", full_name="T-e2e Admin")
        return {"region": str(tree.region.id), "district": str(tree.district.id), "facility": str(tree.facility.id), "code": tree.facility.code, "tag": tree.tag, "login": admin.login}

    data = seed(rows)
    with TestClient(app_instance()) as client:
        sess = staff_session(client, data["login"])
        data.update({"client": client, "h": bearer(sess["accessToken"]), "sess": sess})
        yield data


def _build_catalogue(c: TestClient, h: dict[str, str], region: str) -> dict[str, Any]:
    """Two categories, two published schemas, two schema-bound services, a generic result template and a panel."""
    cat1 = c.post(f"{API}/units/{region}/categories", json={"name": f"T-e2e Gematologiya {RUN}", "color": "#0ea5e9"}, headers=h).json()
    cat2 = c.post(f"{API}/units/{region}/categories", json={"name": f"T-e2e Virusologiya {RUN}", "color": "#f59e0b"}, headers=h).json()
    fields1 = [
        {"key": "hb", "label": "Gemoglobin", "type": "number", "required": True, "order": 1, "unit": "g/l", "references": [{"min": 120, "max": 160}]},
        {"key": "res", "label": "Natija", "type": "select", "required": True, "order": 2, "options": [{"value": "neg", "label": "Manfiy"}, {"value": "pos", "label": "Musbat", "flag": "abnormal"}]},
        {"key": "rows", "label": "Jadval", "type": "table", "required": False, "order": 3, "columns": [{"key": "name", "type": "text"}, {"key": "v", "type": "number"}], "presetRows": [{"name": "WBC", "v": ""}, {"name": "RBC", "v": ""}]},
    ]
    fields2 = [{"key": "igm", "label": "IgM", "type": "boolean", "required": True, "order": 1}, {"key": "when", "label": "Sana", "type": "date", "required": False, "order": 2}]
    schemas = []
    for name, fields in ((f"T-e2e UQT {RUN}", fields1), (f"T-e2e IFA {RUN}", fields2)):
        r = c.post(f"{API}/units/{region}/schemas", json={"name": name, "fields": fields}, headers=h)
        assert r.status_code == 201, r.text
        assert c.post(f"{API}/schemas/{r.json()['id']}/publish", headers=h).status_code == 200
        schemas.append(r.json()["id"])
    st1 = c.post(f"{API}/units/{region}/service-types", json={"categoryId": cat1["id"], "name": f"T-e2e Umumiy qon {RUN}", "code": f"UQ{RUN}", "price": 50000, "schemaId": schemas[0]}, headers=h)
    st2 = c.post(f"{API}/units/{region}/service-types", json={"categoryId": cat2["id"], "name": f"T-e2e IFA HBsAg {RUN}", "code": f"HB{RUN}", "price": 70000, "schemaId": schemas[1]}, headers=h)
    assert st1.status_code == 201 and st2.status_code == 201, st1.text + st2.text
    generic_doc = {"paper": "A4", "orientation": "portrait", "background": "#ffffff", "margin": 40, "elements": [
        _text("t1", 40, "{org.name} · {unit.name} · {category.name}"),
        _text("t2", 70, "Bemor: {patient.fullName} ({patient.gender}, {patient.age}) · {order.number} · {order.date}"),
        {"id": "f1", "type": "field", "x": 40, "y": 110, "w": 500, "h": 24, "fieldKey": "hb", "showLabel": True, "showUnit": True, "showReference": True, "highlightAbnormal": True, "style": {"fontFamily": "sans", "fontSize": 12, "fontWeight": 400, "color": "#000", "align": "left"}},
        {"id": "qr", "type": "qr", "x": 400, "y": 150, "w": 120, "h": 120, "value": "{doc.link}"},
    ]}
    generic = c.post(f"{API}/units/{region}/templates", json={"name": f"T-e2e Natija {RUN}", "doc": generic_doc}, headers=h)
    assert generic.status_code == 201, generic.text
    assert c.post(f"{API}/templates/{generic.json()['id']}/status", json={"status": "active"}, headers=h).status_code == 200
    return {"cats": [cat1, cat2], "services": [st1.json(), st2.json()], "generic": generic.json()["id"]}


def _build_panel(c: TestClient, h: dict[str, str], region: str, service_ids: list[str]) -> str:
    """An order-scope template bound to both services (it also serves item approvals of those services once active,
    so it is created only after the item-scope run)."""
    panel_doc = {"paper": "A4", "orientation": "portrait", "background": "#ffffff", "margin": 40, "elements": [
        _text("t1", 40, "{org.name} — {patient.fullName} — {order.number}"),
        _text("t2", 70, f"Hb: {{svc.UQ{RUN}.hb}} · HBsAg IgM: {{svc.HB{RUN}.igm}}"),
        {"id": "tb", "type": "table", "x": 40, "y": 110, "w": 500, "h": 200, "fieldKey": "items", "showHeader": True, "showRowNumber": True, "highlightAbnormal": False, "rowHeight": 22, "borderColor": "#c3cec9", "borderWidth": 1, "columns": [{"id": "c1", "header": "Xizmat", "bind": "name", "width": 300, "align": "left"}, {"id": "c2", "header": "Holat", "bind": "statusLabel", "width": 100, "align": "left"}], "headerStyle": {"fontFamily": "sans", "fontSize": 10, "fontWeight": 600, "color": "#5c6b66", "align": "left"}, "cellStyle": {"fontFamily": "sans", "fontSize": 10.5, "fontWeight": 400, "color": "#14201d", "align": "left"}},
    ]}
    panel = c.post(f"{API}/units/{region}/templates", json={"name": f"T-e2e Panel {RUN}", "scope": "order", "serviceTypeIds": service_ids, "doc": panel_doc}, headers=h)
    assert panel.status_code == 201, panel.text
    assert c.post(f"{API}/templates/{panel.json()['id']}/status", json={"status": "active"}, headers=h).status_code == 200
    return panel.json()["id"]


def test_staff_to_portal_end_to_end(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    h = env["h"]
    fac = env["facility"]
    sess = env["sess"]
    assert sess["unitId"] == env["region"] and sess["unit"]["level"] == "region" and sess["facilityId"] is None

    me = c.get(f"{API}/auth/staff/me", headers=h)
    assert me.status_code == 200 and me.json()["roleKey"] == "admin"
    assert "reception.patient.write" in me.json()["permissions"]

    env["catalogue"] = _build_catalogue(c, h, env["region"])
    facility = c.get(f"{API}/units/{fac}", headers=h).json()
    categories = c.get(f"{API}/units/{fac}/categories", headers=h).json()
    service_types = c.get(f"{API}/units/{fac}/service-types", params={"unitId": fac}, headers=h).json()
    templates = c.get(f"{API}/units/{fac}/templates", params={"unitId": fac, "status": "active"}, headers=h).json()
    assert facility["code"] == env["code"] and categories and len(service_types) >= 2 and len(templates) >= 1
    assert all(s["available"] is True for s in service_types)

    # patient, registered at the facility
    phone_ = phone("99893")
    r = c.post(
        f"{API}/units/{fac}/patients",
        json={
            "fullName": f"T-e2e Bemor {RUN}",
            "phone": phone_,
            "gender": "female",
            "birthDate": "1990-05-20",
            "address": {"street": "T-e2e kocha 1"},
            "discountPercent": 0,
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    patient = r.json()
    assert patient["phone"] == phone_ and patient["address"]["street"] and patient["stats"]["orders"] == 0 and patient["registeredUnitId"] == fac

    # order with two schema-bound services
    chosen = [s for s in service_types if s.get("schemaId")][:2]
    assert len(chosen) == 2
    r = c.post(f"{API}/units/{fac}/orders", json={"patientId": patient["id"], "serviceTypeIds": [s["id"] for s in chosen]}, headers=h)
    assert r.status_code == 201, r.text
    order, items = r.json()["order"], r.json()["items"]
    assert order["number"].startswith(facility["code"]) and len(items) == 2 and order["total"] == 120000 and order["unitId"] == fac

    # pay in full with SMS receipt
    r = c.post(f"{API}/orders/{order['id']}/pay", json={"amount": order["total"], "method": "cash", "sendSms": True}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["order"]["payment"] == "paid" and r.json()["order"]["status"] == "in_progress"

    # worklist
    cat_ids = sorted({i["categoryId"] for i in items})
    wl = c.get(f"{API}/units/{fac}/worklist", params={"categoryIds": cat_ids, "pageSize": 200}, headers=h)
    assert wl.status_code == 200, wl.text
    wl_ids = {x["id"] for x in wl.json()["items"]}
    assert {i["id"] for i in items} <= wl_ids
    row = next(x for x in wl.json()["items"] if x["id"] == items[0]["id"])
    assert row["orderNumber"] == order["number"] and row["patientName"] == patient["fullName"] and row["unitName"] == facility["name"]

    # values + submit + approve per item (the generic template, real PDF)
    documents: list[dict[str, Any]] = []
    for it in items:
        schema = c.get(f"{API}/schemas/{it['schemaId']}", headers=h)
        assert schema.status_code == 200, schema.text
        values = _fill_values(schema.json())
        r = c.put(f"{API}/items/{it['id']}/values", json={"values": values, "labNote": "T-e2e"}, headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "entered"
        r = c.post(f"{API}/items/{it['id']}/submit", headers=h)
        assert r.status_code == 200 and r.json()["status"] == "submitted", r.text
        r = c.post(f"{API}/items/{it['id']}/approve", json={}, headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["item"]["status"] == "approved"
        documents.append(r.json()["document"])
    doc = documents[0]
    assert doc["status"] == "final" and doc["pdfUrl"] == f"/api/v1/documents/{doc['id']}/pdf" and doc["templateId"] == env["catalogue"]["generic"]
    assert doc["verifyLink"] and "/v/" in doc["verifyLink"]

    r = c.get(f"{API}/documents/{doc['id']}", headers=h)
    assert r.status_code == 200 and r.json()["id"] == doc["id"] and r.json()["unitId"] == fac
    pdf = c.get(f"{API}/documents/{doc['id']}/pdf", headers=h)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF"), pdf.text[:200]
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "e2e-document.pdf").write_bytes(pdf.content)

    order_docs = c.get(f"{API}/units/{fac}/documents", params={"orderId": order["id"]}, headers=h).json()
    assert {d["id"] for d in documents} <= {d["id"] for d in order_docs}
    o = c.get(f"{API}/orders/{order['id']}", headers=h).json()["order"]
    assert o["status"] == "completed" and o["progress"]["approved"] == 2
    # the region administrator sees the cheque from the region too
    assert order["id"] in {x["id"] for x in c.get(f"{API}/units/{env['region']}/orders", params={"search": order["number"]}, headers=h).json()["items"]}

    # outbox has the result_ready sms for this order — recorded as failed: no SMS account anywhere above the facility
    ob = c.get(f"{API}/units/{fac}/outbox", params={"kind": "result_ready", "search": phone_[-7:]}, headers=h)
    assert ob.status_code == 200, ob.text
    mine = [m for m in ob.json()["items"] if m["orderId"] == order["id"] and m["channel"] == "sms"]
    assert mine, ob.text[:500]
    assert mine[0]["status"] == "failed" and mine[0]["error"] == "sms_not_configured" and mine[0]["unitId"] == fac

    # the public card and the QR check name the organisation (the district node) and the facility
    info = c.get(f"{API}/d/{doc['publicToken']}").json()
    assert info["id"] == doc["id"] and info["orgName"] == f"T-{env['tag']} tumani" and info["otpRequired"] is True
    check = c.get(f"{API}/v/{doc['verifyLink'].rsplit('/', 1)[-1]}").json()
    assert check["status"] == "valid" and check["unitName"] == facility["name"] and check["orderNumber"] == order["number"] and check["doctorName"] == "T-e2e Admin"

    # dashboard shape
    d = c.get(f"{API}/units/{fac}/reports/dashboard", params={"dateFrom": "2026-08-01", "dateTo": "2026-08-31"}, headers=h)
    assert d.status_code == 200, d.text
    body = d.json()
    for k in ("todayOrders", "todayRevenue", "pendingLab", "pendingApproval", "patients", "smsQueued", "trend", "byCategory"):
        assert k in body, k
    assert body["patients"] >= 1 and isinstance(body["trend"], list)

    # patient portal
    req = c.post(f"{API}/auth/patient/otp/request", json={"phone": phone_})
    assert req.status_code == 200 and req.json()["devCode"], req.text
    ver = c.post(f"{API}/auth/patient/otp/verify", json={"phone": phone_, "code": req.json()["devCode"], "challengeId": req.json()["challengeId"]})
    assert ver.status_code == 200, ver.text
    ph = bearer(ver.json()["accessToken"])
    ov = c.get(f"{API}/portal/overview", headers=ph)
    assert ov.status_code == 200, ov.text
    assert order["id"] in {x["id"] for x in ov.json()["orders"]}
    assert [u["id"] for u in ov.json()["units"]] == [fac] and ov.json()["orgs"][0]["name"] == f"T-{env['tag']} tumani"
    pdoc = next(x for x in ov.json()["documents"] if x["id"] == doc["id"])
    assert pdoc["pdfUrl"] == f"/api/v1/portal/documents/{doc['id']}/pdf"
    r = c.get(f"{API}/portal/documents/{doc['id']}", headers=ph)
    assert r.status_code == 200 and r.json()["template"]["id"] == doc["templateId"] and r.json()["unit"]["id"] == fac
    r = c.get(f"{API}/portal/documents/{doc['id']}/pdf", headers=ph)
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    assert c.get(f"{API}/auth/patient/me", headers=ph).status_code == 200
    assert c.post(f"{API}/auth/logout", headers=ph).status_code == 200
    assert c.get(f"{API}/auth/patient/me", headers=ph).status_code == 401
    assert c.get(f"{API}/portal/overview", headers=ph).status_code == 401


def test_order_scope_approval_with_the_panel_template(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    h = env["h"]
    fac = env["facility"]
    panel_id = _build_panel(c, h, env["region"], [s["id"] for s in env["catalogue"]["services"]])
    tpl = c.get(f"{API}/templates/{panel_id}", headers=h).json()
    assert tpl["scope"] == "order" and len(tpl["serviceTypeIds"]) == 2 and tpl["status"] == "active"
    r = c.post(f"{API}/units/{fac}/patients", json={"fullName": f"T-e2e Panel {RUN}", "phone": phone("99893"), "gender": "male"}, headers=h)
    assert r.status_code == 201, r.text
    patient = r.json()
    r = c.post(f"{API}/units/{fac}/orders", json={"patientId": patient["id"], "serviceTypeIds": tpl["serviceTypeIds"]}, headers=h)
    assert r.status_code == 201, r.text
    order, items = r.json()["order"], r.json()["items"]
    assert len(items) == 2
    r = c.post(f"{API}/orders/{order['id']}/pay", json={"amount": order["total"], "method": "card"}, headers=h)
    assert r.status_code == 200, r.text
    for it in items:
        values = _fill_values(c.get(f"{API}/schemas/{it['schemaId']}", headers=h).json()) if it["schemaId"] else {}
        assert c.put(f"{API}/items/{it['id']}/values", json={"values": values}, headers=h).status_code == 200
        assert c.post(f"{API}/items/{it['id']}/submit", headers=h).json()["status"] == "submitted"
    scope = c.get(f"{API}/orders/{order['id']}/scope-items", params={"templateId": tpl["id"]}, headers=h)
    assert scope.status_code == 200 and {x["id"] for x in scope.json()} == {i["id"] for i in items}
    r = c.post(f"{API}/orders/{order['id']}/approve", json={"templateId": tpl["id"]}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert {i["id"] for i in body["items"]} == {i["id"] for i in items}
    assert all(i["status"] == "approved" and i["documentId"] == body["document"]["id"] for i in body["items"])
    doc = body["document"]
    assert doc["templateId"] == tpl["id"] and sorted(doc["orderItemIds"]) == sorted(i["id"] for i in items)
    pdf = c.get(f"{API}/documents/{doc['id']}/pdf", headers=h)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "e2e-panel.pdf").write_bytes(pdf.content)
    o = c.get(f"{API}/orders/{order['id']}", headers=h).json()["order"]
    assert o["status"] == "completed"
    # the cheque's receipt renders with the standard cheque template? none exists for the region → 404 no_template
    r = c.get(f"{API}/orders/{order['id']}/receipt.pdf", headers=h)
    assert r.status_code == 404 and r.json()["error"]["code"] == "no_template"
    r = c.post(f"{API}/units/{env['region']}/templates/default-receipt", headers=h)
    assert r.status_code == 201 and c.post(f"{API}/templates/{r.json()['id']}/status", json={"status": "active"}, headers=h).status_code == 200
    r = c.get(f"{API}/orders/{order['id']}/receipt.pdf", headers=h)
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
