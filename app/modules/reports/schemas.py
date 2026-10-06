"""Reports DTOs — mirror `DashboardSummary` in `Clinic-Web/src/domain/notification.ts`."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from app.core.schemas import CamelModel, PageQuery

BreakdownBy = Literal["category", "service", "branch", "employee"]

_DAY = r"^\d{4}-\d{2}-\d{2}$"


class RangeQuery(CamelModel):
    """Inclusive calendar-day range evaluated in Asia/Tashkent; optional branch filter."""

    date_from: str = Field(pattern=_DAY)
    date_to: str = Field(pattern=_DAY)
    branch_id: str | None = None


class BreakdownQuery(RangeQuery):
    by: BreakdownBy


class TrendPoint(CamelModel):
    date: str
    orders: int
    revenue: int


class CategorySlice(CamelModel):
    name: str
    count: int
    revenue: int
    color: str | None = None


class DashboardSummaryOut(CamelModel):
    today_orders: int
    today_revenue: int
    pending_lab: int
    pending_approval: int
    patients: int
    sms_queued: int
    trend: list[TrendPoint]
    by_category: list[CategorySlice]


class BreakdownRowOut(CamelModel):
    name: str
    count: int
    revenue: int


# ----------------------------------------------------------------------------- patients / results / services


class CountSlice(CamelModel):
    key: str
    count: int


class NamedCount(CamelModel):
    #: empty = not filled in
    name: str
    count: int


class PatientTrendPoint(CamelModel):
    date: str
    #: distinct patients with a cheque that day
    patients: int
    #: of them, patients whose very first cheque it was
    new: int


class TopPatientOut(CamelModel):
    patient_id: str
    name: str
    phone: str
    orders: int
    paid: int | None = None
    last_visit: datetime


class PatientReportOut(CamelModel):
    """Patients with a (non-cancelled) cheque in the period. Money fields are null without reports.finance.read."""

    patients: int
    new_patients: int
    returning_patients: int
    orders: int
    avg_orders: float
    avg_check: int | None = None
    debtors: int | None = None
    debt: int | None = None
    telegram_linked: int
    portal_linked: int
    gender: list[CountSlice]
    age_groups: list[CountSlice]
    districts: list[NamedCount]
    trend: list[PatientTrendPoint]
    top_patients: list[TopPatientOut]


class TurnaroundRow(CamelModel):
    name: str
    approved: int
    avg_hours: float | None = None
    #: approved within the service's turnaround days
    on_time: int


class ResultsReportOut(CamelModel):
    """Results of the period's cheques: readiness, and whether patients got them."""

    items: int
    items_waiting: int
    items_submitted: int
    items_ready: int
    items_overdue: int
    patients_waiting: int
    documents: int
    received: int
    received_patients: int
    not_received: int
    not_received_patients: int
    viewed: int
    printed: int
    telegram: int
    sms_sent: int
    avg_hours: float | None = None
    on_time: int
    turnaround: list[TurnaroundRow]
    #: opens and prints are recorded from this day on
    tracking_since: str


ResultListStatus = Literal["not_received", "received", "waiting"]


class ResultListQuery(RangeQuery, PageQuery):
    status: ResultListStatus = "not_received"


class ResultRowOut(CamelModel):
    """`document` rows (ready results) or `order` rows (cheques whose results are not ready yet)."""

    kind: Literal["document", "order"]
    order_id: str
    order_number: str
    patient_id: str
    patient_name: str
    patient_phone: str
    title: str
    ordered_at: datetime
    document_id: str | None = None
    ready_at: datetime | None = None
    viewed_at: datetime | None = None
    view_count: int = 0
    printed_at: datetime | None = None
    print_count: int = 0
    telegram: bool = False
    sms: str | None = None
    waiting: int = 0
    submitted: int = 0
    overdue: bool = False


class ServiceUsageRow(CamelModel):
    service_type_id: str
    name: str
    category: str
    count: int
    patients: int
    prev_count: int
    revenue: int | None = None
    pending: int
    avg_hours: float | None = None


class UnusedServiceRow(CamelModel):
    service_type_id: str
    name: str
    category: str
    prev_count: int


class ServicesReportOut(CamelModel):
    """Service usage in the period vs the same-length period before it; active services nobody ordered."""

    prev_from: str
    prev_to: str
    total: int
    prev_total: int
    rows: list[ServiceUsageRow]
    unused: list[UnusedServiceRow]
