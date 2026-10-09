"""Cheque list filters (pure — SQL is only compiled, no database)."""

from __future__ import annotations

import uuid

from app.modules.orders.repository import ORDER_SORTABLE, order_conditions
from app.modules.orders.schemas import OrderListQuery
from sqlalchemy.dialects import postgresql

CID = uuid.uuid4()


def _sql(q: OrderListQuery) -> list[str]:
    return [str(c.compile(dialect=postgresql.dialect())) for c in order_conditions(CID, q)]


def test_old_queries_build_the_same_conditions() -> None:
    assert len(_sql(OrderListQuery())) == 2  # company + alive
    old = OrderListQuery(status="open", payment="unpaid", date_from="2026-10-01", date_to="2026-10-02", search="UR-1")
    assert len(_sql(old)) == 2 + 4 + 1  # + status, payment, from, to + search


def test_new_filters() -> None:
    q = OrderListQuery.model_validate({"methods": ["cash", "card"], "minTotal": 1000, "maxTotal": 9000, "debt": True, "discount": False, "serviceTypeId": str(uuid.uuid4()), "categoryIds": [str(uuid.uuid4()), "bad"], "createdBy": str(uuid.uuid4())})
    sql = " ".join(_sql(q))
    for part in ("payments.method IN", "payments.refunded_at IS NULL", "orders.total >=", "orders.total <=", "orders.paid_amount < orders.total", "orders.discount_amount =", "order_items.service_type_id =", "order_items.category_id IN", "orders.created_by_employee_id ="):
        assert part in sql, part
    assert "order_items.status != " in sql  # cancelled services do not count


def test_new_sort_keys() -> None:
    assert {"remaining", "itemCount", "discountAmount"} <= set(ORDER_SORTABLE)
