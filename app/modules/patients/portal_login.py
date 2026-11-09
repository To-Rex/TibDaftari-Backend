"""Portal login + password of a patient (DOMAIN_RULES §2 / §11) — issued, renewed and switched off at reception.

Phone + SMS code keeps working; the login is an extra way in. It is global (the portal has one sign-in form) and
case-insensitive. The password is shown to the employee once (optionally sent by SMS, masked in the outbox); only
its argon2 hash is stored. A session opened with the login sees this patient only, so relatives sharing one phone
each get their own results. Renewing the password or switching the login off ends the login sessions at once.
The SMS goes out through the facility's (inherited) messaging account — the employee's working facility when it
is given and allowed, else the facility the patient belongs to.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import RequestMeta, StaffPrincipal, invalidate_session_cache
from app.core.audit import audit
from app.core.config import settings
from app.core.exceptions import ConflictError, ValidationError
from app.core.security import hash_password, random_digits, random_password
from app.core.textutil import is_valid_uz_phone, latin_lower
from app.infrastructure.db.models import Patient
from app.infrastructure.db.models import Session as SessionModel
from app.modules.messaging import service as messaging
from app.modules.patients.schemas import PatientOut, PortalAccessIn, PortalAccessOut
from app.modules.patients.service import get_patient_or_404, patient_dto
from app.modules.units import repository as units_repo

LOGIN_MAX = 40
_LOGIN_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,39}$")
_LOGIN_BASE_MAX = 12
LOGIN_TAKEN = "Bu login band"
LOGIN_INVALID = "Login 3–40 belgi: lotin harflari, raqamlar va . _ - (harf yoki raqam bilan boshlanadi)"


# ----------------------------------------------------------------------------- pure helpers


def norm_login(raw: str | None) -> str:
    """Logins compare case-insensitively and without surrounding spaces."""
    return (raw or "").strip().lower()


def valid_login(login: str) -> bool:
    return bool(_LOGIN_RE.match(login))


def login_base(full_name: str | None) -> str:
    """The readable start of a suggested login: the first word of the name in Latin letters ('Азизов Шахзод' → 'azizov')."""
    words = (full_name or "").split()
    base = re.sub(r"[^a-z]", "", latin_lower(words[0] if words else ""))[:_LOGIN_BASE_MAX]
    return base or "bemor"


def portal_link(meta: RequestMeta | None) -> str:
    """The portal sign-in page of the web app the employee used (allowed Origin), else FRONTEND_URL."""
    return f"{(meta.origin if meta and meta.origin else settings.frontend_url).rstrip('/')}/login"


# ----------------------------------------------------------------------------- queries


async def login_owner(session: AsyncSession, login: str) -> Patient | None:
    """The alive patient holding `login` (anywhere — the login is system-wide)."""
    stmt = select(Patient).where(func.lower(Patient.portal_login) == login, Patient.deleted_at.is_(None)).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def login_taken(session: AsyncSession, login: str, exclude_id: uuid.UUID | None = None) -> bool:
    owner = await login_owner(session, login)
    return owner is not None and owner.id != exclude_id


async def suggest_login(session: AsyncSession, full_name: str | None) -> str:
    """`<name><4 digits>`, free system-wide (6 digits after ten collisions)."""
    base = login_base(full_name)
    for _ in range(10):
        candidate = f"{base}{random_digits(4)}"
        if not await login_taken(session, candidate):
            return candidate
    return f"{base}{random_digits(6)}"


async def revoke_login_sessions(session: AsyncSession, patient_id: uuid.UUID, keep_jti: str | None = None) -> int:
    """End the patient's portal sessions opened with the login (SMS-code sessions are left alone)."""
    stmt = select(SessionModel).where(
        SessionModel.subject_id == patient_id,
        SessionModel.actor == "patient",
        SessionModel.method == "login",
        SessionModel.revoked_at.is_(None),
    )
    rows = (await session.execute(stmt)).scalars().all()
    now = datetime.now(UTC)
    ended = 0
    for row in rows:
        if keep_jti and row.id == keep_jti:
            continue
        row.revoked_at = now
        await invalidate_session_cache(row.id)
        ended += 1
    return ended


async def _sms_unit(session: AsyncSession, p: Patient, staff: StaffPrincipal, requested: str | None) -> uuid.UUID | None:
    """The facility whose (inherited) SMS account sends the text: the employee's working facility when it is an
    alive facility of the patient's country inside their scope; else the patient's registered facility, else the
    facility of their latest cheque. None = nobody can send."""
    if requested:
        try:
            uid = uuid.UUID(str(requested))
        except ValueError:
            uid = None
        if uid and staff.can_see(uid):
            unit = await units_repo.get_unit(session, uid)
            if unit is not None and unit.level == "facility" and unit.country_id == p.country_id:
                return uid
    return await messaging.patient_home_unit(session, p)


