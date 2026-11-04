"""Correcting a payment's method / splitting it over several — the amount, time and cashier stay (pure — the
database calls are faked)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from app.api.deps import StaffPrincipal
from app.core.exceptions import ForbiddenError, StateError, ValidationError
from app.infrastructure.db.models import Payment
from app.modules.orders import router as orders_router
from app.modules.orders import service
from app.modules.orders.schemas import PaymentMethodIn, PaymentOut
from pydantic import ValidationError as PydanticValidationError

TAKEN = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)


def principal(*perms: str) -> StaffPrincipal:
    emp = SimpleNamespace(id=uuid.uuid4(), company_id=uuid.uuid4(), is_super_admin=False, branch_ids=[], full_name="Aliyeva Nodira")
    return StaffPrincipal(employee=emp, role=SimpleNamespace(key="registrator"), permissions=list(perms), jti="t", token_exp=0)  # type: ignore[arg-type]


def body(*parts: tuple[str, int], reason: str = "") -> PaymentMethodIn:
    return PaymentMethodIn.model_validate({"parts": [{"method": m, "amount": a} for m, a in parts], "reason": reason})


def test_parts_validation() -> None:
    assert service.method_change_parts("cash", 100_000, body(("card", 100_000))) == [("card", 100_000)]
    # the part with the current method stays on the payment's own row (first)
    assert service.method_change_parts("cash", 100_000, body(("card", 40_000), ("cash", 60_000))) == [("cash", 60_000), ("card", 40_000)]
    # the same method twice is summed
    assert service.method_change_parts("cash", 100_000, body(("card", 30_000), ("card", 70_000))) == [("card", 100_000)]
    with pytest.raises(ValidationError) as e:
        service.method_change_parts("cash", 100_000, body(("card", 90_000)))
    assert e.value.code == "parts"
    with pytest.raises(ValidationError) as e:
        service.method_change_parts("cash", 100_000, body(("cash", 40_000), ("cash", 60_000)))
    assert e.value.code == "same_method"
    # pydantic: no parts / a non-positive amount / a bogus method
    for bad in ((), (("card", 0),), (("crypto", 100_000),)):
        with pytest.raises(PydanticValidationError):
            body(*bad)


class FakeSession:
    def __init__(self) -> None:
        self.added: list[Payment] = []

    async def refresh(self, _obj) -> None:
        return None

    def add_all(self, rows) -> None:
        for r in rows:
            r.id = uuid.uuid4()  # what the flush in recompute would assign
        self.added.extend(rows)


@pytest.fixture
def world(monkeypatch):
    """One 100 000 cash payment taken at 09:00 on a paid cheque; the database calls of the service faked."""
    company = uuid.uuid4()
    order = SimpleNamespace(id=uuid.uuid4(), company_id=company, branch_id=uuid.uuid4())
    pay = Payment(
        id=uuid.uuid4(), company_id=company, order_id=order.id, branch_id=order.branch_id, amount=100_000, method="cash",
        employee_id=uuid.uuid4(), note=None, refunded_at=None, corrections=[], created_at=TAKEN, updated_at=TAKEN,
    )
    audits: list[dict] = []

    async def get_payment(_s, pid, _c):
        return pay if pid == pay.id else None

    async def get_order(_s, _oid, _cid, **_k):
        return order

    async def noop(*_a, **_k):
        return None

    async def record(_s, **kw):
        audits.append(kw)

    async def out(_s, _o):
        return "OUT"

    monkeypatch.setattr(service.repo, "get_payment", get_payment)
    monkeypatch.setattr(service, "get_order_or_404", get_order)
    monkeypatch.setattr(service, "recompute", noop)
    monkeypatch.setattr(service, "audit", record)
    monkeypatch.setattr(service, "_order_payments_out", out)
    return SimpleNamespace(pay=pay, order=order, audits=audits, meta=SimpleNamespace(ip="1.2.3.4", request_id="r"))


def at(monkeypatch, hours_after: float) -> None:
    monkeypatch.setattr(service, "utcnow", lambda: TAKEN + timedelta(hours=hours_after))


@pytest.mark.asyncio
async def test_change_method_in_place(world, monkeypatch) -> None:
    at(monkeypatch, 2)
    staff, s = principal("reception.payment.create"), FakeSession()
    assert await service.change_payment_method(s, world.pay.id, staff, body(("card", 100_000), reason="  adashib naqd  "), world.meta) == "OUT"  # type: ignore[arg-type]
    p = world.pay
    assert (p.method, p.amount, p.created_at, s.added) == ("card", 100_000, TAKEN, [])
    assert p.corrections == [{"at": "2026-11-04T11:00:00.000Z", "byId": str(staff.id), "byName": "Aliyeva Nodira", "fromMethod": "cash", "fromAmount": 100_000, "reason": "adashib naqd"}]
    a = world.audits[0]
    assert a["action"] == "payment.method_change" and a["before"] == {"method": "cash", "amount": 100_000}
    assert a["after"]["parts"] == [{"method": "card", "amount": 100_000}] and a["after"]["newPaymentIds"] == []
    # the output shape carries the history
    out = service.payment_out(p)
    assert isinstance(out, PaymentOut) and out.corrections[0].from_method == "cash" and out.corrections[0].by_name == "Aliyeva Nodira"


@pytest.mark.asyncio
async def test_split_keeps_time_cashier_and_branch(world, monkeypatch) -> None:
    at(monkeypatch, 5)
    staff, s = principal("reception.payment.create"), FakeSession()
    await service.change_payment_method(s, world.pay.id, staff, body(("card", 40_000), ("cash", 60_000)), world.meta)  # type: ignore[arg-type]
    p = world.pay
    assert (p.method, p.amount) == ("cash", 60_000)
    [extra] = s.added
    assert (extra.method, extra.amount, extra.created_at, extra.employee_id, extra.branch_id, extra.order_id) == ("card", 40_000, TAKEN, p.employee_id, p.branch_id, p.order_id)
    assert extra.created_by == staff.id
    assert extra.corrections[0]["splitFrom"] == str(p.id) and extra.corrections[0]["fromAmount"] == 100_000
    assert "splitFrom" not in p.corrections[0]
    assert world.audits[0]["after"]["newPaymentIds"] == [str(extra.id)]
    # a second correction is appended, the first kept
    await service.change_payment_method(s, world.pay.id, staff, body(("transfer", 60_000)), world.meta)  # type: ignore[arg-type]
    assert [c["fromMethod"] for c in p.corrections] == ["cash", "cash"] and p.method == "transfer"


@pytest.mark.asyncio
async def test_window_and_refunded(world, monkeypatch) -> None:
    reg = principal("reception.payment.create")
    at(monkeypatch, 25)
    with pytest.raises(ForbiddenError) as e:
        await service.change_payment_method(FakeSession(), world.pay.id, reg, body(("card", 100_000)), world.meta)  # type: ignore[arg-type]
    assert e.value.code == "payment_locked" and world.pay.method == "cash"
    # admin / rahbar any time
    await service.change_payment_method(FakeSession(), world.pay.id, principal(service.LATE_EDIT), body(("card", 100_000)), world.meta)  # type: ignore[arg-type]
    assert world.pay.method == "card"
    world.pay.refunded_at = TAKEN
    with pytest.raises(StateError):
        await service.change_payment_method(FakeSession(), world.pay.id, principal(service.LATE_EDIT), body(("cash", 100_000)), world.meta)  # type: ignore[arg-type]


class _ReachedServiceError(Exception):
    pass


@pytest.mark.asyncio
async def test_route_permissions(monkeypatch) -> None:
    async def stop(*a, **k):
        raise _ReachedServiceError

    monkeypatch.setattr(orders_router.service, "change_payment_method", stop)
    for perms in (("reception.payment.create",), (service.LATE_EDIT,)):
        with pytest.raises(_ReachedServiceError):
            await orders_router.change_payment_method(uuid.uuid4(), body(("card", 1)), principal(*perms), None, None)  # type: ignore[arg-type]
    with pytest.raises(ForbiddenError):
        await orders_router.change_payment_method(uuid.uuid4(), body(("card", 1)), principal("reception.order.create", "reports.finance.read"), None, None)  # type: ignore[arg-type]
