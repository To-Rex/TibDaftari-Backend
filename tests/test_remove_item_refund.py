"""Removing a service from a paid cheque: which payments go back (pure — no database)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.modules.orders.service import plan_overpayment_return

T0 = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)


def pay(amount: int, minutes: int, method: str = "cash") -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), amount=amount, method=method, created_at=T0 + timedelta(minutes=minutes))


def test_one_payment_is_refunded_and_the_rest_stays_paid() -> None:
    p = pay(150_000, 0)
    refunded, keep = plan_overpayment_return([p], 50_000)  # type: ignore[list-item]
    assert refunded == [p] and keep == 100_000


def test_the_newest_payments_go_back_first() -> None:
    cash, card = pay(100_000, 0, "cash"), pay(50_000, 5, "card")
    # exactly the card payment: nothing to re-enter
    assert plan_overpayment_return([cash, card], 50_000) == ([card], 0)  # type: ignore[list-item]
    # less than the card payment: the card keeps the rest
    assert plan_overpayment_return([cash, card], 30_000) == ([card], 20_000)  # type: ignore[list-item]
    # more than the card payment: the cash one too, and the cash keeps the rest
    assert plan_overpayment_return([cash, card], 120_000) == ([card, cash], 30_000)  # type: ignore[list-item]
    # everything back (the last service left the cheque)
    assert plan_overpayment_return([cash, card], 150_000) == ([card, cash], 0)  # type: ignore[list-item]


def test_nothing_goes_back_without_an_excess() -> None:
    assert plan_overpayment_return([pay(10_000, 0)], 0) == ([], 0)  # type: ignore[list-item]
