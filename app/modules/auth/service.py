"""Authentication: staff login (argon2 + lockout), patient OTP, session allow-list, logout."""

from __future__ import annotations

import hmac
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import SESSION_KEY, RequestMeta, StaffPrincipal, build_staff_principal, invalidate_session_cache
from app.core.audit import audit
from app.core.config import settings
from app.core.exceptions import AuthError, NotFoundError, RateLimitedError, ValidationError
from app.core.security import (
    decode_token,
    hash_password,
    issue_token,
    needs_rehash,
    random_digits,
    sha256_hex,
    verify_password,
)
from app.core.textutil import is_valid_uz_phone, norm_phone
from app.infrastructure.db.models import Company, Employee, OtpChallenge, Patient
from app.infrastructure.db.models import Session as SessionModel
from app.infrastructure.redis import rate_limit
from app.infrastructure.redis.client import get_redis
from app.modules.auth.schemas import PatientOtpRequestOut, PatientSessionOut, StaffSessionOut
from app.modules.messaging import service as messaging

log = logging.getLogger("auth")


# ----------------------------------------------------------------------------- sessions


async def _open_session(session: AsyncSession, *, actor: str, subject_id: uuid.UUID, company_id: uuid.UUID | None, ttl: timedelta, meta: RequestMeta | None) -> tuple[str, datetime]:
    token, jti, exp = issue_token(subject_id, actor, ttl)  # type: ignore[arg-type]
    session.add(SessionModel(id=jti, actor=actor, subject_id=subject_id, company_id=company_id, expires_at=exp, ip=meta.ip if meta else None, user_agent=meta.user_agent if meta else None))
    try:
        await get_redis().set(SESSION_KEY.format(jti=jti), "1", ex=int(ttl.total_seconds()))
    except Exception:  # pragma: no cover
        pass
    return token, exp


async def revoke_session(session: AsyncSession, jti: str) -> None:
    row = await session.get(SessionModel, jti)
    if row and row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
    await invalidate_session_cache(jti)


async def revoke_all_for_subject(session: AsyncSession, subject_id: uuid.UUID) -> None:
    rows = (await session.execute(select(SessionModel).where(SessionModel.subject_id == subject_id, SessionModel.revoked_at.is_(None)))).scalars().all()
    for row in rows:
        row.revoked_at = datetime.now(UTC)
        await invalidate_session_cache(row.id)


# ----------------------------------------------------------------------------- staff


def staff_session_out(p: StaffPrincipal, token: str, exp: datetime, branch_id: uuid.UUID | None) -> StaffSessionOut:
    return StaffSessionOut(
        employee_id=str(p.employee.id),
        company_id=str(p.employee.company_id),
        branch_id=str(branch_id) if branch_id else None,
        is_super_admin=p.is_super_admin,
        role_key=p.role_key,
        full_name=p.employee.full_name,
        permissions=p.permissions,
        access_token=token,
        expires_at=exp,
    )


