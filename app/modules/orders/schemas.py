"""Order DTOs — mirror of Clinic-Web `src/domain/order.ts` (Order, OrderItem, Payment, ResultDocument, inputs)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import Field

from app.core.schemas import CamelModel, PageQuery

OrderStatus = Literal["draft", "open", "in_progress", "completed", "cancelled"]
PaymentStatus = Literal["unpaid", "partial", "paid", "refunded"]
ItemStatus = Literal["pending", "entered", "submitted", "approved", "rejected", "cancelled"]
PaymentMethod = Literal["cash", "card", "transfer", "insurance"]
DeliveryChannel = Literal["sms", "telegram", "portal", "print"]
DeliveryStatus = Literal["queued", "sent", "delivered", "failed"]


# ----------------------------------------------------------------------------- outputs


class ProgressOut(CamelModel):
    pending: int = 0
    entered: int = 0
    submitted: int = 0
    approved: int = 0
    rejected: int = 0
    cancelled: int = 0


class OrderOut(CamelModel):
    id: str
    company_id: str
    branch_id: str
    number: str
    patient_id: str
    patient_name: str
    patient_phone: str
    created_by_employee_id: str
    status: OrderStatus
    payment: PaymentStatus
    subtotal: int
    discount_percent: int
    discount_amount: int
    total: int
    paid_amount: int
    item_count: int
    progress: ProgressOut
    note: str | None = None
    cancel_reason: str | None = None
    cancelled_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    created_by: str | None = None


class OrderItemOut(CamelModel):
    id: str
    order_id: str
    company_id: str
    branch_id: str
    service_type_id: str
    service_name: str
    category_id: str
    category_name: str
    price: int
    final_price: int
    status: ItemStatus
    schema_id: str | None = None
    schema_version: int | None = None
    values: dict[str, Any]
    technician_id: str | None = None
    technician_name: str | None = None
    entered_at: datetime | None = None
    submitted_at: datetime | None = None
    doctor_id: str | None = None
    doctor_name: str | None = None
    approved_at: datetime | None = None
    reject_reason: str | None = None
    document_id: str | None = None
    lab_note: str | None = None
    created_at: datetime
    updated_at: datetime
    created_by: str | None = None


class WorklistItemOut(OrderItemOut):
    """OrderItem + order/patient join columns for the lab worklist."""

    order_number: str
    patient_name: str
    patient_phone: str
    patient_gender: Literal["male", "female"] | None = None
    patient_birth_date: date | None = None


class PaymentOut(CamelModel):
    id: str
    order_id: str
    company_id: str
    branch_id: str
    amount: int
    method: PaymentMethod
    employee_id: str
    note: str | None = None
    refunded_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    created_by: str | None = None


class DocumentDeliveryOut(CamelModel):
    channel: DeliveryChannel
    status: DeliveryStatus
    at: str
    detail: str | None = None


class ResultDocumentOut(CamelModel):
    id: str
    company_id: str
    order_id: str
    order_item_id: str | None = None
    order_item_ids: list[str] | None = None
    template_id: str
    template_version: int
    title: str
    status: Literal["draft", "final"]
    pdf_url: str | None = None
    deliveries: list[DocumentDeliveryOut]
    created_at: datetime
    updated_at: datetime
    created_by: str | None = None


class OrderBundleOut(CamelModel):
    order: OrderOut
    items: list[OrderItemOut]
    payments: list[PaymentOut]


class OrderItemsOut(CamelModel):
    order: OrderOut
    items: list[OrderItemOut]


class OrderPaymentsOut(CamelModel):
    order: OrderOut
    payments: list[PaymentOut]


class ApproveItemOut(CamelModel):
    item: OrderItemOut
    document: ResultDocumentOut


class ApproveOrderOut(CamelModel):
    items: list[OrderItemOut]
    document: ResultDocumentOut


# ----------------------------------------------------------------------------- inputs


class CreateOrderIn(CamelModel):
    """`CreateOrderInput`."""

    patient_id: str = Field(min_length=1, max_length=64)
    branch_id: str = Field(min_length=1, max_length=64)
    service_type_ids: list[str] = Field(default_factory=list, max_length=200)
    note: str | None = Field(default=None, max_length=2000)


class AddItemsIn(CamelModel):
    service_type_ids: list[str] = Field(min_length=1, max_length=200)


class PayIn(CamelModel):
    """`PayOrderInput` minus `orderId` (taken from the path)."""

    amount: int
    method: PaymentMethod
    send_sms: bool = False


class ReasonIn(CamelModel):
    reason: str = Field(default="", max_length=2000)


class SaveValuesIn(CamelModel):
    values: dict[str, Any] = Field(default_factory=dict)
    lab_note: str | None = Field(default=None, max_length=4000)


class ApproveItemIn(CamelModel):
    template_id: str | None = Field(default=None, max_length=64)


class ApproveOrderIn(CamelModel):
    template_id: str = Field(min_length=1, max_length=64)
    item_ids: list[str] | None = Field(default=None, max_length=500)


# ----------------------------------------------------------------------------- queries


class OrderListQuery(PageQuery):
    branch_id: str | None = None
    status: OrderStatus | None = None
    payment: PaymentStatus | None = None
    date_from: str | None = Field(default=None, max_length=10)
    date_to: str | None = Field(default=None, max_length=10)
    patient_id: str | None = None


class WorklistCountsOut(CamelModel):
    """Counts per item status for the current worklist filters (+ `all`)."""

    all: int
    pending: int
    entered: int
    submitted: int
    approved: int
    rejected: int
    cancelled: int


class WorklistQuery(PageQuery):
    branch_id: str | None = None
    category_ids: list[str] = Field(default_factory=list)
    status: list[ItemStatus] = Field(default_factory=list)
    date_from: str | None = Field(default=None, max_length=10)
    date_to: str | None = Field(default=None, max_length=10)
