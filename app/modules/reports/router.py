"""Reports endpoints — dashboard summary and revenue breakdown. HTTP only."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response

from app.api.deps import DbSession, Staff
from app.core.schemas import Page
from app.modules.reports import service
from app.modules.reports.schemas import (
    BreakdownQuery,
    BreakdownRowOut,
    DashboardSummaryOut,
    PatientReportOut,
    RangeQuery,
    ReportExportQuery,
    ResultListQuery,
    ResultRowOut,
    ResultsReportOut,
    ServicesReportOut,
)

router = APIRouter()

REPORT_READ = ("reports.operations.read", "reports.finance.read")


@router.get("/companies/{company_id}/reports/dashboard", response_model=DashboardSummaryOut, summary="Dashboard counters, daily trend, category split")
async def dashboard(company_id: uuid.UUID, q: Annotated[RangeQuery, Query()], staff: Staff, session: DbSession) -> DashboardSummaryOut:
    staff.require(*REPORT_READ).scope(company_id)
    return await service.dashboard(session, company_id, q, staff)


@router.get("/companies/{company_id}/reports/breakdown", response_model=list[BreakdownRowOut], summary="Revenue/count breakdown by category, service, branch or employee")
async def breakdown(company_id: uuid.UUID, q: Annotated[BreakdownQuery, Query()], staff: Staff, session: DbSession) -> list[BreakdownRowOut]:
    staff.require(*REPORT_READ).scope(company_id)
    return await service.breakdown(session, company_id, q, staff)


@router.get("/companies/{company_id}/reports/export.xlsx", summary="Reports Excel: patients × analyses with amounts, labs and other cuts", response_class=Response,
            responses={200: {"content": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {}}}})
async def export_reports(company_id: uuid.UUID, q: Annotated[ReportExportQuery, Query()], staff: Staff, session: DbSession) -> Response:
    staff.require("reports.export").scope(company_id)
    data = await service.export_workbook(session, company_id, q, staff)
    stamp = q.date_from + (f"_{q.date_to}" if q.date_to != q.date_from else "")
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="hisobot{("-" + stamp) if stamp else ""}.xlsx"', "Cache-Control": "no-store"},
    )


@router.get("/companies/{company_id}/reports/patients", response_model=PatientReportOut, summary="Patients of the period: new / returning, demographics, districts, most frequent")
async def patients_report(company_id: uuid.UUID, q: Annotated[RangeQuery, Query()], staff: Staff, session: DbSession) -> PatientReportOut:
    staff.require(*REPORT_READ).scope(company_id)
    return await service.patients_report(session, company_id, q, staff)


@router.get("/companies/{company_id}/reports/results", response_model=ResultsReportOut, summary="Results of the period's cheques: ready, overdue, turnaround, received by the patient")
async def results_report(company_id: uuid.UUID, q: Annotated[RangeQuery, Query()], staff: Staff, session: DbSession) -> ResultsReportOut:
    staff.require(*REPORT_READ).scope(company_id)
    return await service.results_report(session, company_id, q, staff)


@router.get("/companies/{company_id}/reports/results/list", response_model=Page[ResultRowOut], summary="Results not received / received, or cheques still waiting (paged)")
async def results_list(company_id: uuid.UUID, q: Annotated[ResultListQuery, Query()], staff: Staff, session: DbSession) -> Page[ResultRowOut]:
    staff.require(*REPORT_READ).scope(company_id)
    return await service.results_list(session, company_id, q, staff)


@router.get("/companies/{company_id}/reports/services", response_model=ServicesReportOut, summary="Service usage vs the previous period; active services nobody ordered")
async def services_report(company_id: uuid.UUID, q: Annotated[RangeQuery, Query()], staff: Staff, session: DbSession) -> ServicesReportOut:
    staff.require(*REPORT_READ).scope(company_id)
    return await service.services_report(session, company_id, q, staff)
