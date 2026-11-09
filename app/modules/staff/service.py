"""Staff business rules: employees (home unit + extra units, credentials, overrides, sessions) and roles
(system / unit-scoped, superadmin guards).

Public helpers reused by other modules (see docs/V2_PORT.md): `get_employee_or_404`, `employee_out`,
`list_employees`, `create_employee`, `list_roles`.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import RequestMeta, StaffPrincipal, invalidate_principal_cache
from app.core.audit import audit
from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.core.pagination import page_of
from app.core.permissions import (
    INSTALLER_EXTRA_LEVELS,
    INSTALLER_HOME_LEVELS,
    INSTALLER_ROLE_KEY,
    PLATFORM_PERMISSIONS,
    PROTECTED_ROLE_KEYS,
    invalid_permission_keys,
    normalize_key,
)
from app.core.schemas import Page
from app.core.scope import UnitScope, ancestor_ids, ids_of, narrow_to
from app.core.security import hash_password
from app.core.textutil import slugify
from app.core.timeutil import utcnow
from app.infrastructure.db.models import Employee, OrgUnit, Role
from app.infrastructure.redis import cache
from app.modules.auth import service as auth_service
from app.modules.staff import repository as repo
from app.modules.staff.schemas import (
    EmployeeCreateIn,
    EmployeeOut,
    EmployeeQuery,
    EmployeeUpdateIn,
    PermissionOverrides,
    RoleCreateIn,
    RoleOut,
    RoleUpdateIn,
)
from app.modules.units import repository as units_repo

DEFAULT_PASSWORD = "123456"
ROLES_CACHE_TTL = 300
UNIT_NOT_FOUND = "Birlik topilmadi"


def _roles_cache_key(unit_id: uuid.UUID | str) -> str:
    return f"roles:{unit_id}"


async def _invalidate_roles_cache() -> None:
    """Any role / employee write → every scope's cached role list (a role may be listed by many scopes)."""
    await cache.delete_prefix("roles:")


async def _invalidate_unit_cards(session: AsyncSession, unit_id: uuid.UUID | None) -> None:
    """The unit cards (`unit:{id}:dto`) count employees over their subtree — drop them along the home unit's path."""
    if unit_id is None:
        return
    path = await ancestor_ids(session, unit_id)
    if path:
        await cache.delete(*[f"unit:{u}:dto" for u in path])


# ----------------------------------------------------------------------------- mapping


def employee_out(
    e: Employee,
    units: Mapping[uuid.UUID, OrgUnit] | Iterable[OrgUnit] | None = None,
    *,
    extra_unit_ids: Iterable[uuid.UUID] | None = None,
    reveal_super_admin: bool = False,
) -> EmployeeOut:
    """Employee ORM row → DTO (never includes password data). `units` = unit rows by id (the home unit gives
    unitName / unitLevel); `extra_unit_ids` = the employee's `employee_units`. The superadmin flag is shown
    only when `reveal_super_admin` (superadmin callers) — everyone else sees false."""
    if units is not None and not isinstance(units, Mapping):
        units = {u.id: u for u in units}
    home = units.get(e.unit_id) if units and e.unit_id else None
    ov = e.overrides or {}
    return EmployeeOut(
        id=str(e.id),
        unit_id=str(e.unit_id) if e.unit_id else None,
        unit_ids=[str(u) for u in (extra_unit_ids or []) if u != e.unit_id],
        unit_name=home.name if home is not None else None,
        unit_level=home.level if home is not None else None,  # type: ignore[arg-type]
        full_name=e.full_name,
        login=e.login,
        phone=e.phone,
        email=e.email,
        role_id=str(e.role_id),
        overrides=PermissionOverrides(allow=list(ov.get("allow") or []), deny=list(ov.get("deny") or [])),
        category_ids=[str(c) for c in (e.category_ids or [])],
        status=e.status,  # type: ignore[arg-type]
        avatar_hue=e.avatar_hue,
        is_super_admin=bool(e.is_super_admin) if reveal_super_admin else False,
        last_login_at=e.last_login_at,
        created_at=e.created_at,
        updated_at=e.updated_at,
    )


