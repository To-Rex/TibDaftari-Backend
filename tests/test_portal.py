"""API tests for the patient portal against the real dev DB (fixture rows prefixed `T-portal-`).

Fixture graph: one phone registered in two clinics (A, B); orders in both, a cancelled order,
an approved item with a document, a pending item with unpublished values, and a foreign patient's order.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from app.core.config import settings
from app.core.timeutil import utcnow
from app.infrastructure.db.models import (
    AttributeSchema,
    Category,
    Company,
    Order,
    OrderItem,
    Patient,
    ResultDocument,
    ResultTemplate,
    ServiceType,
)
from app.infrastructure.db.session import dispose_engine, session_scope
from app.infrastructure.redis.client import close_redis
from app.modules.templates import service as templates_svc
from fastapi.testclient import TestClient

RUN = uuid.uuid4().hex[:8]
API = "/api/v1"
FAKE_PDF = b"%PDF-1.4 portal test"


def _phone() -> str:
    return "99893" + "".join(random.choice("0123456789") for _ in range(7))


def _order(co: uuid.UUID, patient: Patient, number: str, **kw: Any) -> Order:
    return Order(
        company_id=co,
        branch_id=uuid.uuid4(),
        number=number,
        patient_id=patient.id,
        patient_name=patient.full_name,
        patient_phone=patient.phone,
        created_by_employee_id=uuid.uuid4(),
        **kw,
    )


def _item(order: Order, st: ServiceType, cat: Category, **kw: Any) -> OrderItem:
    return OrderItem(
        company_id=order.company_id,
        order_id=order.id,
        branch_id=order.branch_id,
        service_type_id=st.id,
        service_name=st.name,
        category_id=cat.id,
        category_name=cat.name,
        price=st.price,
        final_price=st.price,
        **kw,
    )


def _fixture_rows() -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        async with session_scope() as s:
            phone = _phone()
            co_a = Company(name=f"T-portal-A-{RUN}", slug=f"t-portal-a-{RUN}")
            co_b = Company(name=f"T-portal-B-{RUN}", slug=f"t-portal-b-{RUN}")
            s.add_all([co_a, co_b])
            await s.flush()
            p_a = Patient(
                company_id=co_a.id,
                full_name=f"T-portal Bemor {RUN}",
                phone=phone,
                gender="female",
                pinfl="12345678901234",
                passport_number=f"AA{RUN[:7]}",
                discount_percent=15,
                note="maxfiy",
                tags=["vip"],
                stats_orders=2,
                stats_total_spent=90000,
            )
            p_b = Patient(company_id=co_b.id, full_name=f"T-portal Bemor B {RUN}", phone=phone, tags=[])
            foreign = Patient(company_id=co_a.id, full_name=f"T-portal Boshqa {RUN}", phone=_phone(), tags=[])
            cat = Category(company_id=co_a.id, name=f"T-portal Kat {RUN}", order=1)
            schema = AttributeSchema(
                company_id=co_a.id,
                name=f"T-portal sxema {RUN}",
                status="published",
                fields=[{"key": "hb", "label": "Gemoglobin", "type": "number", "required": True, "order": 1}],
            )
            s.add_all([p_a, p_b, foreign, cat, schema])
            await s.flush()
            st = ServiceType(company_id=co_a.id, category_id=cat.id, name=f"T-portal Qon {RUN}", code=f"TP{RUN[:5]}", price=50000, schema_id=schema.id)
            st2 = ServiceType(company_id=co_a.id, category_id=cat.id, name=f"T-portal Konsult {RUN}", price=40000)
            tpl = ResultTemplate(company_id=co_a.id, name=f"T-portal tpl {RUN}", status="active", service_type_ids=[], category_ids=[], doc={"elements": []})
            s.add_all([st, st2, tpl])
            await s.flush()
            o1 = _order(co_a.id, p_a, f"TP{RUN[:4]}-000001", status="in_progress", payment="paid", subtotal=90000, total=90000, paid_amount=90000, item_count=2)
            o_cancel = _order(co_a.id, p_a, f"TP{RUN[:4]}-000002", status="cancelled")
            o_b = _order(co_b.id, p_b, f"TPB{RUN[:4]}-000001", status="open")
            o_foreign = _order(co_a.id, foreign, f"TP{RUN[:4]}-000003", status="open")
            s.add_all([o1, o_cancel, o_b, o_foreign])
            await s.flush()
            now = utcnow()
            it_appr = _item(o1, st, cat, status="approved", schema_id=schema.id, schema_version=1, values={"hb": 130}, lab_note="normada", approved_at=now)
            it_pend = _item(o1, st2, cat, status="entered", values={"secret": 1}, lab_note="hali tayyor emas")
            s.add_all([it_appr, it_pend])
            await s.flush()
            doc = ResultDocument(
                company_id=co_a.id,
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
                "phone": phone,
                "co_a": str(co_a.id),
                "co_b": str(co_b.id),
                "co_a_name": co_a.name,
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

    async def _main() -> dict[str, Any]:
        try:
            return await _run()
        finally:
            await close_redis()
            await dispose_engine()

    return asyncio.run(_main())


@pytest.fixture(scope="module")
def env() -> Iterator[dict[str, Any]]:
    settings.workers_enabled = False
    settings.telegram_enabled = False
    settings.otp_dev_mode = True
    data = _fixture_rows()
    mp = pytest.MonkeyPatch()

    async def _pdf(*_: Any, **__: Any) -> bytes:
        return FAKE_PDF

    mp.setattr(templates_svc, "render_snapshot_pdf", _pdf)
    from app.main import app

    with TestClient(app) as client:
        req = client.post(f"{API}/auth/patient/otp/request", json={"phone": data["phone"]})
        assert req.status_code == 200, req.text
        ver = client.post(
            f"{API}/auth/patient/otp/verify",
            json={"phone": data["phone"], "code": req.json()["devCode"], "challengeId": req.json()["challengeId"]},
        )
        assert ver.status_code == 200, ver.text
        data["client"] = client
        data["h"] = {"Authorization": f"Bearer {ver.json()['accessToken']}"}
        yield data
    mp.undo()


def test_overview_merges_clinics_and_redacts_patient(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    r = c.get(f"{API}/portal/overview", headers=env["h"])
    assert r.status_code == 200, r.text
    body = r.json()
    p = body["patient"]
    assert p["id"] == env["p_a"] and p["phone"] == env["phone"] and p["gender"] == "female"
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
    companies = {x["id"]: x["name"] for x in body["companies"]}
    assert companies == {env["co_a"]: env["co_a_name"], env["co_b"]: f"T-portal-B-{RUN}"}


def test_order_redacts_unapproved_results_and_hides_foreign(env: dict[str, Any]) -> None:
    c: TestClient = env["client"]
    r = c.get(f"{API}/portal/orders/{env['o1']}", headers=env["h"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["order"]["id"] == env["o1"] and body["order"]["payment"] == "paid"
    items = {i["id"]: i for i in body["items"]}
    assert set(items) == {env["it_appr"], env["it_pend"]}
    assert items[env["it_appr"]]["values"] == {"hb": 130} and items[env["it_appr"]]["labNote"] == "normada"
    assert items[env["it_pend"]]["values"] == {} and items[env["it_pend"]]["labNote"] is None
    assert items[env["it_pend"]]["status"] == "entered"
    assert [d["id"] for d in body["documents"]] == [env["doc"]]

    # other clinic, same phone → owned
    assert c.get(f"{API}/portal/orders/{env['o_b']}", headers=env["h"]).status_code == 200
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
    assert body["document"]["id"] == env["doc"] and body["document"]["orderItemIds"] == [env["it_appr"]]
    assert body["template"]["id"] == env["tpl"] and "doc" in body["template"]
    assert body["item"]["id"] == env["it_appr"] and body["item"]["values"] == {"hb": 130}
    assert body["order"]["id"] == env["o1"]
    assert [i["id"] for i in body["items"]] == [env["it_appr"]]
    assert [s["id"] for s in body["schemas"]] == [env["schema"]] and body["schemas"][0]["usedBy"] == 1
    assert body["serviceCodes"] == {env["st"]: env["st_code"]}
    assert body["category"]["id"] == env["cat"]

    r = c.get(f"{API}/portal/documents/{env['doc']}/pdf", headers=env["h"])
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/pdf")
    assert r.content == FAKE_PDF
    # second call served from the stored file
    assert c.get(f"{API}/portal/documents/{env['doc']}/pdf", headers=env["h"]).content == FAKE_PDF

    r = c.get(f"{API}/portal/documents/{uuid.uuid4()}", headers=env["h"])
    assert r.status_code == 404 and r.json()["error"]["message"] == "Hujjat topilmadi"
    assert c.get(f"{API}/portal/documents/{uuid.uuid4()}/pdf", headers=env["h"]).status_code == 404
