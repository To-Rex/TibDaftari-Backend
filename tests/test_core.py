"""Unit tests for pure core helpers (no DB)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from app.core import textutil, timeutil
from app.core.crypto import decrypt, encrypt
from app.core.ids import uuid7
from app.core.pagination import total_pages
from app.core.permissions import PERMISSIONS, invalid_permission_keys, resolve_permissions
from app.core.security import decode_token, hash_password, issue_token, mask_secret, verify_password


def test_fold_matches_frontend_pipeline() -> None:
    assert textutil.fold("Shifo Med Ҳамшира ‘Karimov’") == "sxifo med xamsxira karimov"
    assert textutil.fold("ЧАЙ Щука ЁЖ ЮЛЯ ъь") == "cxay sxuka yoj yulya"
    assert textutil.matches("Xo‘jayev Shuhrat", "hojayev") is True
    assert textutil.matches(None, "x") is False
    assert textutil.matches(None, "") is True


def test_phone_helpers() -> None:
    assert textutil.norm_phone("+998 90 123-45-67") == "998901234567"
    assert textutil.norm_phone("901234567") == "998901234567"
    assert textutil.is_valid_uz_phone("998901234567")
    assert not textutil.is_valid_uz_phone("99890123456")
    assert textutil.fmt_phone("998901234567") == "+998 90 123-45-67"
    assert textutil.fmt_phone("") == "—"
    assert textutil.fmt_phone("12345") == "12345"


def test_money_ru_grouping_uses_nbsp() -> None:
    assert textutil.fmt_money_ru(85000) == "85 000"
    assert textutil.fmt_money_ru(1234567) == "1 234 567"
    assert textutil.fmt_money_ru(999) == "999"


def test_permissions_resolution() -> None:
    perms = resolve_permissions(["lab.worklist.read", "lab.result.write"], {"allow": ["reports.export", "bogus.key"], "deny": ["lab.result.write"]})
    assert perms == ["lab.worklist.read", "reports.export"]
    assert invalid_permission_keys(["lab.worklist.read", "nope"]) == ["nope"]
    assert len(PERMISSIONS) == 31


def test_password_and_tokens() -> None:
    h = hash_password("123456")
    assert verify_password("123456", h)
    assert not verify_password("wrong", h)
    assert not verify_password("x", None)
    token, jti, exp = issue_token(uuid7(), "staff", timedelta(hours=1))
    claims = decode_token(token)
    assert claims["jti"] == jti and claims["act"] == "staff"
    assert exp > datetime.now(UTC)
    assert mask_secret("xab_live_abcdef7f2a") == "xab_live_••••••••7f2a"


def test_crypto_roundtrip() -> None:
    assert decrypt(encrypt("secret-key")) == "secret-key"
    assert decrypt("garbage") is None
    assert decrypt(None) is None


def test_uuid7_monotonic() -> None:
    a, b = uuid7(), uuid7()
    assert a.version == 7 and b.version == 7
    assert a.int < b.int


def test_time_helpers() -> None:
    start, end = timeutil.day_range("2026-08-16", "2026-08-16")
    assert start is not None and end is not None
    assert (end - start) == timedelta(days=1)
    assert start.hour == 19  # 00:00 Tashkent = 19:00 UTC previous day
    assert timeutil.fmt_date(datetime(2026, 8, 16, 12, 0, tzinfo=UTC)) == "16.08.2026"
    assert timeutil.fmt_datetime(datetime(2026, 8, 16, 12, 0, tzinfo=UTC)) == "16.08.2026 17:00"
    assert timeutil.age_years(date(2000, 1, 1), date(2026, 8, 16)) == 26
    assert timeutil.age_months(date(2026, 1, 20), date(2026, 8, 16)) == 6
    assert timeutil.to_iso(datetime(2026, 8, 16, 12, 0, 0, 123000, tzinfo=UTC)) == "2026-08-16T12:00:00.123Z"


def test_total_pages() -> None:
    assert total_pages(0, 20) == 1
    assert total_pages(20, 20) == 1
    assert total_pages(21, 20) == 2
