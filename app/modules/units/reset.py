"""Superadmin reset — bring one organisation unit back to the state it had right after it was created ("like
newborn") without touching anything outside its subtree. Irreversible; superadmin only; previewed; confirmed by
typing the facility code (or the unit name); audited with `unit_id` = the unit being reset.

A reset of unit X removes rows under X's subtree (`app.core.scope.descendant_ids`). It is split into parts the
superadmin picks; their dependencies are added automatically. The dependencies follow the foreign keys of the
schema: a cheque names the employee who wrote it, a document names its template — so those go together.

facility   orders     the facility's cheques, their services, payments, result documents (+ stored PDFs), SMS /
                      Telegram outbox rows and in-app notifications; cheque numbering restarts from 1; the visit
                      statistics of patients who also visit other facilities are recomputed
           patients   patients who belong to this facility only (registered here, no cheque elsewhere) with their
                      portal / Telegram sessions, links and SMS codes; patients registered here who visit other
                      facilities are re-registered at the facility of their first other cheque        ⇒ orders
           templates  templates owned by the facility or bound to it only (+ their assets and images); shared
                      templates just lose this facility                                                  ⇒ orders
           staff      employees of this facility only (superadmins and the caller are kept); employees who also
                      serve elsewhere move there; roles scoped to the facility nobody uses any more       ⇒ orders
           prices     the facility's price / availability overrides (`service_unit_settings`)
unit       orders     every cheque under the unit (+ services, payments, documents, PDFs, outbox, notifications);
(country,             the numbering of every facility under it restarts from 1
region,    patients   patients registered under the unit with no cheque outside it (+ sessions, links, codes);
district)             the others are re-registered where their first outside cheque was written        ⇒ orders
           templates  templates and assets owned inside the subtree; bindings of outside templates to units of
                      the subtree are dropped                                                            ⇒ orders
           catalog    categories, services and result schemas owned inside the subtree          ⇒ templates, orders
           staff      employees homed inside the subtree except superadmins and the caller (those who also serve
                      outside move there); roles scoped inside the subtree nobody uses any more          ⇒ orders
           units      the child units of the subtree — soft-deleted, never the unit itself; their employees and
                      roles move up to the unit, their own catalogue / templates / messaging settings go
                                                                                                ⇒ orders, patients

The unit row itself, its profile and settings, the reference data and the audit log stay. Operational rows are
hard-deleted inside the request's single transaction (all or nothing): cheque numbers are unique per facility, so
restarting the numbering needs the old cheques really gone.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import Select, and_, delete, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

from app.api.deps import RequestMeta, StaffPrincipal, invalidate_principal_cache, invalidate_session_cache
from app.core.audit import audit
from app.core.exceptions import ForbiddenError, ValidationError
from app.core.scope import descendant_ids, invalidate_scope_cache
from app.core.timeutil import utcnow
from app.infrastructure.db.base import alive
from app.infrastructure.db.models import (
    AttributeSchema,
    Category,
    Employee,
    EmployeeUnit,
    MessagingAccount,
    Notification,
    Order,
    OrderCounter,
    OrderItem,
    OrgUnit,
    OtpChallenge,
    OutboxMessage,
    Patient,
    Payment,
    ResultDocument,
    ResultTemplate,
    Role,
    ServiceType,
    ServiceUnitSetting,
    SmsTemplate,
    StoredFile,
    TelegramLink,
    TemplateAsset,
    TemplateUnit,
)
from app.infrastructure.db.models import Session as SessionRow
from app.infrastructure.redis import cache

FACILITY_PARTS: tuple[str, ...] = ("orders", "patients", "templates", "staff", "prices")
UNIT_PARTS: tuple[str, ...] = ("orders", "patients", "templates", "catalog", "staff", "units")
FACILITY_REQUIRES: dict[str, tuple[str, ...]] = {"patients": ("orders",), "templates": ("orders",), "staff": ("orders",)}
UNIT_REQUIRES: dict[str, tuple[str, ...]] = {
    "patients": ("orders",),
    "templates": ("orders",),
    "catalog": ("templates", "orders"),
    "staff": ("orders",),
    "units": ("orders", "patients"),
}

CONFIRM_MISMATCH = "Tasdiqlash so‘zi noto‘g‘ri"

Pred = tuple[Any, ColumnElement[bool]]


def parts_for(unit: OrgUnit) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
    """The parts a reset of this unit offers and what each part pulls in."""
    if unit.level == "facility":
        return FACILITY_PARTS, FACILITY_REQUIRES
    return UNIT_PARTS, UNIT_REQUIRES


def confirm_word(unit: OrgUnit) -> str:
    """What the superadmin types to confirm: the facility code, else the unit name."""
    if unit.level == "facility" and unit.code:
        return unit.code
    return unit.name


def close_parts(parts: Iterable[str], allowed: tuple[str, ...], requires: dict[str, tuple[str, ...]]) -> list[str]:
    """Selected parts + everything they require, in execution order; unknown or no parts → 422."""
    wanted = set(parts)
    unknown = wanted - set(allowed)
    if unknown:
        raise ValidationError(f"Noma’lum bo‘lim: {', '.join(sorted(unknown))}", code="parts")
    if not wanted:
        raise ValidationError("Kamida bitta bo‘limni tanlang", code="parts")
    grew = True
    while grew:
        grew = False
        for p in list(wanted):
            for r in requires.get(p, ()):
                if r not in wanted:
                    wanted.add(r)
                    grew = True
    return [p for p in allowed if p in wanted]


def require_superadmin(staff: StaffPrincipal) -> None:
    if not staff.is_super_admin:
        raise ForbiddenError("Faqat superadmin uchun")


# --------------------------------------------------------------------------------------------- SQL helpers


async def _count(session: AsyncSession, model: Any, where: ColumnElement[bool]) -> int:
    return int((await session.execute(select(func.count()).select_from(model).where(where))).scalar_one())


async def _run(session: AsyncSession, stmt: Any) -> int:
    res = await session.execute(stmt.execution_options(synchronize_session=False))
    return int(res.rowcount or 0)  # type: ignore[attr-defined]


async def _ids(session: AsyncSession, stmt: Select) -> list[uuid.UUID]:
    return [x for x in (await session.execute(stmt)).scalars() if x is not None]


async def _drop_sessions(session: AsyncSession, actor: str, subjects: Sequence[uuid.UUID]) -> int:
    """Delete login sessions of removed employees / patients and mark their tokens revoked in Redis right away."""
    if not subjects:
        return 0
    jtis = list((await session.execute(select(SessionRow.id).where(SessionRow.actor == actor, SessionRow.subject_id.in_(subjects)))).scalars())
    if not jtis:
        return 0
    await _run(session, delete(SessionRow).where(SessionRow.id.in_(jtis)))
    for jti in jtis:
        await invalidate_session_cache(jti)
    return len(jtis)


async def _recompute_patient_stats(session: AsyncSession, patient_ids: list[uuid.UUID]) -> None:
    """Visit statistics from the cheques that remain (cheques opened, Σ paid, last visit)."""
    if not patient_ids:
        return
    alive_orders = and_(Order.patient_id == Patient.id, Order.deleted_at.is_(None))
    await _run(
        session,
        update(Patient)
        .where(Patient.id.in_(patient_ids))
        .values(
            stats_orders=select(func.count()).select_from(Order).where(alive_orders).scalar_subquery(),
            stats_total_spent=select(func.coalesce(func.sum(Order.paid_amount), 0)).where(alive_orders).scalar_subquery(),
            stats_last_visit_at=select(func.max(Order.created_at)).where(alive_orders).scalar_subquery(),
        ),
    )


async def _scope_ids(session: AsyncSession, unit: OrgUnit) -> list[uuid.UUID]:
    """The units a reset of `unit` covers: the facility itself, or the whole subtree (deleted units included —
    their history still belongs here)."""
    if unit.level == "facility":
        return [unit.id]
    ids = await descendant_ids(session, unit.id)
    return ids if unit.id in ids else [unit.id, *ids]


async def _invalidate() -> None:
    """Scopes, unit cards, catalogue / template caches (`unit:*`) and every principal."""
    await invalidate_scope_cache()
    await cache.delete_prefix("unit:")
    await invalidate_principal_cache()


# --------------------------------------------------------------------------------------------- predicates


def _preds(unit: OrgUnit, scope: list[uuid.UUID], staff: StaffPrincipal) -> dict[str, Pred]:
    """(model, predicate) per counted item — shared by the preview and the reset itself. `scope` = the unit ids the
    reset covers. "Outside" predicates look at rows that stay, so the preview (computed while the unit's own rows
    still exist) and the reset (run after the `orders` part) agree."""
    is_facility = unit.level == "facility"
    orders = select(Order.id).where(Order.unit_id.in_(scope))
    docs = select(ResultDocument.id).where(ResultDocument.order_id.in_(orders))

    # patients: ours when registered here (or unregistered but seen only here) and never seen elsewhere
    seen_here = exists().where(Order.patient_id == Patient.id, Order.unit_id.in_(scope))
    seen_elsewhere = exists().where(Order.patient_id == Patient.id, Order.unit_id.not_in(scope))
    registered_here = Patient.registered_unit_id.in_(scope)

    # templates: the facility's own + bound to it only; a higher unit: owned inside the subtree
    bound_in = exists().where(TemplateUnit.template_id == ResultTemplate.id, TemplateUnit.unit_id.in_(scope))
    bound_out = exists().where(TemplateUnit.template_id == ResultTemplate.id, TemplateUnit.unit_id.not_in(scope))
    owned_in = ResultTemplate.owner_unit_id.in_(scope)
    # a document written outside the scope still needs its template (hard FK) — such a template is never ours
    used_outside = exists().where(ResultDocument.template_id == ResultTemplate.id, ResultDocument.unit_id.not_in(scope))
    if is_facility:
        templates = and_(or_(owned_in, and_(bound_in, ~bound_out)), ~used_outside)
        templates_unbound = and_(~owned_in, bound_in, bound_out)
    else:
        templates = and_(owned_in, ~used_outside)
        templates_unbound = and_(~owned_in, bound_in)
    assets = TemplateAsset.owner_unit_id.in_(scope)

    # staff: homed inside; superadmins and the caller stay; who also serves outside moves there instead
    keep = or_(Employee.is_super_admin.is_(True), Employee.id == staff.id)
    extra_out = exists().where(EmployeeUnit.employee_id == Employee.id, EmployeeUnit.unit_id.not_in(scope))
    worked_outside = or_(
        exists().where(Order.created_by_employee_id == Employee.id, Order.unit_id.not_in(scope)),
        exists().where(Payment.employee_id == Employee.id, Payment.unit_id.not_in(scope)),
        exists().where(or_(OrderItem.technician_id == Employee.id, OrderItem.doctor_id == Employee.id), OrderItem.unit_id.not_in(scope)),
    )
    homed_in = and_(Employee.unit_id.in_(scope), ~keep)
    employees = and_(homed_in, ~extra_out, ~worked_outside)
    employees_moved = and_(homed_in, extra_out)
    removed = select(Employee.id).where(employees)
    still_used = select(Employee.role_id).where(Employee.id.not_in(removed))

    children = and_(OrgUnit.id.in_(scope), OrgUnit.id != unit.id, alive(OrgUnit))
    child_ids = select(OrgUnit.id).where(OrgUnit.id.in_(scope), OrgUnit.id != unit.id)
    return {
        "orders": (Order, Order.unit_id.in_(scope)),
        "items": (OrderItem, OrderItem.order_id.in_(orders)),
        "payments": (Payment, Payment.order_id.in_(orders)),
        "documents": (ResultDocument, ResultDocument.order_id.in_(orders)),
        "messages": (OutboxMessage, or_(OutboxMessage.unit_id.in_(scope), OutboxMessage.order_id.in_(orders), OutboxMessage.document_id.in_(docs))),
        "notifications": (Notification, Notification.unit_id.in_(scope)),
        "patients": (Patient, and_(or_(registered_here, and_(Patient.registered_unit_id.is_(None), seen_here)), ~seen_elsewhere)),
        "patientsMoved": (Patient, and_(registered_here, seen_elsewhere)),
        "templates": (ResultTemplate, templates),
        "templatesUnbound": (ResultTemplate, templates_unbound),
        "assets": (TemplateAsset, assets),
        "categories": (Category, Category.owner_unit_id.in_(scope)),
        "services": (ServiceType, ServiceType.owner_unit_id.in_(scope)),
        "schemas": (AttributeSchema, AttributeSchema.owner_unit_id.in_(scope)),
        "employees": (Employee, employees),
        "employeesMoved": (Employee, employees_moved),
        "employeesUnbound": (EmployeeUnit, and_(EmployeeUnit.unit_id.in_(scope), EmployeeUnit.employee_id.not_in(removed))),
        "roles": (Role, and_(Role.scope_unit_id.in_(scope), Role.id.not_in(still_used))),
        "prices": (ServiceUnitSetting, ServiceUnitSetting.unit_id == unit.id),
        "units": (OrgUnit, children),
        "facilities": (OrgUnit, and_(children, OrgUnit.level == "facility")),
        "unitEmployees": (Employee, Employee.unit_id.in_(child_ids)),
        "unitCatalog": (ServiceType, ServiceType.owner_unit_id.in_(child_ids)),
        "unitTemplates": (ResultTemplate, ResultTemplate.owner_unit_id.in_(child_ids)),
    }


FACILITY_COUNTS: dict[str, tuple[str, ...]] = {
    "orders": ("orders", "items", "payments", "documents", "messages", "notifications"),
    "patients": ("patients", "patientsMoved"),
    "templates": ("templates", "templatesUnbound", "assets"),
    "staff": ("employees", "employeesMoved", "employeesUnbound", "roles"),
    "prices": ("prices",),
}
UNIT_COUNTS: dict[str, tuple[str, ...]] = {
    "orders": ("orders", "items", "payments", "documents", "messages", "notifications"),
    "patients": ("patients", "patientsMoved"),
    "templates": ("templates", "templatesUnbound", "assets"),
    "catalog": ("categories", "services", "schemas"),
    "staff": ("employees", "employeesMoved", "employeesUnbound", "roles"),
    "units": ("units", "facilities", "unitEmployees", "unitCatalog", "unitTemplates"),
}


async def preview(session: AsyncSession, unit: OrgUnit, staff: StaffPrincipal) -> dict[str, dict[str, int]]:
    """What each part would remove — counts only, nothing is changed."""
    require_superadmin(staff)
    scope = await _scope_ids(session, unit)
    preds = _preds(unit, scope, staff)
    table = FACILITY_COUNTS if unit.level == "facility" else UNIT_COUNTS
    return {part: {k: await _count(session, *preds[k]) for k in keys} for part, keys in table.items()}


# --------------------------------------------------------------------------------------------- pieces


async def _drop_orders(session: AsyncSession, p: dict[str, Pred], scope: list[uuid.UUID], n: dict[str, int]) -> list[uuid.UUID]:
    """Cheques and everything hanging off them, in foreign-key order; numbering restarts. Returns the patients touched."""
    touched = await _ids(session, select(Order.patient_id).where(p["orders"][1]).distinct())
    pdf_files = await _ids(session, select(ResultDocument.pdf_file_id).where(p["documents"][1], ResultDocument.pdf_file_id.is_not(None)))
    n["messages"] = await _run(session, delete(OutboxMessage).where(p["messages"][1]))
    n["notifications"] = await _run(session, delete(Notification).where(p["notifications"][1]))
    # items ↔ documents reference each other: break the cycle, then documents, their PDFs, payments, items, cheques
    await _run(session, update(OrderItem).where(p["items"][1]).values(document_id=None))
    n["documents"] = await _run(session, delete(ResultDocument).where(p["documents"][1]))
    n["documentFiles"] = await _run(session, delete(StoredFile).where(StoredFile.id.in_(pdf_files))) if pdf_files else 0
    n["payments"] = await _run(session, delete(Payment).where(p["payments"][1]))
    n["items"] = await _run(session, delete(OrderItem).where(p["items"][1]))
    n["orders"] = await _run(session, delete(Order).where(p["orders"][1]))
    await _run(session, update(OrderCounter).where(OrderCounter.unit_id.in_(scope)).values(seq=0))
    return touched


async def _drop_patients(session: AsyncSession, doomed: list[uuid.UUID], n: dict[str, int]) -> None:
    """The patients decided on before the cheques went (+ their sessions, Telegram links, SMS codes, outbox rows)."""
    n["patients"] = 0
    if not doomed:
        return
    n["patientSessions"] = await _drop_sessions(session, "patient", doomed)
    n["telegramLinks"] = await _run(session, delete(TelegramLink).where(TelegramLink.patient_id.in_(doomed)))
    await _run(session, delete(OutboxMessage).where(OutboxMessage.patient_id.in_(doomed)))
    await _run(session, delete(OtpChallenge).where(OtpChallenge.meta["patientId"].astext.in_([str(x) for x in doomed])))
    n["patients"] = await _run(session, delete(Patient).where(Patient.id.in_(doomed)))


async def _drop_templates(session: AsyncSession, template_pred: ColumnElement[bool], asset_pred: ColumnElement[bool]) -> dict[str, int]:
    """Templates (bindings cascade), their assets and the asset images nothing else uses, in foreign-key order."""
    gone_templates = select(ResultTemplate.id).where(template_pred)
    gone_assets = select(TemplateAsset.id).where(asset_pred)
    asset_files = await _ids(session, select(TemplateAsset.file_id).where(asset_pred))
    await _run(session, update(ServiceType).where(ServiceType.default_template_id.in_(gone_templates)).values(default_template_id=None))
    await _run(session, update(Employee).where(Employee.signature_asset_id.in_(gone_assets)).values(signature_asset_id=None))
    out = {
        "templates": await _run(session, delete(ResultTemplate).where(template_pred)),
        "assets": await _run(session, delete(TemplateAsset).where(asset_pred)),
        "assetFiles": 0,
    }
    if asset_files:
        # the same image may serve as a unit logo or as an asset of a unit outside the scope (deduplicated storage)
        in_use = or_(
            exists().where(OrgUnit.logo_file_id == StoredFile.id),
            exists().where(TemplateAsset.file_id == StoredFile.id),
            exists().where(ResultDocument.pdf_file_id == StoredFile.id),
        )
        out["assetFiles"] = await _run(session, delete(StoredFile).where(StoredFile.id.in_(asset_files), ~in_use))
    return out


async def _drop_catalog(session: AsyncSession, owner_ids: Sequence[uuid.UUID]) -> dict[str, int]:
    """Services (their unit settings cascade), schemas, categories owned by `owner_ids`, in foreign-key order."""
    out = {"services": await _run(session, delete(ServiceType).where(ServiceType.owner_unit_id.in_(owner_ids)))}
    out["schemas"] = await _run(session, delete(AttributeSchema).where(AttributeSchema.owner_unit_id.in_(owner_ids)))
    out["categories"] = await _run(session, delete(Category).where(Category.owner_unit_id.in_(owner_ids)))
    await _run(session, update(Employee).where(Employee.unit_id.in_(owner_ids)).values(category_ids=[]))
    return out


async def _drop_staff(session: AsyncSession, p: dict[str, Pred], scope: list[uuid.UUID], n: dict[str, int]) -> None:
    """Employees of the scope (sessions, notifications, signature links), re-homing those who also serve outside,
    then the roles scoped here that nobody uses any more."""
    removed = await _ids(session, select(Employee.id).where(p["employees"][1]))
    n["employees"] = 0
    if removed:
        n["staffSessions"] = await _drop_sessions(session, "staff", removed)
        await _run(session, delete(Notification).where(Notification.employee_id.in_(removed)))
        await _run(session, update(TemplateAsset).where(TemplateAsset.employee_id.in_(removed)).values(employee_id=None))
        n["employees"] = await _run(session, delete(Employee).where(Employee.id.in_(removed)))
    # who also serves outside the scope: their home becomes one of those units
    first_outside = (
        select(EmployeeUnit.unit_id)
        .where(EmployeeUnit.employee_id == Employee.id, EmployeeUnit.unit_id.not_in(scope))
        .order_by(EmployeeUnit.unit_id)
        .limit(1)
        .scalar_subquery()
    )
    n["employeesMoved"] = await _run(session, update(Employee).where(p["employeesMoved"][1]).values(unit_id=first_outside))
    n["employeesUnbound"] = await _run(session, delete(EmployeeUnit).where(EmployeeUnit.unit_id.in_(scope)))
    await _run(session, delete(EmployeeUnit).where(exists().where(Employee.id == EmployeeUnit.employee_id, Employee.unit_id == EmployeeUnit.unit_id)))
    n["roles"] = await _run(session, delete(Role).where(p["roles"][1]))


async def _drop_units(session: AsyncSession, unit: OrgUnit, p: dict[str, Pred], staff: StaffPrincipal, n: dict[str, int]) -> None:
    """Soft-delete the child units: people and roles move up to the unit; the children's own catalogue, templates,
    overrides and messaging settings go (nothing outside the subtree references them)."""
    children = await _ids(session, select(OrgUnit.id).where(p["units"][1]))
    n["units"] = len(children)
    if not children:
        return
    n["unitEmployees"] = await _run(session, update(Employee).where(Employee.unit_id.in_(children)).values(unit_id=unit.id))
    await _run(session, delete(EmployeeUnit).where(EmployeeUnit.unit_id.in_(children)))
    await _run(session, delete(EmployeeUnit).where(exists().where(Employee.id == EmployeeUnit.employee_id, Employee.unit_id == EmployeeUnit.unit_id)))
    await _run(session, update(Role).where(Role.scope_unit_id.in_(children)).values(scope_unit_id=unit.id))
    tpl = await _drop_templates(session, ResultTemplate.owner_unit_id.in_(children), TemplateAsset.owner_unit_id.in_(children))
    cat = await _drop_catalog(session, children)
    n["unitTemplates"] = tpl["templates"]
    n["unitCatalog"] = cat["services"] + cat["schemas"] + cat["categories"]
    await _run(session, delete(TemplateUnit).where(TemplateUnit.unit_id.in_(children)))
    await _run(session, delete(ServiceUnitSetting).where(ServiceUnitSetting.unit_id.in_(children)))
    now = utcnow()
    await _run(session, update(MessagingAccount).where(MessagingAccount.unit_id.in_(children), alive(MessagingAccount)).values(deleted_at=now, deleted_by=staff.id, is_active=False))
    await _run(session, update(SmsTemplate).where(SmsTemplate.unit_id.in_(children), alive(SmsTemplate)).values(deleted_at=now, deleted_by=staff.id, is_active=False))
    await _run(session, update(OrgUnit).where(OrgUnit.id.in_(children)).values(deleted_at=now, deleted_by=staff.id, is_active=False))


# --------------------------------------------------------------------------------------------- the reset


async def reset_unit(session: AsyncSession, unit: OrgUnit, parts: list[str], staff: StaffPrincipal, meta: RequestMeta) -> dict[str, int]:
    """Run the chosen (closed) parts for the unit inside the current transaction. Returns what was removed."""
    require_superadmin(staff)
    scope = await _scope_ids(session, unit)
    # serialise with cheque creation (`next_order_number` bumps the counter row): the counters stay locked until commit
    await session.execute(select(OrderCounter.unit_id).where(OrderCounter.unit_id.in_(scope)).with_for_update())
    p = _preds(unit, scope, staff)
    n: dict[str, int] = {}
    touched: list[uuid.UUID] = []
    doomed: list[uuid.UUID] = []

    # patients are decided while the cheques still exist ("seen only here")
    if "patients" in parts:
        doomed = await _ids(session, select(Patient.id).where(p["patients"][1]))
        first_other = (
            select(Order.unit_id)
            .where(Order.patient_id == Patient.id, Order.unit_id.not_in(scope))
            .order_by(Order.created_at, Order.id)
            .limit(1)
            .scalar_subquery()
        )
        n["patientsMoved"] = await _run(session, update(Patient).where(p["patientsMoved"][1]).values(registered_unit_id=first_other))

    if "orders" in parts:
        touched = await _drop_orders(session, p, scope, n)

    if "patients" in parts:
        await _drop_patients(session, doomed, n)

    if "orders" in parts:
        gone = set(doomed)
        await _recompute_patient_stats(session, [x for x in touched if x not in gone])

    if "templates" in parts:
        unbound = select(ResultTemplate.id).where(p["templatesUnbound"][1])
        n["templatesUnbound"] = await _run(session, delete(TemplateUnit).where(TemplateUnit.unit_id.in_(scope), TemplateUnit.template_id.in_(unbound)))
        n.update(await _drop_templates(session, p["templates"][1], p["assets"][1]))

    if "catalog" in parts:
        n.update(await _drop_catalog(session, scope))

    if "staff" in parts:
        await _drop_staff(session, p, scope, n)

    if "prices" in parts:
        n["prices"] = await _run(session, delete(ServiceUnitSetting).where(p["prices"][1]))

    if "units" in parts:
        await _drop_units(session, unit, p, staff, n)

    await session.flush()
    await audit(
        session,
        actor_type="staff",
        actor_id=staff.id,
        unit_id=unit.id,
        country_id=unit.country_id,
        action="unit.reset",
        entity="unit",
        entity_id=unit.id,
        after={"level": unit.level, "parts": parts, "counts": n},
        ip=meta.ip,
        request_id=meta.request_id,
    )
    await _invalidate()
    return n