async def staff_login(session: AsyncSession, login: str, password: str, meta: RequestMeta) -> StaffSessionOut:
    login_norm = login.strip().lower()
    ip = meta.ip or "?"
    await rate_limit.enforce(f"rl:login:ip:{ip}", settings.auth_rate_limit_per_minute, 60)
    await rate_limit.enforce(f"rl:login:user:{login_norm}", settings.auth_rate_limit_per_minute, 60)

    emp = (
        await session.execute(select(Employee).where(func.lower(Employee.login) == login_norm, Employee.deleted_at.is_(None)).limit(1))
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if emp and emp.locked_until and emp.locked_until > now:
        raise RateLimitedError("Hisob vaqtincha bloklangan. Keyinroq urinib ko‘ring.", details={"retryAfter": int((emp.locked_until - now).total_seconds())})
    ok = verify_password(password, emp.password_hash if emp else None)
    if not emp or not ok:
        if emp:
            emp.failed_logins = (emp.failed_logins or 0) + 1
            if emp.failed_logins >= settings.login_max_attempts:
                emp.locked_until = now + timedelta(minutes=settings.login_lock_minutes)
                emp.failed_logins = 0
                log.warning("employee %s locked after repeated failures", emp.id)
        await audit(session, actor_type="staff", actor_id=emp.id if emp else None, company_id=emp.company_id if emp else None, action="login_failed", entity="employee", entity_id=emp.id if emp else login_norm, ip=meta.ip, request_id=meta.request_id)
        # The request session is rolled back on the exception below - persist the
        # lockout counter and the audit row first, otherwise the lockout is dead.
        await session.commit()
        raise AuthError("Login yoki parol noto‘g‘ri")
    if emp.status != "active":
        from app.core.exceptions import ForbiddenError

        raise ForbiddenError("Hisob faol emas", code="inactive")
    if needs_rehash(emp.password_hash or ""):
        emp.password_hash = hash_password(password)
    emp.failed_logins = 0
    emp.locked_until = None
    emp.last_login_at = now
    ttl = timedelta(hours=settings.staff_token_ttl_hours)
    token, exp = await _open_session(session, actor="staff", subject_id=emp.id, company_id=emp.company_id, ttl=ttl, meta=meta)
    principal = await build_staff_principal(session, emp.id, "", exp)
    await audit(session, actor_type="staff", actor_id=emp.id, company_id=emp.company_id, action="login", entity="employee", entity_id=emp.id, ip=meta.ip, request_id=meta.request_id)
    return staff_session_out(principal, token, exp, principal.branch_id)


async def staff_me(session: AsyncSession, principal: StaffPrincipal, token: str) -> StaffSessionOut:
    """Return the current session; records `last_seen_at`. Expiry is fixed at login (JWT exp)."""
    row = await session.get(SessionModel, principal.jti)
    exp = principal.token_exp
    if row:
        row.last_seen_at = datetime.now(UTC)
        exp = row.expires_at
    return staff_session_out(principal, token, exp, principal.branch_id)


# ----------------------------------------------------------------------------- patient OTP


async def request_patient_otp(session: AsyncSession, phone_raw: str, meta: RequestMeta) -> PatientOtpRequestOut:
    phone = norm_phone(phone_raw)
    if not is_valid_uz_phone(phone):
        raise ValidationError("Telefon raqam noto‘g‘ri", code="invalid_phone")
    await rate_limit.enforce(f"rl:otp:ip:{meta.ip or '?'}", settings.auth_rate_limit_per_minute, 60)
    await rate_limit.enforce(f"rl:otp:phone:{phone}", 5, 3600, "Juda ko‘p urinish. 1 soatdan keyin qayta urinib ko‘ring.")
    cooldown_key = f"otp:cooldown:{phone}"
    try:
        if await get_redis().exists(cooldown_key):
            raise RateLimitedError("Kod allaqachon yuborilgan. Biroz kuting.", details={"retryAfter": settings.otp_resend_cooldown_seconds})
    except RateLimitedError:
        raise
    except Exception:  # pragma: no cover
        pass

    patient = (
        await session.execute(select(Patient).where(Patient.phone == phone, Patient.deleted_at.is_(None)).order_by(Patient.created_at.asc(), Patient.id.asc()).limit(1))
    ).scalar_one_or_none()
    if not patient:
        raise NotFoundError("Bu raqam bilan bemor topilmadi. Klinikaga murojaat qiling.")

    code = random_digits(settings.otp_length)
    challenge = OtpChallenge(
        phone=phone,
        purpose="portal",
        code_hash=sha256_hex(code),
        max_attempts=settings.otp_max_attempts,
        expires_at=datetime.now(UTC) + timedelta(seconds=settings.otp_ttl_seconds),
        company_id=patient.company_id,
        meta={"ip": meta.ip},
    )
    session.add(challenge)
    await session.flush()

    company = await session.get(Company, patient.company_id)
    if company:
        await messaging.enqueue_sms_if_configured(session, company, kind="otp", to=phone, text=messaging.otp_text(company, code), patient_id=patient.id)
    try:
        await get_redis().set(cooldown_key, "1", ex=settings.otp_resend_cooldown_seconds)
    except Exception:  # pragma: no cover
        pass
    if settings.otp_dev_mode:
        log.info("OTP for %s: %s (dev mode)", phone, code)
    return PatientOtpRequestOut(challenge_id=str(challenge.id), dev_code=code if settings.otp_dev_mode else None, expires_in=settings.otp_ttl_seconds)


def patient_session_out(p: Patient, token: str, exp: datetime) -> PatientSessionOut:
    return PatientSessionOut(patient_id=str(p.id), phone=p.phone, full_name=p.full_name, access_token=token, expires_at=exp)


async def verify_patient_otp(session: AsyncSession, challenge_id: str, code: str, meta: RequestMeta) -> PatientSessionOut:
    try:
        cid = uuid.UUID(challenge_id)
    except ValueError as exc:
        raise AuthError("Kod muddati tugagan", code="otp_expired") from exc
    await rate_limit.enforce(f"rl:otp:verify:ip:{meta.ip or '?'}", settings.auth_rate_limit_per_minute, 60)
    await rate_limit.enforce(f"rl:otp:verify:ch:{cid}", settings.otp_max_attempts, settings.otp_ttl_seconds, "Kod muddati tugagan")
    ch = await session.get(OtpChallenge, cid)
    now = datetime.now(UTC)
    if not ch or ch.consumed_at or ch.expires_at < now or ch.purpose != "portal":
        raise AuthError("Kod muddati tugagan", code="otp_expired")
    if ch.attempts >= ch.max_attempts:
        raise AuthError("Kod muddati tugagan", code="otp_expired")
    if not hmac.compare_digest(sha256_hex(code.strip()), ch.code_hash):
        # Persist the failed attempt before raising (the request session is rolled back on error).
        await session.execute(update(OtpChallenge).where(OtpChallenge.id == cid).values(attempts=OtpChallenge.attempts + 1))
        await session.commit()
        raise AuthError("Kod noto‘g‘ri", code="otp_invalid")
    ch.consumed_at = now
    patient = (
        await session.execute(select(Patient).where(Patient.phone == ch.phone, Patient.deleted_at.is_(None)).order_by(Patient.created_at.asc(), Patient.id.asc()).limit(1))
    ).scalar_one_or_none()
    if not patient:
        raise NotFoundError("Bemor topilmadi")
    patient.portal_linked = True
    patient.portal_last_login_at = now
    ttl = timedelta(hours=settings.patient_token_ttl_hours)
    token, exp = await _open_session(session, actor="patient", subject_id=patient.id, company_id=patient.company_id, ttl=ttl, meta=meta)
    await audit(session, actor_type="patient", actor_id=patient.id, company_id=patient.company_id, action="login", entity="patient", entity_id=patient.id, ip=meta.ip, request_id=meta.request_id)
    return patient_session_out(patient, token, exp)


async def patient_me(session: AsyncSession, patient: Patient, jti: str, token: str, exp: datetime) -> PatientSessionOut:
    row = await session.get(SessionModel, jti)
    if row:
        row.last_seen_at = datetime.now(UTC)
        exp = row.expires_at
    return patient_session_out(patient, token, exp)


async def logout(session: AsyncSession, authorization: str | None) -> None:
    if not authorization or not authorization.lower().startswith("bearer "):
        return
    try:
        claims = decode_token(authorization[7:].strip())
    except AuthError:
        return
    await revoke_session(session, claims["jti"])
