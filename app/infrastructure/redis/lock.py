"""Distributed lease lock (SET NX PX) for single-runner background workers."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.infrastructure.redis.client import get_redis

_RELEASE = "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) else return 0 end"
_EXTEND = "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('PEXPIRE', KEYS[1], ARGV[2]) else return 0 end"


class Lease:
    def __init__(self, key: str, ttl_ms: int):
        self.key, self.ttl_ms, self.token = key, ttl_ms, secrets.token_hex(8)

    async def acquire(self) -> bool:
        return bool(await get_redis().set(self.key, self.token, nx=True, px=self.ttl_ms))

    async def extend(self) -> bool:
        return bool(await get_redis().eval(_EXTEND, 1, self.key, self.token, self.ttl_ms))  # type: ignore[misc]

    async def release(self) -> None:
        try:
            await get_redis().eval(_RELEASE, 1, self.key, self.token)  # type: ignore[misc]
        except Exception:  # pragma: no cover
            pass


@asynccontextmanager
async def try_lease(key: str, ttl_ms: int) -> AsyncIterator[Lease | None]:
    """Yields the lease when acquired, else None. Redis outage → yields a lease (single-instance fallback)."""
    lease = Lease(key, ttl_ms)
    try:
        ok = await lease.acquire()
    except Exception:  # pragma: no cover
        ok = True
    try:
        yield lease if ok else None
    finally:
        if ok:
            await lease.release()
