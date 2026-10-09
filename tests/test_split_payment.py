"""One payment over several methods (pure — no database)."""

from __future__ import annotations

import pytest
from app.modules.orders.schemas import PayIn
from app.modules.orders.service import payment_parts
from pydantic import ValidationError


def test_single_method_is_unchanged() -> None:
    assert payment_parts(PayIn(amount=120000, method="cash")) == [("cash", 120000)]


def test_split_parts_keep_their_order_and_merge_the_same_method() -> None:
    body = PayIn.model_validate({"amount": 150000, "method": "cash", "parts": [{"method": "cash", "amount": 80000}, {"method": "card", "amount": 50000}, {"method": "cash", "amount": 20000}]})
    assert payment_parts(body) == [("cash", 100000), ("card", 50000)]
    assert sum(a for _, a in payment_parts(body)) == body.amount


def test_parts_must_be_positive_and_known() -> None:
    with pytest.raises(ValidationError):
        PayIn.model_validate({"amount": 10, "method": "cash", "parts": [{"method": "cash", "amount": 0}]})
    with pytest.raises(ValidationError):
        PayIn.model_validate({"amount": 10, "method": "cash", "parts": [{"method": "bitcoin", "amount": 10}]})
