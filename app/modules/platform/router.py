"""Platform endpoints: activity across the organisation tree by territory. HTTP only — access is decided in the
service (platform permission, or a `unitId` inside the caller's scope)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import DbSession, Staff
from app.modules.platform import service
from app.modules.platform.schemas import RegionStatsOut, RegionStatsQuery

router = APIRouter()


@router.get("/platform/regions", response_model=RegionStatsOut, summary="Activity by country / region / district / facility (numbers only)")
async def region_stats(q: Annotated[RegionStatsQuery, Query()], staff: Staff, session: DbSession) -> RegionStatsOut:
    return await service.region_stats(session, q, staff)
