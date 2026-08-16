"""Orders (cheques) → order items (services) → payments; result documents."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Index, Integer, SmallInteger, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.db.base import AuditMixin, Base, PKMixin, SoftDeleteMixin, TenantMixin

ITEM_STATUSES = ("pending", "entered", "submitted", "approved", "rejected", "cancelled")
ORDER_STATUSES = ("draft", "open", "in_progress", "completed", "cancelled")
PAYMENT_STATUSES = ("unpaid", "partial", "paid", "refunded")


def empty_progress() -> dict[str, int]:
    return {s: 0 for s in ITEM_STATUSES}


class Order(PKMixin, AuditMixin, SoftDeleteMixin, TenantMixin, Base):
    __tablename__ = "orders"

    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    number: Mapped[str] = mapped_column(String(24), nullable=False)
    patient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    patient_name: Mapped[str] = mapped_column(String(200), nullable=False)
    patient_phone: Mapped[str] = mapped_column(String(20), nullable=False)
    created_by_employee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="open")
    payment: Mapped[str] = mapped_column(String(10), nullable=False, default="unpaid")
    subtotal: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    discount_percent: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    discount_amount: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    paid_amount: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    progress: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=empty_progress)
    note: Mapped[str | None] = mapped_column(Text)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_orders_company_created", "company_id", "created_at"),
        Index("ix_orders_company_branch_created", "company_id", "branch_id", "created_at"),
        Index("ix_orders_patient_created", "patient_id", "created_at"),
        Index("ix_orders_company_status", "company_id", "status"),
        Index("uq_orders_company_number", "company_id", "number", unique=True),
    )


class OrderItem(PKMixin, AuditMixin, SoftDeleteMixin, TenantMixin, Base):
    __tablename__ = "order_items"

    order_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    service_type_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    service_name: Mapped[str] = mapped_column(String(300), nullable=False)
    category_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    category_name: Mapped[str] = mapped_column(String(200), nullable=False)
    price: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    final_price: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="pending")
    schema_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    schema_version: Mapped[int | None] = mapped_column(Integer)
    values: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    technician_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    technician_name: Mapped[str | None] = mapped_column(String(200))
    entered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    doctor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    doctor_name: Mapped[str | None] = mapped_column(String(200))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reject_reason: Mapped[str | None] = mapped_column(Text)
    document_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lab_note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_order_items_company_status_created", "company_id", "status", "created_at"),
        Index("ix_order_items_company_category_status", "company_id", "category_id", "status"),
        Index("ix_order_items_company_branch_created", "company_id", "branch_id", "created_at"),
        Index("ix_order_items_service_type", "service_type_id", "created_at"),
    )


class Payment(PKMixin, AuditMixin, SoftDeleteMixin, TenantMixin, Base):
    __tablename__ = "payments"

    order_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    method: Mapped[str] = mapped_column(String(12), nullable=False)  # cash | card | transfer | insurance
    employee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refund_reason: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_payments_company_created", "company_id", "created_at"),)


class ResultDocument(PKMixin, AuditMixin, SoftDeleteMixin, TenantMixin, Base):
    __tablename__ = "result_documents"

    order_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    patient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    order_item_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    order_item_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=False, default=list)
    template_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    template_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="final")  # draft | final
    pdf_file_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    deliveries: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    # frozen render input (context) + template doc snapshot → PDF is reproducible forever
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    public_token: Mapped[str | None] = mapped_column(String(64), index=True)  # unguessable link for SMS/Telegram

    __table_args__ = (Index("ix_result_documents_company_created", "company_id", "created_at"),)
