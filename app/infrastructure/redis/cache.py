"""Tiny JSON cache helpers with per-company namespaces (invalidate on write). Fail-open."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import orjson

from app.infrastructure.redis.client import get_redis


async def get_json(key: str) -> Any | None:
    try:
        raw = await get_redis().get(key)
    except Exception:  # pragma: no cover
        return None
    return orjson.loads(raw) if raw else None


async def set_json(key: str, value: Any, ttl_seconds: int) -> None:
    try:
        await get_redis().set(key, orjson.dumps(value, default=str), ex=ttl_seconds)
    except Exception:  # pragma: no cover
        pass


async def delete(*keys: str) -> None:
    if not keys:
        return
    try:
        await get_redis().delete(*keys)
    except Exception:  # pragma: no cover
        pass


async def delete_prefix(prefix: str) -> None:
    """Invalidate a namespace (SCAN, non-blocking)."""
    try:
        r = get_redis()
        async for key in r.scan_iter(match=f"{prefix}*", count=200):
            await r.delete(key)
    except Exception:  # pragma: no cover
        pass


async def cached(key: str, ttl_seconds: int, loader: Callable[[], Awaitable[Any]]) -> Any:
    hit = await get_json(key)
    if hit is not None:
        return hit
    value = await loader()
    await set_json(key, value, ttl_seconds)
    return value
