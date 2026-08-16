"""Background workers started inside the API process (single dispatcher, Redis-leased)."""

from __future__ import annotations

import asyncio
import logging

from app.core.config import settings
from app.infrastructure.redis.lock import try_lease

log = logging.getLogger("workers")


async def _loop(name: str, fn, interval: float) -> None:
    """Runs `fn()` every `interval` seconds while holding a Redis lease (one runner per name)."""
    while True:
        try:
            async with try_lease(f"lease:worker:{name}", int(interval * 1000) + 30_000) as lease:
                if lease is not None:
                    await fn()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover
            log.exception("worker %s failed", name)
        await asyncio.sleep(interval)


async def start_workers() -> list[asyncio.Task]:
    from app.modules.messaging.dispatcher import dispatch_outbox_once
    from app.modules.messaging.maintenance import maintenance_once

    tasks = [
        asyncio.create_task(_loop("outbox", dispatch_outbox_once, settings.outbox_poll_seconds), name="worker:outbox"),
        asyncio.create_task(_loop("maintenance", maintenance_once, 300), name="worker:maintenance"),
    ]
    log.info("workers started: %s", [t.get_name() for t in tasks])
    return tasks