async def employees_out(session: AsyncSession, rows: Sequence[Employee], staff: StaffPrincipal | None = None) -> list[EmployeeOut]:
    """DTOs for a page of employees: home unit rows + extra units loaded in two queries."""
    if not rows:
        return []
    units = await units_repo.units_by_ids(session, [e.unit_id for e in rows])
    extra = await repo.extra_unit_ids(session, [e.id for e in rows])
    reveal = bool(staff and staff.is_super_admin)
    return [employee_out(e, units, extra_unit_ids=extra.get(e.id), reveal_super_admin=reveal) for e in rows]


async def employee_dto(session: AsyncSession, emp: Employee, staff: StaffPrincipal | None = None) -> EmployeeOut:
    return (await employees_out(session, [emp], staff))[0]


def role_out(r: Role) -> RoleOut:
    """Role ORM row → DTO."""
    return RoleOut(
        id=str(r.id),
        scope_unit_id=str(r.scope_unit_id) if r.scope_unit_id else None,
        key=r.key,
        name=r.name,
        description=r.description,
        permissions=list(r.permissions or []),
        is_system=r.is_system,
    )


def _parse_ids(values: Iterable[str], label: str) -> list[uuid.UUID]:
    out: list[uuid.UUID] = []
    for v in values:
        try:
            out.append(uuid.UUID(str(v)))
        except ValueError as exc:
            raise ValidationError(f"{label} identifikatori noto‘g‘ri: {v}", details={"field": label}) from exc
    return list(dict.fromkeys(out))


def _reject_platform_keys(keys: list[str], staff: StaffPrincipal) -> None:
    """`platform.*` permissions are granted only by superadmins (never through unit roles/overrides) — under their
    old (v1) names too."""
    if staff.is_super_admin:
        return
    bad = [k for k in keys if normalize_key(k) in PLATFORM_PERMISSIONS]
    if bad:
        raise ForbiddenError("Ruxsat yo‘q")


def _normalized(keys: Iterable[str]) -> list[str]:
    """Permission keys as stored: v1 names mapped to their v2 names, duplicates dropped, order kept."""
    return list(dict.fromkeys(normalize_key(k) for k in keys))


def _validate_overrides(ov: PermissionOverrides, staff: StaffPrincipal) -> dict[str, list[str]]:
    bad = invalid_permission_keys([*ov.allow, *ov.deny])
    if bad:
        raise ValidationError("Noma’lum ruxsat kaliti: " + ", ".join(bad), code="invalid_permission", details={"keys": bad})
    _reject_platform_keys(list(ov.allow), staff)
    return {"allow": _normalized(ov.allow), "deny": _normalized(ov.deny)}


def _guard_super_admin_target(emp: Employee, staff: StaffPrincipal) -> None:
    """Only a superadmin may modify a superadmin employee (prevents platform escalation via password reset)."""
    if emp.is_super_admin and not staff.is_super_admin:
        raise ForbiddenError("Ruxsat yo‘q")


async def _guard_protected_target(session: AsyncSession, emp: Employee, staff: StaffPrincipal) -> None:
    """Superadmins and installers (accounts a superadmin handed out) are edited, re-scoped, reset or removed by
    superadmins only — a unit administrator must not widen or hijack them."""
    _guard_super_admin_target(emp, staff)
    if staff.is_super_admin or not emp.role_id:
        return
    role = await session.get(Role, emp.role_id)
    if role is not None and role.scope_unit_id is None and role.key in PROTECTED_ROLE_KEYS:
        raise ForbiddenError("Ruxsat yo‘q")


def _is_installer_role(role: Role) -> bool:
    return role.scope_unit_id is None and role.key == INSTALLER_ROLE_KEY


INSTALLER_SCOPE_ERROR = "O‘rnatuvchi davlat yoki viloyat tuguniga biriktiriladi; qo‘shimcha birliklar — shu davlatning viloyatlari"


