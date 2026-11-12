"""The assistant's tools — thin wrappers over the ordinary service functions, so every call carries the caller's
`StaffPrincipal` and is checked exactly like the HTTP route would check it (403 outside the territory, 404 for
what they may not see). A tool is OFFERED to the model only when the employee holds one of its keys, and a WRITE
tool never runs on the model's word: it becomes a pending action the employee confirms in the UI.

Outputs are DTOs with the secret-looking keys stripped (`_clean`): no password hashes, no API keys, no tokens."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import RequestMeta, StaffPrincipal
from app.core.exceptions import NotFoundError, StateError, ValidationError
from app.core.ids import uuid7
from app.core.permissions import ASSISTANT_PERMISSION
from app.core.scope import UnitScope
from app.core.timeutil import utcnow
from app.infrastructure.db.base import alive
from app.infrastructure.db.models import AssistantFile, Role
from app.modules.assistant import export as xp
from app.modules.assistant import render as rd
from app.modules.catalog import service as catalog_svc
from app.modules.catalog.schemas import CategoryUpdateIn, ServiceTypeCreateIn, ServiceTypeQuery, ServiceTypeUpdateIn
from app.modules.messaging import service as messaging_svc
from app.modules.messaging.schemas import OutboxQuery
from app.modules.messaging.schemas import SendIn as SmsSendIn
from app.modules.orders import service as orders_svc
from app.modules.orders.schemas import OrderListQuery
from app.modules.patients import service as patients_svc
from app.modules.reports import service as reports_svc
from app.modules.reports.schemas import BreakdownQuery, RangeQuery, ResultListQuery
from app.modules.staff import service as staff_svc
from app.modules.staff.schemas import EmployeeCreateIn, EmployeeQuery, EmployeeUpdateIn
from app.modules.templates import service as tpl_svc
from app.modules.templates.schemas import TemplateQuery
from app.modules.units import quickstart
from app.modules.units import service as units_svc
from app.modules.units.schemas import QuickStartIn, UnitCreateIn, UnitTreeQuery, UnitUpdateIn

ToolKind = Literal["read", "write"]
Handler = Callable[["ToolContext", dict[str, Any]], Awaitable[Any]]

#: keys that never reach the model, whatever DTO they sit in
SECRET_KEYS = frozenset({"password", "passwordHash", "password_hash", "apiKey", "apiKeyEnc", "api_key", "api_key_enc", "apiKeyMasked", "botToken", "token", "accessToken", "secret", "sms", "telegram"})
LIST_LIMIT = 200


@dataclass
class ToolContext:
    session: AsyncSession
    staff: StaffPrincipal
    meta: RequestMeta
    #: the unit the employee works in (the header scope); tools without a `unitId` act here
    unit_id: uuid.UUID
    scope: UnitScope
    #: the thread the tools run in and the call being executed — files the tools build are attached to them
    thread_id: uuid.UUID | None = None
    call_id: str | None = None


@dataclass(frozen=True)
class Tool:
    name: str
    title: str
    description: str
    params: dict[str, Any]
    kind: ToolKind
    #: ANY of these grants the tool (superadmin: all); empty = every assistant user
    perms: tuple[str, ...]
    handler: Handler
    #: which argument keys name the thing acted on, for the one-line summary shown to the employee
    summary_keys: tuple[str, ...] = ()


REGISTRY: dict[str, Tool] = {}


def tool(name: str, title: str, description: str, *, params: dict[str, Any] | None = None, kind: ToolKind = "read", perms: Iterable[str] = (), summary_keys: Iterable[str] = ()) -> Callable[[Handler], Handler]:
    schema = {"type": "object", "properties": params or {}, "additionalProperties": False}
    required = [k for k, v in (params or {}).items() if v.pop("req", False)]
    if required:
        schema["required"] = required

    def deco(fn: Handler) -> Handler:
        REGISTRY[name] = Tool(name=name, title=title, description=description, params=schema, kind=kind, perms=tuple(perms), handler=fn, summary_keys=tuple(summary_keys))
        return fn

    return deco


def tools_for(staff: StaffPrincipal) -> list[Tool]:
    """The tools this employee may use: assistant.use plus any of the tool's own keys."""
    if not staff.has(ASSISTANT_PERMISSION):
        return []
    return [t for t in REGISTRY.values() if not t.perms or staff.has(*t.perms)]


def definitions(tools: Iterable[Tool]) -> list[dict[str, Any]]:
    return [{"name": t.name, "description": t.description, "parameters": t.params} for t in tools]


def summarize(t: Tool, args: dict[str, Any]) -> str:
    """'name=Bog‘ot SES · price=50000' — the arguments in one line, secrets masked, long values cut."""
    parts: list[str] = []
    for k, v in args.items():
        if k in SECRET_KEYS or k == "password":
            parts.append(f"{k}=••••")
            continue
        s = str(v) if not isinstance(v, (dict, list)) else "…"
        parts.append(f"{k}={s[:60]}")
    return " · ".join(parts)[:280]


def _clean(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items() if k not in SECRET_KEYS}
    if isinstance(obj, list):
        return [_clean(x) for x in obj]
    return obj


def _dump(dto: Any, keys: Iterable[str] | None = None) -> Any:
    data = dto.model_dump(by_alias=True, mode="json") if hasattr(dto, "model_dump") else dto
    if keys and isinstance(data, dict):
        data = {k: data.get(k) for k in keys if k in data}
    return _clean(data)


