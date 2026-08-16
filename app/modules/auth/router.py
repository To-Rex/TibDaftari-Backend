"""Auth endpoints (staff login/me, patient OTP, logout)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header

from app.api.deps import DbSession, Meta, Patient, Staff
from app.core.schemas import OkOut
from app.modules.auth import service
from app.modules.auth.schemas import (
    PatientOtpRequestIn,
    PatientOtpRequestOut,
    PatientOtpVerifyIn,
    PatientSessionOut,
    StaffLoginIn,
    StaffSessionOut,
)

router = APIRouter()


@router.post("/staff/login", response_model=StaffSessionOut, summary="Staff login (login + password)")
async def staff_login(body: StaffLoginIn, session: DbSession, meta: Meta) -> StaffSessionOut:
    return await service.staff_login(session, body.login, body.password, meta)


@router.get("/staff/me", response_model=StaffSessionOut, summary="Current staff session (rebuilt from DB)")
async def staff_me(staff: Staff, session: DbSession, authorization: Annotated[str | None, Header()] = None) -> StaffSessionOut:
    return await service.staff_me(session, staff, (authorization or "")[7:].strip())


@router.post("/patient/otp/request", response_model=PatientOtpRequestOut, summary="Send OTP to a registered patient phone")
async def patient_otp_request(body: PatientOtpRequestIn, session: DbSession, meta: Meta) -> PatientOtpRequestOut:
    return await service.request_patient_otp(session, body.phone, meta)


@router.post("/patient/otp/verify", response_model=PatientSessionOut, summary="Verify OTP → patient session")
async def patient_otp_verify(body: PatientOtpVerifyIn, session: DbSession, meta: Meta) -> PatientSessionOut:
    return await service.verify_patient_otp(session, body.challenge_id, body.code, meta)


@router.get("/patient/me", response_model=PatientSessionOut, summary="Current patient session")
async def patient_me(patient: Patient, session: DbSession, authorization: Annotated[str | None, Header()] = None) -> PatientSessionOut:
    return await service.patient_me(session, patient.patient, patient.jti, (authorization or "")[7:].strip(), patient.token_exp)


@router.post("/logout", response_model=OkOut, summary="Revoke the current token (staff or patient)")
async def logout(session: DbSession, authorization: Annotated[str | None, Header()] = None) -> OkOut:
    await service.logout(session, authorization)
    return OkOut()
