"""Something to practise on.

A receptionist makes their own patients and cheques, but a laboratory worker cannot open a cheque to get an analysis,
and a doctor has no result to approve until someone enters one. So the sandbox is given, per facility, what the
trainee's tasks need — made with the very services the real requests use (the same numbering, snapshots, validation),
out of that facility's own catalogue, and only when nothing suitable is already waiting there:

* `lab_item`        a paid cheque with one analysis that has a result form and waits for its result;
* `submitted_item`  the same, with the result filled in and sent for approval (a template to print it must exist).

A facility whose catalogue has no such service simply has nothing to practise these on — the task is then reported
as unavailable and does not hold the training back.
"""

from __future__ import annotations

import copy
import logging
import secrets
import uuid
from datetime import date
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import RequestMeta, StaffPrincipal
from app.core.exceptions import AppError
from app.core.scope import load_scope
from app.infrastructure.db import realm
from app.infrastructure.db.base import alive
from app.infrastructure.db.models import AttributeSchema, Order, OrderItem, OrgUnit, Patient
from app.modules.training.tasks import LAB_ITEM, PAID_SERVICE, SERVICE, SUBMITTED_ITEM

log = logging.getLogger("app.training")

#: how the practice rows are told apart on the screen
SAMPLE_NOTE = "O‘quv namunasi"
SAMPLE_PATIENTS = (("Namunaviy Bemor Birinchi", "male", date(1990, 5, 14)), ("Namunaviy Bemor Ikkinchi", "female", date(1985, 9, 2)))
SAMPLE_TECHNICIAN = "O‘quv laboranti"
_META = RequestMeta(ip=None, request_id=None, user_agent="training")
#: how many services are tried before giving up on a facility
_TRIES = 6
_S = realm.SANDBOX_SCHEMA
#: the schema the name `orders` resolves to on this connection
_HOME_OF_ORDERS = "SELECT n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.oid = to_regclass('orders')"


async def _require_sandbox(session: AsyncSession) -> None:
    """Samples are made with the real services, so on a real-data session they would be real patients and cheques.
    Never: the session must resolve the operational tables to the sandbox, or nothing is made."""
    if not realm.is_training() or (await session.execute(text(_HOME_OF_ORDERS))).scalar_one_or_none() != _S:
        raise RuntimeError("training samples asked for outside the sandbox")


def _number(field: dict[str, Any]) -> float | int:
    lo = hi = None
    for ref in field.get("references") or []:
        if ref.get("min") is not None and ref.get("max") is not None:
            lo, hi = ref["min"], ref["max"]
            break
    if lo is None:
        lo, hi = field.get("min"), field.get("max")
    if lo is None and hi is None:
        value: float = 1.0
    elif lo is None:
        value = float(hi) / 2
    elif hi is None:
        value = float(lo) + 1
    else:
        value = (float(lo) + float(hi)) / 2
    decimals = int(field.get("decimals") or 0)
    rounded = round(value, decimals)
    return int(rounded) if decimals == 0 else rounded


def _one(field: dict[str, Any]) -> Any:
    kind = field.get("type")
    if kind == "number":
        return _number(field)
    if kind in ("select", "multiselect"):
        options = [o for o in field.get("options") or [] if isinstance(o, dict) and o.get("value") is not None]
        calm = [o for o in options if o.get("flag") in (None, "normal")] or options
        if not calm:
            return [] if kind == "multiselect" else "—"
        return [calm[0]["value"]] if kind == "multiselect" else calm[0]["value"]
    if kind == "boolean":
        return False
    if kind == "date":
        return date.today().isoformat()
    return "Me’yorda" if kind == "text" else "Namunaviy xulosa: o‘zgarishlar aniqlanmadi."


def sample_values(fields: list[dict[str, Any]]) -> dict[str, Any]:
    """A plausible filled-in result for a schema — every field has something, numbers sit inside their norm."""
    out: dict[str, Any] = {}
    for f in fields or []:
        key = f.get("key")
        if not key:
            continue
        if f.get("type") == "table":
            rows = copy.deepcopy(f.get("presetRows") or [])
            cols = [c for c in f.get("columns") or [] if isinstance(c, dict) and c.get("key")]
            if not rows and cols:
                rows = [{}]
            for row in rows:
                for c in cols:
                    if row.get(c["key"]) in (None, ""):
                        row[c["key"]] = _one(c)
            out[key] = rows
        else:
            out[key] = _one(f)
    return out


async def _waiting(session: AsyncSession, facility_id: uuid.UUID, statuses: tuple[str, ...], allowed: set[uuid.UUID] | None, *, approvable: bool = False) -> bool:
    """Is something already there to practise on (an analysis with a result form, on a paid cheque, in one of
    `statuses`)? `approvable`: … that can really be approved — a template to print it exists."""
    from app.modules.orders import service as orders_svc

    stmt = (
        select(OrderItem)
        .join(Order, Order.id == OrderItem.order_id)
        .where(OrderItem.unit_id == facility_id, OrderItem.status.in_(statuses), OrderItem.schema_id.is_not(None), alive(OrderItem), alive(Order))
        .where(Order.payment.in_(("paid", "partial")), Order.status != "cancelled")
        .order_by(OrderItem.created_at.desc())
        .limit(8 if approvable else 1)
    )
    if allowed is not None:
        stmt = stmt.where(OrderItem.category_id.in_(allowed))
    items = (await session.execute(stmt)).scalars().all()
    if not approvable:
        return bool(items)
    for item in items:
        try:
            await orders_svc._resolve_item_template(session, item, None)
            return True
        except AppError:
            continue
    return False


