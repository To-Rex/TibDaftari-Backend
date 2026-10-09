"""Registrar desk: approved results only — view, print and re-send the SMS (pure — no database)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from app.api.deps import StaffPrincipal
from app.core.exceptions import ForbiddenError
from app.core.permissions import DEFAULT_COMPANY_ROLES, PERMISSION_SET
from app.modules.orders import router as orders_router
from app.modules.orders.schemas import WorklistQuery


def principal(*perms: str) -> StaffPrincipal:
    emp = SimpleNamespace(id=uuid.uuid4(), company_id=uuid.uuid4(), is_super_admin=False, branch_ids=[])
    return StaffPrincipal(employee=emp, role=SimpleNamespace(key="registrator"), permissions=list(perms), jti="t", token_exp=0)  # type: ignore[arg-type]


def test_registrar_role_gets_approved_results() -> None:
    reg = next(r for r in DEFAULT_COMPANY_ROLES if r["key"] == "registrator")
    assert {"confirm.result.view", "confirm.result.resend", "messaging.send"} <= set(reg["permissions"])
    assert "confirm.result.read" not in reg["permissions"] and "confirm.result.approve" not in reg["permissions"]
    assert "confirm.result.view" in PERMISSION_SET


class _ReachedServiceError(Exception):
    pass


async def _worklist(staff: StaffPrincipal, statuses: list[str]) -> None:
    """Run the worklist endpoint up to the service call (which raises _ReachedServiceError)."""

    async def stop(*a, **k):
        raise _ReachedServiceError

    orig = orders_router.service.worklist
    orders_router.service.worklist = stop
    try:
        await orders_router.worklist(staff.company_id, WorklistQuery(status=statuses), staff, None)  # type: ignore[arg-type]
    finally:
        orders_router.service.worklist = orig


@pytest.mark.asyncio
async def test_view_only_lists_approved_results_only() -> None:
    reg = principal("confirm.result.view", "reception.patient.read")
    with pytest.raises(_ReachedServiceError):
        await _worklist(reg, ["approved"])
    for statuses in ([], ["submitted"], ["approved", "submitted"], ["pending", "entered"]):
        with pytest.raises(ForbiddenError):
            await _worklist(reg, statuses)
    lab = principal("lab.worklist.read")
    for statuses in ([], ["submitted"], ["approved"]):
        with pytest.raises(_ReachedServiceError):
            await _worklist(lab, statuses)
    with pytest.raises(ForbiddenError):
        await _worklist(principal("reception.patient.read"), ["approved"])
