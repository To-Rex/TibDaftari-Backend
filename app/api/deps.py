"""FastAPI dependencies: DB session, authenticated principals, permission + tenant guards.

Usage in routers::

    @router.get("/companies/{company_id}/patients")
    async def list_patients(company_id: uuid.UUID, staff: Staff, session: DbSession):
        staff.require("reception.patient.read").scope(company_id)

`Staff` = staff principal (employee + resolved permissions); `Patient` = portal principal.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import AuthError, ForbiddenError
from app.core.permissions import PLATFORM_PERMISSIONS, resolve_permissions
from app.core.security import decode_token
from app.infrastructure.db.models import Employee, Role
from app.infrastructure.db.models import Patient as PatientModel
from app.infrastructure.db.models import Session as SessionModel
from app.infrastructure.db.session import get_session
from app.infrastructure.redis.client import get_redis

DbSession = Annotated[AsyncSession, Depends(get_session)]

SESSION_KEY = "sess:{jti}"


@dataclass(slots=True)
class RequestMeta:
    ip: str | None
    request_id: str | None
    user_agent: str | None


def client_ip(request: Request) -> str | None:
    """Best-effort client address.

    Proxy headers are honoured only when `TRUST_PROXY_HEADERS` is set, and then the
    *right-most* `X-Forwarded-For` hop (the one appended by our own proxy) or
    `X-Real-IP` is used - the left-most value is client-supplied and spoofable.
    """
    if settings.trust_proxy_headers:
        real = (request.headers.get("x-real-ip") or "").strip()
        if real:
            return real
        fwd = request.headers.get("x-forwarded-for") or ""
        hops = [h.strip() for h in fwd.split(",") if h.strip()]
        if hops:
            return hops[-1]
    return request.client.host if request.client else None


def request_meta(request: Request) -> RequestMeta:
    ip = client_ip(request)
    return RequestMeta(ip=ip, request_id=getattr(request.state, "request_id", None), user_agent=(request.headers.get("user-agent") or "")[:300])


Meta = Annotated[RequestMeta, Depends(request_meta)]


@dataclass(slots=True)
class StaffPrincipal:
    employee: Employee
    role: Role | None
    permissions: list[str]
    jti: str
    token_exp: datetime
    branch_id: uuid.UUID | None = None
    _perm_set: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        self._perm_set = frozenset(self.permissions)

    @property
    def id(self) -> uuid.UUID:
        return self.employee.id

    @property
    def company_id(self) -> uuid.UUID:
        return self.employee.company_id

    @property
    def is_super_admin(self) -> bool:
        return bool(self.employee.is_super_admin)

    @property
    def role_key(self) -> str:
        return self.role.key if self.role else "user"

    def has(self, *perms: str) -> bool:
        return self.is_super_admin or any(p in self._perm_set for p in perms)

    def require(self, *perms: str) -> StaffPrincipal:
        """Any of `perms` grants access (OR semantics, like the frontend `hasPermission`)."""
        if not self.has(*perms):
            raise ForbiddenError("Ruxsat yo‘q")
        return self

    def scope(self, company_id: uuid.UUID | str | None) -> StaffPrincipal:
        """Tenant isolation: staff may only touch their own company (superadmin: any)."""
        if company_id is None:
            return self
        if not self.is_super_admin and str(company_id) != str(self.company_id):
            raise ForbiddenError("Ruxsat yo‘q")
        return self


@dataclass(slots=True)
class PatientPrincipal:
    patient: PatientModel
    jti: str
    token_exp: datetime

    @property
    def id(self) -> uuid.UUID:
        return self.patient.id


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthError("Sessiya tugagan")
    return authorization[7:].strip()


async def _session_alive(session: AsyncSession, jti: str, actor: str) -> bool:
    """Allow-list check: Redis first (hot path), DB fallback (Redis flush / restart)."""
    key = SESSION_KEY.format(jti=jti)
    try:
        cached = await get_redis().get(key)
    except Exception:  # pragma: no cover
        cached = None
    if cached == "1":
        return True
    if cached == "0":
        return False
    row = await session.get(SessionModel, jti)
    now = datetime.now(UTC)
    alive = bool(row and row.actor == actor and row.revoked_at is None and row.expires_at > now)
    try:
        ttl = int((row.expires_at - now).total_seconds()) if row and alive else 300
        await get_redis().set(key, "1" if alive else "0", ex=max(60, min(ttl, 24 * 3600)))
    except Exception:  # pragma: no cover
        pass
    return alive


async def build_staff_principal(session: AsyncSession, employee_id: uuid.UUID, jti: str, exp: datetime) -> StaffPrincipal:
    emp = await session.get(Employee, employee_id)
    if not emp or emp.deleted_at is not None:
        raise AuthError("Hisob topilmadi")
    if emp.status != "active":
        raise ForbiddenError("Hisob faol emas", code="inactive")
    role = await session.get(Role, emp.role_id) if emp.role_id else None
    perms = [p for p in resolve_permissions(role.permissions if role else [], emp.overrides) if p not in PLATFORM_PERMISSIONS]
    if emp.is_super_admin:
        from app.core.permissions import PERMISSIONS

        perms = list(PERMISSIONS)
    branch_id = emp.branch_ids[0] if len(emp.branch_ids or []) == 1 else None
    return StaffPrincipal(employee=emp, role=role, permissions=perms, jti=jti, token_exp=exp, branch_id=branch_id)


async def current_staff(session: DbSession, authorization: Annotated[str | None, Header()] = None) -> StaffPrincipal:
    token = _bearer(authorization)
    claims = decode_token(token)
    if claims["act"] != "staff":
        raise AuthError("Sessiya tugagan")
    if not await _session_alive(session, claims["jti"], "staff"):
        raise AuthError("Sessiya tugagan")
    return await build_staff_principal(session, uuid.UUID(claims["sub"]), claims["jti"], datetime.fromtimestamp(claims["exp"], tz=UTC))


async def current_patient(session: DbSession, authorization: Annotated[str | None, Header()] = None) -> PatientPrincipal:
    token = _bearer(authorization)
    claims = decode_token(token)
    if claims["act"] != "patient":
        raise AuthError("Sessiya tugagan")
    if not await _session_alive(session, claims["jti"], "patient"):
        raise AuthError("Sessiya tugagan")
    patient = (await session.execute(select(PatientModel).where(PatientModel.id == uuid.UUID(claims["sub"]), PatientModel.deleted_at.is_(None)))).scalar_one_or_none()
    if not patient:
        raise AuthError("Sessiya tugagan")
    return PatientPrincipal(patient=patient, jti=claims["jti"], token_exp=datetime.fromtimestamp(claims["exp"], tz=UTC))


Staff = Annotated[StaffPrincipal, Depends(current_staff)]
Patient = Annotated[PatientPrincipal, Depends(current_patient)]


def require_permissions(*perms: str):
    """Router-level guard: `dependencies=[Depends(require_permissions('admin.catalog.read'))]`."""

    async def _dep(staff: Staff) -> StaffPrincipal:
        return staff.require(*perms)

    return _dep


async def invalidate_session_cache(jti: str) -> None:
    try:
        await get_redis().set(SESSION_KEY.format(jti=jti), "0", ex=24 * 3600)
    except Exception:  # pragma: no cover
        pass
