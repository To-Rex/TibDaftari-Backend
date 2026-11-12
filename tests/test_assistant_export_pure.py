"""The assistant's export engine — pure: flattening, filters, sort, column choice, the three file formats."""

from __future__ import annotations

import csv
import io
import json

import pytest
from app.core.exceptions import ValidationError
from app.modules.assistant import export as xp

ROWS = [
    {"id": "1", "number": "A-3", "patient": {"name": "Olim", "phone": "+998901"}, "total": 30000, "status": "paid", "createdAt": "2026-11-12T10:00:00", "tags": ["x", "y"]},
    {"id": "2", "number": "A-1", "patient": {"name": "anvar", "phone": None}, "total": 5000, "status": "new", "createdAt": "2026-11-11T09:00:00", "unitId": "u"},
    {"id": "3", "number": "A-2", "patient": {"name": "Zarina", "phone": "+998902"}, "total": None, "status": "paid", "createdAt": "2026-11-12T12:00:00"},
]


def test_flatten_and_default_columns_drop_ids() -> None:
    flat = [xp.flatten(r) for r in ROWS]
    assert flat[0]["patient.name"] == "Olim" and flat[0]["tags"] == "x, y"
    keys = [c.key for c in xp.pick_columns(flat, None)]
    assert "id" not in keys and "unitId" not in keys and "patient.name" in keys and "total" in keys
    assert xp.pick_columns(flat, [{"key": "patient.name", "title": "Bemor"}, "total"])[0].title == "Bemor"
    with pytest.raises(ValidationError) as e:
        xp.pick_columns(flat, ["nope"])
    assert "patient.name" in e.value.details["availableColumns"]


def test_filters_cover_numbers_text_dates_and_lists() -> None:
    flat = [xp.flatten(r) for r in ROWS]
    f = lambda filters: [r["number"] for r in xp.apply_filters(flat, filters)]  # noqa: E731
    assert f([{"column": "status", "op": "eq", "value": "PAID"}]) == ["A-3", "A-2"]
    assert f([{"column": "total", "op": "gte", "value": "10000"}]) == ["A-3"]
    assert f([{"column": "total", "op": "lt", "value": 10000}]) == ["A-1"]  # None never compares
    assert f([{"column": "createdAt", "op": "eq", "value": "2026-11-12"}]) == ["A-3", "A-2"]
    assert f([{"column": "createdAt", "op": "between", "value": ["2026-11-11", "2026-11-11T23:59:59"]}]) == ["A-1"]
    assert f([{"column": "patient.name", "op": "contains", "value": "AR"}]) == ["A-2", "A-1"] or f([{"column": "patient.name", "op": "contains", "value": "AR"}]) == ["A-1", "A-2"]
    assert f([{"column": "patient.phone", "op": "empty"}]) == ["A-1"]
    assert f([{"column": "number", "op": "in", "value": ["A-1", "A-2"]}]) == ["A-1", "A-2"]
    assert f([{"column": "status", "op": "ne", "value": "paid"}, {"column": "total", "op": "gt", "value": 0}]) == ["A-1"]
    with pytest.raises(ValidationError):
        xp.apply_filters(flat, [{"column": "status", "op": "like", "value": "x"}])


def test_sort_is_numeric_when_it_can_be_and_empties_go_last() -> None:
    flat = [xp.flatten(r) for r in ROWS]
    assert [r["number"] for r in xp.apply_sort(flat, [{"column": "total", "dir": "desc"}])] == ["A-3", "A-1", "A-2"]
    assert [r["number"] for r in xp.apply_sort(flat, [{"column": "total", "dir": "asc"}])] == ["A-1", "A-3", "A-2"]
    assert [r["number"] for r in xp.apply_sort(flat, [{"column": "patient.name"}])] == ["A-1", "A-3", "A-2"]  # case-insensitive
    assert [r["number"] for r in xp.apply_sort(flat, [{"column": "status"}, {"column": "number", "dir": "desc"}])] == ["A-1", "A-3", "A-2"]


def test_the_three_formats() -> None:
    cols, rows = xp.prepare(ROWS, filters=None, sort=[{"column": "number"}], columns=[{"key": "number", "title": "Chek"}, {"key": "total", "title": "Summa"}], limit=2)
    assert [r["number"] for r in rows] == ["A-1", "A-2"]
    data, ct = xp.build_file("excel", "Cheklar", cols, rows)
    assert data[:2] == b"PK" and ct.startswith("application/vnd.openxmlformats")
    data, ct = xp.build_file("csv", "Cheklar", cols, rows)
    text = data.decode("utf-8")
    assert text.startswith("﻿") and ct.startswith("text/csv")
    parsed = list(csv.reader(io.StringIO(text.lstrip("﻿")), delimiter=";"))
    assert parsed[0] == ["Chek", "Summa"] and parsed[1] == ["A-1", "5000"] and parsed[2] == ["A-2", ""]
    data, ct = xp.build_file("json", "Cheklar", cols, rows)
    assert json.loads(data) == [{"number": "A-1", "total": 5000}, {"number": "A-2", "total": None}]
    with pytest.raises(ValidationError):
        xp.build_file("pptx", "x", cols, rows)
    assert xp.file_name(None, "Cheklar ro‘yxati", "xlsx", "2026-11-12").endswith("-2026-11-12.xlsx")
    assert xp.file_name("hisobot", None, "csv", "x") == "hisobot.csv" and xp.file_name("a/b.xlsx", None, "xlsx", "x") == "a-b.xlsx"
