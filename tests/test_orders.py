"""API tests for the orders module against the real dev DB (fixture rows prefixed `T-orders-`).

Templates rendering is provided by another module; `build_document_snapshot` / `render_snapshot_pdf`
are monkeypatched here (contract stubs) so approval can be exercised end-to-end.
"""

from __future__ import annotations

import asyncio
import random
import re
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from app.core.config import settings
from app.core.permissions import PERMISSIONS
from app.core.security import hash_password
from app.infrastructure.db.models import (
    AttributeSchema,
    Branch,
    Category,
    Company,
    Employee,
    OutboxMessage,
    Patient,
    ResultDocument,
    ResultTemplate,
    Role,
    ServiceType,
)
from app.infrastructure.db.session import dispose_engine, session_scope
from app.infrastructure.redis.client import close_redis
from app.modules.templates import service as templates_svc
from fastapi.testclient import TestClient
from sqlalchemy import select

RUN = uuid.uuid4().hex[:8]
API = "/api/v1"
FAKE_SNAPSHOT = {"version": 1, "doc": {}, "context": {}, "assets": {}, "language": "uz"}
FAKE_PDF = b"%PDF-1.4 test"


def _phone() -> str:
    return "99897" + "".join(random.choice("0123456789") for _ in range(7))


def _fixture_rows() -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        async with session_scope() as s:
            co_a = Company(name=f"T-orders-A-{RUN}", slug=f"t-orders-a-{RUN}")
            co_b = Company(name=f"T-orders-B-{RUN}", slug=f"t-orders-b-{RUN}")
            s.add_all([co_a, co_b])
            await s.flush()
            perms = [p for p in PERMISSIONS if not p.startswith("platform.")]
            role_a = Role(company_id=co_a.id, key="admin", name="T-orders", permissions=perms)
            role_b = Role(company_id=co_b.id, key="admin", name="T-orders", permissions=perms)
            s.add_all([role_a, role_b])
            await s.flush()
            pw = hash_password("secret123")
            emp_a = Employee(
                company_id=co_a.id,
                full_name="T-orders Doctor",
                login=f"t-orders-a-{RUN}",
                password_hash=pw,
                role_id=role_a.id,
            )
            emp_b = Employee(
                company_id=co_b.id,
                full_name="T-orders B",
                login=f"t-orders-b-{RUN}",
                password_hash=pw,
                role_id=role_b.id,
            )
            branch = Branch(company_id=co_a.id, name=f"T-orders filial {RUN}", code=f"T{RUN[:4].upper()}", order_seq=0)
            patient = Patient(
                company_id=co_a.id,
                full_name=f"T-orders Bemor {RUN}",
                phone=_phone(),
                gender="male",
                discount_percent=10,
                tags=[],
                telegram_chat_id="123456",
            )
            cat = Category(company_id=co_a.id, name=f"T-orders Gematologiya {RUN}", order=1)
            schema = AttributeSchema(
                company_id=co_a.id,
                name=f"T-orders sxema {RUN}",
                version=3,
                status="published",
                fields=[
                    {"key": "hb", "label": "Gemoglobin", "type": "number", "required": True, "order": 1},
                    {
                        "key": "tbl",
                        "label": "Jadval",
                        "type": "table",
                        "required": False,
                        "order": 2,
                        "presetRows": [{"name": "WBC", "value": ""}],
                    },
                ],
            )
            s.add_all([emp_a, emp_b, branch, patient, cat, schema])
            await s.flush()
            st1 = ServiceType(
                company_id=co_a.id,
                category_id=cat.id,
                name=f"T-orders Umumiy qon {RUN}",
                code=f"TO{RUN[:5]}",
                price=100000,
                branch_prices={},
                schema_id=schema.id,
            )
            st2 = ServiceType(
                company_id=co_a.id,
                category_id=cat.id,
                name=f"T-orders Konsultatsiya {RUN}",
                price=60000,
                branch_prices={str(branch.id): 50000},
            )
            s.add_all([st1, st2])
            await s.flush()
            tpl_item = ResultTemplate(
                company_id=co_a.id,
                name=f"T-orders item tpl {RUN}",
                status="active",
                version=2,
                service_type_ids=[st1.id],
                category_ids=[],
                scope="item",
                doc={"elements": []},
            )
            tpl_order = ResultTemplate(
                company_id=co_a.id,
                name=f"T-orders panel {RUN}",
                status="active",
                version=1,
                service_type_ids=[],
                category_ids=[],
                scope="order",
                doc={"elements": []},
            )
            s.add_all([tpl_item, tpl_order])
            await s.flush()
            return {
                "co_a": str(co_a.id),
                "co_b": str(co_b.id),
                "login_a": emp_a.login,
                "login_b": emp_b.login,
                "emp_a": str(emp_a.id),
                "branch": str(branch.id),
                "branch_code": branch.code,
                "patient": str(patient.id),
                "cat": str(cat.id),
                "st1": str(st1.id),
                "st2": str(st2.id),
                "tpl_item": str(tpl_item.id),
                "tpl_order": str(tpl_order.id),
            }

    async def _main() -> dict[str, Any]:
        try:
            return await _run()
        finally:
            await close_redis()
            await dispose_engine()

    return asyncio.run(_main())


