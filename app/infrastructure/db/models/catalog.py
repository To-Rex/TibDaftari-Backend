"""Catalog: categories → service types → attribute schemas; result templates + assets; stored files."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, Index, Integer, LargeBinary, Numeric, SmallInteger, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.db.base import AuditMixin, Base, PKMixin, SoftDeleteMixin, TenantMixin


class Category(PKMixin, AuditMixin, SoftDeleteMixin, TenantMixin, Base):
    __tablename__ = "categories"

    parent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    code: Mapped[str | None] = mapped_column(String(40))
    icon: Mapped[str | None] = mapped_column(String(60))
    color: Mapped[str | None] = mapped_column(String(20))
    order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    phone: Mapped[str | None] = mapped_column(String(40))
    workflow: Mapped[str] = mapped_column(String(20), nullable=False, default="lab")

    __table_args__ = (Index("ix_categories_company_order", "company_id", "order"),)


class ServiceType(PKMixin, AuditMixin, SoftDeleteMixin, TenantMixin, Base):
    __tablename__ = "service_types"

    category_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    code: Mapped[str | None] = mapped_column(String(40))
    description: Mapped[str | None] = mapped_column(Text)
    price: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    branch_prices: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    turnaround_days: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    schema_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    document_scope: Mapped[str] = mapped_column(String(10), nullable=False, default="item")  # item | order
    default_template_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    __table_args__ = (
        Index("ix_service_types_company_category", "company_id", "category_id"),
        Index("uq_service_types_company_code_alive", "company_id", text("lower(code)"), unique=True, postgresql_where=text("deleted_at IS NULL AND code IS NOT NULL")),
    )


class AttributeSchema(PKMixin, AuditMixin, SoftDeleteMixin, TenantMixin, Base):
    __tablename__ = "attribute_schemas"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="draft")  # draft | published | archived
    fields: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)


class ResultTemplate(PKMixin, AuditMixin, SoftDeleteMixin, TenantMixin, Base):
    __tablename__ = "result_templates"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="draft")  # draft | active | archived
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    service_type_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=False, default=list)
    category_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=False, default=list)
    scope: Mapped[str] = mapped_column(String(10), nullable=False, default="item")  # item | order
    language: Mapped[str] = mapped_column(String(2), nullable=False, default="uz")
    doc: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    thumbnail_url: Mapped[str | None] = mapped_column(Text)
    usage: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("ix_result_templates_company_status", "company_id", "status"),
        Index("ix_result_templates_service_type_ids", "service_type_ids", postgresql_using="gin"),
    )


class StoredFile(PKMixin, Base):
    """Binary blobs (assets, generated PDFs) kept in Postgres — durable without a volume.
    Deduplicated by sha256 inside a company."""

    __tablename__ = "stored_files"

    company_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mime: Mapped[str] = mapped_column(String(100), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    filename: Mapped[str | None] = mapped_column(String(200))
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    is_public: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    __table_args__ = (Index("ix_stored_files_company_sha", "company_id", "sha256"),)


class TemplateAsset(PKMixin, AuditMixin, SoftDeleteMixin, TenantMixin, Base):
    __tablename__ = "template_assets"

    kind: Mapped[str] = mapped_column(String(12), nullable=False)  # logo | stamp | signature | image
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    width: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False)
    height: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False)
    employee_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