def _check_installer_scope(role: Role, unit_id: uuid.UUID | None, extra: Sequence[uuid.UUID], units: dict[uuid.UUID, OrgUnit]) -> None:
    """An installer is homed at a country or a region and may get other regions of the SAME country as extra units —
    never a facility, never another country (422 `installer_scope`)."""
    if not _is_installer_role(role):
        return
    home = units.get(unit_id) if unit_id else None
    if home is None or home.level not in INSTALLER_HOME_LEVELS:
        raise ValidationError(INSTALLER_SCOPE_ERROR, code="installer_scope", details={"field": "unitId"})
    for u in extra:
        row = units.get(u)
        if row is None or row.level not in INSTALLER_EXTRA_LEVELS or row.country_id != home.country_id:
            raise ValidationError(INSTALLER_SCOPE_ERROR, code="installer_scope", details={"field": "unitIds"})


def _guard_super_admin_flag(value: bool | None, staff: StaffPrincipal) -> bool | None:
    if value is not None and not staff.is_super_admin:
        raise ForbiddenError("Ruxsat yo‘q")
    return value


def _employee_snapshot(e: Employee, extra_unit_ids: Iterable[uuid.UUID] | None = None) -> dict[str, Any]:
    return {
        "fullName": e.full_name,
        "login": e.login,
        "phone": e.phone,
        "email": e.email,
        "roleId": str(e.role_id),
        "unitId": str(e.unit_id) if e.unit_id else None,
        "unitIds": [str(u) for u in (extra_unit_ids or [])],
        "categoryIds": [str(c) for c in (e.category_ids or [])],
        "overrides": e.overrides,
        "status": e.status,
        "isSuperAdmin": e.is_super_admin,
    }


def _country_of(units: Mapping[uuid.UUID, OrgUnit], unit_id: uuid.UUID | None) -> uuid.UUID | None:
    unit = units.get(unit_id) if unit_id else None
    return unit.country_id if unit is not None else None


# ----------------------------------------------------------------------------- employees


async def _scope_unit_ids(session: AsyncSession, scope: UnitScope, q: EmployeeQuery) -> list[uuid.UUID]:
    """The home units a list request covers: the whole scope, or the subtree of `unitId` (unknown → nothing)."""
    if not q.unit_id:
        return scope.descendant_ids
    ids = ids_of([q.unit_id])
    return await narrow_to(session, scope, ids) if ids else [uuid.UUID(int=0)]


async def list_employees(session: AsyncSession, scope: UnitScope, q: EmployeeQuery, staff: StaffPrincipal | None = None) -> Page[EmployeeOut]:
    """Paged employees whose home unit lies under the scope (filters + folded search, default sort fullName asc).
    Superadmin employees (no unit) are listed only for superadmin callers, and never inside a `unitId` narrowing."""
    include_super = bool(staff and staff.is_super_admin) and not q.unit_id
    rows, total = await repo.list_employees(session, await _scope_unit_ids(session, scope, q), q, include_super_admins=include_super)
    return page_of(await employees_out(session, rows, staff), q, total)


async def get_employee_or_404(session: AsyncSession, employee_id: uuid.UUID, staff: StaffPrincipal) -> Employee:
    """Alive employee or 404 "Xodim topilmadi"; 403 when their home unit is outside the caller's scope
    (a superadmin target — no unit — is visible only to superadmins)."""
    emp = await repo.get_employee(session, employee_id)
    if not emp:
        raise NotFoundError("Xodim topilmadi")
    staff.require_unit(emp.unit_id)
    return emp


async def get_employee_dto(session: AsyncSession, employee_id: uuid.UUID, staff: StaffPrincipal) -> EmployeeOut:
    return await employee_dto(session, await get_employee_or_404(session, employee_id, staff), staff)


