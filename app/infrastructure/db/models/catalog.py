"""Catalogue: categories → service types → attribute schemas; result templates + assets; stored files.

Every catalogue row is owned by a unit (`owner_unit_id`, any level) and is visible to the owner's subtree.
Per-unit price / on-off overrides (`service_unit_settings`) and template bindings (`template_units`) apply to
the unit's whole subtree — the nearest level wins.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.db.base import AuditMixin, Base, OwnerMixin, PKMixin, SoftDeleteMixin


class Category(PKMixin, AuditMixin, SoftDeleteMixin, OwnerMixin, Base):
    __tablename__ = "categories"

    parent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("categories.id"), index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    code: Mapped[str | None] = mapped_column(String(40))
    icon: Mapped[str | None] = mapped_column(String(60))
    color: Mapped[str | None] = mapped_column(String(20))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    phone: Mapped[str | None] = mapped_column(String(40))
    workflow: Mapped[str] = mapped_column(String(20), nullable=False, default="lab")

    __table_args__ = (Index("ix_categories_owner_order", "owner_unit_id", "sort_order"),)


class ServiceType(PKMixin, AuditMixin, SoftDeleteMixin, OwnerMixin, Base):
    __tablename__ = "service_types"

    category_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("categories.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    code: Mapped[str | None] = mapped_column(String(40))
    description: Mapped[str | None] = mapped_column(Text)
    price: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)  # base price; per-unit overrides in service_unit_settings
    turnaround_days: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    schema_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("attribute_schemas.id"), index=True)
    document_scope: Mapped[str] = mapped_column(String(10), nullable=False, default="item")  # item | order
    default_template_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("result_templates.id"))

    __table_args__ = (
        Index("ix_service_types_owner_category", "owner_unit_id", "category_id"),
        Index("uq_service_types_owner_code_alive", "owner_unit_id", text("lower(code)"), unique=True, postgresql_where=text("deleted_at IS NULL AND code IS NOT NULL")),
    )


class ServiceUnitSetting(Base):
    """A service's price / availability for one unit AND everything under it (nearest level wins).
    `price` NULL = the base price; `is_enabled` False = not offered there (409 not_in_branch rule)."""

    __tablename__ = "service_unit_settings"

    service_type_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("service_types.id", ondelete="CASCADE"), primary_key=True)
    unit_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("org_units.id"), primary_key=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    price: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class AttributeSchema(PKMixin, AuditMixin, SoftDeleteMixin, OwnerMixin, Base):
    __tablename__ = "attribute_schemas"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="draft")  # draft | published | archived
    fields: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)


class ResultTemplate(PKMixin, AuditMixin, SoftDeleteMixin, OwnerMixin, Base):
    __tablename__ = "result_templates"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="draft")  # draft | active | archived
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    service_type_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=False, default=list)
    category_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=False, default=list)
    scope: Mapped[str] = mapped_column(String(10), nullable=False, default="item")  # item | order | receipt
    language: Mapped[str] = mapped_column(String(2), nullable=False, default="uz")
    doc: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    thumbnail_url: Mapped[str | None] = mapped_column(Text)
    usage: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("ix_result_templates_owner_status", "owner_unit_id", "status"),
        Index("ix_result_templates_services", "service_type_ids", postgresql_using="gin"),
    )


class TemplateUnit(Base):
    """A template limited to some units (and their subtrees); no rows = the owner's whole subtree."""

    __tablename__ = "template_units"

    template_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("result_templates.id", ondelete="CASCADE"), primary_key=True)
    unit_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("org_units.id"), primary_key=True)


class StoredFile(PKMixin, Base):
    """Binary blobs (assets, generated PDFs). `storage='db'` keeps the bytes in `data`; `'external'` points at
    an object store by `object_key`. Deduplicated by sha256 inside a unit."""

    __tablename__ = "stored_files"

    unit_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("org_units.id"), index=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mime: Mapped[str] = mapped_column(String(100), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    filename: Mapped[str | None] = mapped_column(String(200))
    storage: Mapped[str] = mapped_column(String(10), nullable=False, default="db")
    data: Mapped[bytes | None] = mapped_column(LargeBinary)
    object_key: Mapped[str | None] = mapped_column(Text)
    is_public: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    __table_args__ = (Index("ix_stored_files_unit_sha", "unit_id", "sha256"),)


class TemplateAsset(PKMixin, AuditMixin, SoftDeleteMixin, OwnerMixin, Base):
    __tablename__ = "template_assets"

    kind: Mapped[str] = mapped_column(String(12), nullable=False)  # logo | stamp | signature | image
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("stored_files.id"), nullable=False)
    width: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False)
    height: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False)
    employee_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("employees.id"))
