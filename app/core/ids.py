"""Time-ordered UUIDv7 primary keys (index locality for append-heavy tables)."""

from __future__ import annotations

import os
import time
import uuid

_last_ms = 0
_seq = 0


def uuid7() -> uuid.UUID:
    """RFC 9562 UUIDv7: 48-bit ms timestamp, 12-bit monotonic sequence, 62 random bits."""
    global _last_ms, _seq
    ms = time.time_ns() // 1_000_000
    if ms == _last_ms:
        _seq = (_seq + 1) & 0x0FFF
        if _seq == 0:  # sequence overflow inside one ms → step time forward
            ms += 1
    else:
        _seq = int.from_bytes(os.urandom(2), "big") & 0x0FFF
    _last_ms = ms
    rand = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
    value = (ms << 80) | (0x7 << 76) | (_seq << 64) | (0b10 << 62) | rand
    return uuid.UUID(int=value)


def new_id() -> str:
    return str(uuid7())