async def _resolve_role(session: AsyncSession, role_id_raw: str, unit_id: uuid.UUID | None, staff: StaffPrincipal) -> Role:
    """A role the employee of `unit_id` may hold: system, or scoped to a unit on the path of `unit_id`
    (404 "Rol topilmadi" otherwise); the superadmin and installer roles are assigned by superadmins only."""
    role_id = _parse_ids([role_id_raw], "roleId")[0]
    path = await ancestor_ids(session, unit_id) if unit_id else []
    role = await repo.get_assignable_role(session, role_id, path)
    if not role:
        raise NotFoundError("Rol topilmadi")
    if role.scope_unit_id is None and role.key in PROTECTED_ROLE_KEYS and not staff.is_super_admin:
        raise ForbiddenError("Ruxsat yo‘q")
    return role


async def _alive_units(session: AsyncSession, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, OrgUnit]:
    """The requested unit rows; 404 "Birlik topilmadi" when any is missing or deleted."""
    units = await units_repo.units_by_ids(session, ids)
    if any(units.get(u) is None or units[u].deleted_at is not None for u in ids):
        raise NotFoundError(UNIT_NOT_FOUND)
    return units


async def _ensure_login_free(session: AsyncSession, login: str, exclude_id: uuid.UUID | None) -> None:
    if await repo.login_taken(session, login, exclude_id):
        raise ConflictError("Bu login band")


async def create_employee(session: AsyncSession, scope: UnitScope, body: EmployeeCreateIn, staff: StaffPrincipal, meta: RequestMeta) -> EmployeeOut:
    """Create an employee under the scope: home unit = `unitId` or the path unit, extra units `unitIds` — all inside
    the scope (404 "Birlik topilmadi"); global case-insensitive unique login (409 "Bu login band"); argon2 password
    (default 123456); persisted random avatarHue; overrides validated against the catalogue.
    A superadmin (superadmin callers only) has no unit at all."""
    login = body.login.strip()
    await _ensure_login_free(session, login, None)
    is_super = bool(_guard_super_admin_flag(body.is_super_admin, staff))
    unit_id: uuid.UUID | None = None
    extra: list[uuid.UUID] = []
    units: dict[uuid.UUID, OrgUnit] = {}
    if not is_super:
        unit_id = _parse_ids([body.unit_id], "unitId")[0] if body.unit_id else scope.id
        extra = [u for u in _parse_ids(body.unit_ids, "unitId") if u != unit_id]
        if not all(scope.contains(u) for u in (unit_id, *extra)):
            raise NotFoundError(UNIT_NOT_FOUND)
        units = await _alive_units(session, [unit_id, *extra])
    role = await _resolve_role(session, body.role_id, unit_id, staff)
    _check_installer_scope(role, unit_id, extra, units)
    emp = Employee(
        unit_id=unit_id,
        full_name=body.full_name,
        login=login,
        password_hash=hash_password(body.password or DEFAULT_PASSWORD),
        phone=body.phone or None,
        email=body.email or None,
        role_id=role.id,
        overrides=_validate_overrides(body.overrides or PermissionOverrides(), staff),
        category_ids=_parse_ids(body.category_ids, "categoryId"),
        status=body.status,
        avatar_hue=secrets.randbelow(360),
        is_super_admin=is_super,
        created_by=staff.id,
    )
    session.add(emp)
    await session.flush()
    if extra:
        await repo.replace_employee_units(session, emp.id, extra)
        await session.flush()
    await audit(
        session,
        actor_type="staff",
        actor_id=staff.id,
        unit_id=emp.unit_id or scope.id,
        country_id=scope.country_id,
        action="create",
        entity="employee",
        entity_id=emp.id,
        after=_employee_snapshot(emp, extra),
        ip=meta.ip,
        request_id=meta.request_id,
    )
    await invalidate_principal_cache(emp.id)
    await _invalidate_roles_cache()
    await _invalidate_unit_cards(session, emp.unit_id)
    return employee_out(emp, units, extra_unit_ids=extra, reveal_super_admin=staff.is_super_admin)


