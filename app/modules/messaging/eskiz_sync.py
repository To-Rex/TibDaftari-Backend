"""Delivery states without waiting for a callback: every maintenance pass asks Eskiz about the messages it accepted
in the last days — `status_by_id` for single sends, the dispatch list for batches — writes what it learns into
`payload.eskiz` {status, parts, price, totalPrice, smsc, syncedAt} and flips the row to `delivered` / `failed`
once the state is final. Rows already final, older than the window, or asked less than ten minutes ago are left
alone; a provider problem only postpones the next look."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt
from app.infrastructure.db.base import alive
from app.infrastructure.db.models import MessagingAccount, OutboxMessage
from app.modules.messaging import eskiz
from app.modules.messaging.eskiz import DELIVERED_STATES, FAILED_STATES
from app.modules.messaging.providers import SmsProviderError, SmsTransientError, account_to_sms
from app.modules.messaging.service import mark_document_delivery

log = logging.getLogger("eskiz-sync")

SYNC_WINDOW = timedelta(days=3)
RESYNC_AFTER = timedelta(minutes=10)
BATCH = 200


def _num(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def apply_status(msg: OutboxMessage, data: Mapping[str, Any] | None, now: datetime) -> str | None:
    """Write Eskiz's answer for one message into the row; returns the final status it reached (delivered / failed)
    or None when the message is still on its way (or unknown). A delivered row never goes back."""
    info: dict[str, Any] = {"syncedAt": now.isoformat()}
    final: str | None = None
    state = ""
    reason = ""
    if data:
        state = str(data.get("status") or "").strip().upper()
        parts = [p for p in (data.get("parts") or []) if isinstance(p, dict)] if isinstance(data.get("parts"), list) else []
        part_states = {str(p.get("dlr_state") or "").strip().upper() for p in parts} - {""}
        if state in DELIVERED_STATES or (part_states and part_states <= DELIVERED_STATES):
            final = "delivered"
        elif state in FAILED_STATES or (part_states and part_states & FAILED_STATES and not part_states & DELIVERED_STATES):
            final = "failed"
            reason = state if state in FAILED_STATES else sorted(part_states & FAILED_STATES)[0]
        count = _num(data.get("parts_count"))
        info.update({
            "status": state or None,
            "parts": int(count) if count else (len(parts) or None),
            "price": _num(data.get("price")),
            "totalPrice": _num(data.get("total_price")),
            "smsc": str(data["smsc_id"]) if data.get("smsc_id") not in (None, "") else None,
        })
    previous = (msg.payload or {}).get("eskiz") or {}
    msg.payload = {**(msg.payload or {}), "eskiz": {**previous, **{k: v for k, v in info.items() if v is not None}}}
    if final and msg.status != "delivered" and msg.status != final:
        msg.status = final
        msg.error = None if final == "delivered" else f"eskiz:{reason or 'UNDELIV'}"
        msg.updated_at = now
        return final
    return None


async def _due_rows(session: AsyncSession, now: datetime) -> list[tuple[OutboxMessage, MessagingAccount]]:
    synced = OutboxMessage.payload["eskiz"]["syncedAt"].astext
    stmt = (
        select(OutboxMessage, MessagingAccount)
        .join(MessagingAccount, MessagingAccount.id == OutboxMessage.account_id)
        .where(
            OutboxMessage.channel == "sms",
            OutboxMessage.status == "sent",
            OutboxMessage.sent_at >= now - SYNC_WINDOW,
            OutboxMessage.provider_message_id.is_not(None),
            MessagingAccount.provider == "eskiz",
            alive(OutboxMessage),
            or_(synced.is_(None), synced < (now - RESYNC_AFTER).isoformat()),
        )
        .order_by(OutboxMessage.sent_at.asc())
        .limit(BATCH)
    )
    return [(m, a) for m, a in (await session.execute(stmt)).all()]


async def _finish(session: AsyncSession, msg: OutboxMessage, final: str | None, counts: dict[str, int]) -> None:
    counts["checked"] += 1
    if final:
        counts[final] += 1
        if msg.document_id:
            await mark_document_delivery(session, msg.document_id, "sms", final, msg.error)


async def sync_eskiz_statuses(session: AsyncSession, now: datetime | None = None) -> dict[str, int]:
    """One pass over the due rows, account by account. Returns {checked, delivered, failed, skipped}."""
    now = now or datetime.now(UTC)
    counts = {"checked": 0, "delivered": 0, "failed": 0, "skipped": 0}
    rows = await _due_rows(session, now)
    if not rows:
        return counts
    by_account: dict[uuid.UUID, tuple[MessagingAccount, list[OutboxMessage]]] = {}
    for msg, acc in rows:
        by_account.setdefault(acc.id, (acc, []))[1].append(msg)
    for acc, msgs in by_account.values():
        account = account_to_sms(acc)
        if account is None:
            for m in msgs:
                apply_status(m, None, now)
                counts["skipped"] += 1
            continue
        secret = decrypt(account.key_enc) or ""
        singles = [m for m in msgs if not (m.payload or {}).get("dispatchId")]
        batches: dict[int, list[OutboxMessage]] = {}
        for m in msgs:
            did = (m.payload or {}).get("dispatchId")
            if did:
                batches.setdefault(int(did), []).append(m)
        try:
            for m in singles:
                data = await eskiz.call_with_account(account.account_id or acc.id, account.login or "", secret, lambda tok, rid=m.provider_message_id: eskiz.status_by_id(tok, str(rid)))
                await _finish(session, m, apply_status(m, data, now), counts)
            for did, group in batches.items():
                items = await eskiz.call_with_account(account.account_id or acc.id, account.login or "", secret, lambda tok, d=did: eskiz.messages_by_dispatch(tok, d))
                by_key = {str(it.get("user_sms_id") or ""): it for it in items}
                for m in group:
                    data = by_key.get(str((m.payload or {}).get("eskizKey") or ""))
                    await _finish(session, m, apply_status(m, data, now), counts)
        except (SmsProviderError, SmsTransientError) as exc:
            # this account's turn is over for now — the rows keep their state and are looked at again later
            log.warning("eskiz-sync: account %s: %s", acc.id, exc.message)
            for m in msgs:
                if not ((m.payload or {}).get("eskiz") or {}).get("syncedAt", "") >= now.isoformat():
                    apply_status(m, None, now)
            counts["skipped"] += len(msgs)
    await session.flush()
    return counts
