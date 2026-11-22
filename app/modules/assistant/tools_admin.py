"""The assistant's tools for running the organisation: geography and quick start, the working set of a sample
facility (catalogue + templates), SMS texts, resets, roles and permission overrides, categories, services, result
schemas and templates.

Same rules as `tools`: a tool is offered only to a holder of the key its HTTP route asks for, the service checks
the territory with the employee's own principal, a write waits for their yes. Provider keys, bot tokens and
superadmin rights are not here on purpose — those are entered by a person on the settings pages."""

from __future__ import annotations

import uuid
from typing import Any

from app.core.exceptions import ValidationError
from app.core.permissions import PERMISSIONS, PLATFORM_PERMISSIONS, RESET_PERMISSION
from app.infrastructure.db.models import OrgUnit
from app.modules.assistant.tools import EMP_KEYS, LIST_LIMIT, ToolContext, _dump, _scope, _str, _uuid, tool
from app.modules.catalog import service as catalog_svc
from app.modules.catalog.schemas import CategoryCreateIn, SchemaCreateIn, SchemaUpdateIn
from app.modules.patients import service as patients_svc
from app.modules.staff import service as staff_svc
from app.modules.staff.schemas import PermissionOverrides, RoleCreateIn, RoleUpdateIn
from app.modules.templates import service as tpl_svc
from app.modules.templates.schemas import DefaultReceiptIn, TemplateDuplicateIn, TemplateUpdateIn
from app.modules.units import quickstart, reset
from app.modules.units import repository as units_repo
from app.modules.units import service as units_svc
from app.modules.units.schemas import UnitSmsTemplatesIn

SETTINGS_WRITE = ("admin.settings.write", "admin.unit.write")
UNIT_MANAGE = ("admin.unit.manage", "platform.unit.manage")
ROLE_KEYS = ("id", "key", "name", "description", "scopeUnitId", "isSystem", "permissions")
SCHEMA_KEYS = ("id", "name", "description", "status", "version", "ownerUnitId", "usedBy")
TEMPLATE_KEYS = ("id", "name", "description", "status", "version", "scope", "language", "serviceTypeIds", "categoryIds", "unitIds", "ownerUnitId", "usage", "updatedAt")
SERVICE_KEYS = ("id", "name", "code", "description", "categoryId", "price", "effectivePrice", "unitPrices", "disabledUnitIds", "available", "isActive", "turnaroundDays", "schemaId", "documentScope", "defaultTemplateId", "ownerUnitId")
CATEGORY_KEYS = ("id", "name", "code", "parentId", "isActive", "workflow", "ownerUnitId", "order")


def _opt_uuid(value: Any, label: str) -> uuid.UUID | None:
    return _uuid(value, label) if value else None


async def _facility(ctx: ToolContext, unit_id: Any) -> OrgUnit:
    """The facility a tool acts on: the given one, else the working unit — inside the territory, and a facility."""
    scope = await _scope(ctx, unit_id)
    if not scope.is_facility:
        raise ValidationError("Bu amal muassasa uchun — qaysi muassasa ekanini aniqlang (list_units, level=facility)", code="facility_required")
    return scope.unit


# ============================================================================= geography, quick start, the working set


@tool("list_geo", "Hududlar ma’lumotnomasi", "Reference geography (not the organisation tree): kind=countries, regions (of a country — default: the working unit's) or districts (of a region). Gives the geo ids that create_unit / quick_start need for region and district nodes and that patient addresses use. Search by name.",
      params={"kind": {"type": "string", "enum": ["countries", "regions", "districts"], "req": True}, "countryId": {"type": "string"}, "regionId": {"type": "string"}, "search": {"type": "string"}}, group="units")
async def list_geo(ctx: ToolContext, args: dict[str, Any]) -> Any:
    kind = args["kind"]
    if kind == "countries":
        rows = await patients_svc.list_countries(ctx.session)
    elif kind == "regions":
        rows = await patients_svc.list_regions(ctx.session, _opt_uuid(args.get("countryId"), "countryId") or ctx.scope.country_id)
    else:
        rows = await patients_svc.list_districts(ctx.session, _opt_uuid(args.get("regionId"), "regionId"))
    q = str(args.get("search") or "").strip().lower()
    items = [_dump(r) for r in rows]
    if q:
        items = [r for r in items if q in " ".join(str(v) for v in r.values() if isinstance(v, str)).lower()]
    return {"total": len(items), "items": items[:LIST_LIMIT]}