async def update_employee(session: AsyncSession, employee_id: uuid.UUID, body: EmployeeUpdateIn, staff: StaffPrincipal, meta: RequestMeta) -> EmployeeOut:
    """Partial merge; `unitIds` / `categoryIds` / overrides replaced wholesale; moving `unitId` needs the new unit
    inside the caller's scope; password change or deactivation revokes sessions. A superadmin loses unit and extras."""
    emp = await get_employee_or_404(session, employee_id, staff)
    await _guard_protected_target(session, emp, staff)
    extra_before = (await repo.extra_unit_ids(session, [emp.id])).get(emp.id, [])
    before = _employee_snapshot(emp, extra_before)
    old_unit_id = emp.unit_id
    data = body.model_dump(exclude_unset=True)
    revoke = False
    if data.get("login"):
        login = str(data["login"]).strip()
        if login.lower() != emp.login.lower():
            await _ensure_login_free(session, login, emp.id)
        emp.login = login
    if data.get("full_name"):
        emp.full_name = data["full_name"]
    for field in ("phone", "email"):
        if field in data:
            setattr(emp, field, data[field] or None)
    if "is_super_admin" in data and data["is_super_admin"] is not None:
        emp.is_super_admin = bool(_guard_super_admin_flag(data["is_super_admin"], staff))

    # ---- scope: home unit + extra units
    extra: list[uuid.UUID] | None = None
    if emp.is_super_admin:
        emp.unit_id = None
        extra = []
    else:
        if data.get("unit_id"):
            new_unit = _parse_ids([data["unit_id"]], "unitId")[0]
            staff.require_unit(new_unit)
            emp.unit_id = new_unit
        if emp.unit_id is None:
            raise ValidationError("Xodim uchun birlik tanlanmagan", code="unit_required", details={"field": "unitId"})
        if body.unit_ids is not None:
            extra = [u for u in _parse_ids(body.unit_ids, "unitId") if u != emp.unit_id]
            for u in extra:
                staff.require_unit(u)
        elif emp.unit_id != old_unit_id and emp.unit_id in extra_before:
            extra = [u for u in extra_before if u != emp.unit_id]
    unit_changed = emp.unit_id != old_unit_id
    if unit_changed and emp.unit_id is not None:
        await _alive_units(session, [emp.unit_id])
    if extra:
        await _alive_units(session, extra)
    role: Role | None = None
    if data.get("role_id") or unit_changed:
        # the role must be assignable on the (new) home unit's path
        role = await _resolve_role(session, data.get("role_id") or str(emp.role_id), emp.unit_id, staff)
        emp.role_id = role.id
    if not emp.is_super_admin and (role is not None or extra is not None):
        role = role or (await session.get(Role, emp.role_id) if emp.role_id else None)
        if role is not None and _is_installer_role(role):
            scope_ids = [emp.unit_id, *(extra if extra is not None else extra_before)]
            _check_installer_scope(role, emp.unit_id, scope_ids[1:], await _alive_units(session, [u for u in scope_ids if u]))

    if body.category_ids is not None:
        emp.category_ids = _parse_ids(body.category_ids, "categoryId")
        # lab/confirm category restriction cache (orders module) must follow immediately
        await cache.delete(f"emp:{emp.id}:cats")
    if body.overrides is not None:
        emp.overrides = _validate_overrides(body.overrides, staff)
    if data.get("status") and data["status"] != emp.status:
        emp.status = data["status"]
        revoke = revoke or emp.status == "inactive"
    if body.password:
        emp.password_hash = hash_password(body.password)
        revoke = True
    if extra is not None:
        await repo.replace_employee_units(session, emp.id, extra)
    await session.flush()
    await session.refresh(emp, ["updated_at"])
    if revoke:
        await auth_service.revoke_all_for_subject(session, emp.id)
    extra_after = extra if extra is not None else extra_before
    units = await units_repo.units_by_ids(session, [emp.unit_id, old_unit_id])
    await audit(
        session,
        actor_type="staff",
        actor_id=staff.id,
        unit_id=emp.unit_id or old_unit_id,
        country_id=_country_of(units, emp.unit_id or old_unit_id),
        action="update",
        entity="employee",
        entity_id=emp.id,
        before=before,
        after={**_employee_snapshot(emp, extra_after), "passwordChanged": bool(body.password)},
        ip=meta.ip,
        request_id=meta.request_id,
    )
    await invalidate_principal_cache(emp.id)
    await _invalidate_roles_cache()
    if unit_changed:
        await _invalidate_unit_cards(session, old_unit_id)
        await _invalidate_unit_cards(session, emp.unit_id)
    return employee_out(emp, units, extra_unit_ids=extra_after, reveal_super_admin=staff.is_super_admin)


