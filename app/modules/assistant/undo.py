"""Taking a confirmed write back. Nothing generic and nothing below the services: every tool that can be undone
has its own recipe here, and the recipe is made of the assistant's own tools — so the way back runs with the
employee's principal, through the same permission, territory and business checks as the way there, and is audited
the same way.

A recipe remembers what it needs BEFORE the write runs (`before` — the values about to be replaced, the rows that
exist) and turns it, with the write's result, into a plan kept next to the action: the steps to run and, for an
update, what the write left behind (`expect`). When the employee asks for the undo, a field is put back only if it
still holds what the assistant wrote — something changed since by a person is never overwritten.

What cannot honestly be taken back has no recipe: a sent SMS, a refund, a deletion, a reset, a quick start, a
schema that was published."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.core.exceptions import AppError, ConflictError
from app.modules.assistant import tools as tl
from app.modules.assistant.describe import LABELS
from app.modules.assistant.tools import ToolContext
from app.modules.catalog import service as catalog_svc
from app.modules.orders import service as orders_svc
from app.modules.patients import service as patients_svc
from app.modules.staff import service as staff_svc
from app.modules.templates import service as tpl_svc
from app.modules.units import service as units_svc

Before = Callable[[ToolContext, dict[str, Any]], Awaitable[Any]]
Plan = Callable[[ToolContext, dict[str, Any], Any, Any], Awaitable[list[dict[str, Any]] | None]]
REASON = "Yordamchi orqali kiritilgan amal orqaga qaytarildi"
MSG_CONFLICT = "Bu ma’lumot keyin o‘zgartirilgan — avtomatik orqaga qaytarib bo‘lmaydi"
MSG_NO_RIGHT = "Orqaga qaytarish uchun ruxsat yetarli emas"


@dataclass(frozen=True)
class Recipe:
    #: one line for the employee: what undoing does
    label: str
    plan: Plan
    before: Before | None = None


RECIPES: dict[str, Recipe] = {}


def _wire(dto: Any) -> dict[str, Any]:
    return dto.model_dump(by_alias=True, mode="json") if hasattr(dto, "model_dump") else dict(dto)


# ----------------------------------------------------------------------------- how the current state is read


async def _read_unit(ctx: ToolContext, eid: str) -> dict[str, Any]:
    return _wire(await units_svc.get_unit_dto(ctx.session, tl._uuid(eid, "unitId"), ctx.staff))


async def _read_employee(ctx: ToolContext, eid: str) -> dict[str, Any]:
    return _wire(await staff_svc.get_employee_dto(ctx.session, tl._uuid(eid, "employeeId"), ctx.staff))


async def _read_patient(ctx: ToolContext, eid: str) -> dict[str, Any]:
    row = _wire(await patients_svc.get_patient_dto(ctx.session, tl._uuid(eid, "patientId"), ctx.staff))
    if isinstance(row.get("address"), dict):  # the card's address carries names too — the update takes the three keys
        row["address"] = {k: row["address"].get(k) for k in ("regionId", "districtId", "street")}
    return row


async def _read_service(ctx: ToolContext, eid: str) -> dict[str, Any]:
    return _wire(await catalog_svc.get_service_type_dto(ctx.session, tl._uuid(eid, "serviceTypeId"), ctx.staff))


async def _read_category(ctx: ToolContext, eid: str) -> dict[str, Any]:
    for c in await catalog_svc.list_categories(ctx.session, ctx.scope):
        if str(c.id) == eid:
            return _wire(c)
    raise LookupError(eid)


async def _read_role(ctx: ToolContext, eid: str) -> dict[str, Any]:
    for r in await staff_svc.list_roles(ctx.session, ctx.scope):
        if str(r.id) == eid:
            return _wire(r)
    raise LookupError(eid)


async def _read_template(ctx: ToolContext, eid: str) -> dict[str, Any]:
    return _wire(await tpl_svc.get_template_dto(ctx.session, tl._uuid(eid, "templateId"), ctx.staff))


async def _read_schema(ctx: ToolContext, eid: str) -> dict[str, Any]:
    return _wire(await catalog_svc.get_schema_dto(ctx.session, tl._uuid(eid, "schemaId"), ctx.staff))


async def _read_item(ctx: ToolContext, eid: str) -> dict[str, Any]:
    return _wire(await orders_svc.get_item_dto(ctx.session, tl._uuid(eid, "itemId"), ctx.staff))


READERS: dict[str, Callable[[ToolContext, str], Awaitable[dict[str, Any]]]] = {
    "unit": _read_unit, "employee": _read_employee, "patient": _read_patient, "service": _read_service, "category": _read_category,
    "role": _read_role, "template": _read_template, "schema": _read_schema, "item": _read_item,
}


def _same(a: Any, b: Any) -> bool:
    """Equal for the purpose of "is it still what was written": empty is empty, plain lists regardless of order."""
    if isinstance(a, list) and isinstance(b, list) and all(not isinstance(x, (dict, list)) for x in (*a, *b)):
        return sorted(map(str, a)) == sorted(map(str, b))
    if a in (None, "") and b in (None, ""):
        return True
    return a == b


# ----------------------------------------------------------------------------- recipes


def restores(tool: str, reader: str, id_key: str, fields: tuple[str, ...], label: str, *, alias: dict[str, str] | None = None) -> None:
    """An update is undone by writing the old values of the fields it changed back with the same tool. Fields the
    recipe cannot read back (`fields`) make the whole write non-undoable rather than half-undoable."""
    names = alias or {}

    async def before(ctx: ToolContext, args: dict[str, Any]) -> Any:
        touched = {names.get(k, k) for k in args if k != id_key}
        if not touched or not touched <= set(fields):
            return None
        row = await READERS[reader](ctx, str(args[id_key]))
        return {k: row.get(k) for k in touched}

    async def plan(ctx: ToolContext, args: dict[str, Any], snap: Any, result: Any) -> list[dict[str, Any]] | None:
        if not isinstance(snap, dict):
            return None
        eid = str(args[id_key])
        row = await READERS[reader](ctx, eid)
        changed = {k: old for k, old in snap.items() if not _same(old, row.get(k))}
        if not changed:
            return None
        return [{"tool": tool, "fixed": {id_key: eid}, "restore": changed, "expect": {k: row.get(k) for k in changed}, "reader": reader, "id": eid}]

    RECIPES[tool] = Recipe(label=label, plan=plan, before=before)


def removes(tool: str, inverse: str, id_key: str, label: str, *, pick: Callable[[Any], Any] | None = None, extra: dict[str, Any] | None = None) -> None:
    """Something created is undone by the tool that removes (or switches off) exactly that thing."""

    async def plan(ctx: ToolContext, args: dict[str, Any], snap: Any, result: Any) -> list[dict[str, Any]] | None:
        made = pick(result) if pick else (result or {}).get("id") if isinstance(result, dict) else None
        return [{"tool": inverse, "fixed": {id_key: str(made), **(extra or {})}}] if made else None

    RECIPES[tool] = Recipe(label=label, plan=plan)


restores("update_unit", "unit", "unitId", ("name", "shortName", "code", "kind", "legalName", "phone", "email", "address", "timezone", "isActive", "sortOrder"), "O‘zgargan maydonlar avvalgi qiymatiga qaytariladi")
restores("update_employee", "employee", "employeeId", ("fullName", "login", "phone", "email", "roleId", "unitId", "unitIds", "categoryIds", "status"), "O‘zgargan maydonlar avvalgi qiymatiga qaytariladi", alias={"roleKey": "roleId"})
restores("update_patient", "patient", "patientId", ("fullName", "phone", "phoneExtra", "gender", "birthDate", "passportNumber", "pinfl", "address", "workplace", "discountPercent", "contractNumber", "note", "tags"), "O‘zgargan maydonlar avvalgi qiymatiga qaytariladi")
restores("update_service_type", "service", "serviceTypeId", ("categoryId", "name", "code", "description", "price", "unitPrices", "disabledUnitIds", "turnaroundDays", "order", "isActive", "schemaId", "documentScope", "defaultTemplateId"), "O‘zgargan maydonlar avvalgi qiymatiga qaytariladi")
restores("update_category", "category", "categoryId", ("parentId", "name", "code", "icon", "color", "order", "isActive", "phone", "workflow"), "O‘zgargan maydonlar avvalgi qiymatiga qaytariladi")
restores("update_role", "role", "roleId", ("name", "key", "description", "permissions"), "Rol nomi va ruxsatlari avvalgi holiga qaytariladi")
restores("update_template", "template", "templateId", ("name", "description", "serviceTypeIds", "categoryIds", "unitIds", "scope", "language"), "O‘zgargan maydonlar avvalgi qiymatiga qaytariladi")
restores("update_schema", "schema", "schemaId", ("name", "description", "fields"), "Sxema avvalgi nomi va maydonlariga qaytariladi")
restores("set_template_status", "template", "templateId", ("status",), "Andoza avvalgi holatiga qaytariladi")
restores("save_result_values", "item", "itemId", ("values", "labNote"), "Natija qiymatlari avvalgi holiga qaytariladi")

removes("create_category", "delete_category", "categoryId", "Yaratilgan kategoriya o‘chiriladi")
removes("create_service_type", "delete_service_type", "serviceTypeId", "Yaratilgan xizmat o‘chiriladi")
removes("create_role", "delete_role", "roleId", "Yaratilgan rol o‘chiriladi")
removes("duplicate_template", "delete_template", "templateId", "Yaratilgan nusxa o‘chiriladi")
removes("create_default_receipt", "delete_template", "templateId", "Yaratilgan chek andozasi o‘chiriladi")
removes("create_order", "delete_order", "orderId", "Ochilgan chek o‘chiriladi", pick=lambda r: ((r or {}).get("order") or {}).get("id"))
removes("create_employee", "update_employee", "employeeId", "Yaratilgan xodim nofaol qilinadi (hisob o‘chirilmaydi)", extra={"status": "inactive"})
removes("create_unit", "update_unit", "unitId", "Yaratilgan birlik nofaol qilinadi (daraxtdan o‘chirilmaydi)", extra={"isActive": False})


async def _approve_plan(ctx: ToolContext, args: dict[str, Any], snap: Any, result: Any) -> list[dict[str, Any]] | None:
    return [{"tool": "revoke_result", "fixed": {"itemId": str(args["itemId"]), "reason": REASON}}]


RECIPES["approve_result"] = Recipe(label="Tasdiq qaytarib olinadi: hujjat bekor bo‘ladi, xizmat laboratoriyaga qaytadi", plan=_approve_plan)


async def _overrides_before(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return (await _read_employee(ctx, str(args["employeeId"]))).get("overrides") or {}


async def _overrides_plan(ctx: ToolContext, args: dict[str, Any], snap: Any, result: Any) -> list[dict[str, Any]] | None:
    old = snap if isinstance(snap, dict) else {}
    return [{"tool": "set_employee_overrides", "fixed": {"employeeId": str(args["employeeId"]), "allow": list(old.get("allow") or []), "deny": list(old.get("deny") or [])}}]


RECIPES["set_employee_overrides"] = Recipe(label="Shaxsiy ruxsatlar avvalgi holiga qaytariladi", plan=_overrides_plan, before=_overrides_before)


async def _order_rows(ctx: ToolContext, order_id: Any) -> dict[str, list[str]]:
    b = await orders_svc.get_order_bundle(ctx.session, tl._uuid(order_id, "orderId"), ctx.staff)
    return {"items": [str(i.id) for i in b.items], "payments": [str(p.id) for p in b.payments if not p.refunded_at]}


async def _order_before(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return await _order_rows(ctx, args["orderId"])


async def _pay_plan(ctx: ToolContext, args: dict[str, Any], snap: Any, result: Any) -> list[dict[str, Any]] | None:
    had = set((snap or {}).get("payments") or [])
    new = [p for p in (await _order_rows(ctx, args["orderId"]))["payments"] if p not in had]
    return [{"tool": "refund_payment", "fixed": {"paymentId": p, "reason": REASON}} for p in new] or None


async def _items_plan(ctx: ToolContext, args: dict[str, Any], snap: Any, result: Any) -> list[dict[str, Any]] | None:
    had = set((snap or {}).get("items") or [])
    new = [i for i in (await _order_rows(ctx, args["orderId"]))["items"] if i not in had]
    return [{"tool": "remove_order_item", "fixed": {"orderId": str(args["orderId"]), "itemId": i, "reason": REASON}} for i in new] or None


RECIPES["pay_order"] = Recipe(label="Qabul qilingan to‘lov qaytariladi (qaytarish yozuvi bilan)", plan=_pay_plan, before=_order_before)
RECIPES["add_order_items"] = Recipe(label="Qo‘shilgan xizmatlar chekdan olib tashlanadi", plan=_items_plan, before=_order_before)


async def _submit_before(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return (await _read_item(ctx, str(args["itemId"]))).get("status")


async def _submit_plan(ctx: ToolContext, args: dict[str, Any], snap: Any, result: Any) -> list[dict[str, Any]] | None:
    eid = str(args["itemId"])
    now = (await _read_item(ctx, eid)).get("status")
    # the tool is a toggle: running it again goes back — but only while the item is still where the write left it
    return [{"tool": "submit_result", "fixed": {"itemId": eid}, "restore": {}, "expect": {"status": now}, "reader": "item", "id": eid}] if now != snap else None


RECIPES["submit_result"] = Recipe(label="Natija avvalgi holatiga qaytariladi (yuborilgan bo‘lsa — qaytarib olinadi)", plan=_submit_plan, before=_submit_before)


# ----------------------------------------------------------------------------- remembering and running


async def remember(ctx: ToolContext, tool: str, args: dict[str, Any]) -> Any:
    """Before the write runs: what its recipe needs to know. Never stops the write — no memory just means no undo."""
    recipe = RECIPES.get(tool)
    if recipe is None or recipe.before is None:
        return None
    try:
        return await recipe.before(ctx, args)
    except (AppError, LookupError, KeyError, TypeError, ValueError, AttributeError):
        return None


async def plan(ctx: ToolContext, tool: str, args: dict[str, Any], snap: Any, result: Any) -> dict[str, Any] | None:
    """After the write ran: the plan kept with the action — `{label, steps}` — or None when it cannot be undone
    (no recipe, nothing changed, or the employee does not hold the tool the way back needs)."""
    recipe = RECIPES.get(tool)
    if recipe is None:
        return None
    try:
        steps = await recipe.plan(ctx, args, snap, result)
    except (AppError, LookupError, KeyError, TypeError, ValueError, AttributeError):
        return None
    if not steps:
        return None
    mine = {t.name for t in tl.tools_for(ctx.staff)}
    if not all(s["tool"] in mine for s in steps):
        return None
    return {"label": recipe.label, "steps": steps}


def _names(keys: Any) -> str:
    return ", ".join(LABELS.get(k, k) for k in keys)


async def run(ctx: ToolContext, kept: dict[str, Any]) -> list[str]:
    """Take the write back. Returns what was done, line by line; raises when nothing can be put back safely."""
    mine = {t.name for t in tl.tools_for(ctx.staff)}
    lines: list[str] = []
    for step in kept.get("steps") or []:
        t = tl.REGISTRY.get(step.get("tool", ""))
        if t is None or t.name not in mine:
            raise ConflictError(MSG_NO_RIGHT, code="undo_forbidden")
        args = dict(step.get("fixed") or {})
        restore = dict(step.get("restore") or {})
        if "expect" in step:
            # only what still holds the assistant's value goes back — a later change by a person stays
            row = await READERS[step["reader"]](ctx, str(step["id"]))
            stale = [k for k, v in (step.get("expect") or {}).items() if not _same(row.get(k), v)]
            if stale and (not restore or len(stale) == len(step["expect"])):
                raise ConflictError(f"{MSG_CONFLICT}: {_names(stale)}", code="undo_conflict")
            for k in stale:
                restore.pop(k, None)
            if stale:
                lines.append(f"Keyin o‘zgartirilgani uchun tegilmadi: {_names(stale)}.")
        args.update(restore)
        await t.handler(ctx, args)
        lines.append(f"Tiklandi: {_names(restore)}." if restore else f"{t.title} — bajarildi.")
    if not lines:
        raise ConflictError(MSG_CONFLICT, code="undo_conflict")
    return lines
