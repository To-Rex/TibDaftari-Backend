"""Periodic maintenance (every 5 min from `workers/runner.py`): audit partitions, stuck rows, due schedules."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from app.infrastructure.db.session import session_scope
from app.modules.messaging import repository as repo

log = logging.getLogger("maintenance")


async def maintenance_once() -> dict[str, int]:
    """Ensure audit_log partitions exist, requeue stale `sending` rows, promote due `scheduled` rows,
    prune long-expired sessions / OTP challenges."""
    now = datetime.now(UTC)
    async with session_scope() as session:
        await repo.ensure_audit_partitions(session, 3)
        stale = await repo.requeue_stale_sending(session, now)
        promoted = await repo.promote_due_scheduled(session, now)
        sessions = await repo.prune_expired_sessions(session, now)
        otps = await repo.prune_expired_otp_challenges(session, now)
    if stale or promoted or sessions or otps:
        log.info("maintenance: requeued %d stale sending, promoted %d scheduled, pruned %d sessions, %d otp", stale, promoted, sessions, otps)
    return {"stale": stale, "promoted": promoted, "sessions": sessions, "otp": otps}
