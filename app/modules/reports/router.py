"""Reports endpoints — dashboard summary and revenue breakdown. HTTP only."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import DbSession, Staff
from app.modules.reports import service
from app.modules.reports.schemas import BreakdownQuery, BreakdownRowOut, DashboardSummaryOut, RangeQuery

router = APIRouter()

REPORT_READ = ("reports.operations.read", "reports.finance.read")


@router.get("/companies/{company_id}/reports/dashboard", response_model=DashboardSummaryOut, summary="Dashboard counters, daily trend, category split")
async def dashboard(company_id: uuid.UUID, q: Annotated[RangeQuery, Query()], staff: Staff, session: DbSession) -> DashboardSummaryOut:
    staff.require(*REPORT_READ).scope(company_id)
    return await service.dashboard(session, company_id, q)


@router.get("/companies/{company_id}/reports/breakdown", response_model=list[BreakdownRowOut], summary="Revenue/count breakdown by category, service, branch or employee")
async def breakdown(company_id: uuid.UUID, q: Annotated[BreakdownQuery, Query()], staff: Staff, session: DbSession) -> list[BreakdownRowOut]:
    staff.require(*REPORT_READ).scope(company_id)
    return await service.breakdown(session, company_id, q)
