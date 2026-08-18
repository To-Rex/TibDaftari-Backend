"""Messaging SQL helpers — outbox listing, notifications, queue claiming, maintenance updates."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import Select, delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import paginate_query
from app.core.schemas import PageQuery
from app.core.textutil import digits, fold
from app.infrastructure.db.base import alive
from app.infrastructure.db.models import Company, Notification, OtpChallenge, OutboxMessage
from app.infrastructure.db.models import Session as SessionModel

QUEUE_STATUSES = ("queued", "scheduled")


def outbox_select(company_id: uuid.UUID, *, status: str | None, kind: str | None, search: str | None) -> Select:
    """Company-scoped outbox statement with the §8 filters (uses ix_outbox_company_status_created)."""
    stmt = select(OutboxMessage).where(OutboxMessage.company_id == company_id, alive(OutboxMessage))
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


async def list_outbox(session: AsyncSession, company_id: uuid.UUID, q: PageQuery, *, status: str | None, kind: str | None) -> tuple[list[OutboxMessage], int]:
    """Page of outbox rows, newest first (fixed sort)."""
    stmt = outbox_select(company_id, status=status, kind=kind, search=q.search)
    return await paginate_query(session, stmt, q, order_by=[OutboxMessage.created_at.desc(), OutboxMessage.id.desc()])


async def outbox_counts(session: AsyncSession, company_id: uuid.UUID, *, kind: str | None, search: str | None) -> dict[str, int]:
    """status → count for the outbox filters (one GROUP BY instead of one request per status tab)."""
    base = outbox_select(company_id, status=None, kind=kind, search=search).with_only_columns(OutboxMessage.status, func.count()).order_by(None).group_by(OutboxMessage.status)
    return {str(st): int(n) for st, n in (await session.execute(base)).all()}


async def list_notifications(session: AsyncSession, company_id: uuid.UUID, employee_id: uuid.UUID, limit: int = 50) -> list[Notification]:
    """Newest company-wide + personal notifications for one employee."""
    stmt = (
        select(Notification)
        .where(
            Notification.company_id == company_id,
            alive(Notification),
            or_(Notification.employee_id.is_(None), Notification.employee_id == employee_id),
        )
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def mark_read(session: AsyncSession, company_id: uuid.UUID, employee_id: uuid.UUID, notification_id: uuid.UUID | None) -> int:
    """Append `employee_id` to read_by of one (or every visible) notification; returns rows touched."""
    stmt = (
        update(Notification)
        .where(
            Notification.company_id == company_id,
            alive(Notification),
            or_(Notification.employee_id.is_(None), Notification.employee_id == employee_id),
            ~Notification.read_by.any(employee_id),
        )
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


async def load_companies(session: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, Company]:
    """Companies for a batch in one query (SMS provider settings)."""
    if not ids:
        return {}
    rows = (await session.execute(select(Company).where(Company.id.in_(ids)))).scalars().all()
    return {c.id: c for c in rows}


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
