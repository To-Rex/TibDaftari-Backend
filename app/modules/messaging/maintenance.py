"""Periodic maintenance (every 5 min from `workers/runner.py`): audit partitions, stuck rows, due schedules."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from app.infrastructure.db.session import session_scope
from app.modules.assistant import service as assistant_svc
from app.modules.messaging import eskiz_sync
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
        files = await assistant_svc.purge_expired_files(session)
        try:
            synced = await eskiz_sync.sync_eskiz_statuses(session, now)
        except Exception:  # a provider hiccup never stops the rest of the maintenance
            log.exception("maintenance: eskiz status sync failed")
            synced = {"checked": 0, "delivered": 0, "failed": 0, "skipped": 0}
    if stale or promoted or sessions or otps or files or synced["checked"]:
        log.info(
            "maintenance: requeued %d stale sending, promoted %d scheduled, pruned %d sessions, %d otp, %d assistant files; eskiz: %s",
            stale, promoted, sessions, otps, files, synced,
        )
    return {"stale": stale, "promoted": promoted, "sessions": sessions, "otp": otps, "files": files, **{f"eskiz_{k}": v for k, v in synced.items()}}
