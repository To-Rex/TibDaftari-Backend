"""End-to-end smoke test over the seeded demo tenant (shifomed, admin/123456) against the real dev DB.

Exercises the whole staff -> lab -> doctor -> patient portal chain with the REAL PDF renderer.
Requires `python -m app.cli seed-demo` to have run (company `shifomed`, employee `admin`).
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from app.core.config import settings
from fastapi.testclient import TestClient

API = "/api/v1"
OUT_DIR = Path(__file__).parent / "out"


def _phone() -> str:
    return "99893" + "".join(random.choice("0123456789") for _ in range(7))


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


@pytest.fixture(scope="module")
def env() -> Iterator[dict[str, Any]]:
    settings.workers_enabled = False
    settings.telegram_enabled = False
    settings.otp_dev_mode = True
    from app.main import app

    with TestClient(app) as client:
        r = client.post(f"{API}/auth/staff/login", json={"login": "admin", "password": "123456"})
        assert r.status_code == 200, r.text
        sess = r.json()
        yield {"client": client, "h": {"Authorization": f"Bearer {sess['accessToken']}"}, "sess": sess}


def test_staff_to_portal_end_to_end(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    h = env["h"]
    cid = env["sess"]["companyId"]

    me = c.get(f"{API}/auth/staff/me", headers=h)
    assert me.status_code == 200 and me.json()["roleKey"] == "admin"
    assert "reception.patient.write" in me.json()["permissions"]

    branches = c.get(f"{API}/companies/{cid}/branches", headers=h).json()
    categories = c.get(f"{API}/companies/{cid}/categories", headers=h).json()
    service_types = c.get(f"{API}/companies/{cid}/service-types", headers=h).json()
    templates = c.get(f"{API}/companies/{cid}/templates", headers=h).json()
    assert branches and categories and service_types and len(templates) > 0
    branch = branches[0]

    # patient
    phone = _phone()
    r = c.post(
        f"{API}/companies/{cid}/patients",
        json={
            "fullName": f"T-e2e Bemor {uuid.uuid4().hex[:6]}",
            "phone": phone,
            "gender": "female",
            "birthDate": "1990-05-20",
            "address": {"street": "T-e2e kocha 1"},
            "discountPercent": 0,
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    patient = r.json()
    assert patient["phone"] == phone and patient["address"]["street"] and patient["stats"]["orders"] == 0

    # order with two schema-bound services from the seed
    with_schema = [s for s in service_types if s.get("schemaId") and s.get("isActive", True)]
    assert len(with_schema) >= 2, "seed must contain schema-bound service types"
    chosen = with_schema[:2]
    r = c.post(
        f"{API}/companies/{cid}/orders",
        json={"patientId": patient["id"], "branchId": branch["id"], "serviceTypeIds": [s["id"] for s in chosen]},
        headers=h,
    )
    assert r.status_code == 201, r.text
    order, items = r.json()["order"], r.json()["items"]
    assert order["number"].startswith(branch["code"]) and len(items) == 2 and order["total"] > 0

    # pay in full with SMS receipt
    r = c.post(
        f"{API}/orders/{order['id']}/pay",
        json={"amount": order["total"], "method": "cash", "sendSms": True},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["order"]["payment"] == "paid" and r.json()["order"]["status"] == "in_progress"

    # worklist
    cat_ids = sorted({i["categoryId"] for i in items})
    wl = c.get(f"{API}/companies/{cid}/worklist", params={"categoryIds": cat_ids, "pageSize": 200}, headers=h)
    assert wl.status_code == 200, wl.text
    wl_ids = {x["id"] for x in wl.json()["items"]}
    assert {i["id"] for i in items} <= wl_ids
    row = next(x for x in wl.json()["items"] if x["id"] == items[0]["id"])
    assert row["orderNumber"] == order["number"] and row["patientName"] == patient["fullName"]

    # values + submit + approve per item
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
    assert doc["status"] == "final" and doc["pdfUrl"] == f"/api/v1/documents/{doc['id']}/pdf"

    r = c.get(f"{API}/documents/{doc['id']}", headers=h)
    assert r.status_code == 200 and r.json()["id"] == doc["id"]
    pdf = c.get(f"{API}/documents/{doc['id']}/pdf", headers=h)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF"), pdf.text[:200]
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "e2e-document.pdf").write_bytes(pdf.content)

    order_docs = c.get(f"{API}/companies/{cid}/documents", params={"orderId": order["id"]}, headers=h).json()
    assert {d["id"] for d in documents} <= {d["id"] for d in order_docs}
    o = c.get(f"{API}/orders/{order['id']}", headers=h).json()["order"]
    assert o["status"] == "completed" and o["progress"]["approved"] == 2

    # outbox has the result_ready sms for this order
    ob = c.get(f"{API}/companies/{cid}/outbox", params={"kind": "result_ready", "search": phone[-7:]}, headers=h)
    assert ob.status_code == 200, ob.text
    mine = [m for m in ob.json()["items"] if m["orderId"] == order["id"] and m["channel"] == "sms"]
    assert mine, ob.text[:500]
    assert mine[0]["status"] in ("queued", "failed", "sent")
    if mine[0]["status"] == "failed":
        assert mine[0]["error"] == "sms_not_configured"

    # dashboard shape
    d = c.get(
        f"{API}/companies/{cid}/reports/dashboard", params={"dateFrom": "2026-08-01", "dateTo": "2026-08-31"}, headers=h
    )
    assert d.status_code == 200, d.text
    body = d.json()
    for k in ("todayOrders", "todayRevenue", "pendingLab", "pendingApproval", "patients", "smsQueued", "trend", "byCategory"):
        assert k in body, k
    assert body["patients"] >= 1 and isinstance(body["trend"], list)

    # patient portal
    req = c.post(f"{API}/auth/patient/otp/request", json={"phone": phone})
    assert req.status_code == 200 and req.json()["devCode"], req.text
    ver = c.post(
        f"{API}/auth/patient/otp/verify",
        json={"phone": phone, "code": req.json()["devCode"], "challengeId": req.json()["challengeId"]},
    )
    assert ver.status_code == 200, ver.text
    ph = {"Authorization": f"Bearer {ver.json()['accessToken']}"}
    ov = c.get(f"{API}/portal/overview", headers=ph)
    assert ov.status_code == 200, ov.text
    assert order["id"] in {x["id"] for x in ov.json()["orders"]}
    pdoc = next(x for x in ov.json()["documents"] if x["id"] == doc["id"])
    assert pdoc["pdfUrl"] == f"/api/v1/portal/documents/{doc['id']}/pdf"
    r = c.get(f"{API}/portal/documents/{doc['id']}", headers=ph)
    assert r.status_code == 200 and r.json()["template"]["id"] == doc["templateId"]
    r = c.get(f"{API}/portal/documents/{doc['id']}/pdf", headers=ph)
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    assert c.get(f"{API}/auth/patient/me", headers=ph).status_code == 200
    assert c.post(f"{API}/auth/logout", headers=ph).status_code == 200
    assert c.get(f"{API}/auth/patient/me", headers=ph).status_code == 401
    assert c.get(f"{API}/portal/overview", headers=ph).status_code == 401


def test_order_scope_approval_when_seed_has_panel_template(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    h = env["h"]
    cid = env["sess"]["companyId"]
    templates = c.get(f"{API}/companies/{cid}/templates", params={"status": "active"}, headers=h).json()
    panels = [t for t in templates if t["scope"] == "order" and len(t["serviceTypeIds"]) >= 2]
    if not panels:
        pytest.skip("no active order-scope template in the seed")
    tpl = panels[0]
    branch = c.get(f"{API}/companies/{cid}/branches", headers=h).json()[0]
    r = c.post(
        f"{API}/companies/{cid}/patients",
        json={"fullName": f"T-e2e Panel {uuid.uuid4().hex[:6]}", "phone": _phone(), "gender": "male"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    patient = r.json()
    r = c.post(
        f"{API}/companies/{cid}/orders",
        json={"patientId": patient["id"], "branchId": branch["id"], "serviceTypeIds": tpl["serviceTypeIds"][:2]},
        headers=h,
    )
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
