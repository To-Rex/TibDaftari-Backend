"""FastAPI dependencies: DB session, authenticated principals, permission + unit-scope guards.

Usage in routers::

    @router.get("/units/{unit_id}/patients")
    async def list_patients(scope: Scope, staff: Staff, session: DbSession):
        staff.require("reception.patient.read")
        ...  # scope.descendant_ids = every unit under the selected one

`Staff` = staff principal (employee + resolved permissions + the units they may act in);
`Scope` = the selected unit (path `{unit_id}`), already checked against the principal;
`Patient` = portal principal.
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
from app.core.exceptions import AuthError, ForbiddenError, NotFoundError
from app.core.permissions import INSTALLER_ROLE_KEY, PLATFORM_PERMISSIONS, resolve_permissions
from app.core.scope import UnitScope, descendant_ids, load_scope
from app.core.security import decode_token
from app.infrastructure.db.base import alive
from app.infrastructure.db.models import Employee, EmployeeUnit, OrgUnit, Role
from app.infrastructure.db.models import Patient as PatientModel
from app.infrastructure.db.models import Session as SessionModel
from app.infrastructure.db.session import get_session
from app.infrastructure.db.snapshot import row_restore, row_snapshot
from app.infrastructure.redis.client import get_redis

DbSession = Annotated[AsyncSession, Depends(get_session)]

SESSION_KEY = "sess:{jti}"


@dataclass(slots=True)
class RequestMeta:
    ip: str | None
    request_id: str | None
    user_agent: str | None
    #: the web app the request came from (its Origin), only when it is one of the allowed CORS origins —
    #: links sent to patients (SMS) point at the same app the staff used
    origin: str | None = None


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
    origin = (request.headers.get("origin") or "").strip().rstrip("/")
    allowed = {o.strip().rstrip("/") for o in settings.cors_origins}
    return RequestMeta(
        ip=ip,
        request_id=getattr(request.state, "request_id", None),
        user_agent=(request.headers.get("user-agent") or "")[:300],
        origin=origin if origin and origin in allowed else None,
    )


Meta = Annotated[RequestMeta, Depends(request_meta)]


@dataclass(slots=True)
class StaffPrincipal:
    employee: Employee
    role: Role | None
    permissions: list[str]
    jti: str
    token_exp: datetime
    #: the employee's scope roots: home unit + extra units (empty for a superadmin)
    unit_ids: list[uuid.UUID] = field(default_factory=list)
    #: every unit under those roots (the units the employee may act in); None = superadmin — everything
    allowed_unit_ids: frozenset[uuid.UUID] | None = None
    #: the home unit row (level, name, code …) for the session payload
    home_unit: OrgUnit | None = None
    _perm_set: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        self._perm_set = frozenset(self.permissions)

    @property
    def id(self) -> uuid.UUID:
        return self.employee.id

    @property
    def is_super_admin(self) -> bool:
        return bool(self.employee.is_super_admin)

    @property
    def role_key(self) -> str:
        return self.role.key if self.role else "user"

    @property
    def is_installer(self) -> bool:
        """O‘rnatuvchi: superadmin-like inside their own units (see `INSTALLER_ROLE_KEY`)."""
        return self.role is not None and self.role.scope_unit_id is None and self.role.key == INSTALLER_ROLE_KEY

    @property
    def home_unit_id(self) -> uuid.UUID | None:
        return self.employee.unit_id

    @property
    def home_facility_id(self) -> uuid.UUID | None:
        """The home unit when it is a facility — where this employee's cheques are written by default."""
        return self.home_unit.id if self.home_unit is not None and self.home_unit.level == "facility" else None

    # ---- units ------------------------------------------------------------
    def can_see(self, unit_id: uuid.UUID | str | None) -> bool:
        """May this employee act in `unit_id` (it lies under one of their scope roots)? Superadmin: always."""
        if self.is_super_admin:
            return True
        if unit_id is None or self.allowed_unit_ids is None:
            return False
        try:
            uid = unit_id if isinstance(unit_id, uuid.UUID) else uuid.UUID(str(unit_id))
        except ValueError:
            return False
        return uid in self.allowed_unit_ids

    def can_see_any(self, unit_ids: list[uuid.UUID] | set[uuid.UUID]) -> bool:
        return self.is_super_admin or any(self.can_see(u) for u in unit_ids)

    def require_unit(self, unit_id: uuid.UUID | str | None) -> StaffPrincipal:
        """Unit isolation: 403 unless the unit lies inside the employee's scope (superadmin: any)."""
        if not self.can_see(unit_id):
            raise ForbiddenError("Ruxsat yo‘q")
        return self

    def restrict(self, unit_ids: list[uuid.UUID]) -> list[uuid.UUID]:
        """`unit_ids` ∩ the employee's scope (superadmin: unchanged); nothing left → a list that matches nothing."""
        if self.is_super_admin:
            return unit_ids
        kept = [u for u in unit_ids if self.can_see(u)]
        return kept or [uuid.UUID(int=0)]

    # ---- permissions ------------------------------------------------------
    def has(self, *perms: str) -> bool:
        return self.is_super_admin or any(p in self._perm_set for p in perms)

    def require(self, *perms: str) -> StaffPrincipal:
        """Any of `perms` grants access (OR semantics, like the frontend `hasPermission`)."""
        if not self.has(*perms):
            raise ForbiddenError("Ruxsat yo‘q")
        return self


