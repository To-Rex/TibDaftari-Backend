"""The assistant's tools for the daily work: the reception (patients, cheques, payments), the laboratory (worklist,
result values), the approval of results and their documents, the reports and the message outbox.

Same rules as `tools`: a tool is offered only to a holder of the key its HTTP route asks for, the service checks
the territory with the employee's own principal, a write waits for their yes. One-time secrets have no tool here:
a patient's portal password is issued on the patient card, never through the chat."""

from __future__ import annotations

from typing import Any

from app.core.exceptions import ValidationError
from app.modules.assistant.tools import MASK, ToolContext, _dump, _scope, _str, _uuid, tool
from app.modules.messaging import service as messaging_svc
from app.modules.messaging.schemas import OutboxQuery
from app.modules.orders import service as orders_svc
from app.modules.orders.schemas import (
    AddItemsIn,
    ApproveItemIn,
    ApproveOrderIn,
    CreateOrderIn,
    OrderListQuery,
    PayIn,
    PaymentMethodIn,
    ResultSmsIn,
    SaveValuesIn,
    WorklistQuery,
)
from app.modules.patients import service as patients_svc
from app.modules.patients.schemas import PatientListQuery, PatientPatchIn, PatientUpsertIn
from app.modules.platform import service as platform_svc
from app.modules.platform.schemas import RegionStatsQuery
from app.modules.reports import service as reports_svc
from app.modules.reports.schemas import RangeQuery, ResultListQuery

LATE_EDIT = orders_svc.LATE_EDIT
ORDER_READ = ("reception.order.create", "reception.payment.create", "reception.patient.read", "lab.worklist.read", "confirm.result.read", "confirm.result.view", "reports.operations.read")
ITEM_READ = ("lab.worklist.read", "lab.result.write", "confirm.result.read", "confirm.result.view", "reception.order.create")
DOC_READ = ("confirm.result.read", "confirm.result.view", "reception.patient.read")
REPORT_READ = ("reports.operations.read", "reports.finance.read")
PLATFORM_MANAGE = "platform.unit.manage"
PATIENT_KEYS = ("id", "fullName", "phone", "phoneExtra", "birthDate", "gender", "address", "workplace", "registeredUnitId", "registeredUnitName", "discountPercent", "tags", "relation", "stats", "createdAt")
#: outbox kinds whose text is itself a secret (a one-time code, a portal password)
SECRET_MESSAGE_KINDS = frozenset({"otp", "portal_access"})
DAY = {"type": "string", "description": "YYYY-MM-DD"}


def _page(page: Any, items: list[Any]) -> dict[str, Any]:
    return {"total": page.total, "page": page.page, "totalPages": page.total_pages, "items": items}


def _rest(args: dict[str, Any], *skip: str) -> dict[str, Any]:
    return {k: v for k, v in args.items() if k not in skip}


def _reason(args: dict[str, Any]) -> str:
    reason = str(args.get("reason") or "").strip()
    if not reason:
        raise ValidationError("Sababini yozing")
    return reason


# ============================================================================= patients


@tool("list_patients", "Bemorlar ro‘yxati", "Patients registered at / seen in a unit's subtree: search by name, phone or passport, filter by tag. Paged, 20 per page.",
      params={"unitId": {"type": "string"}, "search": {"type": "string"}, "tag": {"type": "string"}, "page": {"type": "integer", "minimum": 1}}, perms=("reception.patient.read",), group="reception")
async def list_patients(ctx: ToolContext, args: dict[str, Any]) -> Any:
    scope = await _scope(ctx, args.get("unitId"))
    page = await patients_svc.list_patients(ctx.session, scope, PatientListQuery(search=args.get("search"), tag=args.get("tag"), page=int(args.get("page") or 1), page_size=20), ctx.staff)
    return _page(page, [_dump(p, PATIENT_KEYS) for p in page.items])


