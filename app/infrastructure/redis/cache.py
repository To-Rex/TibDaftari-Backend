"""Tiny JSON cache helpers with per-company namespaces (invalidate on write). Fail-open."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextvars import ContextVar, Token
from functools import partial
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


# Writes invalidate immediately AND once more after the surrounding transaction commits: a
# concurrent reader between DELETE and COMMIT would otherwise repopulate the key with stale rows.
_deferred: ContextVar[list[Callable[[], Awaitable[None]]] | None] = ContextVar("cache_deferred", default=None)


def begin_deferred() -> Token[list[Callable[[], Awaitable[None]]] | None]:
    """Start collecting post-commit invalidations for the current task (called by the session scopes)."""
    return _deferred.set([])


def end_deferred(token: Token[list[Callable[[], Awaitable[None]]] | None]) -> None:
    _deferred.reset(token)


async def run_deferred() -> None:
    """Replay the collected invalidations (call right after COMMIT)."""
    pending = _deferred.get()
    if not pending:
        return
    fns, pending[:] = list(pending), []
    for fn in fns:
        await fn()


def _defer(fn: Callable[[], Awaitable[None]]) -> None:
    pending = _deferred.get()
    if pending is not None:
        pending.append(fn)


async def _delete_now(keys: tuple[str, ...]) -> None:
    try:
        await get_redis().delete(*keys)
    except Exception:  # pragma: no cover
        pass


async def _delete_prefix_now(prefix: str) -> None:
    try:
        r = get_redis()
        async for key in r.scan_iter(match=f"{prefix}*", count=200):
            await r.delete(key)
    except Exception:  # pragma: no cover
        pass


async def delete(*keys: str) -> None:
    if not keys:
        return
    await _delete_now(keys)
    _defer(partial(_delete_now, keys))


async def delete_prefix(prefix: str) -> None:
    """Invalidate a namespace (SCAN, non-blocking)."""
    await _delete_prefix_now(prefix)
    _defer(partial(_delete_prefix_now, prefix))


async def cached(key: str, ttl_seconds: int, loader: Callable[[], Awaitable[Any]]) -> Any:
    hit = await get_json(key)
    if hit is not None:
        return hit
    value = await loader()
    await set_json(key, value, ttl_seconds)
    return value
