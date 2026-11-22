"""Every tool of the assistant against the real services, one working day end to end: the catalogue, a template,
a role and an employee, a patient, a cheque with a payment, the laboratory and the approval, the reports, the
messages, a new facility with a sample's working set, a reset. A tool either works or answers with one of the
business refusals the step allows — never with an argument or attribute error of its own wrapper."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from app.api.deps import RequestMeta, build_staff_principal
from app.core.exceptions import AppError
from app.core.timeutil import utcnow
from app.infrastructure.db.models import ResultTemplate
from app.modules.assistant import describe as ds
from app.modules.assistant import tools as tl
from app.modules.assistant import undo as ud
from app.modules.units import service as units_svc
from fastapi.testclient import TestClient

from tests.helpers import app_instance, db, make_employee, make_tree, phone, seed


@pytest.fixture(scope="module")
def ctx() -> Iterator[dict]:
    async def rows(s):
        a = await make_tree(s)
        sup = await make_employee(s, None, super_admin=True)
        reg = await make_employee(s, a.facility, "registrator")
        await s.flush()
        return {"tag": a.tag, "region": str(a.region.id), "district": str(a.district.id), "facility": str(a.facility.id), "geo_region": str(a.geo_region.id), "geo_district": str(a.geo_district.id), "super": sup.id, "reg": reg.id}

    data = seed(rows)
    with TestClient(app_instance()) as client:
        data["client"] = client
        yield data


#: a step whose outcome depends on the state of the data — any business refusal is fine, a wrapper error is not
ANY = ("*",)


class Desk:
    """Calls tools as one employee in one working unit, inside the session it was given."""

    def __init__(self, session: Any, staff: Any, scope: Any) -> None:
        self.ctx = tl.ToolContext(session=session, staff=staff, meta=RequestMeta(ip="127.0.0.1", request_id="t", user_agent="pytest"), unit_id=scope.id, scope=scope)
        self.used: set[str] = set()

    async def call(self, name: str, allow: tuple[str, ...] = (), /, **args: Any) -> Any:
        t = tl.REGISTRY[name]
        assert t in tl.tools_for(self.ctx.staff), f"{name} is not offered to this employee"
        self.used.add(name)
        try:
            async with self.ctx.session.begin_nested():
                if t.kind == "write":  # what the confirmation card would say
                    rows = await ds.describe(self.ctx, t, tl.check_args(t, args))
                    assert all(r["label"] and ("ruxsat yo‘q" not in r["value"] or allow == ANY) for r in rows), (name, rows)
                return await tl.run_tool(t, self.ctx, args)
        except AppError as e:
            assert e.code in allow or allow == ANY, f"{name}: {e.code} — {e.message}"
            return {"refused": e.code}


    async def back(self, name: str, /, **args: Any) -> tuple[Any, list[str]]:
        """A write and its way back, as the confirmation and the undo run them: remember → write → plan → undo."""
        t = tl.REGISTRY[name]
        self.used.add(name)
        memory = await ud.remember(self.ctx, name, args)
        result = await tl.run_tool(t, self.ctx, args)
        kept = await ud.plan(self.ctx, name, args, memory, result)
        assert kept and kept["label"] and kept["steps"], f"{name}: no way back was kept"
        return result, await ud.run(self.ctx, kept)


async def desk(s: Any, employee_id: uuid.UUID, unit_id: str) -> Desk:
    staff = await build_staff_principal(s, employee_id, "t", utcnow() + timedelta(hours=1))
    return Desk(s, staff, await units_svc.scope_or_404(s, uuid.UUID(unit_id)))


def test_a_working_day_through_the_tools(ctx: dict) -> None:
    tag = ctx["tag"]
    today = utcnow().date().isoformat()
    span = {"dateFrom": today, "dateTo": today}

    async def day(s: Any) -> set[str]:
        d = await desk(s, ctx["super"], ctx["facility"])
        call = d.call
        me = await call("whoami")
        assert me["isSuperAdmin"] and me["workingUnit"]["id"] == ctx["facility"]
        found = await call("list_units", search=f"t-{tag.lower()} tumani", level="facility")
        assert [u["id"] for u in found["items"]] == [ctx["facility"]]  # a facility is found by the place above it
        await call("get_unit", unitId=ctx["facility"])
        assert (await call("list_geo", kind="countries"))["total"] >= 1
        assert any(r["id"] == ctx["geo_region"] for r in (await call("list_geo", kind="regions", search=f"T-{tag}"))["items"])
        assert [r["id"] for r in (await call("list_geo", kind="districts", regionId=ctx["geo_region"]))["items"]] == [ctx["geo_district"]]
        await call("update_unit", unitId=ctx["facility"], phone="+998 62 000-00-01", documentFreeViewDays=3)

        # ---- the catalogue: a schema, a category, a service
        schema = await call("create_schema", name=f"Qon {tag}", fields=[{"key": "hb", "label": "Gemoglobin", "type": "number", "required": True}, {"key": "izoh", "label": "Izoh", "type": "text"}])
        await call("update_schema", schemaId=schema["id"], description="Umumiy qon tahlili")
        await call("publish_schema", schemaId=schema["id"])
        assert (await call("get_schema", schemaId=schema["id"]))["status"] == "published"
        assert any(x["id"] == schema["id"] for x in (await call("list_schemas", search="qon"))["items"])
        cat = await call("create_category", name=f"Gematologiya {tag}", workflow="lab")
        await call("update_category", categoryId=cat["id"], code="GEM")
        svc = await call("create_service_type", categoryId=cat["id"], name=f"Umumiy qon {tag}", price=40_000, schemaId=schema["id"], turnaroundDays=1)
        extra = await call("create_service_type", categoryId=cat["id"], name=f"Qand {tag}", price=15_000)
        await call("update_service_type", serviceTypeId=svc["id"], price=45_000, unitPrices={ctx["facility"]: 50_000})
        assert (await call("get_service_type", serviceTypeId=svc["id"]))["price"] == 45_000
        assert {x["id"] for x in (await call("list_service_types", search=tag))["items"]} >= {svc["id"], extra["id"]}
        assert any(x["id"] == cat["id"] for x in (await call("list_categories"))["items"])

        # ---- templates
        receipt = await call("create_default_receipt", name=f"Chek {tag}")
        await call("set_template_status", templateId=receipt["id"], status="active")
        await call("update_template", templateId=receipt["id"], description="Asosiy chek")
        assert (await call("get_template", templateId=receipt["id"]))["status"] == "active"
        copy = await call("duplicate_template", templateId=receipt["id"], name=f"Chek nusxa {tag}")
        await call("delete_template", templateId=copy["id"])
        assert any(x["id"] == receipt["id"] for x in (await call("list_templates"))["items"])

        # ---- roles and staff
        assert "reception.order.create" in (await call("list_permissions"))["all"]
        role = await call("create_role", name=f"Yordamchi {tag}", permissions=["reception.patient.read"])
        await call("update_role", roleId=role["id"], permissions=["reception.patient.read", "reception.order.create"])
        assert any(r["id"] == role["id"] for r in (await call("list_roles"))["items"])
        emp = await call("create_employee", fullName=f"Laborant {tag}", login=f"lab-{tag.lower()}", roleKey="laborant")
        await call("update_employee", employeeId=emp["id"], phone="+998901112233", categoryIds=[cat["id"]])
        await call("set_employee_overrides", employeeId=emp["id"], allow=["reports.operations.read"], deny=[])
        assert (await call("get_employee", employeeId=emp["id"]))["status"] == "active"
        assert (await call("list_employees", search=f"Laborant {tag}"))["total"] == 1
        await call("delete_role", roleId=role["id"])

        # ---- the reception: a patient, a cheque, a payment
        pat = await call("create_patient", fullName=f"Sinov Bemor {tag}", phone=phone(), gender="male", birthDate="1990-05-17")
        await call("update_patient", patientId=pat["id"], workplace="Maktab", tags=["sinov"])
        assert (await call("search_patients", query=f"Sinov Bemor {tag}"))["items"][0]["id"] == pat["id"]
        assert (await call("list_patients", search=tag))["total"] == 1
        await call("get_patient", patientId=pat["id"])
        await call("patient_family", patientId=pat["id"])
        made = await call("create_order", patientId=pat["id"], serviceTypeIds=[svc["id"]], note="AI orqali")
        oid = made["order"]["id"]
        await call("add_order_items", orderId=oid, serviceTypeIds=[extra["id"]])
        bundle = await call("get_order", orderId=oid)
        items = {i["serviceName"]: i["id"] for i in bundle["items"]}
        await call("remove_order_item", orderId=oid, itemId=items[f"Qand {tag}"], reason="xato qo‘shilgan")
        total = (await call("get_order", orderId=oid))["order"]["total"]
        await call("pay_order", orderId=oid, amount=total, method="cash")
        paid = await call("get_order", orderId=oid)
        pay_id = paid["payments"][0]["id"]
        await call("change_payment_method", paymentId=pay_id, parts=[{"method": "card", "amount": total}], reason="karta bilan to‘lagan")
        assert (await call("list_orders", search=made["order"]["number"]))["total"] == 1
        await call("orders_summary", **span)

        # ---- the laboratory and the approval
        item_id = items[f"Umumiy qon {tag}"]
        assert (await call("worklist", status=["pending"]))["total"] >= 1
        await call("worklist_counts")
        assert (await call("get_order_item", itemId=item_id))["id"] == item_id
        await call("save_result_values", itemId=item_id, values={"hb": 132, "izoh": "me’yorda"}, labNote="AI sinovi")
        await call("submit_result", itemId=item_id)
        await call("reject_result", itemId=item_id, reason="qayta tekshirish")
        await call("save_result_values", itemId=item_id, values={"hb": 134, "izoh": "me’yorda"})
        await call("submit_result", itemId=item_id)
        s.add(ResultTemplate(owner_unit_id=uuid.UUID(ctx["facility"]), name=f"Natija {tag}", status="active", version=1, service_type_ids=[uuid.UUID(svc["id"])], category_ids=[], scope="item", doc={"elements": []}))
        await s.flush()
        await call("approve_result", ANY, itemId=item_id)
        docs = await call("list_documents", orderId=oid)
        await call("resend_result_sms", ANY, documentId=docs["items"][0]["id"] if docs["items"] else str(uuid.uuid4()), dryRun=True)
        await call("revoke_result", ANY, itemId=item_id, reason="sinov tugadi")
        await call("order_scope_items", ANY, orderId=oid, templateId=receipt["id"])
        await call("approve_order", ANY, orderId=oid, templateId=receipt["id"])

        # ---- a second cheque: refund, cancel, delete
        o2 = (await call("create_order", patientId=pat["id"], serviceTypeIds=[extra["id"]]))["order"]
        await call("pay_order", orderId=o2["id"], amount=o2["total"], method="cash")
        await call("refund_payment", paymentId=(await call("get_order", orderId=o2["id"]))["payments"][0]["id"], reason="bemor voz kechdi")
        await call("cancel_order", ANY, orderId=o2["id"], reason="bemor voz kechdi")
        o3 = (await call("create_order", patientId=pat["id"], serviceTypeIds=[extra["id"]]))["order"]
        await call("delete_order", orderId=o3["id"])

        # ---- reports and messages
        for name in ("dashboard", "patients_report", "results_report", "sms_report", "services_report"):
            await call(name, **span)
        await call("breakdown", by="service", **span)
        await call("results_list", status="waiting", **span)
        assert (await call("territory_stats", groupBy="unit", unitId=ctx["facility"], **span)) is not None
        await call("outbox_counts")
        box = await call("list_outbox")
        assert all(m["text"] == tl.MASK for m in box["items"] if m["kind"] in ("otp", "portal_access"))
        await call("notifications")
        await call("get_sms_account")
        texts = await call("get_sms_templates")
        assert "templates" in texts
        await call("update_sms_templates", templates={"reminder": "Hurmatli {patient}, tahlil vaqti keldi."})
        await call("check_sms_text", ANY, text="Salom")
        await call("send_sms", ANY, to=["+998901234567"], text="Sinov xabari")

        # ---- a new facility with the sample's working set; a reset
        options = await call("quick_start_options", parentId=ctx["district"])
        assert options["parent"]["id"] == ctx["district"]
        new = await call("create_unit", parentId=ctx["district"], name=f"T-{tag} Laboratoriya", kind="laboratory", code=f"L{tag}")
        samples = await call("list_sample_facilities", facilityId=new["id"])
        assert any(x["id"] == ctx["facility"] for x in samples["items"])
        got = await call("copy_working_set", facilityId=new["id"], sampleFacilityId=ctx["facility"])
        assert got["copied"]["serviceTypes"] == 2 and got["copied"]["categories"] == 1 and got["receipt"]["status"] == "active"
        again = await call("copy_working_set", facilityId=new["id"], sampleFacilityId=ctx["facility"])
        assert again["copied"]["serviceTypes"] == 0 and again["alreadyThere"]["serviceTypes"] == 2
        started = await call("quick_start", parentId=ctx["district"], facility={"name": f"T-{tag} Shifoxona", "kind": "hospital", "code": f"H{tag}"}, staff=[{"role": "registrator", "fullName": "Kassa", "login": f"kassa-{tag.lower()}"}], sampleFacilityId=ctx["facility"])
        assert started["facility"]["name"] == f"T-{tag} Shifoxona" and started["employees"][0]["login"].startswith("kassa-")
        await call("delete_service_type", ANY, serviceTypeId=extra["id"])
        await call("delete_category", ANY, categoryId=cat["id"])
        preview = await call("reset_preview", unitId=new["id"])
        assert next(p["key"] for p in preview["parts"]) == "orders"
        assert await tl.REGISTRY["reset_unit"].confirm_word(d.ctx, {"unitId": new["id"]}) == f"L{tag}"
        wiped = await call("reset_unit", unitId=new["id"], parts=["templates"])
        assert wiped["parts"] == ["orders", "templates"]
        return d.used

    used = db(ctx["client"], day)
    # nothing of the registry is left untried, except the files (their own tests) and the question (the loop's)
    untried = set(tl.REGISTRY) - used - {"export_data", "export_table", "export_document", "export_chart", tl.ASK_TOOL}
    assert untried == set(), sorted(untried)


def test_the_registrar_cannot_reach_past_the_desk(ctx: dict) -> None:
    async def tries(s: Any) -> None:
        d = await desk(s, ctx["reg"], ctx["facility"])
        offered = {t.name for t in tl.tools_for(d.ctx.staff)}
        assert {"create_patient", "create_order", "pay_order", "list_patients"} <= offered
        assert not ({"update_unit", "create_employee", "reset_unit", "copy_working_set", "create_role", "update_service_type", "approve_result", "save_result_values"} & offered)
        # another unit named by id: refused by the same territory check the routes use
        with pytest.raises(AppError) as away:
            await tl.run_tool(tl.REGISTRY["list_patients"], d.ctx, {"unitId": ctx["region"]})
        assert away.value.code in ("forbidden", "not_found")

    db(ctx["client"], tries)


def test_every_way_back_works(ctx: dict) -> None:
    tag = ctx["tag"]

    async def rounds(s: Any) -> set[str]:
        d = await desk(s, ctx["super"], ctx["facility"])
        call, back = d.call, d.back
        get = lambda name, **a: tl.run_tool(tl.REGISTRY[name], d.ctx, a)  # noqa: E731

        # ---- units
        was = (await get("get_unit", unitId=ctx["facility"]))["phone"]
        await back("update_unit", unitId=ctx["facility"], phone="+998 62 555-55-55", shortName="Qisqa")
        assert (await get("get_unit", unitId=ctx["facility"]))["phone"] == was
        made, _ = await back("create_unit", parentId=ctx["district"], name=f"T-{tag} Xato birlik", kind="clinic", code=f"X{tag}")
        assert (await get("get_unit", unitId=made["id"]))["isActive"] is False  # switched off, not erased

        # ---- catalogue
        gone, _ = await back("create_category", name=f"Xato kategoriya {tag}")
        assert all(c["id"] != gone["id"] for c in (await get("list_categories"))["items"])
        cat = await call("create_category", name=f"Qaytish {tag}", code="Q1")
        await back("update_category", categoryId=cat["id"], name=f"Qaytish yangi {tag}", code="Q2")
        assert next(c for c in (await get("list_categories"))["items"] if c["id"] == cat["id"])["code"] == "Q1"
        schema = await call("create_schema", name=f"Qaytish sxema {tag}", fields=[{"key": "a", "label": "A", "type": "number"}])
        await back("update_schema", schemaId=schema["id"], fields=[{"key": "a", "label": "A", "type": "number"}, {"key": "b", "label": "B", "type": "text"}])
        assert [f["key"] for f in (await get("get_schema", schemaId=schema["id"]))["fields"]] == ["a"]
        await call("publish_schema", schemaId=schema["id"])
        gone, _ = await back("create_service_type", categoryId=cat["id"], name=f"Xato xizmat {tag}", price=1)
        svc = await call("create_service_type", categoryId=cat["id"], name=f"Qaytish xizmat {tag}", price=20_000, schemaId=schema["id"])
        cheap = await call("create_service_type", categoryId=cat["id"], name=f"Qaytish arzon {tag}", price=5_000)
        await back("update_service_type", serviceTypeId=svc["id"], price=99_000, turnaroundDays=5, unitPrices={ctx["facility"]: 1})
        after = await get("get_service_type", serviceTypeId=svc["id"])
        assert after["price"] == 20_000 and not after["unitPrices"]

        # ---- templates
        gone, _ = await back("create_default_receipt", name=f"Xato chek {tag}")
        tpl = await call("create_default_receipt", name=f"Qaytish chek {tag}")
        await back("duplicate_template", templateId=tpl["id"], name=f"Xato nusxa {tag}")
        await back("update_template", templateId=tpl["id"], name=f"Boshqa nom {tag}", description="x")
        await back("set_template_status", templateId=tpl["id"], status="active")
        kept = await get("get_template", templateId=tpl["id"])
        assert kept["name"] == f"Qaytish chek {tag}" and kept["status"] == "draft"

        # ---- staff
        gone, _ = await back("create_role", name=f"Xato rol {tag}", permissions=[])
        role = await call("create_role", name=f"Qaytish rol {tag}", permissions=["reception.patient.read"])
        await back("update_role", roleId=role["id"], name=f"Boshqa rol {tag}", permissions=["reception.patient.read", "reception.order.create"])
        assert next(r for r in (await get("list_roles"))["items"] if r["id"] == role["id"])["permissions"] == ["reception.patient.read"]
        off, _ = await back("create_employee", fullName=f"Xato xodim {tag}", login=f"xato-{tag.lower()}", roleKey="laborant")
        assert (await get("get_employee", employeeId=off["id"]))["status"] == "inactive"
        emp = await call("create_employee", fullName=f"Qaytish xodim {tag}", login=f"qayt-{tag.lower()}", roleKey="laborant")
        await back("update_employee", employeeId=emp["id"], fullName="Boshqa ism", roleKey="vrach", phone="+998900000001")
        same = await get("get_employee", employeeId=emp["id"])
        assert same["fullName"] == f"Qaytish xodim {tag}" and same["roleId"] == emp["roleId"]
        await back("set_employee_overrides", employeeId=emp["id"], allow=["reports.operations.read"], deny=[])

        # ---- reception
        pat = await call("create_patient", fullName=f"Qaytish Bemor {tag}", phone=phone(), gender="female", birthDate="1985-01-02")
        await back("update_patient", patientId=pat["id"], fullName="Boshqa Bemor", workplace="Zavod", tags=["x"])
        assert (await get("get_patient", patientId=pat["id"]))["fullName"] == f"Qaytish Bemor {tag}"
        gone, _ = await back("create_order", patientId=pat["id"], serviceTypeIds=[cheap["id"]])
        assert "refused" in await call("get_order", ("not_found",), orderId=gone["order"]["id"])
        order = (await call("create_order", patientId=pat["id"], serviceTypeIds=[svc["id"]]))["order"]
        await back("add_order_items", orderId=order["id"], serviceTypeIds=[cheap["id"]])
        bundle = await get("get_order", orderId=order["id"])
        assert [i["serviceName"] for i in bundle["items"] if i["status"] != "cancelled"] == [f"Qaytish xizmat {tag}"]
        _, lines = await back("pay_order", orderId=order["id"], amount=bundle["order"]["total"], method="cash")
        assert lines and all(p.get("refundedAt") for p in (await get("get_order", orderId=order["id"]))["payments"])
        await call("pay_order", orderId=order["id"], amount=bundle["order"]["total"], method="card")

        # ---- the laboratory and the approval
        item = next(i["id"] for i in bundle["items"] if i["serviceName"] == f"Qaytish xizmat {tag}")
        await call("save_result_values", itemId=item, values={"a": 1})
        await back("save_result_values", itemId=item, values={"a": 2}, labNote="xato")
        assert (await get("get_order_item", itemId=item))["values"] == {"a": 1}
        await back("submit_result", itemId=item)
        assert (await get("get_order_item", itemId=item))["status"] != "submitted"
        await call("submit_result", itemId=item)
        s.add(ResultTemplate(owner_unit_id=uuid.UUID(ctx["facility"]), name=f"Qaytish natija {tag}", status="active", version=1, service_type_ids=[uuid.UUID(svc["id"])], category_ids=[], scope="item", doc={"elements": []}))
        await s.flush()
        d.used.add("approve_result")
        kept = await ud.plan(d.ctx, "approve_result", {"itemId": item}, None, {})
        assert kept and kept["steps"][0]["tool"] == "revoke_result" and kept["steps"][0]["fixed"]["itemId"] == item
        return d.used

    tried = db(ctx["client"], rounds)
    assert set(ud.RECIPES) - tried == set(), sorted(set(ud.RECIPES) - tried)