@tool("list_sample_facilities", "Namuna muassasalar", "Facilities whose working set (catalogue + templates) can be copied to another facility, with what each one holds. Use an id from here as sampleFacilityId in copy_working_set / quick_start.",
      params={"facilityId": {"type": "string", "description": "the facility that would receive the set (default: the working unit) — what it already inherits is taken into account"}}, perms=(*quickstart.COPY_PERMISSIONS, *UNIT_MANAGE), group="templates")
async def list_sample_facilities(ctx: ToolContext, args: dict[str, Any]) -> Any:
    scope = await _scope(ctx, args.get("facilityId"))
    rows = await quickstart._samples(ctx.session, ctx.staff, set(scope.ancestor_ids))
    return {"total": len(rows), "items": [_dump(r) for r in rows if str(getattr(r, "id", "")) != str(scope.id)][:LIST_LIMIT]}


@tool("quick_start_options", "Tezkor ishga tushirish imkoniyatlari", "Before quick_start: what a new facility under a parent (country / region / district node; default the working unit) would inherit, which region / district nodes already exist, the sample facilities and the default roles.",
      params={"parentId": {"type": "string"}, "regionId": {"type": "string", "description": "geo region id (list_geo)"}, "districtId": {"type": "string", "description": "geo district id (list_geo)"}}, perms=UNIT_MANAGE, group="units")
async def quick_start_options(ctx: ToolContext, args: dict[str, Any]) -> Any:
    parent = (await _scope(ctx, args.get("parentId"))).unit
    return _dump(await quickstart.preview(ctx.session, parent, _str(args.get("regionId")), _str(args.get("districtId")), ctx.staff))


@tool("copy_working_set", "Namunadan katalog va andozalarni ko‘chirish",
      "Give an existing facility the working set of a sample facility: the sample's categories, services, result schemas and templates (the cheque too) are copied for it — what the facility already has under the same name is reused, never duplicated — and an active cheque template is ensured. This is how templates / the catalogue are ADDED to a facility. owner = who owns the copies: facility (default), district or region (then the facilities below share them).",
      params={"facilityId": {"type": "string", "description": "the facility that receives the set; default: the working unit when it is a facility"}, "sampleFacilityId": {"type": "string", "req": True, "description": "from list_sample_facilities"}, "owner": {"type": "string", "enum": ["facility", "district", "region"]}},
      kind="write", perms=quickstart.COPY_PERMISSIONS, summary_keys=("facilityId", "sampleFacilityId", "owner"), group="templates")
async def copy_working_set(ctx: ToolContext, args: dict[str, Any]) -> Any:
    quickstart._require_copy_right(ctx.staff)
    facility = await _facility(ctx, args.get("facilityId"))
    sample = await units_svc.get_unit_or_404(ctx.session, _uuid(args["sampleFacilityId"], "sampleFacilityId"))
    await units_svc.require_visible(ctx.session, ctx.staff, sample)
    if sample.level != "facility":
        raise ValidationError("Namuna — muassasa bo‘lishi kerak")
    if sample.id == facility.id:
        raise ValidationError("Namuna va qabul qiluvchi muassasa bir xil")
    owner = facility
    level = str(args.get("owner") or "facility")
    if level != "facility":
        lineage = (await units_repo.units_by_ids(ctx.session, (await units_svc.scope_or_404(ctx.session, facility.id)).ancestor_ids)).values()
        owner = next((u for u in lineage if u.level == level), facility)
        ctx.staff.require_unit(owner.id)
    res = await quickstart.copy_working_set(ctx.session, sample, facility, owner, ctx.staff, skip_existing=True)
    receipt = await quickstart.ensure_active_receipt(ctx.session, facility, owner, ctx.staff)
    return {"facility": facility.name, "sample": sample.name, "owner": owner.name, "copied": _dump(res.copied), "alreadyThere": _dump(res.inherited),
            "receipt": {"id": str(receipt.id), "name": receipt.name, "status": receipt.status} if receipt else None}


# ============================================================================= SMS texts (never the provider's credentials)


@tool("get_sms_account", "SMS hisobi holati", "Which SMS account a unit's messages go through: its own, an inherited one or switched off; provider and sender name. Credentials are never shown and cannot be set here — that is done on the SMS settings page.",
      params={"unitId": {"type": "string"}}, perms=(*SETTINGS_WRITE, "admin.unit.read"), group="messaging")
