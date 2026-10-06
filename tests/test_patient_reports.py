"""Patient / results / services reports — the pure parts (no database)."""

from __future__ import annotations

from datetime import date

from app.modules.reports.service import _sms_status, _telegram_ok, age_group, gender_key, previous_period


def test_age_groups() -> None:
    assert [age_group(a) for a in (0, 17, 17.9, 18, 29, 30, 44, 45, 59, 60, 101)] == ["0-17", "0-17", "0-17", "18-29", "18-29", "30-44", "30-44", "45-59", "45-59", "60+", "60+"]
    assert age_group(None) == "unknown" and age_group(-1) == "unknown" and age_group(200) == "unknown"


def test_gender_key() -> None:
    assert (gender_key("male"), gender_key("female"), gender_key(None), gender_key("x")) == ("male", "female", "unknown", "unknown")


def test_previous_period_has_the_same_length() -> None:
    assert previous_period(date(2026, 9, 1), date(2026, 9, 30)) == (date(2026, 8, 2), date(2026, 8, 31))
    assert previous_period(date(2026, 10, 7), date(2026, 10, 7)) == (date(2026, 10, 6), date(2026, 10, 6))


def test_delivery_helpers() -> None:
    d = [{"channel": "portal", "status": "delivered"}, {"channel": "sms", "status": "failed"}, {"channel": "telegram", "status": "sent"}]
    assert _sms_status(d) == "failed" and _telegram_ok(d) is True
    assert _sms_status([]) is None and _telegram_ok([{"channel": "telegram", "status": "failed"}]) is False and _telegram_ok(None) is False