def _db(client: TestClient, fn: Any) -> Any:
    """Run an async DB read on the app's own event loop (shares the engine pool with the app)."""

    async def _main() -> Any:
        async with session_scope() as s:
            return await fn(s)

    return client.portal.call(_main)


@pytest.fixture(scope="module")
def env() -> Iterator[dict[str, Any]]:
    settings.workers_enabled = False
    settings.telegram_enabled = False
    data = _fixture_rows()
    mp = pytest.MonkeyPatch()

    async def _snapshot(*_: Any, **__: Any) -> dict[str, Any]:
        return dict(FAKE_SNAPSHOT)

    async def _pdf(*_: Any, **__: Any) -> bytes:
        return FAKE_PDF

    mp.setattr(templates_svc, "build_document_snapshot", _snapshot)
    mp.setattr(templates_svc, "render_snapshot_pdf", _pdf)
    from app.main import app

    with TestClient(app) as client:
        tok_a = client.post(f"{API}/auth/staff/login", json={"login": data["login_a"], "password": "secret123"}).json()[
            "accessToken"
        ]
        tok_b = client.post(f"{API}/auth/staff/login", json={"login": data["login_b"], "password": "secret123"}).json()[
            "accessToken"
        ]
        data["client"] = client
        data["h_a"] = {"Authorization": f"Bearer {tok_a}"}
        data["h_b"] = {"Authorization": f"Bearer {tok_b}"}
        yield data
    mp.undo()


def _create(env: dict[str, Any], service_type_ids: list[str], note: str | None = None) -> dict[str, Any]:
    c: TestClient = env["client"]
    r = c.post(
        f"{API}/companies/{env['co_a']}/orders",
        json={"patientId": env["patient"], "branchId": env["branch"], "serviceTypeIds": service_type_ids, "note": note},
        headers=env["h_a"],
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_full_workflow_create_pay_worklist_values_submit_approve(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    h = env["h_a"]
    created = _create(env, [env["st1"], env["st2"], str(uuid.uuid4()), "not-a-uuid"], note="tez")
    order, items = created["order"], created["items"]
    assert re.fullmatch(rf"{env['branch_code']}-\d{{6}}", order["number"])
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

    # tenant isolation
    assert c.get(f"{API}/orders/{order['id']}", headers=env["h_b"]).status_code == 404
    assert c.get(f"{API}/items/{it1['id']}", headers=env["h_b"]).status_code == 404
    bundle = c.get(f"{API}/orders/{order['id']}", headers=h).json()
    assert set(bundle) == {"order", "items", "payments"} and len(bundle["items"]) == 2 and bundle["payments"] == []

    # list + search by number / patient name
    lst = c.get(f"{API}/companies/{env['co_a']}/orders", params={"search": order["number"].lower()}, headers=h).json()
    assert [o["id"] for o in lst["items"]] == [order["id"]] and lst["totalPages"] == 1
    lst = c.get(
        f"{API}/companies/{env['co_a']}/orders", params={"search": "bemor", "patientId": env["patient"]}, headers=h
    ).json()
    assert order["id"] in [o["id"] for o in lst["items"]]

    # worklist: unpaid orders are not there yet
    wl = c.get(f"{API}/companies/{env['co_a']}/worklist", params={"categoryIds": [env["cat"]]}, headers=h).json()
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
    # remove item on a paid order → 409 paid
    r = c.delete(f"{API}/orders/{order['id']}/items/{it2['id']}", headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "paid"

    # worklist now shows the schema item with join columns
    wl = c.get(
        f"{API}/companies/{env['co_a']}/worklist",
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
    assert c.get(f"{API}/documents/{doc['id']}", headers=env["h_b"]).status_code == 404
    docs = c.get(f"{API}/companies/{env['co_a']}/documents", params={"orderId": order["id"]}, headers=h).json()
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

    token, pdf_file_id, kinds = _db(c, _q)
    assert token and pdf_file_id
    assert ("sms", "payment_receipt") in kinds and ("telegram", "payment_receipt") in kinds
    assert ("sms", "result_ready") in kinds and ("telegram", "result_ready") in kinds
    r = c.get(f"{API}/d/{token}")
    assert r.status_code == 200 and r.content == FAKE_PDF
    assert c.get(f"{API}/d/{'x' * 32}").status_code == 404

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
    # bad patient/branch → 404
    r = c.post(
        f"{API}/companies/{env['co_a']}/orders",
        json={"patientId": env["patient"], "branchId": str(uuid.uuid4()), "serviceTypeIds": []},
        headers=h,
    )
    assert r.status_code == 404 and r.json()["error"]["message"] == "Bemor yoki filial topilmadi"
    # cross-tenant create → 403
    r = c.post(
        f"{API}/companies/{env['co_a']}/orders",
        json={"patientId": env["patient"], "branchId": env["branch"], "serviceTypeIds": []},
        headers=env["h_b"],
    )
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
    docs = c.get(f"{API}/companies/{env['co_a']}/documents", params={"patientId": env["patient"]}, headers=h).json()
    assert docs[0]["id"] == out["document"]["id"]