async def get_sms_account(ctx: ToolContext, args: dict[str, Any]) -> Any:
    scope = await _scope(ctx, args.get("unitId"))
    return _dump(await units_svc.get_sms(ctx.session, scope.id, ctx.staff), ("mode", "provider", "sender", "defaultPriority", "senderNote", "effectiveUnitId", "effectiveUnitName"))


@tool("get_sms_templates", "SMS matnlari", "The SMS texts a unit sends (payment receipt, result ready, reminder): its own or inherited, with the placeholders allowed.", params={"unitId": {"type": "string"}}, perms=(*SETTINGS_WRITE, "admin.unit.read"), group="messaging")
async def get_sms_templates(ctx: ToolContext, args: dict[str, Any]) -> Any:
    scope = await _scope(ctx, args.get("unitId"))
    return _dump(await units_svc.get_sms_templates(ctx.session, scope.id, ctx.staff))


@tool("update_sms_templates", "SMS matnlarini o‘zgartirish", "Save a unit's own SMS texts (keys: payment_receipt, result_ready, reminder; an empty text returns to the inherited one). applyToAll pushes them to the whole subtree. Read the current texts and placeholders with get_sms_templates first.",
      model=UnitSmsTemplatesIn, params={"unitId": {"type": "string"}}, kind="write", perms=SETTINGS_WRITE, summary_keys=("unitId", "applyToAll"), group="messaging")
async def update_sms_templates(ctx: ToolContext, args: dict[str, Any]) -> Any:
    scope = await _scope(ctx, args.get("unitId"))
    body = UnitSmsTemplatesIn.model_validate({k: v for k, v in args.items() if k != "unitId"})
    return _dump(await units_svc.set_sms_templates(ctx.session, scope.id, body, ctx.staff, ctx.meta))


@tool("check_sms_text", "SMS matnini tekshirish", "What an SMS text costs with the account in effect at a unit: parts, encoding, price, Eskiz template state. No message is sent.",
      params={"text": {"type": "string", "req": True}, "unitId": {"type": "string"}}, perms=(*SETTINGS_WRITE, "admin.unit.read", "messaging.send"), group="messaging")
async def check_sms_text(ctx: ToolContext, args: dict[str, Any]) -> Any:
    scope = await _scope(ctx, args.get("unitId"))
    return _dump(await units_svc.check_sms_text(ctx.session, scope.id, str(args["text"]), ctx.staff))


# ============================================================================= reset (irreversible)


@tool("reset_preview", "Tozalash ko‘rinishi", "What a reset of a unit (and everything under it) would remove, part by part, with counts. Changes nothing. Always call it before proposing reset_unit.", params={"unitId": {"type": "string", "req": True}}, perms=(RESET_PERMISSION,), group="units")
async def reset_preview(ctx: ToolContext, args: dict[str, Any]) -> Any:
    unit = await units_svc.get_unit_or_404(ctx.session, _uuid(args["unitId"], "unitId"))
    reset.require_reset_right(ctx.staff, unit)
    parts, requires = reset.parts_for(unit)
    counts = await reset.preview(ctx.session, unit, ctx.staff)
    return {"unit": {"id": str(unit.id), "name": unit.name, "level": unit.level}, "parts": [{"key": k, "counts": counts[k], "requires": list(requires.get(k, ()))} for k in parts]}


async def _reset_word(ctx: ToolContext, args: dict[str, Any]) -> str:
    unit = await units_svc.get_unit_or_404(ctx.session, _uuid(args.get("unitId"), "unitId"))
    reset.require_reset_right(ctx.staff, unit)
    return reset.confirm_word(unit)


@tool("reset_unit", "Birlikni tozalash", "IRREVERSIBLY remove the chosen parts of a unit's data (keys from reset_preview; a part pulls in what it requires). The employee must type the unit's code / name to confirm. Propose it only when they clearly asked for a reset of exactly this unit.",
      params={"unitId": {"type": "string", "req": True}, "parts": {"type": "array", "items": {"type": "string"}, "req": True}}, kind="write", perms=(RESET_PERMISSION,), summary_keys=("unitId", "parts"), group="units", danger=True, confirm_word=_reset_word)
