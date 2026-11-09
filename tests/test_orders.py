"""API tests for the orders module against the test database (fixture rows prefixed `T-orders-`).

Templates rendering is provided by another module; `build_document_snapshot` / `render_snapshot_pdf`
are monkeypatched here (contract stubs) so approval can be exercised end-to-end.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from app.core.config import settings
from app.infrastructure.db.models import (
    AttributeSchema,
    Category,
    OutboxMessage,
    Patient,
    ResultDocument,
    ResultTemplate,
    ServiceType,
    ServiceUnitSetting,
)
from app.modules.templates import service as templates_svc
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.helpers import API, COUNTRY_ID, app_instance, bearer, db, make_employee, make_tree, phone, seed, staff_token

RUN = uuid.uuid4().hex[:8]
FAKE_SNAPSHOT = {"version": 1, "doc": {}, "context": {}, "assets": {}, "language": "uz"}
FAKE_PDF = b"%PDF-1.4 test"


async def _fixture_rows(s) -> dict[str, Any]:
    a = await make_tree(s)
    b = await make_tree(s)
    emp_a = await make_employee(s, a.facility, "admin", login=f"t-orders-a-{RUN}", password="secret123", full_name="T-orders Doctor")
    emp_b = await make_employee(s, b.facility, "admin", login=f"t-orders-b-{RUN}", password="secret123", full_name="T-orders B")
    admin_a = await make_employee(s, a.region, "admin", login=f"t-orders-region-{RUN}", password="secret123", full_name="T-orders Region admin")
    patient = Patient(
        country_id=COUNTRY_ID,
        registered_unit_id=a.facility.id,
        full_name=f"T-orders Bemor {RUN}",
        phone=phone("99897"),
        gender="male",
        discount_percent=10,
        tags=[],
        telegram_chat_id="123456",
    )
    cat = Category(owner_unit_id=a.region.id, name=f"T-orders Gematologiya {RUN}", sort_order=1)
    schema = AttributeSchema(
        owner_unit_id=a.region.id,
        name=f"T-orders sxema {RUN}",
        version=3,
        status="published",
        fields=[
            {"key": "hb", "label": "Gemoglobin", "type": "number", "required": True, "order": 1},
            {"key": "tbl", "label": "Jadval", "type": "table", "required": False, "order": 2, "presetRows": [{"name": "WBC", "value": ""}]},
        ],
    )
    s.add_all([patient, cat, schema])
    await s.flush()
    st1 = ServiceType(owner_unit_id=a.region.id, category_id=cat.id, name=f"T-orders Umumiy qon {RUN}", code=f"TO{RUN[:5]}", price=100000, schema_id=schema.id)
    st2 = ServiceType(owner_unit_id=a.region.id, category_id=cat.id, name=f"T-orders Konsultatsiya {RUN}", price=60000)
    s.add_all([st1, st2])
    await s.flush()
    # the facility's own price for the consultation (the nearest level wins over the base price)
    s.add(ServiceUnitSetting(service_type_id=st2.id, unit_id=a.facility.id, is_enabled=True, price=50000))
    tpl_item = ResultTemplate(owner_unit_id=a.region.id, name=f"T-orders item tpl {RUN}", status="active", version=2, service_type_ids=[st1.id], category_ids=[], scope="item", doc={"elements": []})
    tpl_order = ResultTemplate(owner_unit_id=a.region.id, name=f"T-orders panel {RUN}", status="active", version=1, service_type_ids=[], category_ids=[], scope="order", doc={"elements": []})
    s.add_all([tpl_item, tpl_order])
    await s.flush()
    return {
        "a": {k: str(getattr(a, k).id) for k in ("country", "region", "district", "facility")},
        "b": {k: str(getattr(b, k).id) for k in ("country", "region", "district", "facility")},
        "tag": a.tag,
        "login_a": emp_a.login,
        "login_b": emp_b.login,
        "login_region": admin_a.login,
        "emp_a": str(emp_a.id),
        "facility": str(a.facility.id),
        "code": a.facility.code,
        "patient": str(patient.id),
        "cat": str(cat.id),
        "st1": str(st1.id),
        "st2": str(st2.id),
        "tpl_item": str(tpl_item.id),
        "tpl_order": str(tpl_order.id),
    }


@pytest.fixture(scope="module")
def env() -> Iterator[dict[str, Any]]:
    data = seed(_fixture_rows)
    mp = pytest.MonkeyPatch()

    async def _snapshot(*_: Any, doc_link: str | None = None, **__: Any) -> dict[str, Any]:
        # like the real builder, the stub freezes the QR link (`{doc.link}`) into the render context
        return {**FAKE_SNAPSHOT, "context": {"doc": {"link": doc_link}} if doc_link else {}}

    async def _pdf(*_: Any, **__: Any) -> bytes:
        return FAKE_PDF

    mp.setattr(templates_svc, "build_document_snapshot", _snapshot)
    mp.setattr(templates_svc, "render_snapshot_pdf", _pdf)
    with TestClient(app_instance()) as client:
        data["client"] = client
        data["h_a"] = bearer(staff_token(client, data["login_a"], "secret123"))
        data["h_b"] = bearer(staff_token(client, data["login_b"], "secret123"))
        data["h_region"] = bearer(staff_token(client, data["login_region"], "secret123"))
        yield data
    mp.undo()


def _create(env: dict[str, Any], service_type_ids: list[str], note: str | None = None) -> dict[str, Any]:
    c: TestClient = env["client"]
    r = c.post(
        f"{API}/units/{env['facility']}/orders",
        json={"patientId": env["patient"], "serviceTypeIds": service_type_ids, "note": note},
        headers=env["h_a"],
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_full_workflow_create_pay_worklist_values_submit_approve(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    h = env["h_a"]
    created = _create(env, [env["st1"], env["st2"], str(uuid.uuid4()), "not-a-uuid"], note="tez")
    order, items = created["order"], created["items"]
    assert re.fullmatch(rf"{env['code']}-\d{{6}}", order["number"])
    assert order["unitId"] == env["facility"] and order["unitName"] == f"T-{env['tag']} SES"
    assert order["status"] == "open" and order["payment"] == "unpaid"
    assert (
        order["discountPercent"] == 10
        and order["subtotal"] == 150000
        and order["discountAmount"] == 15000
        and order["total"] == 135000
    )
    assert order["itemCount"] == 2 and order["progress"] == {
        "pending": 2,
        "entered": 0,
        "submitted": 0,
        "approved": 0,
        "rejected": 0,
        "cancelled": 0,
    }
    assert order["note"] == "tez" and order["patientPhone"] and order["createdByEmployeeId"] == env["emp_a"]
    assert order["createdAt"].endswith("Z")
    it1, it2 = items
    assert (
        it1["serviceTypeId"] == env["st1"]
        and it1["price"] == 100000
        and it1["finalPrice"] == 90000
        and it1["schemaVersion"] == 3
    )
    assert it1["values"] == {"hb": None, "tbl": [{"name": "WBC", "value": ""}]}
    assert it2["price"] == 50000 and it2["finalPrice"] == 45000 and it2["schemaId"] is None and it2["values"] == {}
    assert it1["categoryName"].startswith("T-orders Gematologiya")

    # unit isolation: the facility's rows are 403 for staff of another facility; the region administrator sees them
    assert c.get(f"{API}/orders/{order['id']}", headers=env["h_b"]).status_code == 403
    assert c.get(f"{API}/items/{it1['id']}", headers=env["h_b"]).status_code == 403
    assert c.get(f"{API}/orders/{order['id']}", headers=env["h_region"]).status_code == 200
    bundle = c.get(f"{API}/orders/{order['id']}", headers=h).json()
    assert set(bundle) == {"order", "items", "payments"} and len(bundle["items"]) == 2 and bundle["payments"] == []

    # list + search by number / patient name
    lst = c.get(f"{API}/units/{env['facility']}/orders", params={"search": order["number"].lower()}, headers=h).json()
    assert [o["id"] for o in lst["items"]] == [order["id"]] and lst["totalPages"] == 1
    lst = c.get(
        f"{API}/units/{env['facility']}/orders", params={"search": "bemor", "patientId": env["patient"]}, headers=h
    ).json()
    assert order["id"] in [o["id"] for o in lst["items"]]
    # the region sees the cheque too (everything under it), narrowed by unitId; another facility matches nothing
    region_list = c.get(f"{API}/units/{env['a']['region']}/orders", params={"search": order["number"]}, headers=env["h_region"]).json()
    assert [o["id"] for o in region_list["items"]] == [order["id"]] and region_list["items"][0]["unitName"] == f"T-{env['tag']} SES"
    assert c.get(f"{API}/units/{env['a']['region']}/orders", params={"unitId": env["b"]["facility"]}, headers=env["h_region"]).json()["total"] == 0

    # worklist: unpaid orders are not there yet
    wl = c.get(f"{API}/units/{env['facility']}/worklist", params={"categoryIds": [env["cat"]]}, headers=h).json()
    assert it1["id"] not in [r["id"] for r in wl["items"]]

    # pay: validation, partial
    r = c.post(f"{API}/orders/{order['id']}/pay", json={"amount": 0, "method": "cash", "sendSms": False}, headers=h)
    assert (
        r.status_code == 422
        and r.json()["error"]["code"] == "amount"
        and r.json()["error"]["message"] == "Summa 1 – 135000 oralig‘ida bo‘lishi kerak"
    )
    r = c.post(f"{API}/orders/{order['id']}/pay", json={"amount": 35000, "method": "card", "sendSms": True}, headers=h)
    assert r.status_code == 200, r.text
    paid = r.json()
    assert (
        paid["order"]["payment"] == "partial"
        and paid["order"]["status"] == "in_progress"
        and paid["order"]["paidAmount"] == 35000
    )
    assert (
        len(paid["payments"]) == 1
        and paid["payments"][0]["method"] == "card"
        and paid["payments"][0]["refundedAt"] is None
    )
    # (a pending service can leave a paid cheque too — see test_remove_from_paid_cheque_gives_the_difference_back)

    # worklist now shows the schema item with join columns
    wl = c.get(
        f"{API}/units/{env['facility']}/worklist",
        params={"categoryIds": [env["cat"]], "status": ["pending", "entered"], "search": "umumiy"},
        headers=h,
    ).json()
    row = next(x for x in wl["items"] if x["id"] == it1["id"])
    assert (
        row["orderNumber"] == order["number"]
        and row["patientName"] == order["patientName"]
        and row["patientGender"] == "male"
    )
    assert it2["id"] not in [x["id"] for x in wl["items"]]

    # submit before values → state; values → entered; required check; toggle
    r = c.post(f"{API}/items/{it1['id']}/submit", headers=h)
    assert (
        r.status_code == 409
        and r.json()["error"]["code"] == "state"
        and r.json()["error"]["message"] == "Avval natijalarni saqlang"
    )
    r = c.put(f"{API}/items/{it1['id']}/values", json={"values": {"hb": None, "tbl": []}, "labNote": "x"}, headers=h)
    assert (
        r.status_code == 200
        and r.json()["status"] == "entered"
        and r.json()["technicianId"] == env["emp_a"]
        and r.json()["labNote"] == "x"
    )
    r = c.post(f"{API}/items/{it1['id']}/submit", headers=h)
    assert r.status_code == 422 and r.json()["error"] == {"code": "required", "message": "To‘ldirilmagan: Gemoglobin"}
    r = c.put(
        f"{API}/items/{it1['id']}/values",
        json={"values": {"hb": 12.5, "tbl": [{"name": "WBC", "value": "4"}]}},
        headers=h,
    )
    assert r.status_code == 200 and r.json()["labNote"] is None
    assert c.post(f"{API}/items/{it1['id']}/submit", headers=h).json()["status"] == "submitted"
    r = c.post(f"{API}/items/{it1['id']}/submit", headers=h).json()
    assert r["status"] == "entered" and r["submittedAt"] is None
    assert c.post(f"{API}/items/{it1['id']}/submit", headers=h).json()["submittedAt"] is not None

    # approve item → document + pdf
    r = c.post(f"{API}/items/{it1['id']}/approve", json={}, headers=h)
    assert r.status_code == 200, r.text
    approved = r.json()
    assert (
        approved["item"]["status"] == "approved"
        and approved["item"]["doctorName"] == "T-orders Doctor"
        and approved["item"]["documentId"] == approved["document"]["id"]
    )
    doc = approved["document"]
    assert doc["templateId"] == env["tpl_item"] and doc["templateVersion"] == 2 and doc["status"] == "final"
    assert (
        doc["title"].endswith(" — natija")
        and doc["orderItemId"] == it1["id"]
        and doc["pdfUrl"] == f"/api/v1/documents/{doc['id']}/pdf"
    )
    assert [d["channel"] for d in doc["deliveries"]] == ["portal", "sms"]
    r = c.get(doc["pdfUrl"], headers=h)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/pdf") and r.content == FAKE_PDF
    assert c.get(f"{API}/documents/{doc['id']}", headers=env["h_b"]).status_code == 403
    assert doc["unitId"] == env["facility"] and doc["unitName"] == f"T-{env['tag']} SES"
    docs = c.get(f"{API}/units/{env['facility']}/documents", params={"orderId": order["id"]}, headers=h).json()
    assert [d["id"] for d in docs] == [doc["id"]]
    # order still in progress (it2 pending)
    o = c.get(f"{API}/orders/{order['id']}", headers=h).json()["order"]
    assert o["status"] == "in_progress" and o["progress"]["approved"] == 1 and o["progress"]["pending"] == 1
    # values on approved → 409 approved
    r = c.put(f"{API}/items/{it1['id']}/values", json={"values": {}}, headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "approved"

    # public token link + outbox side effects (sms result_ready + telegram push)
    async def _q(s: Any) -> Any:
        d = (await s.execute(select(ResultDocument).where(ResultDocument.id == uuid.UUID(doc["id"])))).scalar_one()
        msgs = (
            (await s.execute(select(OutboxMessage).where(OutboxMessage.order_id == uuid.UUID(order["id"]))))
            .scalars()
            .all()
        )
        return d.public_token, d.pdf_file_id, [(m.channel, m.kind) for m in msgs]

    token, pdf_file_id, kinds = db(c, _q)
    assert token and pdf_file_id
    assert ("sms", "payment_receipt") in kinds and ("telegram", "payment_receipt") in kinds
    assert ("sms", "result_ready") in kinds and ("telegram", "result_ready") in kinds
    # public verification link (the QR value): authenticity info, unlimited scans
    info = c.get(f"{API}/d/{token}").json()
    assert info["id"] == doc["id"] and info["title"] and info["orgName"] == f"T-{env['tag']} tumani"  # the organisation above the facility
    assert info["issuedAt"].endswith("Z")
    assert c.get(f"{API}/d/{token}").status_code == 200
    # the PDF itself is behind an SMS code sent to the document's phone (dev mode returns it)
    otp_req = c.post(f"{API}/d/{token}/otp/request")
    assert otp_req.status_code == 200, otp_req.text
    req = otp_req.json()
    assert req["challengeId"] and req["phoneMasked"] and req["devCode"]
    r = c.post(f"{API}/d/{token}/otp/verify", json={"challengeId": req["challengeId"], "code": req["devCode"]})
    assert r.status_code == 200 and r.content == FAKE_PDF
    # the fixture facility has no SMS account: outside dev mode the request must say so instead of "code sent"
    settings.otp_dev_mode = False
    try:
        r = c.post(f"{API}/d/{token}/otp/request")
        assert r.status_code == 409 and r.json()["error"]["code"] == "sms_not_configured", r.text
    finally:
        settings.otp_dev_mode = True
    assert c.get(f"{API}/d/{'x' * 32}").status_code == 404
    # the organisation's code-free window (inherited down the tree): by default (0) a code is always needed and `open` refuses
    assert info["otpRequired"] is True and info["freeUntil"] is None
    r = c.post(f"{API}/d/{token}/open")
    assert r.status_code == 403 and r.json()["error"]["code"] == "otp_required", r.text
    r = c.put(f"{API}/units/{env['a']['region']}", json={"documentFreeViewDays": 3}, headers=env["h_region"])
    assert r.status_code == 200 and r.json()["settings"]["documentFreeViewDays"] == 3, r.text
    try:
        free = c.get(f"{API}/d/{token}").json()
        assert free["otpRequired"] is False and free["freeUntil"].endswith("Z")
        r = c.post(f"{API}/d/{token}/open")
        assert r.status_code == 200 and r.content == FAKE_PDF and r.headers["cache-control"] == "no-store"
        assert c.put(f"{API}/units/{env['facility']}", json={"documentFreeViewDays": 91}, headers=h).status_code == 422
    finally:
        assert c.put(f"{API}/units/{env['a']['region']}", json={"documentFreeViewDays": 0}, headers=env["h_region"]).status_code == 200
    # back to 0: closed again at once
    assert c.get(f"{API}/d/{token}").json()["otpRequired"] is True
    assert c.post(f"{API}/d/{token}/open").status_code == 403

    # the QR has its own link (`/v/…`): authenticity only — it never opens the PDF nor sends an SMS code
    qr_link = doc["verifyLink"]
    assert qr_link and "/v/" in qr_link and token not in qr_link
    vtoken = qr_link.rsplit("/", 1)[-1]
    check = c.get(f"{API}/v/{vtoken}").json()
    assert check["id"] == doc["id"] and check["status"] == "valid" and check["revokedAt"] is None
    assert check["title"] == doc["title"] and check["orgName"] == f"T-{env['tag']} tumani" and check["orderNumber"] == order["number"]
    assert check["doctorName"] == "T-orders Doctor" and check["unitName"] == f"T-{env['tag']} SES" and check["issuedAt"].endswith("Z")
    assert check["patient"].endswith("***") and "values" not in check and "phone" not in str(check).lower()
    assert c.get(f"{API}/d/{vtoken}").status_code == 404
    assert c.post(f"{API}/d/{vtoken}/otp/request").status_code == 404
    assert c.get(f"{API}/v/{token}").status_code == 404
    assert c.get(f"{API}/v/{'x' * 22}").status_code == 404

    # reject path on it2 (no schema → no required fields)
    c.put(f"{API}/items/{it2['id']}/values", json={"values": {"note": "ok"}}, headers=h)
    assert c.post(f"{API}/items/{it2['id']}/submit", headers=h).json()["status"] == "submitted"
    r = c.post(f"{API}/items/{it2['id']}/reject", json={"reason": "qayta"}, headers=h).json()
    assert r["status"] == "rejected" and r["rejectReason"] == "qayta" and r["submittedAt"] is None
    r = c.put(f"{API}/items/{it2['id']}/values", json={"values": {"note": "ok2"}}, headers=h).json()
    assert r["status"] == "entered" and r["rejectReason"] is None
    r = c.post(f"{API}/items/{it2['id']}/reject", json={"reason": "x"}, headers=h)
    assert r.status_code == 409 and r.json()["error"]["message"] == "Faqat yuborilgan natijani qaytarish mumkin"

    # pay the rest → paid; already_paid; refund → partial again + patient stats
    r = c.post(
        f"{API}/orders/{order['id']}/pay", json={"amount": 100000, "method": "cash", "sendSms": False}, headers=h
    ).json()
    assert r["order"]["payment"] == "paid" and r["order"]["paidAmount"] == 135000
    r = c.post(f"{API}/orders/{order['id']}/pay", json={"amount": 1, "method": "cash", "sendSms": False}, headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "already_paid"
    pay_id = paid["payments"][0]["id"]
    r = c.post(f"{API}/payments/{pay_id}/refund", json={"reason": "xato"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["order"]["payment"] == "partial" and r.json()["order"]["paidAmount"] == 100000
    assert next(p for p in r.json()["payments"] if p["id"] == pay_id)["refundedAt"] is not None
    r = c.post(f"{API}/payments/{pay_id}/refund", json={"reason": "again"}, headers=h)
    assert r.status_code == 409
    p = c.get(f"{API}/patients/{env['patient']}", headers=h).json()
    assert p["stats"]["orders"] >= 1 and p["stats"]["totalSpent"] == 100000 and p["stats"]["lastVisitAt"] is not None

    # the doctor takes the result back: the SMS link answers 410, the QR check says the paper is withdrawn
    r = c.post(f"{API}/items/{it1['id']}/revoke", json={"reason": "birlik xato"}, headers=h)
    assert r.status_code == 200, r.text
    assert c.get(f"{API}/d/{token}").status_code == 410
    check = c.get(f"{API}/v/{vtoken}").json()
    assert check["status"] == "revoked" and check["revokedAt"] and check["id"] == doc["id"]


def test_remove_from_paid_cheque_gives_the_difference_back(env: dict[str, Any]) -> None:
    """A service added by mistake can leave a paid cheque: the excess is refunded, the rest stays paid (same method)."""
    c: TestClient = env["client"]
    h = env["h_a"]
    created = _create(env, [env["st1"], env["st2"]], note="adashib")
    order, items = created["order"], created["items"]
    total = order["total"]
    r = c.post(f"{API}/orders/{order['id']}/pay", json={"amount": total, "method": "cash", "sendSms": False}, headers=h)
    assert r.status_code == 200 and r.json()["order"]["payment"] == "paid", r.text
    r = c.delete(f"{API}/orders/{order['id']}/items/{items[1]['id']}", params={"reason": "adashib qo'shildi"}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    new_total = body["order"]["total"]
    assert new_total < total and body["refunded"] == total - new_total
    assert body["order"]["paidAmount"] == new_total and body["order"]["payment"] == "paid"
    assert [i["id"] for i in body["items"]] == [items[0]["id"]]
    pays = c.get(f"{API}/orders/{order['id']}", headers=h).json()["payments"]
    gone = [p for p in pays if p["refundedAt"]]
    live = [p for p in pays if not p["refundedAt"]]
    assert len(gone) == 1 and gone[0]["amount"] == total
    assert len(live) == 1 and live[0]["amount"] == new_total and live[0]["method"] == "cash"
    # the last service as well: everything goes back, the cheque is unpaid again
    r = c.delete(f"{API}/orders/{order['id']}/items/{items[0]['id']}", headers=h)
    assert r.status_code == 200 and r.json()["refunded"] == new_total, r.text
    assert r.json()["order"]["paidAmount"] == 0 and r.json()["order"]["payment"] == "unpaid" and r.json()["items"] == []


def test_remove_cancel_and_closed(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    h = env["h_a"]
    created = _create(env, [env["st1"], env["st2"]], note="eslatma")
    order, items = created["order"], created["items"]
    # cannot cancel/pay checks: pay on empty later; remove pending item on unpaid order → ok
    r = c.delete(f"{API}/orders/{order['id']}/items/{items[1]['id']}", headers=h)
    assert (
        r.status_code == 200
        and [i["id"] for i in r.json()["items"]] == [items[0]["id"]]
        and r.json()["order"]["total"] == 90000
        and r.json()["refunded"] == 0
    )
    # item of another order → 404
    assert c.delete(f"{API}/orders/{order['id']}/items/{uuid.uuid4()}", headers=h).status_code == 404
    r = c.post(f"{API}/orders/{order['id']}/cancel", json={"reason": "bemor keldi"}, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "cancelled" and r.json()["note"] == "eslatma"
    assert r.json()["itemCount"] == 0 and r.json()["progress"]["cancelled"] == 1
    r = c.post(f"{API}/orders/{order['id']}/items", json={"serviceTypeIds": [env["st2"]]}, headers=h)
    assert r.status_code == 409 and r.json()["error"] == {"code": "closed", "message": "Chek yopilgan"}
    r = c.post(f"{API}/orders/{order['id']}/pay", json={"amount": 10, "method": "cash", "sendSms": False}, headers=h)
    assert r.status_code == 422 and r.json()["error"]["code"] == "empty"
    # a service switched off for the cheque's facility: flagged off in that facility's list, refused on a new cheque (409)
    r = c.put(f"{API}/service-types/{env['st2']}", json={"disabledUnitIds": [env["facility"]]}, headers=h)
    assert r.status_code == 200 and r.json()["disabledUnitIds"] == [env["facility"]], r.text
    try:
        listed = {s["id"]: s for s in c.get(f"{API}/units/{env['facility']}/service-types", params={"unitId": env["facility"]}, headers=h).json()}
        assert listed[env["st2"]]["available"] is False and listed[env["st1"]]["available"] is True and listed[env["st2"]]["effectivePrice"] == 50000
        r = c.post(f"{API}/units/{env['facility']}/orders", json={"patientId": env["patient"], "serviceTypeIds": [env["st1"], env["st2"]]}, headers=h)
        assert r.status_code == 409 and r.json()["error"]["code"] == "not_in_branch", r.text
    finally:
        assert c.put(f"{API}/service-types/{env['st2']}", json={"disabledUnitIds": []}, headers=h).status_code == 200
    # bad patient / facility → 404
    r = c.post(f"{API}/units/{env['facility']}/orders", json={"patientId": env["patient"], "unitId": str(uuid.uuid4()), "serviceTypeIds": []}, headers=h)
    assert r.status_code == 404 and r.json()["error"]["message"] == "Muassasa topilmadi"
    r = c.post(f"{API}/units/{env['facility']}/orders", json={"patientId": str(uuid.uuid4()), "serviceTypeIds": []}, headers=h)
    assert r.status_code == 404 and r.json()["error"]["message"] == "Bemor topilmadi"
    # from a higher unit the facility must be named; another facility of the tree is fine, the other tree is not
    r = c.post(f"{API}/units/{env['a']['region']}/orders", json={"patientId": env["patient"], "serviceTypeIds": []}, headers=env["h_region"])
    assert r.status_code == 422 and r.json()["error"]["code"] == "facility_required"
    r = c.post(f"{API}/units/{env['a']['region']}/orders", json={"patientId": env["patient"], "unitId": env["facility"], "serviceTypeIds": [env["st2"]]}, headers=env["h_region"])
    assert r.status_code == 201 and r.json()["order"]["unitId"] == env["facility"] and r.json()["items"][0]["price"] == 50000, r.text
    r = c.post(f"{API}/units/{env['a']['region']}/orders", json={"patientId": env["patient"], "unitId": env["b"]["facility"], "serviceTypeIds": []}, headers=env["h_region"])
    assert r.status_code == 404 and r.json()["error"]["message"] == "Muassasa topilmadi"
    # a facility that cannot see the service's owner: not offered there (409)
    r = c.post(f"{API}/units/{env['b']['facility']}/orders", json={"patientId": env["patient"], "serviceTypeIds": [env["st1"]]}, headers=env["h_b"])
    assert r.status_code == 404  # the patient is not seen at facility B
    # staff of another facility → 403
    r = c.post(f"{API}/units/{env['facility']}/orders", json={"patientId": env["patient"], "serviceTypeIds": []}, headers=env["h_b"])
    assert r.status_code == 403


def test_order_scope_approval(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    h = env["h_a"]
    created = _create(env, [env["st1"], env["st1"]])
    order, items = created["order"], created["items"]
    assert len(items) == 2  # duplicates allowed
    r = c.post(
        f"{API}/orders/{order['id']}/pay",
        json={"amount": order["total"], "method": "transfer", "sendSms": False},
        headers=h,
    )
    assert r.json()["order"]["payment"] == "paid"
    for it in items:
        c.put(f"{API}/items/{it['id']}/values", json={"values": {"hb": 1}}, headers=h)
    # nothing submitted yet → 409 state
    r = c.post(f"{API}/orders/{order['id']}/approve", json={"templateId": env["tpl_order"]}, headers=h)
    assert r.status_code == 409 and r.json()["error"]["message"] == "Tasdiqlash uchun yuborilgan tahlil yo‘q"
    for it in items:
        assert c.post(f"{API}/items/{it['id']}/submit", headers=h).json()["status"] == "submitted"
    scope = c.get(f"{API}/orders/{order['id']}/scope-items", params={"templateId": env["tpl_order"]}, headers=h).json()
    assert sorted(i["id"] for i in scope) == sorted(i["id"] for i in items)
    r = c.post(f"{API}/orders/{order['id']}/approve", json={"templateId": env["tpl_item"]}, headers=h)
    assert r.status_code == 422 and r.json()["error"]["code"] == "scope"
    r = c.post(f"{API}/orders/{order['id']}/approve", json={"templateId": str(uuid.uuid4())}, headers=h)
    assert r.status_code == 422 and r.json()["error"]["code"] == "no_template"
    r = c.post(f"{API}/orders/{order['id']}/approve", json={"templateId": env["tpl_order"]}, headers=h)
    assert r.status_code == 200, r.text
    out = r.json()
    assert len(out["items"]) == 2 and all(
        i["status"] == "approved" and i["documentId"] == out["document"]["id"] for i in out["items"]
    )
    assert sorted(out["document"]["orderItemIds"]) == sorted(i["id"] for i in items)
    assert out["document"]["title"] == f"T-orders panel {RUN}" and out["document"]["orderItemId"] == items[0]["id"]
    o = c.get(f"{API}/orders/{order['id']}", headers=h).json()["order"]
    assert o["status"] == "completed" and o["progress"]["approved"] == 2
    # completed → closed for new items
    r = c.post(f"{API}/orders/{order['id']}/items", json={"serviceTypeIds": [env["st2"]]}, headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "closed"
    docs = c.get(f"{API}/units/{env['facility']}/documents", params={"patientId": env["patient"]}, headers=h).json()
    assert docs[0]["id"] == out["document"]["id"]
