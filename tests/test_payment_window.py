"""Payment edit window: reception changes a payment only within a day; admin / rahbar (late_edit) and superadmin any
time (pure — no database)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from app.api.deps import StaffPrincipal
from app.core.exceptions import ForbiddenError
from app.core.permissions import COMPANY_ADMIN_PERMISSIONS, DEFAULT_COMPANY_ROLES, PERMISSION_SET
from app.modules.orders import router as orders_router
from app.modules.orders.schemas import ReasonIn
from app.modules.orders.service import (
    LATE_EDIT,
    PAYMENT_EDIT_WINDOW,
    ensure_payments_editable,
    payment_locked,
    plan_overpayment_return,
)

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
REGISTRAR = ("reception.order.create", "reception.order.delete", "reception.payment.create", "reception.payment.refund")


def principal(*perms: str, super_admin: bool = False) -> StaffPrincipal:
    emp = SimpleNamespace(id=uuid.uuid4(), company_id=uuid.uuid4(), is_super_admin=super_admin, branch_ids=[])
    return StaffPrincipal(employee=emp, role=SimpleNamespace(key="registrator"), permissions=list(perms), jti="t", token_exp=0)  # type: ignore[arg-type]


def pay(amount: int, hours_ago: float) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), amount=amount, method="cash", created_at=NOW - timedelta(hours=hours_ago))


def test_window_is_one_day() -> None:
    assert PAYMENT_EDIT_WINDOW == timedelta(hours=24)
    assert not payment_locked(NOW - timedelta(hours=23, minutes=59), NOW)
    assert payment_locked(NOW - timedelta(hours=24), NOW)
    assert payment_locked(NOW - timedelta(days=3), NOW)


def test_reception_only_within_the_day() -> None:
    reg = principal(*REGISTRAR)
    ensure_payments_editable(reg, [pay(50_000, 1), pay(10_000, 23.5)], NOW)  # type: ignore[list-item]
    ensure_payments_editable(reg, [], NOW)
    with pytest.raises(ForbiddenError) as e:
        ensure_payments_editable(reg, [pay(50_000, 1), pay(10_000, 25)], NOW)  # type: ignore[list-item]
    assert e.value.code == "payment_locked" and e.value.status == 403
    assert "administrator yoki rahbar" in e.value.message


def test_admin_rahbar_and_superadmin_any_time() -> None:
    old = [pay(50_000, 24 * 30)]
    ensure_payments_editable(principal(*REGISTRAR, LATE_EDIT), old, NOW)  # type: ignore[arg-type]
    ensure_payments_editable(principal(LATE_EDIT), old, NOW)  # type: ignore[arg-type]
    ensure_payments_editable(principal(super_admin=True), old, NOW)  # type: ignore[arg-type]


def test_removed_service_checks_only_the_payments_given_back() -> None:
    """Money back comes from the newest payments; an old one only matters once the refund reaches it."""
    reg = principal(*REGISTRAR)
    old, new = pay(50_000, 48), pay(50_000, 2)
    refunded, _ = plan_overpayment_return([old, new], 30_000)  # type: ignore[list-item]
    assert refunded == [new]
    ensure_payments_editable(reg, refunded, NOW)
    refunded, _ = plan_overpayment_return([old, new], 70_000)  # type: ignore[list-item]
    assert refunded == [new, old]
    with pytest.raises(ForbiddenError):
        ensure_payments_editable(reg, refunded, NOW)
    # nothing given back (only the debt shrinks) — open for reception however old the cheque is
    refunded, _ = plan_overpayment_return([old], 0)  # type: ignore[list-item]
    ensure_payments_editable(reg, refunded, NOW)


def test_roles() -> None:
    roles = {r["key"]: set(r["permissions"]) for r in DEFAULT_COMPANY_ROLES}
    assert LATE_EDIT in PERMISSION_SET and LATE_EDIT in COMPANY_ADMIN_PERMISSIONS
    assert LATE_EDIT in roles["admin"] and LATE_EDIT in roles["rahbar"]
    assert LATE_EDIT not in roles["registrator"] and LATE_EDIT not in roles["laborant"] and LATE_EDIT not in roles["vrach"]


class _ReachedServiceError(Exception):
    pass


async def _call(name: str, staff: StaffPrincipal) -> None:
    """Run a cheque endpoint up to its service call (which raises _ReachedServiceError)."""

    async def stop(*a, **k):
        raise _ReachedServiceError

    orig = getattr(orders_router.service, name)
    setattr(orders_router.service, name, stop)
    try:
        if name == "remove_item":
            await orders_router.remove_item(uuid.uuid4(), uuid.uuid4(), staff, None, None)  # type: ignore[arg-type]
        elif name == "delete_order":
            await orders_router.delete_order(uuid.uuid4(), staff, None, None)  # type: ignore[arg-type]
        else:
            await orders_router.refund(uuid.uuid4(), ReasonIn(reason="x"), staff, None, None)  # type: ignore[arg-type]
    finally:
        setattr(orders_router.service, name, orig)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["remove_item", "delete_order", "refund"])
async def test_late_edit_alone_reaches_the_corrections(name: str) -> None:
    """The rahbar role has no reception desk permissions, yet corrects old payments."""
    with pytest.raises(_ReachedServiceError):
        await _call(name, principal(LATE_EDIT))
    with pytest.raises(ForbiddenError):
        await _call(name, principal("reception.patient.read", "reports.finance.read"))