async def reset_unit(ctx: ToolContext, args: dict[str, Any]) -> Any:
    unit = await units_svc.get_unit_or_404(ctx.session, _uuid(args["unitId"], "unitId"))
    reset.require_reset_right(ctx.staff, unit)
    parts, requires = reset.parts_for(unit)
    chosen = reset.close_parts([str(p) for p in args["parts"]], parts, requires)
    return {"unit": unit.name, "parts": chosen, "removed": await reset.reset_unit(ctx.session, unit, chosen, ctx.staff, ctx.meta)}


# ============================================================================= roles and permissions


@tool("list_permissions", "Ruxsatlar ro‘yxati", "Every permission key a role or an override may carry, and the ones the current employee holds (they cannot grant more than they hold).", perms=("admin.role.write", "admin.employee.write"), group="staff")
async def list_permissions(ctx: ToolContext, args: dict[str, Any]) -> Any:
    s = ctx.staff
    return {"all": [p for p in PERMISSIONS if p not in PLATFORM_PERMISSIONS or s.is_super_admin], "mine": ["*"] if s.is_super_admin else sorted(s.permissions)}


@tool("create_role", "Rol yaratish", "Create a role scoped to a unit (default: the working unit) with a set of permission keys (list_permissions).",
      model=RoleCreateIn, params={"unitId": {"type": "string"}}, kind="write", perms=("admin.role.write",), summary_keys=("name", "unitId"), group="staff")
async def create_role(ctx: ToolContext, args: dict[str, Any]) -> Any:
    scope = await _scope(ctx, args.get("unitId"))
    return _dump(await staff_svc.create_role(ctx.session, scope, RoleCreateIn.model_validate({k: v for k, v in args.items() if k != "unitId"}), ctx.staff, ctx.meta), ROLE_KEYS)


@tool("update_role", "Rolni tahrirlash", "Rename a role or replace its permission keys (wholesale — send the full list). System roles cannot be changed.",
      model=RoleUpdateIn, params={"roleId": {"type": "string", "req": True}}, kind="write", perms=("admin.role.write",), summary_keys=("roleId", "name"), group="staff")
async def update_role(ctx: ToolContext, args: dict[str, Any]) -> Any:
    fields = {k: v for k, v in args.items() if k != "roleId"}
    if not fields:
        raise ValidationError("O‘zgartiriladigan maydon yo‘q")
    return _dump(await staff_svc.update_role(ctx.session, _uuid(args["roleId"], "roleId"), RoleUpdateIn.model_validate(fields), ctx.staff, ctx.meta), ROLE_KEYS)


@tool("delete_role", "Rolni o‘chirish", "Delete a role nobody holds (system roles stay).", params={"roleId": {"type": "string", "req": True}}, kind="write", perms=("admin.role.write",), summary_keys=("roleId",), group="staff", danger=True)
async def delete_role(ctx: ToolContext, args: dict[str, Any]) -> Any:
    await staff_svc.delete_role(ctx.session, _uuid(args["roleId"], "roleId"), ctx.staff, ctx.meta)
    return {"deleted": True}


@tool("set_employee_overrides", "Xodim ruxsatlarini sozlash", "Replace an employee's personal permission overrides: allow adds keys on top of the role, deny takes keys away. Both lists are wholesale.",
      model=PermissionOverrides, params={"employeeId": {"type": "string", "req": True}}, kind="write", perms=("admin.employee.write", "admin.role.write"), summary_keys=("employeeId",), group="staff")
async def set_employee_overrides(ctx: ToolContext, args: dict[str, Any]) -> Any:
    body = PermissionOverrides.model_validate({k: v for k, v in args.items() if k != "employeeId"})
    return _dump(await staff_svc.set_overrides(ctx.session, _uuid(args["employeeId"], "employeeId"), body, ctx.staff, ctx.meta), (*EMP_KEYS, "permissions", "overrides"))


# ============================================================================= the catalogue


@tool("get_service_type", "Xizmat kartasi", "One service type in full: price, per-unit prices, where it is switched off, its result schema and default template.", params={"serviceTypeId": {"type": "string", "req": True}},
      perms=("admin.catalog.read", "reception.order.create", "lab.worklist.read", "reports.operations.read"), group="catalog")
