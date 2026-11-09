"""Shared async Redis client (connection pool per process).

`REDIS_URL=fake://` (tests / local development without a Redis server) uses an in-process fake server
(`fakeredis`) with the same async API — never used in production.
"""

from __future__ import annotations

from redis.asyncio import Redis, from_url

from app.core.config import settings

_client: Redis | None = None


def _make_client() -> Redis:
    if settings.redis_url.startswith("fake://"):
        from fakeredis import aioredis as fake  # test-only dependency

        return fake.FakeRedis(encoding="utf-8", decode_responses=True)
    return from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
        socket_timeout=5,
        socket_connect_timeout=5,
        health_check_interval=30,
        max_connections=50,
    )


def get_redis() -> Redis:
    global _client
    if _client is None:
        _client = _make_client()
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
