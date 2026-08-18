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
from typing import Annotated, Any

from fastapi import Depends, Header, Request
from sqlalchemy import DateTime, select
from sqlalchemy.dialects.postgresql import ARRAY, UUID
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


PRINCIPAL_KEY = "principal:{employee_id}"
PRINCIPAL_TTL = 60  # seconds; staff/role writes invalidate explicitly (see invalidate_principal_cache)


def _row_snapshot(obj: Any) -> dict[str, Any]:
    """Column values of an ORM row as JSON-safe primitives (UUID → str, datetime → iso)."""
    out: dict[str, Any] = {}
    for col in obj.__table__.columns:  # type: ignore[attr-defined]
        v = getattr(obj, col.name)
        if isinstance(v, uuid.UUID):
            v = str(v)
        elif isinstance(v, datetime):
            v = v.isoformat()
        elif isinstance(v, list):
            v = [str(x) if isinstance(x, uuid.UUID) else x for x in v]
        out[col.name] = v
    return out


def _row_restore(model: Any, data: dict[str, Any]) -> Any:
    """Transient ORM instance from a snapshot (never attached to a session)."""
    kwargs: dict[str, Any] = {}
    for col in model.__table__.columns:
        v = data.get(col.name)
        if v is not None:
            if isinstance(col.type, UUID):
                v = uuid.UUID(v)
            elif isinstance(col.type, ARRAY) and isinstance(col.type.item_type, UUID):
                v = [uuid.UUID(x) for x in v]
            elif isinstance(col.type, DateTime):
                v = datetime.fromisoformat(v)
        kwargs[col.name] = v
    return model(**kwargs)


async def invalidate_principal_cache(employee_id: uuid.UUID | str | None = None) -> None:
    """Employee/role writes call this so permission changes apply on the very next request."""
    from app.infrastructure.redis import cache

    if employee_id:
        await cache.delete(PRINCIPAL_KEY.format(employee_id=employee_id))
    else:
        await cache.delete_prefix("principal:")


def _principal_from(emp: Employee, role: Role | None, jti: str, exp: datetime) -> StaffPrincipal:
    if emp.deleted_at is not None:
        raise AuthError("Hisob topilmadi")
    if emp.status != "active":
        raise ForbiddenError("Hisob faol emas", code="inactive")
    perms = [p for p in resolve_permissions(role.permissions if role else [], emp.overrides) if p not in PLATFORM_PERMISSIONS]
    if emp.is_super_admin:
        from app.core.permissions import PERMISSIONS

        perms = list(PERMISSIONS)
    branch_id = emp.branch_ids[0] if len(emp.branch_ids or []) == 1 else None
    return StaffPrincipal(employee=emp, role=role, permissions=perms, jti=jti, token_exp=exp, branch_id=branch_id)


async def build_staff_principal(session: AsyncSession, employee_id: uuid.UUID, jti: str, exp: datetime) -> StaffPrincipal:
    """Employee + role → principal. Hot path: served from a short Redis snapshot (no DB round trips);
    the snapshot is invalidated by employee/role writes and expires after PRINCIPAL_TTL anyway."""
    from app.infrastructure.redis import cache

    key = PRINCIPAL_KEY.format(employee_id=employee_id)
    snap = await cache.get_json(key)
    if isinstance(snap, dict) and snap.get("emp"):
        emp = _row_restore(Employee, snap["emp"])
        role = _row_restore(Role, snap["role"]) if snap.get("role") else None
        return _principal_from(emp, role, jti, exp)
    emp = await session.get(Employee, employee_id)
    if not emp:
        raise AuthError("Hisob topilmadi")
    role = await session.get(Role, emp.role_id) if emp.role_id else None
    principal = _principal_from(emp, role, jti, exp)
    await cache.set_json(key, {"emp": _row_snapshot(emp), "role": _row_snapshot(role) if role else None}, PRINCIPAL_TTL)
    return principal


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