async def _offered(session: AsyncSession, facility: OrgUnit) -> list[Any]:
    """The services this facility offers now (active, not switched off for it)."""
    from app.modules.catalog import service as catalog_svc
    from app.modules.catalog.schemas import ServiceTypeQuery

    scope = await load_scope(session, facility.id)
    if scope is None:
        return []
    return [s for s in await catalog_svc.list_service_types(session, scope, ServiceTypeQuery(active_only=True, unit_id=str(facility.id))) if s.available is not False]


def _price(service: Any) -> int:
    return int(service.effective_price if service.effective_price is not None else service.price or 0)


async def _candidates(session: AsyncSession, staff: StaffPrincipal, facility: OrgUnit, allowed: set[uuid.UUID] | None) -> list[Any]:
    """Services this facility offers that have a published result form with at least one field (simplest first)."""
    services = [s for s in await _offered(session, facility) if s.schema_id]
    if allowed is not None:
        services = [s for s in services if uuid.UUID(s.category_id) in allowed]
    if not services:
        return []
    schemas = {s.id: s for s in (await session.execute(select(AttributeSchema).where(AttributeSchema.id.in_({uuid.UUID(s.schema_id) for s in services}), alive(AttributeSchema)))).scalars()}
    usable = [(s, schemas[uuid.UUID(s.schema_id)]) for s in services if uuid.UUID(s.schema_id) in schemas and schemas[uuid.UUID(s.schema_id)].status == "published" and schemas[uuid.UUID(s.schema_id)].fields]
    usable.sort(key=lambda pair: (len(pair[1].fields), pair[0].name))
    return usable[:_TRIES]


async def _patient(session: AsyncSession, staff: StaffPrincipal, facility: OrgUnit, index: int = 0) -> uuid.UUID:
    """The trainee's own sample patient in this facility (made once)."""
    from app.modules.patients import service as patients_svc
    from app.modules.patients.schemas import PatientUpsertIn

    name, gender, born = SAMPLE_PATIENTS[index % len(SAMPLE_PATIENTS)]
    found = (
        await session.execute(select(Patient.id).where(Patient.registered_unit_id == facility.id, Patient.created_by == staff.id, Patient.full_name == name, alive(Patient)).limit(1))
    ).scalar_one_or_none()
    if found is not None:
        return found
    scope = await load_scope(session, facility.id)
    # a number nobody has: the 99800… block is not given to subscribers
    phone = "99800" + "".join(secrets.choice("0123456789") for _ in range(7))
    out = await patients_svc.create_patient(
        session, scope, staff, PatientUpsertIn(full_name=name, phone=phone, gender=gender, birth_date=born, note=SAMPLE_NOTE), _META  # type: ignore[arg-type]
    )
    return uuid.UUID(out.id)


async def _make(session: AsyncSession, staff: StaffPrincipal, facility: OrgUnit, allowed: set[uuid.UUID] | None, *, submitted: bool) -> bool:
    """One paid sample cheque with one analysis — waiting for its result, or (`submitted`) for approval."""
    from app.modules.orders import service as orders_svc
    from app.modules.orders.schemas import CreateOrderIn, PayIn, SaveValuesIn

    scope = await load_scope(session, facility.id)
    if scope is None:
        return False
    for service, schema in await _candidates(session, staff, facility, allowed):
        try:
            # a savepoint per attempt: a service that cannot be used (no template, a rule of the facility) leaves nothing behind
            async with session.begin_nested():
                patient_id = await _patient(session, staff, facility, 1 if submitted else 0)
                made = await orders_svc.create_order(
                    session, scope, staff, CreateOrderIn(patient_id=str(patient_id), unit_id=str(facility.id), service_type_ids=[service.id], note=SAMPLE_NOTE), _META
                )
                if not made.items:
                    raise AppError("no item")
                order_id, item_id = uuid.UUID(made.order.id), uuid.UUID(made.items[0].id)
                if made.order.total > 0:
                    await orders_svc.pay(session, order_id, staff, PayIn(amount=made.order.total, method="cash"), _META)
                else:  # a free service never becomes "paid" by a payment: mark the cheque so the laboratory sees it
                    await session.execute(text(f"UPDATE {_S}.orders SET payment = 'paid' WHERE id = :id"), {"id": order_id})
                if submitted:
                    item = await session.get(OrderItem, item_id)
                    await orders_svc._resolve_item_template(session, item, None)
                    await orders_svc.save_values(session, item_id, staff, SaveValuesIn(values=sample_values(schema.fields)), _META)
                    await orders_svc.submit_item(session, item_id, staff, _META)
                    # entered "by the laboratory", not by the doctor who is about to approve it
                    await session.execute(text(f"UPDATE {_S}.order_items SET technician_id = NULL, technician_name = :n WHERE id = :id"), {"n": SAMPLE_TECHNICIAN, "id": item_id})
            return True
        except AppError as exc:
            log.info("training sample: service %s skipped (%s)", service.id, exc.message)
            continue
    return False