async def set_overrides(session: AsyncSession, employee_id: uuid.UUID, overrides: PermissionOverrides, staff: StaffPrincipal, meta: RequestMeta) -> EmployeeOut:
    """Replace `{allow, deny}` wholesale (keys must be catalogue permissions → else 422)."""
    emp = await get_employee_or_404(session, employee_id, staff)
    await _guard_protected_target(session, emp, staff)
    before = {"overrides": emp.overrides}
    emp.overrides = _validate_overrides(overrides, staff)
    await session.flush()
    await session.refresh(emp, ["updated_at"])
    units = await units_repo.units_by_ids(session, [emp.unit_id])
    await audit(
        session,
        actor_type="staff",
        actor_id=staff.id,
        unit_id=emp.unit_id,
        country_id=_country_of(units, emp.unit_id),
        action="set_overrides",
        entity="employee",
        entity_id=emp.id,
        before=before,
        after={"overrides": emp.overrides},
        ip=meta.ip,
        request_id=meta.request_id,
    )
    await invalidate_principal_cache(emp.id)
    extra = (await repo.extra_unit_ids(session, [emp.id])).get(emp.id, [])
    return employee_out(emp, units, extra_unit_ids=extra, reveal_super_admin=staff.is_super_admin)


# ----------------------------------------------------------------------------- roles


async def list_roles(session: AsyncSession, scope: UnitScope) -> list[RoleOut]:
    """System roles + roles scoped to the scope's lineage (ancestors + subtree), with the number of alive
    employees under the scope holding each. Cached 5 min per scope unit, invalidated on any role write."""
    key = _roles_cache_key(scope.id)
    hit = await cache.get_json(key)
    if hit is not None:
        return [RoleOut.model_validate(r) for r in hit]
    counts = await repo.employee_counts_by_role(session, scope.descendant_ids)
    roles = [role_out(r).model_copy(update={"employee_count": counts.get(r.id, 0)}) for r in await repo.list_roles(session, scope.lineage_ids)]
    await cache.set_json(key, [r.model_dump(by_alias=True, mode="json") for r in roles], ROLES_CACHE_TTL)
    return roles


async def get_role_or_404(session: AsyncSession, role_id: uuid.UUID, staff: StaffPrincipal) -> Role:
    """Alive role visible to the caller — system, inside their scope, or scoped to a unit on the path above it
    (listed for them, read-only) — else 404 "Rol topilmadi"."""
    role = await repo.get_role(session, role_id)
    if not role:
        raise NotFoundError("Rol topilmadi")
    if role.scope_unit_id is not None and not staff.can_see(role.scope_unit_id):
        if role.scope_unit_id not in await units_repo.ancestors_of(session, staff.unit_ids):
            raise NotFoundError("Rol topilmadi")
    return role


def _guard_role_write(role: Role, staff: StaffPrincipal) -> None:
    """System roles (scope NULL) are edited by superadmins only; a unit role by callers who see its unit."""
    if role.scope_unit_id is None:
        if not staff.is_super_admin:
            raise ForbiddenError("Ruxsat yo‘q")
        return
    staff.require_unit(role.scope_unit_id)


def _role_snapshot(r: Role) -> dict[str, Any]:
    return {"key": r.key, "name": r.name, "description": r.description, "permissions": list(r.permissions or []), "isSystem": r.is_system, "scopeUnitId": str(r.scope_unit_id) if r.scope_unit_id else None}


