"""Symmetric encryption for secrets at rest (Xabarchi API keys, Telegram bot tokens).

Uses Fernet (AES-128-CBC + HMAC-SHA256, versioned, timestamped). The key comes
from ENCRYPTION_KEY; when it is empty (development) a key is derived from
JWT_SECRET so the app still boots — production deployments must set it.
"""

from __future__ import annotations

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = settings.encryption_key.strip()
    if not key:
        digest = hashlib.sha256(("tibdaftari:" + settings.jwt_secret).encode()).digest()
        key = base64.urlsafe_b64encode(digest).decode()
    return Fernet(key)


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(token: str | None) -> str | None:
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken:
        return None
