"""Shared async Redis client (connection pool per process)."""

from __future__ import annotations

from redis.asyncio import Redis, from_url

from app.core.config import settings

_client: Redis | None = None


def get_redis() -> Redis:
    global _client
    if _client is None:
        _client = from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_timeout=5,
            socket_connect_timeout=5,
            health_check_interval=30,
            max_connections=50,
        )
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
