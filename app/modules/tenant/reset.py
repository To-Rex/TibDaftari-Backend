"""Superadmin reset — bring a company, or one branch, back to the state it had right after it was created
("like newborn") without touching any other company or branch. Requested by the product owner as an
irreversible operation; superadmin only, previewed, confirmed by typing the slug / code, audited.

Both resets are split into parts the superadmin picks (their dependencies are added automatically):

company  orders     cheques, their services, payments, result documents (+ stored PDFs), SMS/Telegram outbox,
                    in-app notifications; every branch's cheque numbering restarts from 1
         patients   the company's patients (+ their portal/Telegram sessions, links and OTP codes)   ⇒ orders
         templates  result + cheque templates and template assets (logos, stamps, signatures)
         catalog    categories, services, result schemas                               ⇒ templates, orders
         staff      employees (superadmins and the caller are kept) + their sessions; roles back to the defaults
         branches   the branches themselves                                            ⇒ orders, patients
branch   orders     the branch's cheques (+ services, payments, documents, PDFs, outbox); numbering from 1;
                    the visit statistics of patients who also visit other branches are recomputed
         patients   patients who belong to this branch only (registered here, no cheques elsewhere); patients
                    registered here who visit other branches move to the branch of their first other cheque   ⇒ orders
         templates  templates bound to this branch only; shared templates just lose this branch
         staff      employees of this branch only (superadmins and the caller are kept); others lose this branch
         prices     this branch's price overrides on services

The company / branch rows themselves, the company profile and settings, reference data and the audit log stay.
Rows are hard-deleted inside the request's single transaction (all or nothing): order numbers are unique per
company, so restarting the numbering needs the old cheques really gone.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import Select, and_, delete, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

from app.api.deps import RequestMeta, StaffPrincipal, invalidate_principal_cache, invalidate_session_cache
from app.core.audit import audit
from app.core.exceptions import ForbiddenError, ValidationError
from app.core.permissions import DEFAULT_COMPANY_ROLES
from app.infrastructure.db.models import (
    AttributeSchema,
    Branch,
    Category,
    Company,
    Employee,
    Notification,
    Order,
    OrderItem,
    OtpChallenge,
    OutboxMessage,
    Patient,
    Payment,
    ResultDocument,
    ResultTemplate,
    Role,
    ServiceType,
    StoredFile,
    TelegramChatPref,
    TelegramLink,
    TemplateAsset,
)
from app.infrastructure.db.models import Session as SessionRow
from app.infrastructure.redis import cache

COMPANY_PARTS: tuple[str, ...] = ("orders", "patients", "templates", "catalog", "staff", "branches")
BRANCH_PARTS: tuple[str, ...] = ("orders", "patients", "templates", "staff", "prices")
COMPANY_REQUIRES: dict[str, tuple[str, ...]] = {"patients": ("orders",), "catalog": ("templates", "orders"), "branches": ("orders", "patients")}
BRANCH_REQUIRES: dict[str, tuple[str, ...]] = {"patients": ("orders",)}

CONFIRM_MISMATCH = "Tasdiqlash so‘zi noto‘g‘ri"


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


async def _count(session: AsyncSession, model: Any, where: ColumnElement[bool]) -> int:
    return int((await session.execute(select(func.count()).select_from(model).where(where))).scalar_one())


async def _run(session: AsyncSession, stmt: Any) -> int:
    res = await session.execute(stmt.execution_options(synchronize_session=False))
    return int(res.rowcount or 0)  # type: ignore[attr-defined]


async def _drop_sessions(session: AsyncSession, actor: str, subjects: Select) -> int:
    """Delete login sessions of removed employees / patients and mark their tokens revoked in Redis right away."""
    jtis = list((await session.execute(select(SessionRow.id).where(SessionRow.actor == actor, SessionRow.subject_id.in_(subjects)))).scalars())
    if not jtis:
        return 0
    await _run(session, delete(SessionRow).where(SessionRow.id.in_(jtis)))
    for jti in jtis:
        await invalidate_session_cache(jti)
    return len(jtis)


async def _reset_roles(session: AsyncSession, company_id: uuid.UUID, created_by: uuid.UUID) -> int:
    """Roles back to the default set: unused roles go, the default ones get their default name / permissions back."""
    in_use = select(Employee.role_id).where(Employee.role_id.is_not(None))
    removed = await _run(session, delete(Role).where(Role.company_id == company_id, Role.id.not_in(in_use)))
    existing = {r.key: r for r in (await session.execute(select(Role).where(Role.company_id == company_id, Role.deleted_at.is_(None)))).scalars()}
    for spec in DEFAULT_COMPANY_ROLES:
        role = existing.get(spec["key"])
        if role is None:
            session.add(Role(company_id=company_id, key=spec["key"], name=spec["name"], permissions=list(spec["permissions"]), is_system=bool(spec["is_system"]), created_by=created_by))
        else:
            role.name, role.permissions, role.is_system = spec["name"], list(spec["permissions"]), bool(spec["is_system"])
    await session.flush()
    return removed


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


async def _invalidate(company_id: uuid.UUID) -> None:
    await cache.delete_prefix(f"co:{company_id}:")
    await invalidate_principal_cache()


# --------------------------------------------------------------------------------------------- company


def _keep_employees(cid: uuid.UUID, staff: StaffPrincipal) -> Select:
    """Employees a company reset never removes: superadmins and the person resetting."""
    return select(Employee.id).where(Employee.company_id == cid, or_(Employee.is_super_admin.is_(True), Employee.id == staff.id))


def _company_preds(cid: uuid.UUID, keep: Select) -> dict[str, tuple[Any, ColumnElement[bool]]]:
    """(model, predicate) per counted item — shared by the preview and the reset itself."""
    docs = select(ResultDocument.pdf_file_id).where(ResultDocument.company_id == cid, ResultDocument.pdf_file_id.is_not(None))
    asset_files = select(TemplateAsset.file_id).where(TemplateAsset.company_id == cid)
    return {
        "orders": (Order, Order.company_id == cid),
        "items": (OrderItem, OrderItem.company_id == cid),
        "payments": (Payment, Payment.company_id == cid),
        "documents": (ResultDocument, ResultDocument.company_id == cid),
        "documentFiles": (StoredFile, StoredFile.id.in_(docs)),
        "messages": (OutboxMessage, OutboxMessage.company_id == cid),
        "notifications": (Notification, Notification.company_id == cid),
        "patients": (Patient, Patient.company_id == cid),
        "telegramLinks": (TelegramLink, TelegramLink.company_id == cid),
        "templates": (ResultTemplate, ResultTemplate.company_id == cid),
        "assets": (TemplateAsset, TemplateAsset.company_id == cid),
        "assetFiles": (StoredFile, StoredFile.id.in_(asset_files)),
        "categories": (Category, Category.company_id == cid),
        "services": (ServiceType, ServiceType.company_id == cid),
        "schemas": (AttributeSchema, AttributeSchema.company_id == cid),
        "employees": (Employee, and_(Employee.company_id == cid, Employee.id.not_in(keep))),
        "branches": (Branch, Branch.company_id == cid),
    }


COMPANY_COUNTS: dict[str, tuple[str, ...]] = {
    "orders": ("orders", "items", "payments", "documents", "messages", "notifications"),
    "patients": ("patients", "telegramLinks"),
    "templates": ("templates", "assets"),
    "catalog": ("categories", "services", "schemas"),
    "staff": ("employees",),
    "branches": ("branches",),
}


async def preview_company(session: AsyncSession, company: Company, staff: StaffPrincipal) -> dict[str, dict[str, int]]:
    require_superadmin(staff)
    keep = _keep_employees(company.id, staff)
    preds = _company_preds(company.id, keep)
    out = {part: {k: await _count(session, *preds[k]) for k in keys} for part, keys in COMPANY_COUNTS.items()}
    # roles a staff reset removes: those no remaining employee (of any company) would still use
    still_used = select(Employee.role_id).where(Employee.role_id.is_not(None), or_(Employee.company_id != company.id, Employee.id.in_(keep)))
    out["staff"]["roles"] = await _count(session, Role, and_(Role.company_id == company.id, Role.id.not_in(still_used)))
    return out


async def reset_company(session: AsyncSession, company: Company, parts: list[str], staff: StaffPrincipal, meta: RequestMeta) -> dict[str, int]:
    require_superadmin(staff)
    cid = company.id
    # serialise with cheque creation (it bumps the branch row): every branch of the company stays locked until commit
    await session.execute(select(Branch.id).where(Branch.company_id == cid).with_for_update())
    keep = _keep_employees(cid, staff)
    p = _company_preds(cid, keep)
    n: dict[str, int] = {}

    if "orders" in parts:
        n["documentFiles"] = await _run(session, delete(StoredFile).where(p["documentFiles"][1]))
        for key, model in (("documents", ResultDocument), ("payments", Payment), ("items", OrderItem), ("messages", OutboxMessage), ("notifications", Notification), ("orders", Order)):
            n[key] = await _run(session, delete(model).where(p[key][1]))
        await _run(session, update(Branch).where(Branch.company_id == cid).values(order_seq=0))
        if "patients" not in parts:
            await _run(session, update(Patient).where(Patient.company_id == cid).values(stats_orders=0, stats_total_spent=0, stats_last_visit_at=None))

    if "patients" in parts:
        n["patientSessions"] = await _drop_sessions(session, "patient", select(Patient.id).where(Patient.company_id == cid))
        n["telegramLinks"] = await _run(session, delete(TelegramLink).where(p["telegramLinks"][1]))
        await _run(session, delete(TelegramChatPref).where(TelegramChatPref.company_id == cid))
        await _run(session, delete(OtpChallenge).where(OtpChallenge.company_id == cid))
        n["patients"] = await _run(session, delete(Patient).where(p["patients"][1]))

    if "templates" in parts:
        await _run(session, update(Employee).where(Employee.company_id == cid).values(signature_asset_id=None))
        if "catalog" not in parts:
            await _run(session, update(ServiceType).where(ServiceType.company_id == cid).values(default_template_id=None))
        # asset images go only when no issued document is left that could need them for a re-render
        if "orders" in parts:
            n["assetFiles"] = await _run(session, delete(StoredFile).where(p["assetFiles"][1]))
        n["assets"] = await _run(session, delete(TemplateAsset).where(p["assets"][1]))
        n["templates"] = await _run(session, delete(ResultTemplate).where(p["templates"][1]))

    if "catalog" in parts:
        for key, model in (("services", ServiceType), ("schemas", AttributeSchema), ("categories", Category)):
            n[key] = await _run(session, delete(model).where(p[key][1]))
        await _run(session, update(Employee).where(Employee.company_id == cid).values(category_ids=[]))

    if "staff" in parts:
        removed = select(Employee.id).where(p["employees"][1])
        n["staffSessions"] = await _drop_sessions(session, "staff", removed)
        if "orders" not in parts:
            await _run(session, delete(Notification).where(Notification.company_id == cid, Notification.employee_id.in_(removed)))
        n["employees"] = await _run(session, delete(Employee).where(p["employees"][1]))
        n["roles"] = await _reset_roles(session, cid, staff.id)

    if "branches" in parts:
        n["branches"] = await _run(session, delete(Branch).where(p["branches"][1]))
        await _run(session, update(Employee).where(Employee.company_id == cid).values(branch_ids=[]))
        if "templates" not in parts:
            await _run(session, update(ResultTemplate).where(ResultTemplate.company_id == cid).values(branch_ids=[]))
        if "catalog" not in parts:
            await _run(session, update(ServiceType).where(ServiceType.company_id == cid).values(branch_prices={}))

    await session.flush()
    await audit(session, actor_type="staff", actor_id=staff.id, company_id=cid, action="company.reset", entity="company", entity_id=cid, after={"parts": parts, "counts": n}, ip=meta.ip, request_id=meta.request_id)
    await _invalidate(cid)
    return n


# --------------------------------------------------------------------------------------------- branch


def _branch_preds(cid: uuid.UUID, bid: uuid.UUID, staff: StaffPrincipal) -> dict[str, tuple[Any, ColumnElement[bool]]]:
    orders = select(Order.id).where(Order.company_id == cid, Order.branch_id == bid)
    docs = select(ResultDocument.pdf_file_id).where(ResultDocument.company_id == cid, ResultDocument.order_id.in_(orders), ResultDocument.pdf_file_id.is_not(None))
    in_branch = exists().where(Order.patient_id == Patient.id, Order.company_id == cid, Order.branch_id == bid)
    elsewhere = exists().where(Order.patient_id == Patient.id, Order.company_id == cid, Order.branch_id != bid)
    only_here = and_(func.cardinality(ResultTemplate.branch_ids) == 1, ResultTemplate.branch_ids.any(bid))
    emp_only_here = and_(func.cardinality(Employee.branch_ids) == 1, Employee.branch_ids.any(bid))
    return {
        "orders": (Order, and_(Order.company_id == cid, Order.branch_id == bid)),
        "items": (OrderItem, and_(OrderItem.company_id == cid, OrderItem.order_id.in_(orders))),
        "payments": (Payment, and_(Payment.company_id == cid, Payment.order_id.in_(orders))),
        "documents": (ResultDocument, and_(ResultDocument.company_id == cid, ResultDocument.order_id.in_(orders))),
        "documentFiles": (StoredFile, StoredFile.id.in_(docs)),
        "messages": (OutboxMessage, and_(OutboxMessage.company_id == cid, or_(OutboxMessage.branch_id == bid, OutboxMessage.order_id.in_(orders)))),
        # this branch's own patients: registered here (or unregistered but seen only here) and with no cheque elsewhere
        "patients": (Patient, and_(Patient.company_id == cid, or_(Patient.branch_id == bid, and_(Patient.branch_id.is_(None), in_branch)), ~elsewhere)),
        "patientsMoved": (Patient, and_(Patient.company_id == cid, Patient.branch_id == bid, elsewhere)),
        "templates": (ResultTemplate, and_(ResultTemplate.company_id == cid, only_here)),
        "templatesUnbound": (ResultTemplate, and_(ResultTemplate.company_id == cid, ResultTemplate.branch_ids.any(bid), ~only_here)),
        "employees": (Employee, and_(Employee.company_id == cid, emp_only_here, Employee.is_super_admin.is_(False), Employee.id != staff.id)),
        "employeesUnbound": (Employee, and_(Employee.company_id == cid, Employee.branch_ids.any(bid), or_(~emp_only_here, Employee.is_super_admin.is_(True), Employee.id == staff.id))),
        "prices": (ServiceType, and_(ServiceType.company_id == cid, ServiceType.branch_prices.has_key(str(bid)))),
    }


BRANCH_COUNTS: dict[str, tuple[str, ...]] = {
    "orders": ("orders", "items", "payments", "documents", "messages"),
    "patients": ("patients", "patientsMoved"),
    "templates": ("templates", "templatesUnbound"),
    "staff": ("employees", "employeesUnbound"),
    "prices": ("prices",),
}


async def preview_branch(session: AsyncSession, branch: Branch, staff: StaffPrincipal) -> dict[str, dict[str, int]]:
    require_superadmin(staff)
    preds = _branch_preds(branch.company_id, branch.id, staff)
    return {part: {k: await _count(session, *preds[k]) for k in keys} for part, keys in BRANCH_COUNTS.items()}


async def reset_branch(session: AsyncSession, branch: Branch, parts: list[str], staff: StaffPrincipal, meta: RequestMeta) -> dict[str, int]:
    require_superadmin(staff)
    cid, bid = branch.company_id, branch.id
    await session.execute(select(Branch.id).where(Branch.id == bid).with_for_update())
    p = _branch_preds(cid, bid, staff)
    n: dict[str, int] = {}

    # patients first: "seen only here" is decided while this branch's cheques still exist
    if "patients" in parts:
        doomed = list((await session.execute(select(Patient.id).where(p["patients"][1]))).scalars())
        n["patients"] = 0
        if doomed:
            n["patientSessions"] = await _drop_sessions(session, "patient", select(Patient.id).where(Patient.id.in_(doomed)))
            await _run(session, delete(TelegramLink).where(TelegramLink.company_id == cid, TelegramLink.patient_id.in_(doomed)))
            await _run(session, delete(OtpChallenge).where(OtpChallenge.company_id == cid, OtpChallenge.meta["patientId"].astext.in_([str(x) for x in doomed])))
            n["patients"] = await _run(session, delete(Patient).where(Patient.id.in_(doomed)))
        # registered here but visiting other branches: they now belong to the branch of their first other cheque
        first_other = select(Order.branch_id).where(Order.patient_id == Patient.id, Order.company_id == cid, Order.branch_id != bid).order_by(Order.created_at, Order.id).limit(1).scalar_subquery()
        n["patientsMoved"] = await _run(session, update(Patient).where(p["patientsMoved"][1]).values(branch_id=first_other))

    if "orders" in parts:
        touched = list((await session.execute(select(Order.patient_id).where(p["orders"][1]).distinct())).scalars())
        n["documentFiles"] = await _run(session, delete(StoredFile).where(p["documentFiles"][1]))
        for key, model in (("documents", ResultDocument), ("payments", Payment), ("items", OrderItem), ("messages", OutboxMessage), ("orders", Order)):
            n[key] = await _run(session, delete(model).where(p[key][1]))
        await _run(session, update(Branch).where(Branch.id == bid).values(order_seq=0))
        await _recompute_patient_stats(session, touched)

    if "templates" in parts:
        gone = select(ResultTemplate.id).where(p["templates"][1])
        await _run(session, update(ServiceType).where(ServiceType.company_id == cid, ServiceType.default_template_id.in_(gone)).values(default_template_id=None))
        n["templates"] = await _run(session, delete(ResultTemplate).where(p["templates"][1]))
        n["templatesUnbound"] = await _run(session, update(ResultTemplate).where(p["templatesUnbound"][1]).values(branch_ids=func.array_remove(ResultTemplate.branch_ids, bid)))

    if "staff" in parts:
        removed = select(Employee.id).where(p["employees"][1])
        n["staffSessions"] = await _drop_sessions(session, "staff", removed)
        await _run(session, delete(Notification).where(Notification.company_id == cid, Notification.employee_id.in_(removed)))
        n["employees"] = await _run(session, delete(Employee).where(p["employees"][1]))
        n["employeesUnbound"] = await _run(session, update(Employee).where(p["employeesUnbound"][1]).values(branch_ids=func.array_remove(Employee.branch_ids, bid)))

    if "prices" in parts:
        n["prices"] = await _run(session, update(ServiceType).where(p["prices"][1]).values(branch_prices=ServiceType.branch_prices.op("-", return_type=JSONB)(str(bid))))

    await session.flush()
    await audit(session, actor_type="staff", actor_id=staff.id, company_id=cid, action="branch.reset", entity="branch", entity_id=bid, after={"parts": parts, "counts": n}, ip=meta.ip, request_id=meta.request_id)
    await _invalidate(cid)
    return n
