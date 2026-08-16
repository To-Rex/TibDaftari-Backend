"""Append-only audit trail. Called by services after meaningful mutations.

Nothing in the system is ever physically deleted; the audit log additionally records
who changed what, with before/after snapshots (small dicts — never binary blobs).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.models import AuditLog

_SECRET_KEYS = {"password", "passwordHash", "password_hash", "apiKey", "api_key", "sms_api_key_enc", "telegram_bot_token_enc", "botToken"}


def _scrub(data: dict[str, Any] | None) -> dict[str, Any] | None:
    if not data:
        return data
    return {k: ("***" if k in _SECRET_KEYS else v) for k, v in data.items()}


async def audit(
    session: AsyncSession,
    *,
    actor_type: str,
    actor_id: uuid.UUID | str | None,
    company_id: uuid.UUID | str | None,
    action: str,
    entity: str,
    entity_id: uuid.UUID | str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    ip: str | None = None,
    request_id: str | None = None,
) -> None:
    session.add(
        AuditLog(
            actor_type=actor_type,
            actor_id=uuid.UUID(str(actor_id)) if actor_id else None,
            company_id=uuid.UUID(str(company_id)) if company_id else None,
            action=action,
            entity=entity,
            entity_id=str(entity_id) if entity_id else None,
            before=_scrub(before),
            after=_scrub(after),
            ip=ip,
            request_id=request_id,
        )
    )
