"""ORM row ⇄ JSON-safe dict, for Redis snapshots of hot rows (principals, unit scopes)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import Date, DateTime
from sqlalchemy.dialects.postgresql import ARRAY, UUID


def row_snapshot(obj: Any) -> dict[str, Any]:
    """Column values of an ORM row as JSON-safe primitives (UUID → str, datetime → iso)."""
    out: dict[str, Any] = {}
    for col in obj.__table__.columns:  # type: ignore[attr-defined]
        v = getattr(obj, col.name)
        if isinstance(v, uuid.UUID):
            v = str(v)
        elif isinstance(v, datetime | date):
            v = v.isoformat()
        elif isinstance(v, list):
            v = [str(x) if isinstance(x, uuid.UUID) else x for x in v]
        out[col.name] = v
    return out


def row_restore(model: Any, data: dict[str, Any]) -> Any:
    """Transient ORM instance from a snapshot (never attached to a session)."""
    kwargs: dict[str, Any] = {}
    for col in model.__table__.columns:
        v = data.get(col.name)
        if v is not None:
            if isinstance(col.type, UUID):
                v = uuid.UUID(v)
            elif isinstance(col.type, ARRAY) and isinstance(col.type.item_type, UUID):
                v = [uuid.UUID(x) for x in v]
            elif isinstance(col.type, DateTime):
                v = datetime.fromisoformat(v)
            elif isinstance(col.type, Date):
                v = date.fromisoformat(v)
        kwargs[col.name] = v
    return model(**kwargs)
