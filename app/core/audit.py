"""Append-only audit trail. Called by services after meaningful mutations.

Nothing in the system is ever physically deleted; the audit log additionally records
who changed what, with before/after snapshots (small dicts — never binary blobs).
Rows carry the unit they concern (the facility, or the node being administered) and its country.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.models import AuditLog

_SECRET_KEYS = {"password", "passwordHash", "password_hash", "apiKey", "api_key", "api_key_enc", "sms_api_key_enc", "telegram_bot_token_enc", "botToken"}


def _scrub(data: dict[str, Any] | None) -> dict[str, Any] | None:
    if not data:
        return data
    return {k: ("***" if k in _SECRET_KEYS else v) for k, v in data.items()}


def _uuid(value: uuid.UUID | str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except ValueError:
        return None


async def audit(
    session: AsyncSession,
    *,
    actor_type: str,
    actor_id: uuid.UUID | str | None,
    action: str,
    entity: str,
    entity_id: uuid.UUID | str | None = None,
    unit_id: uuid.UUID | str | None = None,
    country_id: uuid.UUID | str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    ip: str | None = None,
    request_id: str | None = None,
) -> None:
    session.add(
        AuditLog(
            actor_type=actor_type,
            actor_id=_uuid(actor_id),
            unit_id=_uuid(unit_id),
            country_id=_uuid(country_id),
            action=action,
            entity=entity,
            entity_id=str(entity_id) if entity_id else None,
            before=_scrub(before),
            after=_scrub(after),
            ip=ip,
            request_id=request_id,
        )
    )