@dataclass(slots=True)
class PatientPrincipal:
    patient: PatientModel
    jti: str
    token_exp: datetime
    #: "otp" = phone + SMS code (acts for every patient of the phone); "login" = portal login + password (this patient only)
    via: str = "otp"

    @property
    def id(self) -> uuid.UUID:
        return self.patient.id

    @property
    def own_only(self) -> bool:
        return self.via == "login"


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
    alive_ = bool(row and row.actor == actor and row.revoked_at is None and row.expires_at > now)
    try:
        ttl = int((row.expires_at - now).total_seconds()) if row and alive_ else 300
        await get_redis().set(key, "1" if alive_ else "0", ex=max(60, min(ttl, 24 * 3600)))
    except Exception:  # pragma: no cover
        pass
    return alive_


PRINCIPAL_KEY = "principal:{employee_id}"
PRINCIPAL_TTL = 60  # seconds; staff/role/org-tree writes invalidate explicitly (see invalidate_principal_cache)


async def invalidate_principal_cache(employee_id: uuid.UUID | str | None = None) -> None:
    """Employee / role / org-tree writes call this so permission and scope changes apply on the very next request."""
    from app.infrastructure.redis import cache

    if employee_id:
        await cache.delete(PRINCIPAL_KEY.format(employee_id=employee_id))
    else:
        await cache.delete_prefix("principal:")


def _principal_from(
    emp: Employee, role: Role | None, jti: str, exp: datetime, unit_ids: list[uuid.UUID], allowed: list[uuid.UUID] | None, home_unit: OrgUnit | None
) -> StaffPrincipal:
    if emp.deleted_at is not None:
        raise AuthError("Hisob topilmadi")
    if emp.status != "active":
        raise ForbiddenError("Hisob faol emas", code="inactive")
    perms = [p for p in resolve_permissions(role.permissions if role else [], emp.overrides) if p not in PLATFORM_PERMISSIONS]
    if emp.is_super_admin:
        from app.core.permissions import PERMISSIONS

        perms = list(PERMISSIONS)
    return StaffPrincipal(
        employee=emp,
        role=role,
        permissions=perms,
        jti=jti,
        token_exp=exp,
        unit_ids=unit_ids,
        allowed_unit_ids=None if emp.is_super_admin else frozenset(allowed or []),
        home_unit=home_unit,
    )


async def build_staff_principal(session: AsyncSession, employee_id: uuid.UUID, jti: str, exp: datetime) -> StaffPrincipal:
    """Employee + role + scope → principal. Hot path: served from a short Redis snapshot (no DB round trips);
    the snapshot is invalidated by employee / role / org-tree writes and expires after PRINCIPAL_TTL anyway."""
    from app.infrastructure.redis import cache

    key = PRINCIPAL_KEY.format(employee_id=employee_id)
    snap = await cache.get_json(key)
    if isinstance(snap, dict) and snap.get("emp") and "units" in snap:
        emp = row_restore(Employee, snap["emp"])
        role = row_restore(Role, snap["role"]) if snap.get("role") else None
        home = row_restore(OrgUnit, snap["home"]) if snap.get("home") else None
        allowed = [uuid.UUID(x) for x in snap["allowed"]] if snap.get("allowed") is not None else None
        return _principal_from(emp, role, jti, exp, [uuid.UUID(x) for x in snap["units"]], allowed, home)
    emp = await session.get(Employee, employee_id)
    if not emp:
        raise AuthError("Hisob topilmadi")
    role = await session.get(Role, emp.role_id) if emp.role_id else None
    extra = (await session.execute(select(EmployeeUnit.unit_id).where(EmployeeUnit.employee_id == emp.id))).scalars().all()
    unit_ids = list(dict.fromkeys([*([emp.unit_id] if emp.unit_id else []), *extra]))
    home = (await session.execute(select(OrgUnit).where(OrgUnit.id == emp.unit_id, alive(OrgUnit)))).scalar_one_or_none() if emp.unit_id else None
    allowed = None if emp.is_super_admin else await descendant_ids(session, unit_ids)
    principal = _principal_from(emp, role, jti, exp, unit_ids, allowed, home)
    await cache.set_json(
        key,
        {
            "emp": row_snapshot(emp),
            "role": row_snapshot(role) if role else None,
            "home": row_snapshot(home) if home else None,
            "units": [str(u) for u in unit_ids],
            "allowed": None if allowed is None else [str(u) for u in allowed],
        },
        PRINCIPAL_TTL,
    )
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
    via = "login" if claims.get("via") == "login" else "otp"
    return PatientPrincipal(patient=patient, jti=claims["jti"], token_exp=datetime.fromtimestamp(claims["exp"], tz=UTC), via=via)


Staff = Annotated[StaffPrincipal, Depends(current_staff)]
Patient = Annotated[PatientPrincipal, Depends(current_patient)]


async def unit_scope(unit_id: uuid.UUID, staff: Staff, session: DbSession) -> UnitScope:
    """The `{unit_id}` of the route as a UnitScope — 403 outside the employee's scope, 404 when it does not exist."""
    staff.require_unit(unit_id)
    scope = await load_scope(session, unit_id)
    if scope is None:
        raise NotFoundError("Birlik topilmadi")
    return scope


Scope = Annotated[UnitScope, Depends(unit_scope)]


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
