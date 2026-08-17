"""Staff business rules: employees (credentials, overrides, sessions) and roles (system/platform guards)."""

from __future__ import annotations

import secrets
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import RequestMeta, StaffPrincipal
from app.core.audit import audit
from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.core.pagination import page_of
from app.core.permissions import PLATFORM_PERMISSIONS, SUPERADMIN_ROLE_KEY, invalid_permission_keys
from app.core.schemas import Page
from app.core.security import hash_password
from app.core.textutil import slugify
from app.core.timeutil import utcnow
from app.infrastructure.db.base import alive
from app.infrastructure.db.models import Branch, Employee, Role
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
from app.modules.tenant import service as tenant_service

DEFAULT_PASSWORD = "123456"
ROLES_CACHE_TTL = 300


def _roles_cache_key(company_id: uuid.UUID | str) -> str:
    return f"roles:{company_id}"


async def _invalidate_roles_cache(company_id: uuid.UUID | None) -> None:
    """Company role change → that company's list; platform role change → every company's list."""
    if company_id is None:
        await cache.delete_prefix("roles:")
    else:
        await cache.delete(_roles_cache_key(company_id))


# ----------------------------------------------------------------------------- mapping


def employee_out(e: Employee) -> EmployeeOut:
    """Employee ORM row → DTO (never includes password data or the superadmin flag)."""
    ov = e.overrides or {}
    return EmployeeOut(
        id=str(e.id),
        company_id=str(e.company_id),
        branch_ids=[str(b) for b in (e.branch_ids or [])],
        full_name=e.full_name,
        login=e.login,
        phone=e.phone,
        email=e.email,
        role_id=str(e.role_id),
        overrides=PermissionOverrides(allow=list(ov.get("allow") or []), deny=list(ov.get("deny") or [])),
        category_ids=[str(c) for c in (e.category_ids or [])],
        status=e.status,  # type: ignore[arg-type]
        avatar_hue=e.avatar_hue,
        last_login_at=e.last_login_at,
        created_at=e.created_at,
        updated_at=e.updated_at,
    )


def role_out(r: Role) -> RoleOut:
    """Role ORM row → DTO."""
    return RoleOut(id=str(r.id), company_id=str(r.company_id) if r.company_id else None, key=r.key, name=r.name, description=r.description, permissions=list(r.permissions or []), is_system=r.is_system)


def _tenant_filter(staff: StaffPrincipal) -> uuid.UUID | None:
    return None if staff.is_super_admin else staff.company_id


def _parse_ids(values: list[str], label: str) -> list[uuid.UUID]:
    out: list[uuid.UUID] = []
    for v in values:
        try:
            out.append(uuid.UUID(str(v)))
        except ValueError as exc:
            raise ValidationError(f"{label} identifikatori noto‘g‘ri: {v}", details={"field": label}) from exc
    return list(dict.fromkeys(out))


def _reject_platform_keys(keys: list[str], staff: StaffPrincipal) -> None:
    """`platform.*` permissions are granted only by superadmins (never through company roles/overrides)."""
    if staff.is_super_admin:
        return
    bad = [k for k in keys if k in PLATFORM_PERMISSIONS]
    if bad:
        raise ForbiddenError("Ruxsat yo‘q")


def _validate_overrides(ov: PermissionOverrides, staff: StaffPrincipal) -> dict[str, list[str]]:
    bad = invalid_permission_keys([*ov.allow, *ov.deny])
    if bad:
        raise ValidationError("Noma’lum ruxsat kaliti: " + ", ".join(bad), code="invalid_permission", details={"keys": bad})
    _reject_platform_keys(list(ov.allow), staff)
    return {"allow": list(dict.fromkeys(ov.allow)), "deny": list(dict.fromkeys(ov.deny))}


def _guard_super_admin_target(emp: Employee, staff: StaffPrincipal) -> None:
    """Only a superadmin may modify a superadmin employee (prevents platform escalation via password reset)."""
    if emp.is_super_admin and not staff.is_super_admin:
        raise ForbiddenError("Ruxsat yo‘q")


