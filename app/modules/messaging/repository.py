"""Messaging SQL helpers — outbox listing, notifications, queue claiming, maintenance updates."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import Select, delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import paginate_query
from app.core.schemas import PageQuery
from app.core.textutil import digits, fold
from app.infrastructure.db.base import alive
from app.infrastructure.db.models import (
    Employee,
    MessagingAccount,
    Notification,
    Order,
    OrgUnitClosure,
    OtpChallenge,
    OutboxMessage,
    Patient,
)
from app.infrastructure.db.models import Session as SessionModel

QUEUE_STATUSES = ("queued", "scheduled")


def outbox_select(
    unit_ids: Sequence[uuid.UUID], *, status: str | None, kind: str | None, search: str | None, created_from: datetime | None = None, created_to: datetime | None = None
) -> Select:
    """Outbox rows of the given facilities with the §8 filters (uses ix_outbox_unit_status_created); `created_from`
    / `created_to` = the half-open interval the message was written in."""
    stmt = select(OutboxMessage).where(OutboxMessage.unit_id.in_(unit_ids), alive(OutboxMessage))
    if created_from is not None:
        stmt = stmt.where(OutboxMessage.created_at >= created_from)
    if created_to is not None:
        stmt = stmt.where(OutboxMessage.created_at < created_to)
    if status:
        # the UI never sees `sending`; it belongs to the `queued` bucket
        stmt = stmt.where(OutboxMessage.status.in_(("queued", "sending")) if status == "queued" else OutboxMessage.status == status)
    if kind:
        stmt = stmt.where(OutboxMessage.kind == kind)
    if search and search.strip():
        needle = fold(search)
        d = digits(search)
        conds = [func.fold_text(OutboxMessage.text).contains(needle, autoescape=True)] if needle else []
        if d:
            conds.append(OutboxMessage.to.contains(d, autoescape=True))
        if conds:
            stmt = stmt.where(or_(*conds))
    return stmt


async def list_outbox(
    session: AsyncSession, unit_ids: Sequence[uuid.UUID], q: PageQuery, *, status: str | None, kind: str | None, created_from: datetime | None = None, created_to: datetime | None = None
) -> tuple[list[OutboxMessage], int]:
    """Page of outbox rows, newest first (fixed sort)."""
    stmt = outbox_select(unit_ids, status=status, kind=kind, search=q.search, created_from=created_from, created_to=created_to)
    return await paginate_query(session, stmt, q, order_by=[OutboxMessage.created_at.desc(), OutboxMessage.id.desc()])


async def outbox_counts(
    session: AsyncSession, unit_ids: Sequence[uuid.UUID], *, kind: str | None, search: str | None, created_from: datetime | None = None, created_to: datetime | None = None
) -> dict[str, int]:
    """status → count for the outbox filters (one GROUP BY instead of one request per status tab)."""
    base = (
        outbox_select(unit_ids, status=None, kind=kind, search=search, created_from=created_from, created_to=created_to)
        .with_only_columns(OutboxMessage.status, func.count())
        .order_by(None)
        .group_by(OutboxMessage.status)
    )
    return {str(st): int(n) for st, n in (await session.execute(base)).all()}


async def get_outbox(session: AsyncSession, unit_ids: Sequence[uuid.UUID], message_id: uuid.UUID, *, lock: bool = False) -> OutboxMessage | None:
    """One alive outbox row of the given facilities (`lock` = FOR UPDATE: whoever acts on it next waits)."""
    stmt = select(OutboxMessage).where(OutboxMessage.id == message_id, OutboxMessage.unit_id.in_(unit_ids), alive(OutboxMessage))
    if lock:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def outbox_names(session: AsyncSession, rows: Sequence[OutboxMessage]) -> tuple[dict[uuid.UUID, str], dict[uuid.UUID, str], dict[uuid.UUID, str]]:
    """`(patient names, cheque numbers, sender names)` for a page of outbox rows — one query per kind that is
    present. Deleted rows are named too: the journal still says who a message was for."""

    async def named(model: type, column, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
        if not ids:
            return {}
        return {i: str(n) for i, n in (await session.execute(select(model.id, column).where(model.id.in_(ids)))).all()}

    return (
        await named(Patient, Patient.full_name, {m.patient_id for m in rows if m.patient_id}),
        await named(Order, Order.number, {m.order_id for m in rows if m.order_id}),
        await named(Employee, Employee.full_name, {m.created_by for m in rows if m.created_by}),
    )


async def cancel_outbox(session: AsyncSession, message_id: uuid.UUID, now: datetime) -> OutboxMessage | None:
    """`scheduled` / `queued` → `cancelled`, in one statement: a row the dispatcher has already picked up
    (`sending`) or finished is left alone and None comes back."""
    stmt = (
        update(OutboxMessage)
        .where(OutboxMessage.id == message_id, alive(OutboxMessage), OutboxMessage.status.in_(QUEUE_STATUSES))
        .values(status="cancelled", next_attempt_at=None, leased_until=None, updated_at=now)
        .returning(OutboxMessage)
        .execution_options(populate_existing=True)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def repeat_in_flight(session: AsyncSession, original: OutboxMessage) -> bool:
    """Is a copy of `original` (sent again from the journal) still on its way?"""
    stmt = (
        select(OutboxMessage.id)
        .where(
            alive(OutboxMessage),
            OutboxMessage.unit_id == original.unit_id,
            OutboxMessage.to == original.to,
            OutboxMessage.status.in_((*QUEUE_STATUSES, "sending")),
            OutboxMessage.payload["resendOf"].astext == str(original.id),
        )
        .limit(1)
    )
    return (await session.execute(stmt)).first() is not None


async def count_queued(session: AsyncSession, unit_ids: Sequence[uuid.UUID] | None) -> int:
    """Rows still waiting to go out (`queued` / `scheduled`) in the given facilities; None = everywhere."""
    stmt = select(func.count()).select_from(OutboxMessage).where(alive(OutboxMessage), OutboxMessage.status.in_(QUEUE_STATUSES))
    if unit_ids is not None:
        if not unit_ids:
            return 0
        stmt = stmt.where(OutboxMessage.unit_id.in_(unit_ids))
    return int((await session.execute(stmt)).scalar_one())


async def latest_order_unit(session: AsyncSession, patient_id: uuid.UUID) -> uuid.UUID | None:
    """The facility of the patient's newest alive cheque (where their messages go out from when unregistered)."""
    stmt = select(Order.unit_id).where(Order.patient_id == patient_id, alive(Order)).order_by(Order.created_at.desc(), Order.id.desc()).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