def _uuid(value: Any, label: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        raise ValidationError(f"{label} noto‘g‘ri (UUID kutiladi)") from None


async def _scope(ctx: ToolContext, unit_id: Any) -> UnitScope:
    """The scope a tool acts in: the given unit (inside the employee's territory) or the working unit."""
    if not unit_id:
        return ctx.scope
    uid = _uuid(unit_id, "unitId")
    ctx.staff.require_unit(uid)
    return await units_svc.scope_or_404(ctx.session, uid)


async def _system_role_id(session: AsyncSession, key: str) -> str:
    row = (await session.execute(select(Role).where(Role.scope_unit_id.is_(None), Role.key == key, alive(Role)))).scalar_one_or_none()
    if row is None:
        raise NotFoundError("Rol topilmadi")
    return str(row.id)


def _str(v: Any) -> str | None:
    return None if v is None else str(v)


# ============================================================================= read tools

UNIT_KEYS = ("id", "name", "level", "kind", "code", "path", "parentId", "isActive", "phone", "address", "counts")
EMP_KEYS = ("id", "fullName", "login", "phone", "email", "roleId", "roleKey", "roleName", "unitId", "unitName", "unitIds", "status", "categoryIds")


@tool("whoami", "Mening hisobim", "Who the current employee is: name, role, home unit, working unit, permissions and the tools available to them. Call it when the user asks what they can do.")
async def whoami(ctx: ToolContext, args: dict[str, Any]) -> Any:
    s = ctx.staff
    return {
        "fullName": s.employee.full_name,
        "login": s.employee.login,
        "role": {"key": s.role_key, "name": s.role.name if s.role else None},
        "isSuperAdmin": s.is_super_admin,
        "homeUnit": {"id": _str(s.home_unit.id), "name": s.home_unit.name, "level": s.home_unit.level} if s.home_unit else None,
        "workingUnit": {"id": str(ctx.scope.id), "name": ctx.scope.unit.name, "level": ctx.scope.unit.level},
        "permissions": sorted(s.permissions) if not s.is_super_admin else ["*"],
        "tools": [t.name for t in tools_for(s)],
    }


@tool("list_units", "Birliklar ro‘yxati", "List organisation units (country / region / district / facility) the user may see, optionally under one parent, filtered by name/code and level. Returns id, name, level, code, path, counts.",
      params={"parentId": {"type": "string", "description": "limit to the subtree of this unit"}, "search": {"type": "string"}, "level": {"type": "string", "enum": ["country", "region", "district", "facility"]}, "includeInactive": {"type": "boolean"}})
async def list_units(ctx: ToolContext, args: dict[str, Any]) -> Any:
    rows = await units_svc.list_tree(ctx.session, ctx.staff, UnitTreeQuery(root_id=_str(args.get("parentId")), include_inactive=bool(args.get("includeInactive", False))))
    q = str(args.get("search") or "").strip().lower()
    level = args.get("level")
    out = [u for u in rows if (not level or u.level == level) and (not q or q in f"{u.name} {u.code or ''} {u.path}".lower())]
    return {"total": len(out), "items": [_dump(u, UNIT_KEYS) for u in out[:LIST_LIMIT]]}


@tool("get_unit", "Birlik kartasi", "One unit's card: profile, contacts, effective settings, counts.", params={"unitId": {"type": "string", "req": True}})
async def get_unit(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return _dump(await units_svc.get_unit_dto(ctx.session, _uuid(args["unitId"], "unitId"), ctx.staff), (*UNIT_KEYS, "shortName", "legalName", "email", "timezone", "settings", "createdAt"))


@tool("list_employees", "Xodimlar ro‘yxati", "Employees under a unit (default: the working unit): search by name/login/phone, filter by role or status. Paged, 20 per page.",
      params={"unitId": {"type": "string"}, "search": {"type": "string"}, "roleId": {"type": "string"}, "status": {"type": "string", "enum": ["active", "inactive"]}, "page": {"type": "integer", "minimum": 1}},
      perms=("admin.employee.read",))
async def list_employees(ctx: ToolContext, args: dict[str, Any]) -> Any:
    scope = await _scope(ctx, args.get("unitId"))
    page = await staff_svc.list_employees(ctx.session, scope, EmployeeQuery(search=args.get("search"), role_id=_str(args.get("roleId")), status=args.get("status"), page=int(args.get("page") or 1), page_size=20), ctx.staff)
    return {"total": page.total, "page": page.page, "totalPages": page.total_pages, "items": [_dump(e, EMP_KEYS) for e in page.items]}


@tool("get_employee", "Xodim kartasi", "One employee: profile, role, units, status.", params={"employeeId": {"type": "string", "req": True}}, perms=("admin.employee.read",))
async def get_employee(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return _dump(await staff_svc.get_employee_dto(ctx.session, _uuid(args["employeeId"], "employeeId"), ctx.staff), (*EMP_KEYS, "permissions", "overrides", "createdAt"))


@tool("list_roles", "Rollar", "Roles assignable under a unit: system roles and the unit's own. Use the ids when creating or moving employees.", params={"unitId": {"type": "string"}}, perms=("admin.employee.read", "admin.role.write"))
async def list_roles(ctx: ToolContext, args: dict[str, Any]) -> Any:
    rows = await staff_svc.list_roles(ctx.session, await _scope(ctx, args.get("unitId")))
    return {"items": [_dump(r, ("id", "key", "name", "scopeUnitId", "isSystem", "permissions")) for r in rows]}


@tool("list_categories", "Kategoriyalar", "Service categories visible at a unit (the catalogue it inherits + its own).", params={"unitId": {"type": "string"}}, perms=("admin.catalog.read", "reception.order.create", "lab.worklist.read", "reports.operations.read"))
async def list_categories(ctx: ToolContext, args: dict[str, Any]) -> Any:
    rows = await catalog_svc.list_categories(ctx.session, await _scope(ctx, args.get("unitId")))
    return {"items": [_dump(c, ("id", "name", "code", "parentId", "isActive", "workflow", "ownerUnitId")) for c in rows]}


@tool("list_service_types", "Xizmatlar", "Service types (analyses) usable at a unit with their prices; search by name or code, filter by category.",
      params={"unitId": {"type": "string"}, "search": {"type": "string"}, "categoryId": {"type": "string"}, "activeOnly": {"type": "boolean"}},
      perms=("admin.catalog.read", "reception.order.create", "lab.worklist.read", "reports.operations.read"))
async def list_service_types(ctx: ToolContext, args: dict[str, Any]) -> Any:
    scope = await _scope(ctx, args.get("unitId"))
    rows = await catalog_svc.list_service_types(ctx.session, scope, ServiceTypeQuery(search=args.get("search"), category_id=_str(args.get("categoryId")), active_only=bool(args.get("activeOnly", False)), unit_id=str(scope.id)))
    return {"total": len(rows), "items": [_dump(s, ("id", "name", "code", "categoryId", "price", "effectivePrice", "available", "isActive", "turnaroundDays", "ownerUnitId")) for s in rows[:LIST_LIMIT]]}


@tool("list_templates", "Andozalar", "Result / cheque templates usable at a unit, by status.", params={"unitId": {"type": "string"}, "status": {"type": "string", "enum": ["draft", "active", "archived"]}, "search": {"type": "string"}}, perms=("admin.template.read",))
async def list_templates(ctx: ToolContext, args: dict[str, Any]) -> Any:
    scope = await _scope(ctx, args.get("unitId"))
    rows = await tpl_svc.list_templates(ctx.session, scope, TemplateQuery(status=args.get("status"), search=args.get("search"), unit_id=str(scope.id)))
    return {"total": len(rows), "items": [_dump(t, ("id", "name", "status", "scope", "language", "serviceTypeIds", "categoryIds", "unitIds", "ownerUnitId", "usage")) for t in rows[:LIST_LIMIT]]}


@tool("dashboard", "Ko‘rsatkichlar", "Key figures of a unit for a date range (YYYY-MM-DD): cheques, revenue, pending lab / approval, patients, SMS queue, daily trend, by category.",
      params={"unitId": {"type": "string"}, "dateFrom": {"type": "string", "req": True}, "dateTo": {"type": "string", "req": True}},
      perms=("reports.finance.read", "reports.finance.recent", "reports.operations.read"))
async def dashboard(ctx: ToolContext, args: dict[str, Any]) -> Any:
    scope = await _scope(ctx, args.get("unitId"))
    d = await reports_svc.dashboard(ctx.session, scope, RangeQuery(date_from=str(args["dateFrom"]), date_to=str(args["dateTo"])), ctx.staff)
    data = _dump(d)
    if isinstance(data, dict) and isinstance(data.get("trend"), list):
        data["trend"] = data["trend"][-31:]
    return data


@tool("breakdown", "Hisobot bo‘yicha taqsimot", "Revenue and cheque counts for a date range grouped by category, service, unit or employee.",
      params={"unitId": {"type": "string"}, "dateFrom": {"type": "string", "req": True}, "dateTo": {"type": "string", "req": True}, "by": {"type": "string", "enum": ["category", "service", "unit", "employee"], "req": True}},
      perms=("reports.finance.read", "reports.finance.recent", "reports.operations.read"))
async def breakdown(ctx: ToolContext, args: dict[str, Any]) -> Any:
    scope = await _scope(ctx, args.get("unitId"))
    rows = await reports_svc.breakdown(ctx.session, scope, BreakdownQuery(date_from=str(args["dateFrom"]), date_to=str(args["dateTo"]), by=args["by"]), ctx.staff)
    return {"items": [_dump(r) for r in rows[:LIST_LIMIT]]}


@tool("list_orders", "Cheklar", "Cheques (orders) under a unit: search by number / patient, filter by status, payment, dates (YYYY-MM-DD), patient. Paged, 20 per page.",
      params={"unitId": {"type": "string"}, "search": {"type": "string"}, "status": {"type": "string"}, "payment": {"type": "string"}, "dateFrom": {"type": "string"}, "dateTo": {"type": "string"}, "patientId": {"type": "string"}, "page": {"type": "integer", "minimum": 1}},
      perms=("reception.order.create", "reports.operations.read", "reports.finance.read", "confirm.result.read"))
async def list_orders(ctx: ToolContext, args: dict[str, Any]) -> Any:
    scope = await _scope(ctx, args.get("unitId"))
    page = await orders_svc.list_orders(ctx.session, scope, OrderListQuery(search=args.get("search"), status=args.get("status"), payment=args.get("payment"), date_from=args.get("dateFrom"), date_to=args.get("dateTo"), patient_id=_str(args.get("patientId")), page=int(args.get("page") or 1), page_size=20))
    return {"total": page.total, "page": page.page, "totalPages": page.total_pages, "items": [_dump(o, ("id", "number", "patientName", "patientPhone", "status", "payment", "total", "paid", "unitName", "createdAt")) for o in page.items]}


@tool("get_order", "Chek tafsilotlari", "One cheque with its services (and their statuses) and payments.", params={"orderId": {"type": "string", "req": True}},
      perms=("reception.order.create", "reports.operations.read", "lab.worklist.read", "confirm.result.read"))
async def get_order(ctx: ToolContext, args: dict[str, Any]) -> Any:
    b = await orders_svc.get_order_bundle(ctx.session, _uuid(args["orderId"], "orderId"), ctx.staff)
    data = _dump(b)
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        data["items"] = [{k: it.get(k) for k in ("id", "serviceName", "status", "price", "documentId") if k in it} for it in data["items"]]
    return data


@tool("search_patients", "Bemorlarni qidirish", "Find patients by name, phone or passport (first 10 matches) in the user's scope.", params={"query": {"type": "string", "req": True}, "unitId": {"type": "string"}}, perms=("reception.patient.read",))
async def search_patients(ctx: ToolContext, args: dict[str, Any]) -> Any:
    scope = await _scope(ctx, args.get("unitId"))
    rows = await patients_svc.search_patients(ctx.session, scope, ctx.staff, str(args["query"]), 10)
    return {"items": [_dump(p, ("id", "fullName", "phone", "birthDate", "gender", "registeredUnitName", "stats")) for p in rows]}


@tool("get_patient", "Bemor kartasi", "One patient's card and visit statistics.", params={"patientId": {"type": "string", "req": True}}, perms=("reception.patient.read",))
async def get_patient(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return _dump(await patients_svc.get_patient_dto(ctx.session, _uuid(args["patientId"], "patientId"), ctx.staff), ("id", "fullName", "phone", "birthDate", "gender", "address", "registeredUnitName", "discountPercent", "tags", "stats", "createdAt"))


@tool("outbox_counts", "SMS navbati", "SMS outbox counters of a unit (queued, sent, delivered, failed).", params={"unitId": {"type": "string"}}, perms=("messaging.send", "messaging.broadcast"))
async def outbox_counts(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return _dump(await messaging_svc.outbox_counts(ctx.session, await _scope(ctx, args.get("unitId")), OutboxQuery(), ctx.staff))


# ============================================================================= write tools (confirmed by the employee)


@tool("update_unit", "Birlikni tahrirlash", "Change a unit's profile: name, short name, legal name, phone, email, address, active flag; for a facility also its kind and cheque code. Only the given fields change.",
      params={"unitId": {"type": "string", "req": True}, "name": {"type": "string"}, "shortName": {"type": "string"}, "legalName": {"type": "string"}, "phone": {"type": "string"}, "email": {"type": "string"}, "address": {"type": "string"}, "isActive": {"type": "boolean"}, "kind": {"type": "string", "enum": ["ses", "clinic", "laboratory", "polyclinic", "hospital", "pharmacy", "other"]}, "code": {"type": "string"}},
      kind="write", perms=("admin.unit.write",), summary_keys=("unitId", "name", "phone", "isActive"))
async def update_unit(ctx: ToolContext, args: dict[str, Any]) -> Any:
    uid = _uuid(args["unitId"], "unitId")
    fields = {k: v for k, v in args.items() if k != "unitId"}
    if not fields:
        raise ValidationError("O‘zgartiriladigan maydon yo‘q")
    return _dump(await units_svc.update_unit(ctx.session, uid, UnitUpdateIn.model_validate(fields), ctx.staff, ctx.meta), UNIT_KEYS)


@tool("create_unit", "Birlik yaratish", "Create a unit under a parent: a region node needs the geo regionId, a district node districtId, a facility needs kind and a cheque code.",
      params={"parentId": {"type": "string", "req": True}, "name": {"type": "string", "req": True}, "kind": {"type": "string"}, "code": {"type": "string"}, "regionId": {"type": "string"}, "districtId": {"type": "string"}, "phone": {"type": "string"}, "address": {"type": "string"}},
      kind="write", perms=("admin.unit.manage",), summary_keys=("name", "kind", "code"))
async def create_unit(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return _dump(await units_svc.create_unit(ctx.session, UnitCreateIn.model_validate(args), ctx.staff, ctx.meta), UNIT_KEYS)


@tool("quick_start", "Muassasani tezkor ishga tushirish", "Create a facility ready to work in one go under a country / region / district: missing region / district nodes (geo ids), the facility, default staff accounts (registrator, vrach, laborant, rahbar) with one password, and the catalogue + templates copied from a sample facility.",
      params={"parentId": {"type": "string", "req": True}, "regionId": {"type": "string"}, "districtId": {"type": "string"}, "facility": {"type": "object", "req": True, "properties": {"name": {"type": "string"}, "kind": {"type": "string"}, "code": {"type": "string"}, "phone": {"type": "string"}, "address": {"type": "string"}}, "required": ["name", "kind", "code"]},
              "staff": {"type": "array", "items": {"type": "object", "properties": {"role": {"type": "string", "enum": ["registrator", "vrach", "laborant", "rahbar", "admin"]}, "fullName": {"type": "string"}, "login": {"type": "string"}}, "required": ["role", "fullName", "login"]}},
              "password": {"type": "string"}, "sampleFacilityId": {"type": "string"}},
      kind="write", perms=("admin.unit.manage",), summary_keys=("facility", "regionId", "districtId", "sampleFacilityId"))
async def quick_start(ctx: ToolContext, args: dict[str, Any]) -> Any:
    parent = await units_svc.get_unit_or_404(ctx.session, _uuid(args["parentId"], "parentId"))
    body = QuickStartIn.model_validate({
        "regionId": args.get("regionId"), "districtId": args.get("districtId"), "facility": args["facility"], "staff": args.get("staff") or [],
        "password": args.get("password") or "123456", "sample": {"facilityId": args["sampleFacilityId"], "owner": "region"} if args.get("sampleFacilityId") else None,
    })
    out = await quickstart.quick_start(ctx.session, parent, body, ctx.staff, ctx.meta)
    return {"facility": _dump(out.facility, UNIT_KEYS), "regionCreated": out.region.name if out.region else None, "districtCreated": out.district.name if out.district else None,
            "employees": [{"fullName": e.full_name, "login": e.login, "role": e.role_key} for e in out.employees], "catalog": _dump(out.catalog), "receipt": _dump(out.receipt) if out.receipt else None}


@tool("create_employee", "Xodim qo‘shish", "Create an employee at a unit (default: the working unit) with a role (roleKey: registrator | vrach | laborant | rahbar | admin, or a roleId from list_roles). Password defaults to 123456.",
      params={"fullName": {"type": "string", "req": True}, "login": {"type": "string", "req": True}, "roleKey": {"type": "string"}, "roleId": {"type": "string"}, "unitId": {"type": "string"}, "phone": {"type": "string"}, "email": {"type": "string"}, "password": {"type": "string"}},
      kind="write", perms=("admin.employee.write",), summary_keys=("fullName", "login", "roleKey", "roleId", "unitId"))
async def create_employee(ctx: ToolContext, args: dict[str, Any]) -> Any:
    scope = await _scope(ctx, args.get("unitId"))
    role_id = _str(args.get("roleId")) or await _system_role_id(ctx.session, str(args.get("roleKey") or "registrator"))
    body = EmployeeCreateIn(full_name=str(args["fullName"]), login=str(args["login"]), password=args.get("password"), phone=args.get("phone"), email=args.get("email"), role_id=role_id, unit_id=str(scope.id))
    return _dump(await staff_svc.create_employee(ctx.session, scope, body, ctx.staff, ctx.meta), EMP_KEYS)


@tool("update_employee", "Xodimni tahrirlash", "Change an employee: name, phone, email, status (active / inactive), role (roleKey or roleId), home unit, or set a new password. Only the given fields change.",
      params={"employeeId": {"type": "string", "req": True}, "fullName": {"type": "string"}, "phone": {"type": "string"}, "email": {"type": "string"}, "status": {"type": "string", "enum": ["active", "inactive"]}, "roleKey": {"type": "string"}, "roleId": {"type": "string"}, "unitId": {"type": "string"}, "password": {"type": "string"}},
      kind="write", perms=("admin.employee.write",), summary_keys=("employeeId", "fullName", "status", "roleKey", "roleId", "unitId"))
async def update_employee(ctx: ToolContext, args: dict[str, Any]) -> Any:
    eid = _uuid(args["employeeId"], "employeeId")
    fields = {k: v for k, v in args.items() if k not in ("employeeId", "roleKey")}
    if args.get("roleKey") and not fields.get("roleId"):
        fields["roleId"] = await _system_role_id(ctx.session, str(args["roleKey"]))
    if not fields:
        raise ValidationError("O‘zgartiriladigan maydon yo‘q")
    return _dump(await staff_svc.update_employee(ctx.session, eid, EmployeeUpdateIn.model_validate(fields), ctx.staff, ctx.meta), EMP_KEYS)


@tool("update_service_type", "Xizmatni tahrirlash", "Change a service type: name, code, base price (UZS), turnaround days, active flag.",
      params={"serviceTypeId": {"type": "string", "req": True}, "name": {"type": "string"}, "code": {"type": "string"}, "price": {"type": "integer", "minimum": 0}, "turnaroundDays": {"type": "integer", "minimum": 0}, "isActive": {"type": "boolean"}},
      kind="write", perms=("admin.catalog.write",), summary_keys=("serviceTypeId", "name", "price", "isActive"))
async def update_service_type(ctx: ToolContext, args: dict[str, Any]) -> Any:
    sid = _uuid(args["serviceTypeId"], "serviceTypeId")
    fields = {k: v for k, v in args.items() if k != "serviceTypeId"}
    if not fields:
        raise ValidationError("O‘zgartiriladigan maydon yo‘q")
    return _dump(await catalog_svc.update_service_type(ctx.session, sid, ServiceTypeUpdateIn.model_validate(fields), ctx.staff, ctx.meta), ("id", "name", "code", "price", "isActive", "turnaroundDays", "categoryId"))


@tool("create_service_type", "Xizmat qo‘shish", "Create a service type in a category, owned by the working unit (or unitId): name, base price, optional code and turnaround days.",
      params={"categoryId": {"type": "string", "req": True}, "name": {"type": "string", "req": True}, "price": {"type": "integer", "minimum": 0}, "code": {"type": "string"}, "turnaroundDays": {"type": "integer", "minimum": 0}, "unitId": {"type": "string"}},
      kind="write", perms=("admin.catalog.write",), summary_keys=("name", "price", "categoryId"))
async def create_service_type(ctx: ToolContext, args: dict[str, Any]) -> Any:
    scope = await _scope(ctx, args.get("unitId"))
    body = ServiceTypeCreateIn.model_validate({k: v for k, v in args.items() if k != "unitId"})
    return _dump(await catalog_svc.create_service_type(ctx.session, scope, body, ctx.staff, ctx.meta), ("id", "name", "code", "price", "isActive", "turnaroundDays", "categoryId"))


@tool("update_category", "Kategoriyani tahrirlash", "Rename a category, change its order or active flag.",
      params={"categoryId": {"type": "string", "req": True}, "name": {"type": "string"}, "isActive": {"type": "boolean"}, "order": {"type": "integer", "minimum": 0}},
      kind="write", perms=("admin.catalog.write",), summary_keys=("categoryId", "name", "isActive"))
async def update_category(ctx: ToolContext, args: dict[str, Any]) -> Any:
    cid = _uuid(args["categoryId"], "categoryId")
    fields = {k: v for k, v in args.items() if k != "categoryId"}
    if not fields:
        raise ValidationError("O‘zgartiriladigan maydon yo‘q")
    return _dump(await catalog_svc.update_category(ctx.session, cid, CategoryUpdateIn.model_validate(fields), ctx.staff, ctx.meta), ("id", "name", "code", "isActive", "parentId"))


@tool("set_template_status", "Andoza holati", "Activate, archive or return a template to draft.", params={"templateId": {"type": "string", "req": True}, "status": {"type": "string", "enum": ["draft", "active", "archived"], "req": True}},
      kind="write", perms=("admin.template.publish",), summary_keys=("templateId", "status"))
async def set_template_status(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return _dump(await tpl_svc.set_status(ctx.session, _uuid(args["templateId"], "templateId"), str(args["status"]), ctx.staff, ctx.meta), ("id", "name", "status", "scope"))


@tool("send_sms", "SMS yuborish", "Send an SMS text to one or more phone numbers from a facility (the working unit when it is one, else unitId). More than one recipient needs the broadcast right.",
      params={"to": {"type": "array", "items": {"type": "string"}, "req": True}, "text": {"type": "string", "req": True}, "unitId": {"type": "string"}},
      kind="write", perms=("messaging.send", "messaging.broadcast"), summary_keys=("to", "text"))
async def send_sms(ctx: ToolContext, args: dict[str, Any]) -> Any:
    scope = await _scope(ctx, args.get("unitId"))
    body = SmsSendIn(to=[str(x) for x in args["to"]], text=str(args["text"]), kind="broadcast", unit_id=None if scope.is_facility else _str(args.get("unitId")))
    sent, rejected = await messaging_svc.send(ctx.session, scope, ctx.staff, body, ctx.meta)
    return {"queued": len(sent), "rejected": rejected}



# ============================================================================= files on request (export)

#: how long a built file stays downloadable
FILE_TTL = timedelta(days=7)
PAGE = 200
REPORT_PERMS = ("reports.finance.read", "reports.finance.recent", "reports.operations.read")
ORDER_PERMS = ("reception.order.create", "reports.operations.read", "reports.finance.read", "confirm.result.read")
CATALOG_PERMS = ("admin.catalog.read", "reception.order.create", "lab.worklist.read", "reports.operations.read")
PATIENT_KEYS = ("id", "fullName", "phone", "birthDate", "gender", "address", "registeredUnitName", "discountPercent", "tags", "stats", "createdAt")


def _range(args: dict[str, Any]) -> tuple[str, str]:
    if not args.get("dateFrom") or not args.get("dateTo"):
        raise ValidationError("Bu to‘plam uchun dateFrom va dateTo (YYYY-MM-DD) kerak", code="export_range", details={"field": "params"})
    return str(args["dateFrom"]), str(args["dateTo"])


async def _paged(fetch: Callable[[int], Awaitable[Any]], limit: int) -> list[Any]:
    out: list[Any] = []
    page = 1
    while len(out) < limit:
        p = await fetch(page)
        out.extend(p.items)
        if page >= p.total_pages or not p.items:
            break
        page += 1
    return out[:limit]


async def _ds_units(ctx: ToolContext, a: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    rows = await units_svc.list_tree(ctx.session, ctx.staff, UnitTreeQuery(root_id=_str(a.get("parentId")), include_inactive=bool(a.get("includeInactive", False))))
    q, level = str(a.get("search") or "").strip().lower(), a.get("level")
    rows = [u for u in rows if (not level or u.level == level) and (not q or q in f"{u.name} {u.code or ''} {u.path}".lower())]
    return [_dump(u, (*UNIT_KEYS, "shortName", "email", "createdAt")) for u in rows[:limit]]


async def _ds_employees(ctx: ToolContext, a: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    scope = await _scope(ctx, a.get("unitId"))
    rows = await _paged(lambda page: staff_svc.list_employees(ctx.session, scope, EmployeeQuery(search=a.get("search"), role_id=_str(a.get("roleId")), status=a.get("status"), page=page, page_size=PAGE), ctx.staff), limit)
    return [_dump(e, (*EMP_KEYS, "createdAt")) for e in rows]


async def _ds_orders(ctx: ToolContext, a: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    scope = await _scope(ctx, a.get("unitId"))
    rows = await _paged(lambda page: orders_svc.list_orders(ctx.session, scope, OrderListQuery(search=a.get("search"), status=a.get("status"), payment=a.get("payment"), date_from=a.get("dateFrom"), date_to=a.get("dateTo"), patient_id=_str(a.get("patientId")), page=page, page_size=PAGE)), limit)
    return [_dump(o) for o in rows]


async def _ds_patients(ctx: ToolContext, a: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    scope = await _scope(ctx, a.get("unitId"))
    rows = await patients_svc.search_patients(ctx.session, scope, ctx.staff, str(a.get("query") or a.get("search") or ""), limit)
    return [_dump(p, PATIENT_KEYS) for p in rows]


async def _ds_service_types(ctx: ToolContext, a: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    scope = await _scope(ctx, a.get("unitId"))
    rows = await catalog_svc.list_service_types(ctx.session, scope, ServiceTypeQuery(search=a.get("search"), category_id=_str(a.get("categoryId")), active_only=bool(a.get("activeOnly", False)), unit_id=str(scope.id)))
    return [_dump(r) for r in rows[:limit]]


async def _ds_categories(ctx: ToolContext, a: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    rows = await catalog_svc.list_categories(ctx.session, await _scope(ctx, a.get("unitId")))
    return [_dump(c, ("id", "name", "code", "parentId", "isActive", "workflow", "ownerUnitId")) for c in rows[:limit]]


async def _ds_templates(ctx: ToolContext, a: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    scope = await _scope(ctx, a.get("unitId"))
    rows = await tpl_svc.list_templates(ctx.session, scope, TemplateQuery(status=a.get("status"), search=a.get("search"), unit_id=str(scope.id)))
    return [_dump(t, ("id", "name", "status", "scope", "language", "serviceTypeIds", "categoryIds", "unitIds", "ownerUnitId", "usage", "updatedAt")) for t in rows[:limit]]


async def _ds_roles(ctx: ToolContext, a: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    rows = await staff_svc.list_roles(ctx.session, await _scope(ctx, a.get("unitId")))
    return [_dump(r, ("id", "key", "name", "scopeUnitId", "isSystem", "permissions")) for r in rows[:limit]]


async def _ds_breakdown(ctx: ToolContext, a: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    d_from, d_to = _range(a)
    by = a.get("by") or "service"
    rows = await reports_svc.breakdown(ctx.session, await _scope(ctx, a.get("unitId")), BreakdownQuery(date_from=d_from, date_to=d_to, by=by), ctx.staff)
    return [_dump(r) for r in rows[:limit]]


async def _ds_daily(ctx: ToolContext, a: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    d_from, d_to = _range(a)
    d = _dump(await reports_svc.dashboard(ctx.session, await _scope(ctx, a.get("unitId")), RangeQuery(date_from=d_from, date_to=d_to), ctx.staff))
    return list(d.get("trend") or [])[:limit] if isinstance(d, dict) else []


async def _ds_results(ctx: ToolContext, a: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    d_from, d_to = _range(a)
    scope = await _scope(ctx, a.get("unitId"))
    rows = await _paged(lambda page: reports_svc.results_list(ctx.session, scope, ResultListQuery(date_from=d_from, date_to=d_to, status=a.get("status") or "not_received", page=page, page_size=PAGE), ctx.staff), limit)
    return [_dump(r) for r in rows]


@dataclass(frozen=True)
class Dataset:
    title: str
    perms: tuple[str, ...]
    params: str
    load: Callable[[ToolContext, dict[str, Any], int], Awaitable[list[dict[str, Any]]]]


DATASETS: dict[str, Dataset] = {
    "units": Dataset("Birliklar", (), "parentId, search, level (country|region|district|facility), includeInactive", _ds_units),
    "employees": Dataset("Xodimlar", ("admin.employee.read",), "unitId, search, roleId, status (active|inactive)", _ds_employees),
    "orders": Dataset("Cheklar", ORDER_PERMS, "unitId, search, status, payment, dateFrom, dateTo (YYYY-MM-DD), patientId", _ds_orders),
    "patients": Dataset("Bemorlar", ("reception.patient.read",), "unitId, query (name / phone / passport; empty = recent visitors)", _ds_patients),
    "service_types": Dataset("Xizmatlar", CATALOG_PERMS, "unitId, search, categoryId, activeOnly", _ds_service_types),
    "categories": Dataset("Kategoriyalar", CATALOG_PERMS, "unitId", _ds_categories),
    "templates": Dataset("Andozalar", ("admin.template.read",), "unitId, status (draft|active|archived), search", _ds_templates),
    "roles": Dataset("Rollar", ("admin.employee.read", "admin.role.write"), "unitId", _ds_roles),
    "breakdown": Dataset("Taqsimot", REPORT_PERMS, "unitId, dateFrom*, dateTo*, by (category|service|unit|employee)", _ds_breakdown),
    "daily": Dataset("Kunlik ko‘rsatkichlar", REPORT_PERMS, "unitId, dateFrom*, dateTo*", _ds_daily),
    "results": Dataset("Natijalar", REPORT_PERMS, "unitId, dateFrom*, dateTo*, status (not_received|received|waiting)", _ds_results),
}

FILTER_SCHEMA = {"type": "array", "description": "AND-ed row filters applied after loading: {column, op, value}; value is a list for in / notIn / between", "items": {"type": "object", "properties": {"column": {"type": "string"}, "op": {"type": "string", "enum": list(xp.OPS)}, "value": {"description": "string | number | boolean | list"}}, "required": ["column", "op"]}}
SORT_SCHEMA = {"type": "array", "description": "sort order, first entry wins: {column, dir}", "items": {"type": "object", "properties": {"column": {"type": "string"}, "dir": {"type": "string", "enum": ["asc", "desc"]}}, "required": ["column"]}}
COLUMNS_SCHEMA = {"type": "array", "description": "columns of the file in order: {key, title} (title in the user's language) or just the key; omitted = every column except ids", "items": {"type": "object", "properties": {"key": {"type": "string"}, "title": {"type": "string"}}, "required": ["key"]}}
FORMAT_SCHEMA = {"type": "string", "enum": list(xp.FORMATS), "description": "default xlsx (Excel); pdf / docx = a printable table, md / txt = text"}


async def _store_file(ctx: ToolContext, *, name: str, content_type: str, data: bytes, rows: int, meta: dict[str, Any]) -> dict[str, Any]:
    if ctx.thread_id is None:
        raise StateError("Fayl faqat suhbat ichida yaratiladi", code="export_no_thread")
    f = AssistantFile(id=uuid7(), thread_id=ctx.thread_id, employee_id=ctx.staff.id, tool_call_id=ctx.call_id, name=name, content_type=content_type, size=len(data), rows=rows, data=data, meta=meta, expires_at=utcnow() + FILE_TTL)
    ctx.session.add(f)
    await ctx.session.flush()
    return {"id": str(f.id), "name": f.name, "contentType": f.content_type, "size": f.size, "rows": rows, "expiresAt": f.expires_at.isoformat()}


def _export_result(file: dict[str, Any], columns: list[xp.Column], rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "file": file,
        "rowCount": len(rows),
        "columns": [{"key": c.key, "title": c.title} for c in columns],
        "preview": [{c.key: r.get(c.key) for c in columns} for r in rows[:5]],
        "note": "Fayl chatda yuklab olish tugmasi bilan ko‘rsatiladi — foydalanuvchiga qisqa ayt, jadvalni qayta yozma.",
    }


@tool("export_data", "Faylga eksport (ma’lumotlar to‘plami)",
      "Build a downloadable Excel / CSV / JSON file from one of the system's datasets, with any row filters, sort order, column choice and limit the user asked for (sorting and filtering happen on the server over the FULL dataset, not only what you have seen). Datasets and their source params: "
      + "; ".join(f"{k} ({d.title}): {d.params}" for k, d in DATASETS.items())
      + ". Column keys are the same camelCase keys the list_* tools return (nested as a.b). Unknown column → the error lists the available ones. The file appears in the chat with a download button.",
      params={"dataset": {"type": "string", "enum": list(DATASETS), "req": True}, "params": {"type": "object", "description": "the dataset's source params (see the list above)", "additionalProperties": True},
              "format": FORMAT_SCHEMA, "title": {"type": "string", "description": "sheet / file caption in the user's language"}, "fileName": {"type": "string"}, "note": {"type": "string", "description": "one line under the title (pdf / docx): period, filters as the user said them"},
              "columns": COLUMNS_SCHEMA, "filters": FILTER_SCHEMA, "sort": SORT_SCHEMA, "limit": {"type": "integer", "minimum": 1, "maximum": xp.MAX_ROWS}},
      perms=("reports.export",), summary_keys=("dataset", "format"))
async def export_data(ctx: ToolContext, args: dict[str, Any]) -> Any:
    name = str(args.get("dataset") or "")
    ds = DATASETS.get(name)
    if ds is None:
        raise ValidationError(f"Noma’lum to‘plam: {name}", code="export_dataset", details={"datasets": list(DATASETS)})
    if ds.perms and not ctx.staff.has(*ds.perms):
        raise ValidationError(f"«{ds.title}» to‘plamini eksport qilish huquqi yo‘q", code="forbidden")
    limit = max(1, min(int(args.get("limit") or xp.MAX_ROWS), xp.MAX_ROWS))
    source = dict(args.get("params") or {})
    raw = await ds.load(ctx, source, xp.MAX_ROWS)
    columns, rows = xp.prepare(raw, filters=args.get("filters"), sort=args.get("sort"), columns=args.get("columns"), limit=limit)
    fmt = xp.norm_format(args.get("format"))
    title = str(args.get("title") or ds.title)
    data, content_type = xp.build_file(fmt, title, columns, rows, note=args.get("note"))
    file = await _store_file(ctx, name=xp.file_name(args.get("fileName"), title, fmt, utcnow().date().isoformat()), content_type=content_type, data=data, rows=len(rows),
                             meta={"dataset": name, "params": _clean(source), "filters": args.get("filters"), "sort": args.get("sort"), "columns": [c.key for c in columns], "format": fmt})
    return _export_result(file, columns, rows)


@tool("export_table", "Faylga eksport (tayyor jadval)",
      "Build a downloadable Excel / CSV / JSON file from rows YOU composed (a table you already computed or summarised, an answer the user wants as a file). Rows are objects keyed by column key, or arrays in column order. Optional filters / sort apply as in export_data. Prefer export_data when the rows come straight from a dataset.",
      params={"title": {"type": "string", "req": True}, "columns": {**COLUMNS_SCHEMA, "req": True}, "rows": {"type": "array", "items": {}, "req": True}, "format": FORMAT_SCHEMA, "fileName": {"type": "string"}, "note": {"type": "string"}, "filters": FILTER_SCHEMA, "sort": SORT_SCHEMA},
      perms=("reports.export",), summary_keys=("title", "format"))
async def export_table(ctx: ToolContext, args: dict[str, Any]) -> Any:
    cols_in = list(args.get("columns") or [])
    keys = [c["key"] if isinstance(c, dict) else str(c) for c in cols_in]
    if not keys:
        raise ValidationError("Ustunlar kerak", code="export_column")
    raw: list[dict[str, Any]] = []
    for r in list(args.get("rows") or [])[: xp.MAX_ROWS]:
        if isinstance(r, dict):
            raw.append(_clean(r))
        elif isinstance(r, list):
            raw.append({k: (r[i] if i < len(r) else None) for i, k in enumerate(keys)})
    columns, rows = xp.prepare(raw, filters=args.get("filters"), sort=args.get("sort"), columns=cols_in, limit=xp.MAX_ROWS)
    fmt = xp.norm_format(args.get("format"))
    title = str(args.get("title") or "Jadval")
    data, content_type = xp.build_file(fmt, title, columns, rows, note=args.get("note"))
    file = await _store_file(ctx, name=xp.file_name(args.get("fileName"), title, fmt, utcnow().date().isoformat()), content_type=content_type, data=data, rows=len(rows), meta={"table": title, "columns": keys, "format": fmt})
    return _export_result(file, columns, rows)



@tool("export_document", "Hujjat tayyorlash (Word / PDF / matn)",
      "Build a downloadable DOCUMENT from text YOU wrote in Markdown — a report, a letter, a notice, a summary, meeting notes, instructions. Use # headings, paragraphs, - bullets, 1. numbered lists, | tables | and **bold**; the server lays it out as Word (docx), PDF, Markdown or plain text. Write the content in the user's language, complete and ready to print. For tables of system data prefer export_data.",
      params={"title": {"type": "string", "req": True}, "content": {"type": "string", "description": "the document body in Markdown", "req": True}, "format": {"type": "string", "enum": list(xp.DOC_FORMATS), "description": "default docx (Word)"}, "fileName": {"type": "string"}},
      perms=("reports.export",), summary_keys=("title", "format"))
async def export_document(ctx: ToolContext, args: dict[str, Any]) -> Any:
    title = str(args.get("title") or "Hujjat").strip()
    content = str(args.get("content") or "")
    if not content.strip():
        raise ValidationError("Hujjat matni bo‘sh", code="export_document")
    fmt = xp.norm_format(args.get("format"), "docx")
    data, content_type = xp.build_document(fmt, title, content)
    blocks = rd.body_blocks(title, content)
    file = await _store_file(ctx, name=xp.file_name(args.get("fileName"), title, fmt, utcnow().date().isoformat()), content_type=content_type, data=data, rows=None, meta={"document": title, "format": fmt, "chars": len(content)})
    return {"file": file, "sections": [b.text for b in blocks if b.type == "h"][:30], "words": len(content.split()), "note": "Hujjat chatda yuklab olish tugmasi bilan ko‘rsatiladi — mazmunini qayta yozma, qisqa ayt."}


CHART_SERIES_SCHEMA = {"type": "array", "description": "the data lines / bar groups: {name, values[]} aligned with labels (pie: the first series only)", "items": {"type": "object", "properties": {"name": {"type": "string"}, "values": {"type": "array", "items": {}}}, "required": ["values"]}}


@tool("export_chart", "Diagramma (rasm)",
      "Build a chart picture — bar, line or pie — as PNG (default) or SVG. EITHER give the data yourself (labels + series) OR point at a dataset (dataset + params + x = the label column + y = the value column(s), with optional filters / sort / limit as in export_data); the server loads, filters, sorts and draws. Keep the series on one scale (revenue and counts in one chart hide each other). The picture appears in the chat with a preview and a download button.",
      params={"title": {"type": "string", "req": True}, "type": {"type": "string", "enum": ["bar", "line", "pie"], "req": True}, "format": {"type": "string", "enum": list(xp.CHART_FORMATS), "description": "default png"},
              "labels": {"type": "array", "items": {"type": "string"}}, "series": CHART_SERIES_SCHEMA,
              "dataset": {"type": "string", "enum": list(DATASETS)}, "params": {"type": "object", "additionalProperties": True}, "x": {"type": "string", "description": "dataset mode: the column used for labels"}, "y": {"type": "array", "items": {"type": "string"}, "description": "dataset mode: the numeric column(s) drawn"},
              "filters": FILTER_SCHEMA, "sort": SORT_SCHEMA, "limit": {"type": "integer", "minimum": 1, "maximum": 60, "description": "dataset mode: at most this many labels (default 20)"},
              "xTitle": {"type": "string"}, "yTitle": {"type": "string"}, "fileName": {"type": "string"}},
      perms=("reports.export",), summary_keys=("title", "type"))
async def export_chart(ctx: ToolContext, args: dict[str, Any]) -> Any:
    title = str(args.get("title") or "Diagramma").strip()
    kind = str(args.get("type") or "bar")
    labels: list[str]
    series: list[dict[str, Any]]
    meta: dict[str, Any] = {"chart": title, "type": kind}
    if args.get("dataset"):
        name = str(args["dataset"])
        ds = DATASETS.get(name)
        if ds is None:
            raise ValidationError(f"Noma’lum to‘plam: {name}", code="export_dataset", details={"datasets": list(DATASETS)})
        if ds.perms and not ctx.staff.has(*ds.perms):
            raise ValidationError(f"«{ds.title}» to‘plamini o‘qish huquqi yo‘q", code="forbidden")
        x, ys = str(args.get("x") or ""), [str(c) for c in (args.get("y") or [])]
        if not x or not ys:
            raise ValidationError("Dataset rejimida x (yorliq ustuni) va y (qiymat ustunlari) kerak", code="chart_data")
        raw = await ds.load(ctx, dict(args.get("params") or {}), xp.MAX_ROWS)
        columns, rows = xp.prepare(raw, filters=args.get("filters"), sort=args.get("sort"), columns=[x, *ys], limit=min(int(args.get("limit") or 20), 60))
        labels = [str(r.get(x) if r.get(x) is not None else "") for r in rows]
        series = [{"name": c.title, "values": [r.get(c.key) for r in rows]} for c in columns[1:]]
        meta.update({"dataset": name, "x": x, "y": ys, "rows": len(rows)})
    else:
        labels = [str(v) for v in (args.get("labels") or [])]
        series = [dict(s) for s in (args.get("series") or []) if isinstance(s, dict)]
    spec = rd.chart_spec({"type": kind, "title": title, "labels": labels, "series": series, "xTitle": args.get("xTitle"), "yTitle": args.get("yTitle")})
    fmt = xp.norm_format(args.get("format"), "png")
    data, content_type = xp.build_chart(fmt, spec)
    file = await _store_file(ctx, name=xp.file_name(args.get("fileName"), title, fmt, utcnow().date().isoformat()), content_type=content_type, data=data, rows=len(spec.labels), meta=meta)
    return {"file": file, "labels": spec.labels, "series": [{"name": s.name, "values": s.values} for s in spec.series], "note": "Rasm chatda ko‘rinadi — raqamlarni qisqa sharhla, hammasini qayta yozma."}


async def run_tool(t: Tool, ctx: ToolContext, args: dict[str, Any]) -> Any:
    return await t.handler(ctx, dict(args or {}))