# ----------------------------------------------------------------------------- reception


async def set_portal_access(
    session: AsyncSession, patient_id: uuid.UUID, staff: StaffPrincipal, body: PortalAccessIn, meta: RequestMeta
) -> PortalAccessOut:
    """Issue or renew the login: a new password every time (given or generated), lockout cleared, old login sessions ended."""
    p = await get_patient_or_404(session, patient_id, staff)
    wanted = norm_login(body.login)
    login = wanted or p.portal_login or await suggest_login(session, p.full_name)
    if not valid_login(login):
        raise ValidationError(LOGIN_INVALID, code="invalid_login")
    if await login_taken(session, login, p.id):
        raise ConflictError(LOGIN_TAKEN, code="login_taken")
    password = body.password or random_password()
    if password != password.strip():
        raise ValidationError("Parol bo‘sh joy bilan boshlanmasin va tugamasin", code="invalid_password")

    before = {"login": p.portal_login, "enabled": p.portal_password_hash is not None}
    p.portal_login = login
    p.portal_password_hash = hash_password(password)
    p.portal_password_set_at = datetime.now(UTC)
    p.portal_failed_logins = 0
    p.portal_locked_until = None
    try:
        await session.flush()
    except IntegrityError as exc:  # lost a race for the same login (the request session is rolled back)
        if "uq_patients_portal_login_alive" in str(exc.orig):
            raise ConflictError(LOGIN_TAKEN, code="login_taken") from exc
        raise
    ended = await revoke_login_sessions(session, p.id)

    sms: Literal["queued", "not_configured", "invalid_phone"] | None = None
    sms_unit_id: uuid.UUID | None = None
    if body.send_sms:
        if not is_valid_uz_phone(p.phone):
            sms = "invalid_phone"
        else:
            sms_unit_id = await _sms_unit(session, p, staff, body.unit_id)
            msg = None
            if sms_unit_id is not None:
                link = portal_link(meta)
                msg = await messaging.enqueue_sms_if_configured(
                    session,
                    sms_unit_id,
                    kind="portal_access",
                    to=p.phone,
                    text=await messaging.portal_access_text(session, sms_unit_id, login, password, link),
                    masked_text=await messaging.portal_access_text(session, sms_unit_id, login, "••••••", link),
                    patient_id=p.id,
                )
            sms = "queued" if msg is not None else "not_configured"

    await audit(
        session,
        actor_type="staff",
        actor_id=staff.id,
        unit_id=p.registered_unit_id or sms_unit_id,
        country_id=p.country_id,
        action="portal_access_set",
        entity="patient",
        entity_id=p.id,
        before=before,
        after={
            "login": login,
            "enabled": True,
            "generatedPassword": not body.password,
            "sms": sms,
            "smsUnitId": str(sms_unit_id) if sms_unit_id else None,
            "endedSessions": ended,
        },
        ip=meta.ip,
        request_id=meta.request_id,
    )
    await session.refresh(p)
    return PortalAccessOut(login=login, password=password, sms=sms, patient=await patient_dto(session, p))


async def remove_portal_access(session: AsyncSession, patient_id: uuid.UUID, staff: StaffPrincipal, meta: RequestMeta) -> PatientOut:
    """Switch the login off: login and password cleared, login sessions ended (phone + SMS code still works)."""
    p = await get_patient_or_404(session, patient_id, staff)
    if p.portal_login is None and p.portal_password_hash is None:
        return await patient_dto(session, p)
    before = {"login": p.portal_login, "enabled": p.portal_password_hash is not None}
    p.portal_login = None
    p.portal_password_hash = None
    p.portal_password_set_at = None
    p.portal_failed_logins = 0
    p.portal_locked_until = None
    ended = await revoke_login_sessions(session, p.id)
    await audit(
        session,
        actor_type="staff",
        actor_id=staff.id,
        unit_id=p.registered_unit_id,
        country_id=p.country_id,
        action="portal_access_remove",
        entity="patient",
        entity_id=p.id,
        before=before,
        after={"login": None, "enabled": False, "endedSessions": ended},
        ip=meta.ip,
        request_id=meta.request_id,
    )
    await session.flush()
    await session.refresh(p)
    return await patient_dto(session, p)