def _employee_snapshot(e: Employee) -> dict[str, Any]:
    return {
        "fullName": e.full_name,
        "login": e.login,
        "phone": e.phone,
        "email": e.email,
        "roleId": str(e.role_id),
        "branchIds": [str(b) for b in (e.branch_ids or [])],
        "categoryIds": [str(c) for c in (e.category_ids or [])],
        "overrides": e.overrides,
        "status": e.status,
        "isSuperAdmin": e.is_super_admin,
    }


# ----------------------------------------------------------------------------- employees


async def list_employees(session: AsyncSession, company_id: uuid.UUID, q: EmployeeQuery) -> Page[EmployeeOut]:
    """Paged employees of a company (filters + folded search, default sort fullName asc)."""
    rows, total = await repo.list_employees(session, company_id, q)
    return page_of([employee_out(e) for e in rows], q, total)


async def get_employee_or_404(session: AsyncSession, employee_id: uuid.UUID, company_id: uuid.UUID | None) -> Employee:
    """Alive employee or 404 "Xodim topilmadi"; `company_id=None` = platform-wide (superadmin)."""
    emp = await repo.get_employee(session, employee_id, company_id)
    if not emp:
        raise NotFoundError("Xodim topilmadi")
    return emp


async def get_employee_dto(session: AsyncSession, employee_id: uuid.UUID, staff: StaffPrincipal) -> EmployeeOut:
    return employee_out(await get_employee_or_404(session, employee_id, _tenant_filter(staff)))


async def _resolve_role(session: AsyncSession, role_id_raw: str, company_id: uuid.UUID, staff: StaffPrincipal) -> Role:
    role_id = _parse_ids([role_id_raw], "roleId")[0]
    role = await repo.get_assignable_role(session, role_id, company_id)
    if not role:
        raise NotFoundError("Rol topilmadi")
    if role.key == SUPERADMIN_ROLE_KEY and not staff.is_super_admin:
        raise ForbiddenError("Ruxsat yo‘q")
    return role


async def _resolve_branch_ids(session: AsyncSession, company_id: uuid.UUID, raw: list[str]) -> list[uuid.UUID]:
    ids = _parse_ids(raw, "branchId")
    if not ids:
        return []
    found = (await session.execute(select(Branch.id).where(Branch.company_id == company_id, Branch.id.in_(ids), alive(Branch)))).scalars().all()
    if len(found) != len(ids):
        raise NotFoundError("Filial topilmadi")
    return ids


async def _ensure_login_free(session: AsyncSession, login: str, exclude_id: uuid.UUID | None) -> None:
    if await repo.login_taken(session, login, exclude_id):
        raise ConflictError("Bu login band")


def _guard_super_admin_flag(value: bool | None, staff: StaffPrincipal) -> bool | None:
    if value is not None and not staff.is_super_admin:
        raise ForbiddenError("Ruxsat yo‘q")
    return value


async def create_employee(session: AsyncSession, company_id: uuid.UUID, body: EmployeeCreateIn, staff: StaffPrincipal, meta: RequestMeta) -> EmployeeOut:
    """Create an employee: global case-insensitive unique login (409 "Bu login band"), argon2 password
    (default 123456), persisted random avatarHue, overrides validated against the catalogue."""
    company = await tenant_service.get_company_or_404(session, company_id)
    login = body.login.strip()
    await _ensure_login_free(session, login, None)
    role = await _resolve_role(session, body.role_id, company.id, staff)
    branch_ids = await _resolve_branch_ids(session, company.id, body.branch_ids)
    is_super = _guard_super_admin_flag(body.is_super_admin, staff)
    emp = Employee(
        company_id=company.id,
        branch_ids=branch_ids,
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
        is_super_admin=bool(is_super),
        created_by=staff.id,
    )
    session.add(emp)
    await session.flush()
    await audit(session, actor_type="staff", actor_id=staff.id, company_id=company.id, action="create", entity="employee", entity_id=emp.id, after=_employee_snapshot(emp), ip=meta.ip, request_id=meta.request_id)
    await tenant_service.invalidate_company_cache(company.id)
    return employee_out(emp)


