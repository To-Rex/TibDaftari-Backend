"""Fixed-window rate limiter and login lockout counters (Redis, atomic via INCR+EXPIRE)."""

from __future__ import annotations

from app.core.exceptions import RateLimitedError
from app.infrastructure.redis.client import get_redis

_LUA = """
local c = redis.call('INCR', KEYS[1])
if c == 1 then redis.call('PEXPIRE', KEYS[1], ARGV[1]) end
local ttl = redis.call('PTTL', KEYS[1])
return {c, ttl}
"""

DEFAULT_MESSAGE = "Juda ko‘p urinish. Keyinroq urinib ko‘ring."


async def hit(key: str, window_seconds: int) -> tuple[int, int]:
    """Increment `key`; returns (count, ttl_ms). Fails open if Redis is unavailable."""
    try:
        r = get_redis()
        count, ttl = await r.eval(_LUA, 1, key, window_seconds * 1000)  # type: ignore[misc]
        return int(count), int(ttl)
    except Exception:  # pragma: no cover - degraded mode
        return 0, 0


async def enforce(key: str, limit: int, window_seconds: int, message: str = DEFAULT_MESSAGE) -> None:
    count, ttl = await hit(key, window_seconds)
    if count > limit:
        raise RateLimitedError(message, details={"retryAfter": max(1, ttl // 1000)})


async def get_count(key: str) -> int:
    try:
        v = await get_redis().get(key)
        return int(v) if v else 0
    except Exception:  # pragma: no cover
        return 0


async def reset(key: str) -> None:
    try:
        await get_redis().delete(key)
    except Exception:  # pragma: no cover
        pass
