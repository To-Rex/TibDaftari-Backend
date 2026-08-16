"""Time helpers. Storage is UTC; business days are evaluated in the clinic timezone."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

DEFAULT_TZ = ZoneInfo("Asia/Tashkent")


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def day_range(day_from: str | date | None, day_to: str | date | None, tz: ZoneInfo = DEFAULT_TZ) -> tuple[datetime | None, datetime | None]:
    """Inclusive calendar days in `tz` → half-open UTC interval [start, end)."""
    start = end = None
    if day_from:
        d = date.fromisoformat(str(day_from)[:10]) if isinstance(day_from, str) else day_from
        start = datetime.combine(d, time.min, tzinfo=tz).astimezone(UTC)
    if day_to:
        d = date.fromisoformat(str(day_to)[:10]) if isinstance(day_to, str) else day_to
        end = (datetime.combine(d, time.min, tzinfo=tz) + timedelta(days=1)).astimezone(UTC)
    return start, end


def local_date(dt: datetime, tz: ZoneInfo = DEFAULT_TZ) -> date:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(tz).date()


def today_local(tz: ZoneInfo = DEFAULT_TZ) -> date:
    return datetime.now(tz).date()


def fmt_date(dt: datetime | date | None, tz: ZoneInfo = DEFAULT_TZ) -> str:
    if dt is None:
        return "—"
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        dt = dt.astimezone(tz)
    return dt.strftime("%d.%m.%Y")


def fmt_datetime(dt: datetime | None, tz: ZoneInfo = DEFAULT_TZ) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(tz).strftime("%d.%m.%Y %H:%M")


def age_years(birth: date | None, on: date | None = None) -> int | None:
    if not birth:
        return None
    on = on or today_local()
    return max(0, on.year - birth.year - ((on.month, on.day) < (birth.month, birth.day)))


def age_months(birth: date | None, on: date | None = None) -> int | None:
    if not birth:
        return None
    on = on or today_local()
    return max(0, (on.year - birth.year) * 12 + (on.month - birth.month) - (1 if on.day < birth.day else 0))
