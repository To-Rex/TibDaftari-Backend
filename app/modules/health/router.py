"""Liveness / readiness probes."""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from app.api.deps import DbSession
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
    return {"status": "ok", "db": "ok", "redis": redis}
