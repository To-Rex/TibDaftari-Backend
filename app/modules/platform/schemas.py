"""Platform schemas — the territory activity view (Clinic-Web `src/domain/platform.ts`)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.core.schemas import CamelModel

_DAY = r"^\d{4}-\d{2}-\d{2}$"

#: the level the rows are grouped at; omitted = one level below the narrowest filter (`unit` = facility)
RegionGroupBy = Literal["country", "region", "district", "unit"]


class RegionStatsQuery(CamelModel):
    """Inclusive calendar-day range over the facilities inside the chosen territory and / or under one unit of the
    organisation tree (`unitId` — a region administrator passes their region node)."""

    date_from: str = Field(pattern=_DAY)
    date_to: str = Field(pattern=_DAY)
    country_id: str | None = None
    region_id: str | None = None
    district_id: str | None = None
    #: the root unit to fold under (its whole subtree)
    unit_id: str | None = None
    group_by: RegionGroupBy | None = None


class UnitMetrics(CamelModel):
    """Activity of one territory (or one facility) in the period — numbers only, nothing personal."""

    #: alive facilities here
    units: int = 0
    #: non-cancelled cheques opened in the period
    orders: int = 0
    #: Σ non-refunded payments taken in the period
    revenue: int = 0
    #: patients registered in the period (at a facility here)
    patients_new: int = 0
    #: result documents issued (final) in the period
    results_ready: int = 0
    #: SMS sent in the period from a facility here
    sms_sent: int = 0


class UnitRow(UnitMetrics):
    #: country / region / district id, or the facility id
    id: str
    kind: RegionGroupBy
    name: str
    #: the level above (a district's region, a region's country, a facility's region + district)
    parent_name: str | None = None
    #: the organisation-tree node of this row (the country / region / district node, or the facility itself) when it exists
    unit_id: str | None = None
    #: facilities only: the cheque prefix, the kind (ses, clinic …) and whether it is switched on
    code: str | None = None
    facility_kind: str | None = None
    is_active: bool | None = None


class RegionTrendPoint(CamelModel):
    date: str
    orders: int
    revenue: int


class RegionStatsOut(CamelModel):
    group_by: RegionGroupBy
    date_from: str
    date_to: str
    total: UnitMetrics
    rows: list[UnitRow]
    trend: list[RegionTrendPoint]