def _validate_role_key(key: str) -> str:
    key = slugify(key)
    if key in PROTECTED_ROLE_KEYS:
        raise ValidationError("Bu rol kaliti band (tizim uchun ajratilgan)", code="reserved")
    return key


def _validate_permissions(keys: list[str], staff: StaffPrincipal) -> list[str]:
    bad = invalid_permission_keys(keys)
    if bad:
        raise ValidationError("Noma’lum ruxsat kaliti: " + ", ".join(bad), code="invalid_permission", details={"keys": bad})
    _reject_platform_keys(keys, staff)
    return _normalized(keys)


async def create_role(session: AsyncSession, scope: UnitScope, body: RoleCreateIn, staff: StaffPrincipal, meta: RequestMeta) -> RoleOut:
    """Create a role scoped to the path unit: key = slug(key or name), unique within that scope; `superadmin` reserved."""
    key = _validate_role_key(body.key or body.name)
    if await repo.role_key_taken(session, scope.id, key):
        raise ConflictError("Bu rol kaliti band")
    role = Role(scope_unit_id=scope.id, key=key, name=body.name, description=body.description or None, permissions=_validate_permissions(body.permissions, staff), is_system=False, created_by=staff.id)
    session.add(role)
    await session.flush()
    await audit(session, actor_type="staff", actor_id=staff.id, unit_id=scope.id, country_id=scope.country_id, action="create", entity="role", entity_id=role.id, after=_role_snapshot(role), ip=meta.ip, request_id=meta.request_id)
    await _invalidate_roles_cache()
    return role_out(role)


async def update_role(session: AsyncSession, role_id: uuid.UUID, body: RoleUpdateIn, staff: StaffPrincipal, meta: RequestMeta) -> RoleOut:
    """Partial merge; system roles: only name/description/permissions (superadmin only); unit roles: callers who see the unit."""
    role = await get_role_or_404(session, role_id, staff)
    _guard_role_write(role, staff)
    before = _role_snapshot(role)
    data = body.model_dump(exclude_unset=True)
    if data.get("key") and not role.is_system:
        key = _validate_role_key(data["key"])
        if key != role.key and await repo.role_key_taken(session, role.scope_unit_id, key, role.id):
            raise ConflictError("Bu rol kaliti band")
        role.key = key
    if data.get("name"):
        role.name = data["name"]
    if "description" in data:
        role.description = data["description"] or None
    if body.permissions is not None:
        role.permissions = _validate_permissions(body.permissions, staff)
    await session.flush()
    units = await units_repo.units_by_ids(session, [role.scope_unit_id])
    await audit(session, actor_type="staff", actor_id=staff.id, unit_id=role.scope_unit_id, country_id=_country_of(units, role.scope_unit_id), action="update", entity="role", entity_id=role.id, before=before, after=_role_snapshot(role), ip=meta.ip, request_id=meta.request_id)
    await _invalidate_roles_cache()
    await invalidate_principal_cache()
    return role_out(role)


async def delete_role(session: AsyncSession, role_id: uuid.UUID, staff: StaffPrincipal, meta: RequestMeta) -> None:
    """Soft delete; 409 in_use when assigned to employees or when it is a system role."""
    role = await get_role_or_404(session, role_id, staff)
    _guard_role_write(role, staff)
    if role.is_system or role.scope_unit_id is None:
        raise ConflictError("Tizim rolini o‘chirib bo‘lmaydi", code="in_use")
    if await repo.role_in_use(session, role.id):
        raise ConflictError("Bu rol xodimlarga biriktirilgan", code="in_use")
    role.deleted_at = utcnow()
    role.deleted_by = staff.id
    await session.flush()
    units = await units_repo.units_by_ids(session, [role.scope_unit_id])
    await audit(session, actor_type="staff", actor_id=staff.id, unit_id=role.scope_unit_id, country_id=_country_of(units, role.scope_unit_id), action="delete", entity="role", entity_id=role.id, before=_role_snapshot(role), ip=meta.ip, request_id=meta.request_id)
    await _invalidate_roles_cache()
    await invalidate_principal_cache()
