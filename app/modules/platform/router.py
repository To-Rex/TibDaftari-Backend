"""Platform endpoints (superadmin): activity across every clinic by territory. HTTP only."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import DbSession, Staff
from app.modules.platform import service
from app.modules.platform.schemas import RegionStatsOut, RegionStatsQuery

router = APIRouter()


@router.get("/platform/regions", response_model=RegionStatsOut, summary="Activity by country / region / district / clinic (numbers only)")
async def region_stats(q: Annotated[RegionStatsQuery, Query()], staff: Staff, session: DbSession) -> RegionStatsOut:
    staff.require("platform.company.manage")
    return await service.region_stats(session, q)
