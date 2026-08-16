"""Password hashing, access tokens and secret masking.

* Passwords — argon2id (memory-hard, side-channel resistant), auto-rehash on
  parameter upgrades.
* Access tokens — signed JWT carrying only `sub`, `act` (staff|patient), `jti`,
  `exp`. The `jti` is looked up in Redis (session allow-list) so logout /
  revocation is immediate even though the token itself is stateless.
* OTP / API-key style secrets are stored as SHA-256 digests only.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal, TypedDict

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from app.core.config import settings
from app.core.exceptions import AuthError

_hasher = PasswordHasher(time_cost=2, memory_cost=64 * 1024, parallelism=2, hash_len=32, salt_len=16)

Actor = Literal["staff", "patient"]


class TokenClaims(TypedDict):
    sub: str
    act: Actor
    jti: str
    exp: int
    iat: int


def hash_password(raw: str) -> str:
    return _hasher.hash(raw)


def verify_password(raw: str, hashed: str | None) -> bool:
    if not hashed:
        # Burn similar CPU so a missing user is not distinguishable by timing.
        _hasher.hash(raw)
        return False
    try:
        return _hasher.verify(hashed, raw)
    except (VerifyMismatchError, InvalidHashError):
        return False


def needs_rehash(hashed: str) -> bool:
    try:
        return _hasher.check_needs_rehash(hashed)
    except InvalidHashError:
        return True


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def constant_time_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def random_digits(length: int) -> str:
    return "".join(secrets.choice("0123456789") for _ in range(length))


def random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def mask_secret(value: str, keep: int = 4) -> str:
    """'xab_live_abcdef7f2a' → 'xab_live_••••••••7f2a'. Keeps a recognisable prefix."""
    if not value:
        return ""
    prefix = value.split("_")
    head = "_".join(prefix[:2]) + "_" if len(prefix) >= 3 else ""
    return f"{head}{'•' * 8}{value[-keep:]}"


def issue_token(subject: uuid.UUID | str, actor: Actor, ttl: timedelta) -> tuple[str, str, datetime]:
    """Returns (token, jti, expires_at)."""
    now = datetime.now(UTC)
    exp = now + ttl
    jti = uuid.uuid4().hex
    claims: TokenClaims = {"sub": str(subject), "act": actor, "jti": jti, "exp": int(exp.timestamp()), "iat": int(now.timestamp())}
    token = jwt.encode(dict(claims), settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, jti, exp


def decode_token(token: str) -> TokenClaims:
    try:
        data = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm], options={"require": ["exp", "sub", "jti"]})
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("Sessiya muddati tugagan", code="token_expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError("Token yaroqsiz", code="token_invalid") from exc
    if data.get("act") not in ("staff", "patient"):
        raise AuthError("Token yaroqsiz", code="token_invalid")
    return data  # type: ignore[return-value]
