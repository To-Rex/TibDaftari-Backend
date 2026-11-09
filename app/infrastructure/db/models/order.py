"""Orders (cheques) → order items (services) → payments; result documents. `unit_id` = the facility."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.db.base import AuditMixin, Base, PKMixin, SoftDeleteMixin, UnitMixin

ITEM_STATUSES = ("pending", "entered", "submitted", "approved", "rejected", "cancelled")
ORDER_STATUSES = ("draft", "open", "in_progress", "completed", "cancelled")
PAYMENT_STATUSES = ("unpaid", "partial", "paid", "refunded")


def empty_progress() -> dict[str, int]:
    return {s: 0 for s in ITEM_STATUSES}


class Order(PKMixin, AuditMixin, SoftDeleteMixin, UnitMixin, Base):
    __tablename__ = "orders"

    number: Mapped[str] = mapped_column(String(24), nullable=False)  # <facility code>-000123
    patient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("patients.id"), nullable=False)
    patient_name: Mapped[str] = mapped_column(String(200), nullable=False)
    patient_phone: Mapped[str] = mapped_column(String(20), nullable=False)
    created_by_employee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("employees.id"), nullable=False)
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
    #: first live payment — the day-long reception edit window counts from it (DB trigger sets it; service keeps it exact)
    first_paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_orders_unit_created", "unit_id", "created_at"),
        Index("ix_orders_unit_status", "unit_id", "status"),
        Index("ix_orders_patient_created", "patient_id", "created_at"),
        UniqueConstraint("unit_id", "number", name="uq_orders_unit_number"),
    )


class OrderItem(PKMixin, AuditMixin, SoftDeleteMixin, UnitMixin, Base):
    __tablename__ = "order_items"

    order_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("orders.id"), nullable=False, index=True)
    service_type_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("service_types.id"), nullable=False)
    service_name: Mapped[str] = mapped_column(String(300), nullable=False)
    category_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("categories.id"), nullable=False)
    category_name: Mapped[str] = mapped_column(String(200), nullable=False)
    price: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    final_price: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="pending")
    schema_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("attribute_schemas.id"))
    schema_version: Mapped[int | None] = mapped_column(Integer)
    values: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    technician_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("employees.id"))
    technician_name: Mapped[str | None] = mapped_column(String(200))
    entered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    doctor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("employees.id"))
    doctor_name: Mapped[str | None] = mapped_column(String(200))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reject_reason: Mapped[str | None] = mapped_column(Text)
    document_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("result_documents.id"))
    lab_note: Mapped[str | None] = mapped_column(Text)
    # the result's trail: [{type: submitted|unsubmitted|returned|approved|revoked|reopened, at, byId, byName, reason?, documentId?}]
    history: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))

    __table_args__ = (
        Index("ix_order_items_unit_status_created", "unit_id", "status", "created_at"),
        Index("ix_order_items_category_status", "category_id", "status"),
        Index("ix_order_items_unit_created", "unit_id", "created_at"),
        Index("ix_order_items_service_created", "service_type_id", "created_at"),
    )


class Payment(PKMixin, AuditMixin, SoftDeleteMixin, UnitMixin, Base):
    __tablename__ = "payments"

    order_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("orders.id"), nullable=False, index=True)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    method: Mapped[str] = mapped_column(String(12), nullable=False)  # cash | card | transfer | insurance
    employee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("employees.id"), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refund_reason: Mapped[str | None] = mapped_column(Text)
    #: method corrections, oldest first: {at, byId, byName, fromMethod, fromAmount, reason, splitFrom?}
    corrections: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))

    __table_args__ = (Index("ix_payments_unit_created", "unit_id", "created_at"),)


class ResultDocument(PKMixin, AuditMixin, SoftDeleteMixin, UnitMixin, Base):
    __tablename__ = "result_documents"

    order_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("orders.id"), nullable=False, index=True)
    patient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("patients.id"), nullable=False, index=True)
    order_item_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("order_items.id"), index=True)
    order_item_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=False, default=list)
    template_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("result_templates.id"), nullable=False)
    template_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="final")  # draft | final | revoked
    pdf_file_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("stored_files.id"))
    deliveries: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    # frozen render input (context) + template doc snapshot → PDF is reproducible forever
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    public_token: Mapped[str | None] = mapped_column(String(64))  # unguessable link for SMS/Telegram
    # the QR code's own token (`/v/{token}`): authenticity check only — never opens the PDF, never sends an SMS
    verify_token: Mapped[str | None] = mapped_column(String(64))
    # did the patient get it: opened by the patient (public link / portal), printed by staff (handed over)
    viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    view_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    printed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    print_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    # public link: the first open is direct, later opens need an SMS code to the patient's phone
    public_view_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    public_first_viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    public_last_viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_result_documents_unit_created", "unit_id", "created_at"),)