async def ensure(session: AsyncSession, staff: StaffPrincipal, facility: OrgUnit, needs: set[str], *, patients: bool = False) -> dict[str, bool]:
    """Put what `needs` names into the facility's sandbox when it is not there; says, per need, whether it is there now.
    `patients` also leaves two sample patients for a receptionist to look up."""
    from app.modules.orders import service as orders_svc

    await _require_sandbox(session)
    # one filler at a time per employee (the status is asked often, sometimes twice at once)
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"training-samples:{staff.id}"})
    allowed = await orders_svc.allowed_category_ids(session, staff)
    out: dict[str, bool] = {}
    if SERVICE in needs or PAID_SERVICE in needs:
        offered = await _offered(session, facility)
        out[SERVICE] = bool(offered)
        out[PAID_SERVICE] = any(_price(s) > 0 for s in offered)
    if LAB_ITEM in needs:
        out[LAB_ITEM] = await _waiting(session, facility.id, ("pending", "entered", "rejected"), allowed) or await _make(session, staff, facility, allowed, submitted=False)
    if SUBMITTED_ITEM in needs:
        out[SUBMITTED_ITEM] = await _waiting(session, facility.id, ("submitted",), allowed, approvable=True) or await _make(session, staff, facility, allowed, submitted=True)
    if patients:
        try:
            async with session.begin_nested():
                for i in range(len(SAMPLE_PATIENTS)):
                    await _patient(session, staff, facility, i)
        except AppError as exc:
            log.info("training sample patients skipped (%s)", exc.message)
    return out


async def purge(session: AsyncSession, employee_id: uuid.UUID) -> dict[str, int]:
    """Remove everything this employee made in the sandbox (their cheques with all that hangs off them, the patients
    nobody else's cheque uses, their notices and audit lines). Every table is named WITH the sandbox schema: whatever
    session this is given, it cannot delete a real row."""
    e = {"e": employee_id}
    orders = f"SELECT id FROM {_S}.orders WHERE created_by_employee_id = :e"
    lonely = f"SELECT p.id FROM {_S}.patients p WHERE p.created_by = :e AND NOT EXISTS (SELECT 1 FROM {_S}.orders o WHERE o.patient_id = p.id)"
    steps = (
        ("items_unlinked", f"UPDATE {_S}.order_items SET document_id = NULL WHERE order_id IN ({orders})"),
        ("messages", f"DELETE FROM {_S}.outbox_messages WHERE order_id IN ({orders}) OR document_id IN (SELECT id FROM {_S}.result_documents WHERE order_id IN ({orders})) OR created_by = :e"),
        ("documents", f"DELETE FROM {_S}.result_documents WHERE order_id IN ({orders})"),
        ("payments", f"DELETE FROM {_S}.payments WHERE order_id IN ({orders})"),
        ("items", f"DELETE FROM {_S}.order_items WHERE order_id IN ({orders})"),
        ("orders", f"DELETE FROM {_S}.orders WHERE created_by_employee_id = :e"),
        ("relations", f"UPDATE {_S}.patients SET related_patient_id = NULL, relation = NULL WHERE related_patient_id IN ({lonely})"),
        ("patient_messages", f"DELETE FROM {_S}.outbox_messages WHERE patient_id IN ({lonely})"),
        ("patients", f"DELETE FROM {_S}.patients WHERE id IN ({lonely})"),
        ("notifications", f"DELETE FROM {_S}.notifications WHERE employee_id = :e"),
        ("audit", f"DELETE FROM {_S}.audit_log WHERE actor_id = :e"),
    )
    done: dict[str, int] = {}
    for name, sql in steps:
        done[name] = int((await session.execute(text(sql), e)).rowcount or 0)
    return done


async def sweep(session: AsyncSession) -> int:
    """Clear what people who are no longer practising left behind in the sandbox (a clean-up that failed at the
    time, an account that was deleted). Returns how many people were cleared. Run at start."""
    stale = (
        await session.execute(
            text(
                f"SELECT DISTINCT x.who FROM (SELECT created_by_employee_id AS who FROM {_S}.orders UNION SELECT created_by FROM {_S}.patients) x "
                "WHERE x.who IS NOT NULL AND NOT EXISTS ("
                "SELECT 1 FROM public.employee_training t JOIN public.employees e ON e.id = t.employee_id "
                "WHERE t.employee_id = x.who AND t.status = 'active' AND e.deleted_at IS NULL)"
            )
        )
    ).scalars().all()  # fmt: skip
    for employee_id in stale:
        await purge(session, employee_id)
    return len(stale)