async def update_employee(session: AsyncSession, employee_id: uuid.UUID, body: EmployeeUpdateIn, staff: StaffPrincipal, meta: RequestMeta) -> EmployeeOut:
    """Partial merge; lists/overrides replaced wholesale; password change or deactivation revokes sessions."""
    emp = await get_employee_or_404(session, employee_id, _tenant_filter(staff))
    _guard_super_admin_target(emp, staff)
    before = _employee_snapshot(emp)
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
    if data.get("role_id"):
        emp.role_id = (await _resolve_role(session, data["role_id"], emp.company_id, staff)).id
    if body.branch_ids is not None:
        emp.branch_ids = await _resolve_branch_ids(session, emp.company_id, body.branch_ids)
    if body.category_ids is not None:
        emp.category_ids = _parse_ids(body.category_ids, "categoryId")
        # lab/confirm restriction cache (orders.service.allowed_category_ids) must follow immediately
        await cache.delete(f"co:{emp.company_id}:empcats:{emp.id}")
    if body.overrides is not None:
        emp.overrides = _validate_overrides(body.overrides, staff)
    if data.get("status") and data["status"] != emp.status:
        emp.status = data["status"]
        revoke = revoke or emp.status == "inactive"
    if "is_super_admin" in data and data["is_super_admin"] is not None:
        emp.is_super_admin = bool(_guard_super_admin_flag(data["is_super_admin"], staff))
    if body.password:
        emp.password_hash = hash_password(body.password)
        revoke = True
    await session.flush()
    await session.refresh(emp, ["updated_at"])
    if revoke:
        await auth_service.revoke_all_for_subject(session, emp.id)
    await audit(session, actor_type="staff", actor_id=staff.id, company_id=emp.company_id, action="update", entity="employee", entity_id=emp.id, before=before, after={**_employee_snapshot(emp), "passwordChanged": bool(body.password)}, ip=meta.ip, request_id=meta.request_id)
    return employee_out(emp)


async def set_overrides(session: AsyncSession, employee_id: uuid.UUID, overrides: PermissionOverrides, staff: StaffPrincipal, meta: RequestMeta) -> EmployeeOut:
    """Replace `{allow, deny}` wholesale (keys must be catalogue permissions → else 422)."""
    emp = await get_employee_or_404(session, employee_id, _tenant_filter(staff))
    _guard_super_admin_target(emp, staff)
    before = {"overrides": emp.overrides}
    emp.overrides = _validate_overrides(overrides, staff)
    await session.flush()
    await session.refresh(emp, ["updated_at"])
    await audit(session, actor_type="staff", actor_id=staff.id, company_id=emp.company_id, action="set_overrides", entity="employee", entity_id=emp.id, before=before, after={"overrides": emp.overrides}, ip=meta.ip, request_id=meta.request_id)
    return employee_out(emp)


# ----------------------------------------------------------------------------- roles


async def list_roles(session: AsyncSession, company_id: uuid.UUID) -> list[RoleOut]:
    """Company roles + platform roles (cached 5 min, invalidated on any role write)."""
    key = _roles_cache_key(company_id)
    hit = await cache.get_json(key)
    if hit is not None:
        return [RoleOut.model_validate(r) for r in hit]
    await tenant_service.get_company_or_404(session, company_id)
    roles = [role_out(r) for r in await repo.list_roles(session, company_id)]
    await cache.set_json(key, [r.model_dump(by_alias=True, mode="json") for r in roles], ROLES_CACHE_TTL)
    return roles


async def get_role_or_404(session: AsyncSession, role_id: uuid.UUID, staff: StaffPrincipal) -> Role:
    """Alive role visible to the caller (own company or platform) or 404 "Rol topilmadi"."""
    role = await repo.get_role(session, role_id)
    if not role or (not staff.is_super_admin and role.company_id is not None and role.company_id != staff.company_id):
        raise NotFoundError("Rol topilmadi")
    return role


def _guard_platform_role(role: Role, staff: StaffPrincipal) -> None:
    if role.company_id is None and not staff.is_super_admin:
        raise ForbiddenError("Ruxsat yo‘q")


