"""Reports DTOs — mirror `DashboardSummary` in `Clinic-Web/src/domain/notification.ts`."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.core.schemas import CamelModel

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