# ----------------------------------------------------------------------------- notifications


def _visible_notifications(employee_id: uuid.UUID, roots: Sequence[uuid.UUID] | None):
    """Filter: alive, addressed to everyone or to me, and in my scope — `unit_id` NULL (everyone), under one of my
    scope roots, or on the path above them (a region-wide notice reaches its facilities). `roots` None = superadmin."""
    conds = [alive(Notification), or_(Notification.employee_id.is_(None), Notification.employee_id == employee_id)]
    if roots is not None:
        if not roots:
            conds.append(Notification.unit_id.is_(None))
        else:
            below = select(OrgUnitClosure.descendant_id).where(OrgUnitClosure.ancestor_id.in_(roots))
            above = select(OrgUnitClosure.ancestor_id).where(OrgUnitClosure.descendant_id.in_(roots))
            conds.append(or_(Notification.unit_id.is_(None), Notification.unit_id.in_(below), Notification.unit_id.in_(above)))
    return conds


async def list_notifications(session: AsyncSession, employee_id: uuid.UUID, roots: Sequence[uuid.UUID] | None, limit: int = 50) -> list[Notification]:
    """Newest notifications one employee may see (see `_visible_notifications`)."""
    stmt = select(Notification).where(*_visible_notifications(employee_id, roots)).order_by(Notification.created_at.desc(), Notification.id.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def mark_read(session: AsyncSession, employee_id: uuid.UUID, roots: Sequence[uuid.UUID] | None, notification_id: uuid.UUID | None) -> int:
    """Append `employee_id` to read_by of one (or every visible) notification; returns rows touched."""
    stmt = (
        update(Notification)
        .where(*_visible_notifications(employee_id, roots), ~Notification.read_by.any(employee_id))
        .values(read_by=func.array_append(Notification.read_by, employee_id))
    )
    if notification_id is not None:
        stmt = stmt.where(Notification.id == notification_id)
    return (await session.execute(stmt)).rowcount


# ----------------------------------------------------------------------------- dispatcher / maintenance


async def claim_batch(session: AsyncSession, now: datetime, batch_size: int, lease_seconds: int = 60) -> list[OutboxMessage]:
    """Lock up to `batch_size` due rows (SKIP LOCKED) and mark them `sending` with a lease."""
    due = (
        select(OutboxMessage.id)
        .where(
            alive(OutboxMessage),
            OutboxMessage.status.in_(QUEUE_STATUSES),
            OutboxMessage.next_attempt_at <= now,
            or_(OutboxMessage.leased_until.is_(None), OutboxMessage.leased_until < now),
        )
        .order_by(OutboxMessage.next_attempt_at)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )
    ids = list((await session.execute(due)).scalars().all())
    if not ids:
        return []
    stmt = (
        update(OutboxMessage)
        .where(OutboxMessage.id.in_(ids))
        .values(status="sending", leased_until=now + timedelta(seconds=lease_seconds), updated_at=now)
        .returning(OutboxMessage)
    )
    return list((await session.execute(stmt)).scalars().all())


async def load_messages(session: AsyncSession, ids: list[uuid.UUID]) -> list[OutboxMessage]:
    """Re-read a claimed batch in the delivery session (one query, original claim order)."""
    rows = (await session.execute(select(OutboxMessage).where(OutboxMessage.id.in_(ids)))).scalars().all()
    by_id = {r.id: r for r in rows}
    return [by_id[i] for i in ids if i in by_id]


async def load_accounts(session: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, MessagingAccount]:
    """The messaging accounts a batch was queued with, in one query (deleted / inactive rows included — the
    dispatcher decides whether they may still send)."""
    if not ids:
        return {}
    rows = (await session.execute(select(MessagingAccount).where(MessagingAccount.id.in_(ids)))).scalars().all()
    return {a.id: a for a in rows}


async def ensure_audit_partitions(session: AsyncSession, months_ahead: int = 3) -> None:
    """Pre-create monthly audit_log partitions (SQL function from the initial migration)."""
    await session.execute(text("SELECT ensure_audit_partitions(:m)"), {"m": months_ahead})


async def requeue_stale_sending(session: AsyncSession, now: datetime) -> int:
    """Rows a crashed worker left in `sending` past their lease go back to the queue."""
    stmt = (
        update(OutboxMessage)
        .where(alive(OutboxMessage), OutboxMessage.status == "sending", OutboxMessage.leased_until < now)
        .values(status="queued", leased_until=None, next_attempt_at=now, updated_at=now)
    )
    return (await session.execute(stmt)).rowcount


async def prune_expired_sessions(session: AsyncSession, now: datetime, keep: timedelta = timedelta(days=30)) -> int:
    """Physically drop session rows expired for longer than `keep` (ephemeral security data, not domain data)."""
    return (await session.execute(delete(SessionModel).where(SessionModel.expires_at < now - keep))).rowcount


async def prune_expired_otp_challenges(session: AsyncSession, now: datetime, keep: timedelta = timedelta(days=1)) -> int:
    """Physically drop OTP challenges expired for longer than `keep`."""
    return (await session.execute(delete(OtpChallenge).where(OtpChallenge.expires_at < now - keep))).rowcount


async def promote_due_scheduled(session: AsyncSession, now: datetime) -> int:
    """`scheduled` → `queued` once the scheduled moment has passed."""
    stmt = (
        update(OutboxMessage)
        .where(alive(OutboxMessage), OutboxMessage.status == "scheduled", OutboxMessage.scheduled_at <= now)
        .values(status="queued", next_attempt_at=func.least(OutboxMessage.next_attempt_at, now), updated_at=now)
    )
    return (await session.execute(stmt)).rowcount