@tool("patient_family", "Bemor oilasi", "Everyone on the patient's phone number and the relatives linked to them.", params={"patientId": {"type": "string", "req": True}}, perms=("reception.patient.read",), group="reception")
async def patient_family(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return _dump(await patients_svc.patient_family(ctx.session, _uuid(args["patientId"], "patientId"), ctx.staff))


@tool("create_patient", "Bemorni ro‘yxatga olish", "Register a patient at a facility (default: the working unit; otherwise unitId or registeredUnitId): full name and phone are required; birth date YYYY-MM-DD, gender, passport, address {regionId, districtId, street} (geo ids from list_geo), a relative on the same phone (relation + relatedPatientId). Search first (search_patients) — do not register a person twice.",
      model=PatientUpsertIn, params={"unitId": {"type": "string"}}, kind="write", perms=("reception.patient.write",), summary_keys=("fullName", "phone", "unitId"), group="reception")
async def create_patient(ctx: ToolContext, args: dict[str, Any]) -> Any:
    scope = await _scope(ctx, args.get("unitId"))
    return _dump(await patients_svc.create_patient(ctx.session, scope, ctx.staff, PatientUpsertIn.model_validate(_rest(args, "unitId")), ctx.meta), PATIENT_KEYS)


@tool("update_patient", "Bemor ma’lumotlarini tahrirlash", "Change a patient's card — only the given fields.", model=PatientPatchIn, params={"patientId": {"type": "string", "req": True}},
      kind="write", perms=("reception.patient.write",), summary_keys=("patientId", "fullName", "phone"), group="reception")
async def update_patient(ctx: ToolContext, args: dict[str, Any]) -> Any:
    fields = _rest(args, "patientId")
    if not fields:
        raise ValidationError("O‘zgartiriladigan maydon yo‘q")
    return _dump(await patients_svc.update_patient(ctx.session, _uuid(args["patientId"], "patientId"), ctx.staff, PatientPatchIn.model_validate(fields), ctx.meta), PATIENT_KEYS)


# ============================================================================= cheques and payments


@tool("orders_summary", "Cheklar yig‘indisi", "The cheques of a unit at a glance for the same filters as list_orders: how many, totals, paid, debt.",
      params={"unitId": {"type": "string"}, "search": {"type": "string"}, "status": {"type": "string"}, "payment": {"type": "string"}, "dateFrom": DAY, "dateTo": DAY, "patientId": {"type": "string"}}, perms=ORDER_READ, group="reception")
async def orders_summary(ctx: ToolContext, args: dict[str, Any]) -> Any:
    scope = await _scope(ctx, args.get("unitId"))
    q = OrderListQuery(search=args.get("search"), status=args.get("status"), payment=args.get("payment"), date_from=args.get("dateFrom"), date_to=args.get("dateTo"), patient_id=_str(args.get("patientId")))
    return _dump(await orders_svc.orders_summary(ctx.session, scope, q))


@tool("create_order", "Chek ochish", "Open a cheque for a patient at a facility (default: the working unit when it is a facility, else unitId) with the chosen services (serviceTypeIds from list_service_types of that facility). Payment is a separate step (pay_order).",
      model=CreateOrderIn, kind="write", perms=("reception.order.create",), summary_keys=("patientId", "serviceTypeIds", "unitId"), group="reception")
async def create_order(ctx: ToolContext, args: dict[str, Any]) -> Any:
    scope = await _scope(ctx, args.get("unitId"))
    body = CreateOrderIn.model_validate(_rest(args, "unitId") if scope.is_facility else args)
    return _dump(await orders_svc.create_order(ctx.session, scope, ctx.staff, body, ctx.meta))


@tool("add_order_items", "Chekka xizmat qo‘shish", "Add services to an open cheque.", model=AddItemsIn, params={"orderId": {"type": "string", "req": True}}, kind="write", perms=("reception.order.create",), summary_keys=("orderId", "serviceTypeIds"), group="reception")
async def add_order_items(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return _dump(await orders_svc.add_items(ctx.session, _uuid(args["orderId"], "orderId"), ctx.staff, AddItemsIn.model_validate(_rest(args, "orderId")), ctx.meta))


@tool("remove_order_item", "Chekdan xizmatni olib tashlash", "Remove a service added by mistake while it is still pending; on a paid cheque the difference is given back.",
      params={"orderId": {"type": "string", "req": True}, "itemId": {"type": "string", "req": True, "description": "from get_order"}, "reason": {"type": "string"}}, kind="write", perms=("reception.order.create", LATE_EDIT), summary_keys=("orderId", "itemId", "reason"), group="reception", danger=True)
async def remove_order_item(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return _dump(await orders_svc.remove_item(ctx.session, _uuid(args["orderId"], "orderId"), _uuid(args["itemId"], "itemId"), ctx.staff, ctx.meta, _str(args.get("reason"))))


@tool("pay_order", "To‘lovni qabul qilish", "Record a payment on a cheque: amount in UZS and the method (cash / card / transfer / insurance), or parts to split it over several methods. sendSms sends the receipt SMS.",
      model=PayIn, params={"orderId": {"type": "string", "req": True}}, kind="write", perms=("reception.payment.create",), summary_keys=("orderId", "amount", "method"), group="reception")
async def pay_order(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return _dump(await orders_svc.pay(ctx.session, _uuid(args["orderId"], "orderId"), ctx.staff, PayIn.model_validate(_rest(args, "orderId")), ctx.meta))


@tool("change_payment_method", "To‘lov usulini tuzatish", "Correct the method of a recorded payment or split it over several — the amount stays. paymentId from get_order.",
      model=PaymentMethodIn, params={"paymentId": {"type": "string", "req": True}}, kind="write", perms=("reception.payment.create", LATE_EDIT), summary_keys=("paymentId", "parts", "reason"), group="reception")
async def change_payment_method(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return _dump(await orders_svc.change_payment_method(ctx.session, _uuid(args["paymentId"], "paymentId"), ctx.staff, PaymentMethodIn.model_validate(_rest(args, "paymentId")), ctx.meta))


@tool("refund_payment", "To‘lovni qaytarish", "Refund a recorded payment (paymentId from get_order) with a reason.", params={"paymentId": {"type": "string", "req": True}, "reason": {"type": "string", "req": True}},
      kind="write", perms=("reception.payment.refund", LATE_EDIT), summary_keys=("paymentId", "reason"), group="reception", danger=True)
async def refund_payment(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return _dump(await orders_svc.refund(ctx.session, _uuid(args["paymentId"], "paymentId"), ctx.staff, _reason(args), ctx.meta))


@tool("cancel_order", "Chekni bekor qilish", "Cancel an unpaid cheque with a reason.", params={"orderId": {"type": "string", "req": True}, "reason": {"type": "string", "req": True}},
      kind="write", perms=("reception.order.cancel",), summary_keys=("orderId", "reason"), group="reception", danger=True)
async def cancel_order(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return _dump(await orders_svc.cancel_order(ctx.session, _uuid(args["orderId"], "orderId"), ctx.staff, _reason(args), ctx.meta))


@tool("delete_order", "Chekni o‘chirish", "Delete a cheque on which no laboratory work has started (paid or not).", params={"orderId": {"type": "string", "req": True}},
      kind="write", perms=("reception.order.delete", LATE_EDIT), summary_keys=("orderId",), group="reception", danger=True)
async def delete_order(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return _dump(await orders_svc.delete_order(ctx.session, _uuid(args["orderId"], "orderId"), ctx.staff, ctx.meta))


# ============================================================================= the laboratory


def _worklist_query(args: dict[str, Any]) -> WorklistQuery:
    status = args.get("status")
    given = {"status": [status] if isinstance(status, str) else status} if status else {}
    return WorklistQuery.model_validate({**_rest(args, "unitId", "status"), **given, "pageSize": 20})


@tool("worklist", "Laboratoriya ish ro‘yxati", "The laboratory worklist of a unit: paid services waiting for results, by status (pending, entered, submitted, approved, rejected), search by patient / cheque, dates. Paged, 20 per page. Each row has the itemId the result tools need.",
      model=WorklistQuery, only=("search", "status", "categoryIds", "dateFrom", "dateTo", "page"), params={"unitId": {"type": "string"}}, perms=("lab.worklist.read", "confirm.result.view"), group="lab")
async def worklist(ctx: ToolContext, args: dict[str, Any]) -> Any:
    scope = await _scope(ctx, args.get("unitId"))
    q = _worklist_query(args)
    # approved results only may be listed with the "view" key alone — everything else needs the worklist key
    if not (q.status and set(q.status) == {"approved"} and ctx.staff.has("confirm.result.view")):
        ctx.staff.require("lab.worklist.read")
    page = await orders_svc.worklist(ctx.session, scope, q, ctx.staff)
    return _page(page, [_dump(i) for i in page.items])


@tool("worklist_counts", "Ish ro‘yxati hisobi", "How many services wait in the laboratory per status (same filters as worklist).",
      model=WorklistQuery, only=("search", "categoryIds", "dateFrom", "dateTo"), params={"unitId": {"type": "string"}}, perms=("lab.worklist.read",), group="lab")
async def worklist_counts(ctx: ToolContext, args: dict[str, Any]) -> Any:
    scope = await _scope(ctx, args.get("unitId"))
    return _dump(await orders_svc.worklist_counts(ctx.session, scope, _worklist_query(args), ctx.staff))


@tool("get_order_item", "Xizmat natijasi", "One ordered service with its result schema (the fields to fill, their keys and types), the values entered so far and its status.", params={"itemId": {"type": "string", "req": True}}, perms=ITEM_READ, group="lab")
async def get_order_item(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return _dump(await orders_svc.get_item_dto(ctx.session, _uuid(args["itemId"], "itemId"), ctx.staff))


@tool("save_result_values", "Natijani kiritish", "Save the result values of an ordered service: values = {fieldKey: value} for EVERY field of its schema (wholesale — read get_order_item first and keep what is already there), optional labNote. Enter only numbers and texts the employee gave; never invent a result.",
      model=SaveValuesIn, params={"itemId": {"type": "string", "req": True}}, kind="write", perms=("lab.result.write",), summary_keys=("itemId",), group="lab")
async def save_result_values(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return _dump(await orders_svc.save_values(ctx.session, _uuid(args["itemId"], "itemId"), ctx.staff, SaveValuesIn.model_validate(_rest(args, "itemId")), ctx.meta))


@tool("submit_result", "Natijani tasdiqqa yuborish", "Send an entered result to approval — or take it back when it is already submitted (a toggle).", params={"itemId": {"type": "string", "req": True}}, kind="write", perms=("lab.result.submit",), summary_keys=("itemId",), group="lab")
async def submit_result(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return _dump(await orders_svc.submit_item(ctx.session, _uuid(args["itemId"], "itemId"), ctx.staff, ctx.meta))


# ============================================================================= approval and documents


@tool("approve_result", "Natijani tasdiqlash", "Approve a submitted result: it becomes a document (with the service's default template, or templateId) and the patient is notified.",
      model=ApproveItemIn, params={"itemId": {"type": "string", "req": True}}, kind="write", perms=("confirm.result.approve",), summary_keys=("itemId", "templateId"), group="results")
async def approve_result(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return _dump(await orders_svc.approve_item(ctx.session, _uuid(args["itemId"], "itemId"), ctx.staff, ApproveItemIn.model_validate(_rest(args, "itemId")), ctx.meta))


@tool("approve_order", "Chek natijalarini bitta hujjat qilib tasdiqlash", "Approve several submitted results of one cheque as ONE document with an order-scope template (templateId; itemIds optional — see order_scope_items).",
      model=ApproveOrderIn, params={"orderId": {"type": "string", "req": True}}, kind="write", perms=("confirm.result.approve",), summary_keys=("orderId", "templateId"), group="results")
async def approve_order(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return _dump(await orders_svc.approve_order(ctx.session, _uuid(args["orderId"], "orderId"), ctx.staff, ApproveOrderIn.model_validate(_rest(args, "orderId")), ctx.meta))


@tool("order_scope_items", "Bitta hujjatga kiradigan natijalar", "Which results of a cheque an order-scope template would cover.", params={"orderId": {"type": "string", "req": True}, "templateId": {"type": "string", "req": True}}, perms=("confirm.result.read", "confirm.result.approve"), group="results")
async def order_scope_items(ctx: ToolContext, args: dict[str, Any]) -> Any:
    rows = await orders_svc.order_scope_items(ctx.session, _uuid(args["orderId"], "orderId"), _uuid(args["templateId"], "templateId"), ctx.staff)
    return {"items": [_dump(r) for r in rows]}


@tool("reject_result", "Natijani laboratoriyaga qaytarish", "Send a submitted result back to the laboratory with a reason.", params={"itemId": {"type": "string", "req": True}, "reason": {"type": "string", "req": True}},
      kind="write", perms=("confirm.result.approve",), summary_keys=("itemId", "reason"), group="results")
async def reject_result(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return _dump(await orders_svc.reject_item(ctx.session, _uuid(args["itemId"], "itemId"), ctx.staff, _reason(args), ctx.meta))


@tool("revoke_result", "Tasdiqlangan natijani qaytarib olish", "Take back an APPROVED result: its document is withdrawn (the patient's link stops working) and the service returns to the laboratory.", params={"itemId": {"type": "string", "req": True}, "reason": {"type": "string", "req": True}},
      kind="write", perms=("confirm.result.approve",), summary_keys=("itemId", "reason"), group="results", danger=True)
async def revoke_result(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return _dump(await orders_svc.revoke_item(ctx.session, _uuid(args["itemId"], "itemId"), ctx.staff, _reason(args), ctx.meta))


@tool("list_documents", "Natija hujjatlari", "Result documents under a unit by cheque or patient: number, status, when approved, printed / received.", params={"unitId": {"type": "string"}, "orderId": {"type": "string"}, "patientId": {"type": "string"}}, perms=DOC_READ, group="results")
async def list_documents(ctx: ToolContext, args: dict[str, Any]) -> Any:
    if not args.get("orderId") and not args.get("patientId"):
        raise ValidationError("orderId yoki patientId kerak")
    scope = await _scope(ctx, args.get("unitId"))
    rows = await orders_svc.list_documents(ctx.session, scope, order_id=_str(args.get("orderId")), patient_id=_str(args.get("patientId")), unit_id=None)
    return {"total": len(rows), "items": [_dump(r) for r in rows[:50]]}


@tool("resend_result_sms", "Natija SMSini qayta yuborish", "Send the result-ready SMS of a document again (to the patient's number, or `to`). dryRun only builds the text.",
      model=ResultSmsIn, params={"documentId": {"type": "string", "req": True}}, kind="write", perms=("confirm.result.approve", "messaging.send", "confirm.result.resend"), summary_keys=("documentId", "to"), group="results")
async def resend_result_sms(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return _dump(await orders_svc.resend_result_sms(ctx.session, _uuid(args["documentId"], "documentId"), ctx.staff, ResultSmsIn.model_validate(_rest(args, "documentId")), ctx.meta))


# ============================================================================= reports


def _range(args: dict[str, Any]) -> RangeQuery:
    return RangeQuery(date_from=str(args["dateFrom"]), date_to=str(args["dateTo"]))


RANGE_PARAMS = {"unitId": {"type": "string"}, "dateFrom": {**DAY, "req": True}, "dateTo": {**DAY, "req": True}}


@tool("patients_report", "Bemorlar hisoboti", "Patients of a period: new / returning, age and gender, districts, the most frequent.", params=RANGE_PARAMS, perms=REPORT_READ, group="reports")
async def patients_report(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return _dump(await reports_svc.patients_report(ctx.session, await _scope(ctx, args.get("unitId")), _range(args), ctx.staff))


@tool("results_report", "Natijalar hisoboti", "Results of a period's cheques: ready, overdue, turnaround, received by the patient.", params=RANGE_PARAMS, perms=REPORT_READ, group="reports")
async def results_report(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return _dump(await reports_svc.results_report(ctx.session, await _scope(ctx, args.get("unitId")), _range(args), ctx.staff))


@tool("results_list", "Olinmagan / kutilayotgan natijalar", "Results of a period row by row: not_received, received, or cheques still waiting. Paged, 20 per page.",
      model=ResultListQuery, only=("dateFrom", "dateTo", "status", "search", "page"), params={"unitId": {"type": "string"}}, perms=REPORT_READ, group="reports")
async def results_list(ctx: ToolContext, args: dict[str, Any]) -> Any:
    scope = await _scope(ctx, args.get("unitId"))
    page = await reports_svc.results_list(ctx.session, scope, ResultListQuery.model_validate({**_rest(args, "unitId"), "pageSize": 20}), ctx.staff)
    return _page(page, [_dump(r) for r in page.items])


@tool("sms_report", "SMS hisoboti", "SMS of a period by territory, day, kind and provider: counts, deliveries, failures, cost.", params=RANGE_PARAMS, perms=REPORT_READ, group="reports")
async def sms_report(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return _dump(await reports_svc.sms_report(ctx.session, await _scope(ctx, args.get("unitId")), _range(args), ctx.staff))


@tool("services_report", "Xizmatlar hisoboti", "Service usage of a period against the previous one, and active services nobody ordered.", params=RANGE_PARAMS, perms=REPORT_READ, group="reports")
async def services_report(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return _dump(await reports_svc.services_report(ctx.session, await _scope(ctx, args.get("unitId")), _range(args), ctx.staff))


@tool("territory_stats", "Hududlar bo‘yicha faollik", "Activity (cheques, revenue, patients, results) grouped by company, country, region, district or facility for a period — numbers only. Without the platform right it covers the working unit (or unitId inside the territory).",
      model=RegionStatsQuery, perms=(*REPORT_READ, PLATFORM_MANAGE), group="reports")
async def territory_stats(ctx: ToolContext, args: dict[str, Any]) -> Any:
    q = dict(args)
    if not ctx.staff.has(PLATFORM_MANAGE):
        ctx.staff.require(*REPORT_READ)
        q["unitId"] = str((await _scope(ctx, args.get("unitId"))).id)
    return _dump(await platform_svc.region_stats(ctx.session, RegionStatsQuery.model_validate(q), ctx.staff))


# ============================================================================= messages


@tool("list_outbox", "Yuborilgan xabarlar", "SMS / Telegram messages sent under a unit, newest first: filter by status and kind, search by number. Paged, 20 per page. One-time codes and portal passwords are never shown.",
      model=OutboxQuery, only=("search", "status", "kind", "page"), params={"unitId": {"type": "string"}}, perms=("messaging.send", "reports.operations.read"), group="messaging")
async def list_outbox(ctx: ToolContext, args: dict[str, Any]) -> Any:
    scope = await _scope(ctx, args.get("unitId"))
    page = await messaging_svc.list_outbox(ctx.session, scope, OutboxQuery.model_validate({**_rest(args, "unitId"), "pageSize": 20}), ctx.staff)
    items = []
    for m in page.items:
        row = _dump(m, ("id", "unitName", "channel", "kind", "to", "text", "status", "sentAt", "attempts", "error", "parts", "cost", "providerStatus", "createdAt"))
        if row.get("kind") in SECRET_MESSAGE_KINDS:
            row["text"] = MASK
        items.append(row)
    return _page(page, items)


@tool("notifications", "Bildirishnomalarim", "The current employee's own in-app notifications (newest 50).", group="messaging")
async def notifications(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return {"items": [_dump(n) for n in await messaging_svc.list_notifications(ctx.session, ctx.staff)]}