async def get_service_type(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return _dump(await catalog_svc.get_service_type_dto(ctx.session, _uuid(args["serviceTypeId"], "serviceTypeId"), ctx.staff), SERVICE_KEYS)


@tool("create_category", "Kategoriya qo‘shish", "Create a service category owned by a unit (default: the working unit), optionally under a parent category.",
      model=CategoryCreateIn, params={"unitId": {"type": "string"}}, kind="write", perms=("admin.catalog.write",), summary_keys=("name", "unitId"), group="catalog")
async def create_category(ctx: ToolContext, args: dict[str, Any]) -> Any:
    scope = await _scope(ctx, args.get("unitId"))
    return _dump(await catalog_svc.create_category(ctx.session, scope, CategoryCreateIn.model_validate({k: v for k, v in args.items() if k != "unitId"}), ctx.staff, ctx.meta), CATEGORY_KEYS)


@tool("delete_category", "Kategoriyani o‘chirish", "Delete a category that has no sub-categories and no services.", params={"categoryId": {"type": "string", "req": True}}, kind="write", perms=("admin.catalog.write",), summary_keys=("categoryId",), group="catalog", danger=True)
async def delete_category(ctx: ToolContext, args: dict[str, Any]) -> Any:
    await catalog_svc.delete_category(ctx.session, _uuid(args["categoryId"], "categoryId"), ctx.staff, ctx.meta)
    return {"deleted": True}


@tool("delete_service_type", "Xizmatni o‘chirish", "Delete a service type that was never ordered (otherwise switch it off with update_service_type isActive=false).", params={"serviceTypeId": {"type": "string", "req": True}},
      kind="write", perms=("admin.catalog.write",), summary_keys=("serviceTypeId",), group="catalog", danger=True)
async def delete_service_type(ctx: ToolContext, args: dict[str, Any]) -> Any:
    await catalog_svc.delete_service_type(ctx.session, _uuid(args["serviceTypeId"], "serviceTypeId"), ctx.staff, ctx.meta)
    return {"deleted": True}


@tool("list_schemas", "Natija sxemalari", "Result schemas (the fields a laborant fills for a service) seen from a unit, with which services use each.", params={"unitId": {"type": "string"}, "search": {"type": "string"}},
      perms=("admin.catalog.read", "admin.schema.write", "lab.worklist.read"), group="catalog")
async def list_schemas(ctx: ToolContext, args: dict[str, Any]) -> Any:
    rows = await catalog_svc.list_schemas(ctx.session, await _scope(ctx, args.get("unitId")))
    q = str(args.get("search") or "").strip().lower()
    items = [_dump(r, SCHEMA_KEYS) for r in rows if not q or q in r.name.lower()]
    return {"total": len(items), "items": items[:LIST_LIMIT]}


@tool("get_schema", "Sxema tafsilotlari", "One result schema with its fields (key, label, type, options, reference ranges …).", params={"schemaId": {"type": "string", "req": True}}, perms=("admin.catalog.read", "admin.schema.write", "lab.worklist.read"), group="catalog")
async def get_schema(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return _dump(await catalog_svc.get_schema_dto(ctx.session, _uuid(args["schemaId"], "schemaId"), ctx.staff))


@tool("create_schema", "Sxema yaratish", "Create a result schema (draft) owned by a unit: a name and its fields — each {key (no spaces), label, type, required?, order?} plus the type's own extras (options, unit, reference ranges). Look at an existing schema (get_schema) for the shape before writing one.",
      model=SchemaCreateIn, params={"unitId": {"type": "string"}}, kind="write", perms=("admin.schema.write",), summary_keys=("name", "unitId"), group="catalog")
async def create_schema(ctx: ToolContext, args: dict[str, Any]) -> Any:
    scope = await _scope(ctx, args.get("unitId"))
    return _dump(await catalog_svc.create_schema(ctx.session, scope, SchemaCreateIn.model_validate({k: v for k, v in args.items() if k != "unitId"}), ctx.staff, ctx.meta), SCHEMA_KEYS)


@tool("update_schema", "Sxemani tahrirlash", "Change a result schema: name, description or its fields (wholesale — send every field; a published schema gets a new version).",
      model=SchemaUpdateIn, params={"schemaId": {"type": "string", "req": True}}, kind="write", perms=("admin.schema.write",), summary_keys=("schemaId", "name"), group="catalog")
async def update_schema(ctx: ToolContext, args: dict[str, Any]) -> Any:
    fields = {k: v for k, v in args.items() if k != "schemaId"}
    if not fields:
        raise ValidationError("O‘zgartiriladigan maydon yo‘q")
    return _dump(await catalog_svc.update_schema(ctx.session, _uuid(args["schemaId"], "schemaId"), SchemaUpdateIn.model_validate(fields), ctx.staff, ctx.meta), SCHEMA_KEYS)


@tool("publish_schema", "Sxemani e’lon qilish", "Publish a draft result schema so services can use it.", params={"schemaId": {"type": "string", "req": True}}, kind="write", perms=("admin.schema.write",), summary_keys=("schemaId",), group="catalog")
async def publish_schema(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return _dump(await catalog_svc.publish_schema(ctx.session, _uuid(args["schemaId"], "schemaId"), ctx.staff, ctx.meta), SCHEMA_KEYS)


# ============================================================================= templates


@tool("get_template", "Andoza tafsilotlari", "One template: status, scope (item / order / receipt), language, the services / categories / units it is bound to. The layout itself is edited in the visual editor, not here.", params={"templateId": {"type": "string", "req": True}}, perms=("admin.template.read",), group="templates")
async def get_template(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return _dump(await tpl_svc.get_template_dto(ctx.session, _uuid(args["templateId"], "templateId"), ctx.staff), TEMPLATE_KEYS)


@tool("duplicate_template", "Andozadan nusxa olish", "Copy a template as a new draft, optionally renamed and bound to other units (unitIds). Use it to give a unit its own copy of one template; for a whole set use copy_working_set.",
      model=TemplateDuplicateIn, params={"templateId": {"type": "string", "req": True}}, kind="write", perms=("admin.template.write",), summary_keys=("templateId", "name", "unitIds"), group="templates")
async def duplicate_template(ctx: ToolContext, args: dict[str, Any]) -> Any:
    body = TemplateDuplicateIn.model_validate({k: v for k, v in args.items() if k != "templateId"})
    return _dump(await tpl_svc.duplicate_template(ctx.session, _uuid(args["templateId"], "templateId"), ctx.staff, ctx.meta, body), TEMPLATE_KEYS)


@tool("update_template", "Andozani tahrirlash", "Change a template's name, description, language, scope or what it is bound to — serviceTypeIds, categoryIds, unitIds (each list wholesale; an empty unitIds = every unit under the owner). The layout is edited in the visual editor.",
      model=TemplateUpdateIn, omit=("doc", "thumbnailUrl"), params={"templateId": {"type": "string", "req": True}}, kind="write", perms=("admin.template.write",), summary_keys=("templateId", "name", "unitIds"), group="templates")
async def update_template(ctx: ToolContext, args: dict[str, Any]) -> Any:
    fields = {k: v for k, v in args.items() if k != "templateId"}
    if not fields:
        raise ValidationError("O‘zgartiriladigan maydon yo‘q")
    return _dump(await tpl_svc.update_template(ctx.session, _uuid(args["templateId"], "templateId"), TemplateUpdateIn.model_validate(fields), ctx.staff, ctx.meta), TEMPLATE_KEYS)


@tool("create_default_receipt", "Standart chek andozasi", "Create the standard cheque as a new draft receipt template owned by a unit (default: the working unit); activate it with set_template_status.",
      model=DefaultReceiptIn, params={"unitId": {"type": "string"}}, kind="write", perms=("admin.template.write",), summary_keys=("unitId", "name", "paper"), group="templates")
async def create_default_receipt(ctx: ToolContext, args: dict[str, Any]) -> Any:
    scope = await _scope(ctx, args.get("unitId"))
    return _dump(await tpl_svc.create_default_receipt(ctx.session, scope, DefaultReceiptIn.model_validate({k: v for k, v in args.items() if k != "unitId"}), ctx.staff, ctx.meta), TEMPLATE_KEYS)


@tool("delete_template", "Andozani o‘chirish", "Delete a template that is not active (archive an active one first with set_template_status).", params={"templateId": {"type": "string", "req": True}}, kind="write", perms=("admin.template.write",), summary_keys=("templateId",), group="templates", danger=True)
async def delete_template(ctx: ToolContext, args: dict[str, Any]) -> Any:
    await tpl_svc.delete_template(ctx.session, _uuid(args["templateId"], "templateId"), ctx.staff, ctx.meta)
    return {"deleted": True}


