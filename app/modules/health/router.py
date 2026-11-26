"""Liveness / readiness probes."""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from app.api.deps import DbSession
from app.core.config import settings
from app.infrastructure.redis.client import get_redis

router = APIRouter()


@router.get("/health", summary="Liveness")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready", summary="Readiness (DB + Redis)")
async def ready(session: DbSession) -> dict[str, str]:
    await session.execute(text("SELECT 1"))
    redis = "ok"
    try:
        await get_redis().ping()
    except Exception:
        redis = "down"
    from app.modules.training import sandbox

    # the background workers (SMS outbox, maintenance, the assistant's scheduled tasks) run inside this process when enabled
    return {
        "status": "ok", "db": "ok", "redis": redis, "workers": "on" if settings.workers_enabled else "off",
        # the sandbox new employees practise in (see app/modules/training)
        "training": sandbox.state() if settings.training_enabled else "off",
    }  # fmt: skip