def _role_snapshot(r: Role) -> dict[str, Any]:
    return {"key": r.key, "name": r.name, "description": r.description, "permissions": list(r.permissions or []), "isSystem": r.is_system}


def _validate_role_key(key: str, company_id: uuid.UUID | None) -> str:
    key = slugify(key)
    if key == SUPERADMIN_ROLE_KEY and company_id is not None:
        raise ValidationError("Bu rol kaliti band (tizim uchun ajratilgan)", code="reserved")
    return key


def _validate_permissions(keys: list[str], staff: StaffPrincipal) -> list[str]:
    bad = invalid_permission_keys(keys)
    if bad:
        raise ValidationError("Noma’lum ruxsat kaliti: " + ", ".join(bad), code="invalid_permission", details={"keys": bad})
    _reject_platform_keys(keys, staff)
    return list(dict.fromkeys(keys))


async def create_role(session: AsyncSession, company_id: uuid.UUID, body: RoleCreateIn, staff: StaffPrincipal, meta: RequestMeta) -> RoleOut:
    """Create a company role: key = slug(key or name), unique per company; `superadmin` reserved."""
    company = await tenant_service.get_company_or_404(session, company_id)
    key = _validate_role_key(body.key or body.name, company.id)
    if await repo.role_key_taken(session, company.id, key):
        raise ConflictError("Bu rol kaliti band")
    role = Role(company_id=company.id, key=key, name=body.name, description=body.description or None, permissions=_validate_permissions(body.permissions, staff), is_system=False, created_by=staff.id)
    session.add(role)
    await session.flush()
    await audit(session, actor_type="staff", actor_id=staff.id, company_id=company.id, action="create", entity="role", entity_id=role.id, after=_role_snapshot(role), ip=meta.ip, request_id=meta.request_id)
    await _invalidate_roles_cache(company.id)
    return role_out(role)


async def update_role(session: AsyncSession, role_id: uuid.UUID, body: RoleUpdateIn, staff: StaffPrincipal, meta: RequestMeta) -> RoleOut:
    """Partial merge; system roles: only name/description/permissions; platform roles: superadmin only."""
    role = await get_role_or_404(session, role_id, staff)
    _guard_platform_role(role, staff)
    before = _role_snapshot(role)
    data = body.model_dump(exclude_unset=True)
    if data.get("key") and not role.is_system:
        key = _validate_role_key(data["key"], role.company_id)
        if key != role.key and await repo.role_key_taken(session, role.company_id, key, role.id):
            raise ConflictError("Bu rol kaliti band")
        role.key = key
    if data.get("name"):
        role.name = data["name"]
    if "description" in data:
        role.description = data["description"] or None
    if body.permissions is not None:
        role.permissions = _validate_permissions(body.permissions, staff)
    await session.flush()
    await audit(session, actor_type="staff", actor_id=staff.id, company_id=role.company_id, action="update", entity="role", entity_id=role.id, before=before, after=_role_snapshot(role), ip=meta.ip, request_id=meta.request_id)
    await _invalidate_roles_cache(role.company_id)
    return role_out(role)


async def delete_role(session: AsyncSession, role_id: uuid.UUID, staff: StaffPrincipal, meta: RequestMeta) -> None:
    """Soft delete; 409 in_use when assigned to employees or when it is a system role."""
    role = await get_role_or_404(session, role_id, staff)
    _guard_platform_role(role, staff)
    if role.is_system:
        raise ConflictError("Tizim rolini o‘chirib bo‘lmaydi", code="in_use")
    if await repo.role_in_use(session, role.id):
        raise ConflictError("Bu rol xodimlarga biriktirilgan", code="in_use")
    role.deleted_at = utcnow()
    role.deleted_by = staff.id
    await session.flush()
    await audit(session, actor_type="staff", actor_id=staff.id, company_id=role.company_id, action="delete", entity="role", entity_id=role.id, before=_role_snapshot(role), ip=meta.ip, request_id=meta.request_id)
    await _invalidate_roles_cache(role.company_id)
