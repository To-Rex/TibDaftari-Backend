"""Declarative base + reusable column mixins.

Conventions (see ARCHITECTURE.md):
* UUIDv7 primary keys generated in the app (`app.core.ids.uuid7`).
* Every business table is soft-deleted (`deleted_at`/`deleted_by`) — rows are
  never physically removed. `alive(Model)` gives the standard filter.
* Tenant tables carry `company_id`; services always scope by it.
* Timestamps are timezone-aware UTC.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, MetaData, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.ids import uuid7

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    type_annotation_map = {dict[str, Any]: JSONB, list[Any]: JSONB}


class PKMixin:
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AuditMixin(TimestampMixin):
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class SoftDeleteMixin:
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


class TenantMixin:
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)


class Money:
    """Integer UZS (no tiyin) — matches the frontend `Money = number` contract."""

    type = BigInteger


def alive(model: Any):
    """Standard soft-delete filter: `select(M).where(alive(M))`."""
    return model.deleted_at.is_(None)
