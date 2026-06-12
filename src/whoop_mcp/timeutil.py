"""Date and time handling.

Two distinct concerns live here:

* Parsing the human-friendly date expressions tools accept ("today",
  "yesterday", "7 days ago", "last week", "2026-06-01", ...). These are
  interpreted in the *user's* timezone (``WHOOP_MCP_TZ`` or the system zone)
  and converted to aware UTC datetimes for the WHOOP API.

* Bucketing WHOOP records onto calendar days. Every cycle/sleep/workout
  record carries its own ``timezone_offset`` (where the user physically was),
  so day attribution uses the record's offset — not UTC and not the current
  machine zone. A sleep belongs to the local day you woke up; cycles and
  workouts belong to the local day they started.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

from whoop_mcp.errors import WhoopError

SUPPORTED_FORMS = (
    "now | today | yesterday | N days ago | N weeks ago | last N days | "
    "this week | last week | this month | last month | YYYY-MM | YYYY-MM-DD | "
    "ISO-8601 datetime"
)

_DAYS_AGO = re.compile(r"^(\d{1,4})\s*days?\s*ago$")
_WEEKS_AGO = re.compile(r"^(\d{1,3})\s*weeks?\s*ago$")
_LAST_N_DAYS = re.compile(r"^(?:last|past)\s+(\d{1,4})\s*days?$")
_MONTH = re.compile(r"^(\d{4})-(\d{2})$")
_OFFSET = re.compile(r"^([+-])(\d{2}):(\d{2})$")


def resolve_tz(name: str | None) -> tzinfo:
    """Return the configured IANA timezone, or the system local zone."""
    if name:
        try:
            return ZoneInfo(name)
        except Exception as exc:  # noqa: BLE001 - zoneinfo raises several types
            raise WhoopError(
                f"Unknown timezone {name!r} in WHOOP_MCP_TZ; use an IANA name "
                "like 'America/New_York'."
            ) from exc
    local = datetime.now().astimezone().tzinfo
    return local if local is not None else timezone.utc


def _day_point(day: date, tz: tzinfo, end: bool) -> datetime:
    clock = time(23, 59, 59, 999000) if end else time(0, 0)
    return datetime.combine(day, clock, tzinfo=tz)


def _month_bounds(anchor: date) -> tuple[date, date]:
    first = anchor.replace(day=1)
    if first.month == 12:
        next_first = first.replace(year=first.year + 1, month=1)
    else:
        next_first = first.replace(month=first.month + 1)
    return first, next_first - timedelta(days=1)


def _week_monday(anchor: date) -> date:
    return anchor - timedelta(days=anchor.weekday())


def parse_point(
    expr: str, *, tz: tzinfo, end: bool = False, now: datetime | None = None
) -> datetime:
    """Parse a date expression into an aware datetime.

    ``end=True`` widens day/week/month granularity expressions to the end of
    that period (23:59:59 of the last day), so ``start="last week",
    end="last week"`` covers the whole previous week.
    """
    current = (now or datetime.now(tz)).astimezone(tz)
    today = current.date()
    text = expr.strip().lower()
    if not text:
        raise WhoopError(f"Empty date expression. Supported forms: {SUPPORTED_FORMS}")

    if text == "now":
        return current
    if text == "today":
        return _day_point(today, tz, end)
    if text == "yesterday":
        return _day_point(today - timedelta(days=1), tz, end)

    if match := _DAYS_AGO.match(text):
        return _day_point(today - timedelta(days=int(match.group(1))), tz, end)
    if match := _WEEKS_AGO.match(text):
        return _day_point(today - timedelta(weeks=int(match.group(1))), tz, end)
    if match := _LAST_N_DAYS.match(text):
        # "last 7 days" = a 7-day window that includes today.
        days = max(int(match.group(1)), 1)
        if end:
            return _day_point(today, tz, True)
        return _day_point(today - timedelta(days=days - 1), tz, False)

    if text == "this week":
        monday = _week_monday(today)
        return _day_point(monday + timedelta(days=6) if end else monday, tz, end)
    if text == "last week":
        monday = _week_monday(today) - timedelta(weeks=1)
        return _day_point(monday + timedelta(days=6) if end else monday, tz, end)
    if text == "this month":
        first, last = _month_bounds(today)
        return _day_point(last if end else first, tz, end)
    if text == "last month":
        first, last = _month_bounds(_month_bounds(today)[0] - timedelta(days=1))
        return _day_point(last if end else first, tz, end)

    if match := _MONTH.match(text):
        try:
            anchor = date(int(match.group(1)), int(match.group(2)), 1)
        except ValueError:
            raise WhoopError(
                f"Invalid month {expr!r}: the month must be 01-12."
            ) from None
        first, last = _month_bounds(anchor)
        return _day_point(last if end else first, tz, end)

    raw = expr.strip()
    # Date-only ISO forms get day granularity (so end=True means end of day).
    if len(raw) <= 10:
        try:
            return _day_point(date.fromisoformat(raw), tz, end)
        except ValueError:
            pass

    # Full ISO datetimes (fromisoformat handles offsets; map 'Z' too).
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise WhoopError(
            f"Could not parse date expression {expr!r}. Supported forms: {SUPPORTED_FORMS}"
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed


def parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp from the WHOOP API into an aware UTC datetime."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def to_api_iso(moment: datetime) -> str:
    """Format an aware datetime the way WHOOP expects: UTC with milliseconds + Z."""
    as_utc = moment.astimezone(timezone.utc)
    return as_utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{as_utc.microsecond // 1000:03d}Z"


def parse_record_offset(offset: str | None) -> timedelta:
    """Parse WHOOP's ``timezone_offset`` field ('-05:00', '+05:30', 'Z')."""
    if not offset or offset.upper() == "Z":
        return timedelta(0)
    match = _OFFSET.match(offset.strip())
    if not match:
        return timedelta(0)
    sign = 1 if match.group(1) == "+" else -1
    return sign * timedelta(hours=int(match.group(2)), minutes=int(match.group(3)))


def record_local_datetime(utc_iso: str, offset: str | None) -> datetime:
    """Shift a UTC record timestamp into the record's own local time."""
    delta = parse_record_offset(offset)
    local = parse_iso(utc_iso) + delta
    return local.replace(tzinfo=timezone(delta))


def record_local_date(utc_iso: str, offset: str | None) -> date:
    """Calendar day a record timestamp falls on, in the record's own timezone."""
    return record_local_datetime(utc_iso, offset).date()


def local_iso(utc_iso: str | None, offset: str | None) -> str | None:
    """Render a record timestamp as local ISO-8601 with its offset attached."""
    if not utc_iso:
        return None
    return record_local_datetime(utc_iso, offset).isoformat(timespec="minutes")


def day_bounds(day: date, tz: tzinfo) -> tuple[datetime, datetime]:
    """[start, end] datetimes covering one local calendar day."""
    start = datetime.combine(day, time(0, 0), tzinfo=tz)
    return start, start + timedelta(days=1) - timedelta(milliseconds=1)


def week_bounds_for(point: date) -> tuple[date, date]:
    """Monday..Sunday of the week containing ``point``."""
    monday = _week_monday(point)
    return monday, monday + timedelta(days=6)
