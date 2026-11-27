"""Where exactly a task is done.

«Open the Laboratory page and find the sample» makes a newcomer look for something they have never seen. The
checklist therefore leads to the very record: the patient to open a cheque for, the cheque to add a service to or
to take the payment for, the analysis that waits for its result, the result that waits for approval. Coming back
tomorrow, the employee continues where they stopped — on what THEY made, when they made something.

Everything is looked for in the trainee's sandbox (the session resolves the tables there) and inside the facility
they practise in. No record → no target: the interface then opens the page of the task as before.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import StaffPrincipal
from app.core.exceptions import AppError
from app.infrastructure.db import realm
from app.infrastructure.db.base import alive
from app.infrastructure.db.models import Order, OrderItem, Patient
from app.modules.training.samples import SAMPLE_NOTE
from app.modules.training.schemas import TrainingTargetOut

#: an analysis whose result may still be entered
ENTERABLE = ("pending", "rejected", "entered")
_OPEN_ORDER = ("cancelled", "completed")


def _own_first(note_column):  # what the employee made themselves comes before the samples put there for them
    return case((note_column == SAMPLE_NOTE, 1), else_=0)


async def _patient(session: AsyncSession, staff: StaffPrincipal, facility_id: uuid.UUID) -> uuid.UUID | None:
    return (
        await session.execute(
            select(Patient.id)
            .where(Patient.created_by == staff.id, Patient.registered_unit_id == facility_id, alive(Patient))
            .order_by(_own_first(Patient.note), Patient.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _order(session: AsyncSession, staff: StaffPrincipal, facility_id: uuid.UUID, *, unpaid: bool = False) -> uuid.UUID | None:
    """The employee's newest cheque that is still open — (`unpaid`) one with something left to pay."""
    stmt = select(Order.id).where(Order.created_by_employee_id == staff.id, Order.unit_id == facility_id, Order.status.notin_(_OPEN_ORDER), alive(Order))
    if unpaid:
        stmt = stmt.where(Order.payment.in_(("unpaid", "partial")), Order.total > Order.paid_amount)
    return (await session.execute(stmt.order_by(_own_first(Order.note), Order.created_at.desc()).limit(1))).scalar_one_or_none()


async def _items(session: AsyncSession, facility_id: uuid.UUID, statuses: tuple[str, ...], allowed: set[uuid.UUID] | None, limit: int) -> list[OrderItem]:
    stmt = (
        select(OrderItem)
        .join(Order, Order.id == OrderItem.order_id)
        .where(OrderItem.unit_id == facility_id, OrderItem.status.in_(statuses), OrderItem.schema_id.is_not(None), alive(OrderItem), alive(Order))
        .where(Order.payment.in_(("paid", "partial")), Order.status != "cancelled")
        .order_by(OrderItem.created_at.desc())
        .limit(limit)
    )
    if allowed is not None:
        stmt = stmt.where(OrderItem.category_id.in_(allowed))
    return list((await session.execute(stmt)).scalars().all())


def _pick(items: list[OrderItem], order: tuple[str, ...], staff_id: uuid.UUID) -> OrderItem | None:
    """The first status of `order` that has an item; among those the employee's own work first."""
    for status in order:
        found = [i for i in items if i.status == status]
        if found:
            return next((i for i in found if i.technician_id == staff_id), found[0])
    return None


async def _approvable(session: AsyncSession, items: list[OrderItem]) -> OrderItem | None:
    from app.modules.orders import service as orders_svc

    for item in items:
        try:
            await orders_svc._resolve_item_template(session, item, None)
            return item
        except AppError:
            continue
    return None


def _of(item: OrderItem | None) -> TrainingTargetOut | None:
    return TrainingTargetOut(item_id=str(item.id), order_id=str(item.order_id)) if item is not None else None


async def find(session: AsyncSession, staff: StaffPrincipal, facility_id: uuid.UUID, keys: Iterable[str]) -> dict[str, TrainingTargetOut]:
    """Task key → the record to open, for the tasks named. Only ever asked on a trainee's own session: the ids are
    of the sandbox and mean nothing anywhere else."""
    from app.modules.orders import service as orders_svc

    wanted = set(keys)
    if not wanted or not realm.is_training():
        return {}
    out: dict[str, TrainingTargetOut] = {}
    desk = wanted & {"order_create", "order_add_item", "payment_take"}
    if desk:
        # a cheque to continue with; without one — the patient to open a cheque for (the step before it)
        open_order = await _order(session, staff, facility_id) if desk - {"order_create"} else None
        patient_id = await _patient(session, staff, facility_id) if "order_create" in desk or open_order is None else None
        patient = TrainingTargetOut(patient_id=str(patient_id)) if patient_id is not None else None
        if "order_create" in desk and patient is not None:
            out["order_create"] = patient
        if "order_add_item" in desk:
            target = TrainingTargetOut(order_id=str(open_order)) if open_order is not None else patient
            if target is not None:
                out["order_add_item"] = target
        if "payment_take" in desk:
            # a cheque with something left to pay; else the open one (a service is added to it first)
            unpaid = await _order(session, staff, facility_id, unpaid=True) if open_order is not None else None
            target = TrainingTargetOut(order_id=str(unpaid or open_order)) if open_order is not None else patient
            if target is not None:
                out["payment_take"] = target
    lab = wanted & {"result_enter", "result_submit"}
    if lab or "result_approve" in wanted:
        allowed = await orders_svc.allowed_category_ids(session, staff)
        if lab:
            items = await _items(session, facility_id, ENTERABLE, allowed, 12)
            # to enter: an untouched analysis first; to send: one whose result is already in
            for key, order in (("result_enter", ("pending", "rejected", "entered")), ("result_submit", ("entered", "pending", "rejected"))):
                target = _of(_pick(items, order, staff.id)) if key in wanted else None
                if target is not None:
                    out[key] = target
        if "result_approve" in wanted:
            target = _of(await _approvable(session, await _items(session, facility_id, ("submitted",), allowed, 8)))
            if target is not None:
                out["result_approve"] = target
    return out
