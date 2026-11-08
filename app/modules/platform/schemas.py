"""Platform schemas — the regional activity view (superadmin)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.core.schemas import CamelModel

_DAY = r"^\d{4}-\d{2}-\d{2}$"

#: the level the rows are grouped at; omitted = one level below the narrowest filter
RegionGroupBy = Literal["country", "region", "district", "company"]


class RegionStatsQuery(CamelModel):
    """Inclusive calendar-day range (Asia/Tashkent) over the clinics inside the chosen territory."""

    date_from: str = Field(pattern=_DAY)
    date_to: str = Field(pattern=_DAY)
    country_id: str | None = None
    region_id: str | None = None
    district_id: str | None = None
    group_by: RegionGroupBy | None = None


class UnitMetrics(CamelModel):
    """Activity of one territory (or one clinic) in the period — numbers only, nothing personal."""

    #: clinics with at least one branch here (a clinic with branches in two districts counts in both)
    companies: int = 0
    branches: int = 0
    #: non-cancelled cheques opened in the period
    orders: int = 0
    #: Σ non-refunded payments taken in the period
    revenue: int = 0
    #: patients registered in the period (at a branch here)
    patients_new: int = 0
    #: results approved in the period
    results_ready: int = 0
    #: SMS sent in the period through a branch here
    sms_sent: int = 0


class UnitRow(UnitMetrics):
    #: country / region / district / company id
    id: str
    kind: RegionGroupBy
    name: str
    #: the level above (a district's region, a region's country, a clinic's own location)
    parent_name: str | None = None
    #: clinics only: the slug (links) and whether it is switched on
    slug: str | None = None
    is_active: bool | None = None


class RegionTrendPoint(CamelModel):
    date: str
    orders: int
    revenue: int


class CoverageOut(CamelModel):
    """How many clinics / branches carry a district — the rest are invisible to the regional view."""

    companies: int
    companies_located: int
    branches: int
    branches_located: int


class RegionStatsOut(CamelModel):
    group_by: RegionGroupBy
    date_from: str
    date_to: str
    total: UnitMetrics
    rows: list[UnitRow]
    #: branches inside the scope with no value at this level (e.g. a region's branches without a district)
    unassigned: UnitMetrics | None = None
    trend: list[RegionTrendPoint]
    coverage: CoverageOut
