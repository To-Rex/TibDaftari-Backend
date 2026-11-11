"""Permission catalogue — mirror of the frontend `src/domain/access/permissions.ts`.

WHERE a permission applies is not part of the key: it comes from the employee's unit (their home unit and
everything under it). The same `admin.unit.write` edits a facility for a facility administrator and a whole
viloyat for a regional one."""

from __future__ import annotations

from collections.abc import Iterable

PERMISSIONS: tuple[str, ...] = (
    "reception.patient.read", "reception.patient.write", "reception.order.create", "reception.order.cancel",
    "reception.order.delete",
    "reception.payment.create", "reception.payment.refund",
    # change a payment more than a day after it was taken (refund, money back on a removed service, delete the
    # cheque) — reception can only within the day; admin and rahbar any time
    "reception.payment.late_edit",
    "lab.worklist.read", "lab.result.write", "lab.result.submit",
    # confirm.result.view = approved results only (view / print / re-send) — the registrar desk
    "confirm.result.read", "confirm.result.view", "confirm.result.approve", "confirm.result.resend",
    "reports.finance.read", "reports.finance.recent", "reports.operations.read", "reports.export",
    "messaging.send", "messaging.broadcast",
    # the organisation unit: its profile / settings (read, write) and the units under it (manage = create / edit)
    "admin.unit.read", "admin.unit.write", "admin.unit.manage",
    "admin.employee.read", "admin.employee.write", "admin.role.write",
    "admin.catalog.read", "admin.catalog.write", "admin.schema.write",
    "admin.template.read", "admin.template.write", "admin.template.publish",
    "admin.settings.write",
    # wipe a unit (and everything under it) back to "newborn" — superadmin and the installer, inside their scope
    "admin.unit.reset",
    # the AI assistant («Yordamchi»): chat with the model and run its tools — each tool is gated by the keys above
    "assistant.use",
    # the whole tree, every country (superadmin)
    "platform.unit.manage",
)
PERMISSION_SET = frozenset(PERMISSIONS)
PLATFORM_PERMISSIONS = frozenset(p for p in PERMISSIONS if p.startswith("platform."))
RESET_PERMISSION = "admin.unit.reset"
ASSISTANT_PERMISSION = "assistant.use"
#: what a unit administrator may hold: everything but the platform keys and the reset
UNIT_ADMIN_PERMISSIONS = tuple(p for p in PERMISSIONS if not p.startswith("platform.") and p != RESET_PERMISSION)
#: the installer: a unit administrator who may also reset units — a "superadmin of their own territory"
INSTALLER_PERMISSIONS = (*UNIT_ADMIN_PERMISSIONS, RESET_PERMISSION)

#: v1 → v2 key renames (roles / overrides saved before the port are read through this map)
RENAMED: dict[str, str] = {
    "admin.company.read": "admin.unit.read",
    "admin.company.write": "admin.unit.write",
    "admin.branch.write": "admin.unit.manage",
    "platform.company.manage": "platform.unit.manage",
}

SUPERADMIN_ROLE_KEY = "superadmin"
#: O‘rnatuvchi — like a superadmin, but only inside the units a superadmin gave them (their viloyat, several
#: viloyats, or a whole country); they never cross into another country
INSTALLER_ROLE_KEY = "installer"
ADMIN_ROLE_KEY = "admin"
#: roles only a superadmin may grant, and whose holders only a superadmin may edit or remove
PROTECTED_ROLE_KEYS = frozenset({SUPERADMIN_ROLE_KEY, INSTALLER_ROLE_KEY})
#: where an installer may be homed (extra units: regions of the same country)
INSTALLER_HOME_LEVELS = frozenset({"country", "region"})
INSTALLER_EXTRA_LEVELS = frozenset({"region"})


def normalize_key(key: str) -> str:
    return RENAMED.get(key, key)


def resolve_permissions(role_permissions: Iterable[str] | None, overrides: dict | None) -> list[str]:
    """role ∪ allow − deny, order preserved (role order, then appended allows). Old key names are mapped."""
    result: list[str] = []
    seen: set[str] = set()
    for raw in list(role_permissions or []) + list((overrides or {}).get("allow") or []):
        p = normalize_key(raw)
        if p in PERMISSION_SET and p not in seen:
            seen.add(p)
            result.append(p)
    deny = {normalize_key(p) for p in (overrides or {}).get("deny") or []}
    return [p for p in result if p not in deny]


def invalid_permission_keys(keys: Iterable[str]) -> list[str]:
    return [k for k in keys if normalize_key(k) not in PERMISSION_SET]


# System roles (scope_unit_id NULL) — seeded by db/v2/21_seed_roles.sql with fixed ids; keys are stable machine names
# used by the frontend. WHERE a role applies comes from the employee's unit.
SYSTEM_ROLES: tuple[dict, ...] = (
    {"key": SUPERADMIN_ROLE_KEY, "name": "Superadmin", "is_system": True, "permissions": list(PERMISSIONS)},
    {"key": INSTALLER_ROLE_KEY, "name": "O‘rnatuvchi", "is_system": True, "permissions": list(INSTALLER_PERMISSIONS)},
    {"key": ADMIN_ROLE_KEY, "name": "Administrator", "is_system": True, "permissions": list(UNIT_ADMIN_PERMISSIONS)},
    {
        "key": "registrator",
        "name": "Registrator / Kassir",
        "is_system": True,
        "permissions": [
            "reception.patient.read", "reception.patient.write", "reception.order.create", "reception.order.cancel",
            "reception.order.delete",
            "reception.payment.create", "reports.operations.read", "messaging.send",
            # reports like the admin's: full revenue (no 7-day cap) + CSV / Excel export — still confined to the
            # employee's units; `reports.finance.recent` stays as the fallback if an admin takes finance.read away
            "reports.finance.read", "reports.export", "reports.finance.recent",
            # approved results: view, print and re-send the SMS (no approving, nothing unapproved)
            "confirm.result.view", "confirm.result.resend",
            ASSISTANT_PERMISSION,
        ],
    },
    {"key": "laborant", "name": "Laborant", "is_system": True, "permissions": ["lab.worklist.read", "lab.result.write", "lab.result.submit", ASSISTANT_PERMISSION]},
    {
        "key": "vrach",
        "name": "Vrach",
        "is_system": True,
        "permissions": [
            "lab.worklist.read", "lab.result.write", "confirm.result.read", "confirm.result.approve", "confirm.result.resend",
            "reception.patient.read", ASSISTANT_PERMISSION,
        ],
    },
    {
        "key": "rahbar",
        "name": "Rahbar",
        "is_system": True,
        "permissions": [
            "reports.finance.read", "reports.operations.read", "reports.export", "reception.patient.read", "admin.employee.read",
            # corrects old payments on a cheque (remove a service with money back, delete the cheque)
            "reception.payment.late_edit", ASSISTANT_PERMISSION,
        ],
    },
)
#: kept for callers written against v1 naming
DEFAULT_COMPANY_ROLES = SYSTEM_ROLES
