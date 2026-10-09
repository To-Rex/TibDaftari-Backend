"""Cheque list → Excel (pure — no database): a real .xlsx with the rows, money by method and totals."""

from __future__ import annotations

import io
import re
import uuid
import zipfile
from datetime import UTC, datetime
from types import SimpleNamespace

from app.modules.orders.export import build_xlsx, fmt_phone
from app.modules.orders.repository import ORDER_SORTABLE, order_conditions
from app.modules.orders.schemas import OrderExportQuery, OrderListQuery
from sqlalchemy.dialects import postgresql


def _order(number: str, total: int, paid: int, status: str = "in_progress") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(), number=number, created_at=datetime(2026, 10, 10, 5, 30, tzinfo=UTC), patient_name="Karimova Aziza", patient_phone="998901234567",
        item_count=2, subtotal=total, discount_percent=0, discount_amount=0, total=total, paid_amount=paid, payment="partial" if paid < total else "paid", status=status,
    )


def _xml(data: bytes) -> tuple[str, str]:
    z = zipfile.ZipFile(io.BytesIO(data))
    return z.read("xl/worksheets/sheet1.xml").decode(), z.read("xl/sharedStrings.xml").decode()


def test_workbook_rows_methods_totals() -> None:
    a, b, c = _order("UR-1", 150000, 120000), _order("UR-2", 50000, 50000), _order("UR-3", 70000, 0, status="cancelled")
    rows = [SimpleNamespace(Order=o, creator="Kassir", branch="Markaziy") for o in (a, b, c)]
    payments = {a.id: {"cash": 100000, "card": 20000}, b.id: {"card": 50000}}
    data = build_xlsx(rows, payments, {a.id: ["Qon tahlili", "Siydik"]}, lang="uz", caption="Davr: bugun · To‘lov: Naqd", generated_at=datetime(2026, 10, 10, 6, 0, tzinfo=UTC))
    sheet, strings = _xml(data)
    for text in ("Cheklar", "Davr: bugun · To‘lov: Naqd", "Chek", "Naqd", "Karta", "UR-1", "Karimova Aziza", "+998 90 123-45-67", "Qon tahlili, Siydik", "Jarayonda", "Bekor qilingan", "Jami"):
        assert text in strings, text
    assert "<autoFilter" in sheet and "<pane" in sheet  # filter on the header, frozen header
    nums = re.findall(r"<v>(\d+)</v>", sheet)
    assert "120000" in nums and "20000" in nums and "100000" in nums
    # totals leave the cancelled cheque out: total 200000, paid 170000, debt 30000; card 70000
    assert "200000" in nums and "170000" in nums and "30000" in nums and "70000" in nums


def test_languages_and_empty() -> None:
    data = build_xlsx([], {}, {}, lang="ru", generated_at=datetime(2026, 10, 10, tzinfo=UTC))
    _, strings = _xml(data)
    assert "Чеки" in strings and "Наличные" in strings
    assert fmt_phone("998901234567") == "+998 90 123-45-67" and fmt_phone("123") == "123"


def test_export_query_and_new_filters() -> None:
    q = OrderExportQuery.model_validate({"lang": "en", "caption": "x", "results": "partial", "minItems": 2, "maxItems": 5, "refunded": True, "sortBy": "completedAt"})
    sql = " ".join(str(c.compile(dialect=postgresql.dialect())) for c in order_conditions(uuid.uuid4(), q))
    assert "order_items.status = " in sql and "order_items.status != " in sql and "orders.item_count >=" in sql and "payments.refunded_at IS NOT NULL" in sql
    assert {"completedAt", "discountPercent"} <= set(ORDER_SORTABLE)
    assert len(order_conditions(uuid.uuid4(), OrderListQuery())) == 2  # nothing new unless asked
