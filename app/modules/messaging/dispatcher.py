"""Outbox dispatcher — delivers queued messages (SMS via Xabarchi, Telegram via the company bot).

Runs from `workers/runner.py` every `settings.outbox_poll_seconds` under a Redis lease.
One pass = claim a batch (FOR UPDATE SKIP LOCKED → `sending` + 60s lease, committed), then
deliver each message and persist the outcome. Nothing here runs inside an HTTP request.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.crypto import decrypt
from app.infrastructure.db.models import Company, OutboxMessage
from app.infrastructure.db.session import session_scope
from app.modules.messaging import repository as repo
from app.modules.messaging import xabarchi
from app.modules.messaging.service import mark_document_delivery, outgoing_text
from app.modules.messaging.xabarchi import XabarchiError, XabarchiTransientError

log = logging.getLogger("outbox")

BACKOFF: tuple[timedelta, ...] = (timedelta(seconds=30), timedelta(minutes=2), timedelta(minutes=10), timedelta(minutes=30), timedelta(hours=2))
LEASE_SECONDS = 60
NOT_READY_RETRY = timedelta(minutes=5)


def backoff_for(attempts: int) -> timedelta:
    """Delay before the next try after `attempts` failed attempts (1-based, capped at the last step)."""
    return BACKOFF[min(max(attempts, 1), len(BACKOFF)) - 1]


def _mark_sent(msg: OutboxMessage, now: datetime, provider_id: str | None) -> None:
    msg.status = "sent"
    msg.sent_at = now
    msg.provider_message_id = provider_id
    msg.error = None
    msg.leased_until = None
    msg.next_attempt_at = None


def _mark_failed(msg: OutboxMessage, error: str) -> None:
    msg.status = "failed"
    msg.error = error[:2000]
    msg.leased_until = None
    msg.next_attempt_at = None


def _mark_retry(msg: OutboxMessage, now: datetime, error: str) -> None:
    """Transient failure: count the attempt and re-queue with backoff, or give up after max attempts."""
    msg.attempts = (msg.attempts or 0) + 1
    if msg.attempts >= settings.outbox_max_attempts:
        _mark_failed(msg, error)
        return
    msg.status = "queued"
    msg.error = error[:2000]
    msg.leased_until = None
    msg.next_attempt_at = now + backoff_for(msg.attempts)


def _defer(msg: OutboxMessage, now: datetime, delay: timedelta, note: str) -> None:
    """Channel not ready (e.g. bot not started): keep the row queued without burning an attempt."""
    msg.status = "queued"
    msg.error = note[:2000]
    msg.leased_until = None
    msg.next_attempt_at = now + delay


async def _deliver_sms(msg: OutboxMessage, company: Company | None, now: datetime) -> None:
    api_key = decrypt(company.sms_api_key_enc) if company and company.sms_provider != "none" else None
    if not api_key:
        _mark_failed(msg, "sms_not_configured")
        return
    priority = company.sms_default_priority if company else "transactional"
    try:
        results = await xabarchi.send_sms(api_key, [msg.to], outgoing_text(msg), priority)
    except XabarchiTransientError as exc:
        _mark_retry(msg, now, exc.message)
        return
    except XabarchiError as exc:
        _mark_failed(msg, exc.message)
        return
    provider_id = next((r.provider_id for r in results if r.provider_id), None)
    _mark_sent(msg, now, provider_id)


async def _deliver_telegram(session: AsyncSession, msg: OutboxMessage, now: datetime) -> None:
    from app.modules.telegram.manager import bot_manager

    try:
        ok = await bot_manager.deliver(session, msg)
    except (NotImplementedError, AttributeError) as exc:
        _defer(msg, now, NOT_READY_RETRY, f"telegram_not_ready: {exc.__class__.__name__}")
        return
    except Exception as exc:  # network / API errors → retry with backoff
        _mark_retry(msg, now, f"{exc.__class__.__name__}: {exc}"[:500])
        return
    if ok:
        _mark_sent(msg, now, msg.provider_message_id)
    else:
        _mark_failed(msg, msg.error or "telegram_not_configured")


async def deliver_one(session: AsyncSession, msg: OutboxMessage, company: Company | None) -> str:
    """Deliver a claimed (`sending`) message and update it in place; returns the resulting status."""
    now = datetime.now(UTC)
    if msg.channel == "sms":
        await _deliver_sms(msg, company, now)
    elif msg.channel == "telegram":
        await _deliver_telegram(session, msg, now)
    else:  # portal messages are informational — nothing to push
        _mark_sent(msg, now, None)
    msg.updated_at = now
    if msg.document_id and msg.status in ("sent", "failed"):
        await mark_document_delivery(session, msg.document_id, msg.channel, msg.status, msg.error)
    return msg.status


async def dispatch_outbox_once() -> int:
    """One dispatcher pass: claim due rows, deliver each, persist outcomes. Returns rows processed."""
    now = datetime.now(UTC)
    async with session_scope() as session:
        claimed = await repo.claim_batch(session, now, settings.outbox_batch_size, LEASE_SECONDS)
        ids = [m.id for m in claimed]
    if not ids:
        return 0
    counts: dict[str, int] = {}
    async with session_scope() as session:
        rows = await repo.load_messages(session, ids)
        companies = await repo.load_companies(session, {r.company_id for r in rows})
        for msg in rows:
            try:
                # SAVEPOINT per row: a DB-side failure inside delivery rolls back only this row's
                # writes and leaves the session usable for the rest of the batch.
                async with session.begin_nested():
                    status = await deliver_one(session, msg, companies.get(msg.company_id))
            except Exception:  # never let one bad row poison the batch
                log.exception("outbox %s: unexpected delivery error", msg.id)
                await session.refresh(msg)
                _mark_retry(msg, datetime.now(UTC), "internal_error")
                status = msg.status
            counts[status] = counts.get(status, 0) + 1
            try:
                await session.commit()  # each outcome is durable on its own — a crash never re-sends a sent SMS
            except Exception:  # connection-level failure: remaining rows are re-queued once their lease ends
                log.exception("outbox %s: commit failed", msg.id)
                await session.rollback()
    log.info("outbox pass: %d processed %s", len(rows), counts)
    return len(rows)
