"""Permission catalogue — mirror of the frontend `src/domain/access/permissions.ts`."""

from __future__ import annotations

from collections.abc import Iterable

PERMISSIONS: tuple[str, ...] = (
    "reception.patient.read", "reception.patient.write", "reception.order.create", "reception.order.cancel",
    "reception.payment.create", "reception.payment.refund",
    "lab.worklist.read", "lab.result.write", "lab.result.submit",
    "confirm.result.read", "confirm.result.approve", "confirm.result.resend",
    "reports.finance.read", "reports.operations.read", "reports.export",
    "messaging.send", "messaging.broadcast",
    "admin.company.read", "admin.company.write", "admin.branch.write",
    "admin.employee.read", "admin.employee.write", "admin.role.write",
    "admin.catalog.read", "admin.catalog.write", "admin.schema.write",
    "admin.template.read", "admin.template.write", "admin.template.publish",
    "admin.settings.write",
    "platform.company.manage",
)
PERMISSION_SET = frozenset(PERMISSIONS)
PLATFORM_PERMISSIONS = frozenset(p for p in PERMISSIONS if p.startswith("platform."))
COMPANY_ADMIN_PERMISSIONS = tuple(p for p in PERMISSIONS if not p.startswith("platform."))

SUPERADMIN_ROLE_KEY = "superadmin"
ADMIN_ROLE_KEY = "admin"


def resolve_permissions(role_permissions: Iterable[str] | None, overrides: dict | None) -> list[str]:
    """role ∪ allow − deny, order preserved (role order, then appended allows)."""
    result: list[str] = []
    seen: set[str] = set()
    for p in list(role_permissions or []) + list((overrides or {}).get("allow") or []):
        if p in PERMISSION_SET and p not in seen:
            seen.add(p)
            result.append(p)
    deny = set((overrides or {}).get("deny") or [])
    return [p for p in result if p not in deny]


def invalid_permission_keys(keys: Iterable[str]) -> list[str]:
    return [k for k in keys if k not in PERMISSION_SET]
