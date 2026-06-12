from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from whoop_mcp.errors import WhoopError
from whoop_mcp.timeutil import (
    day_bounds,
    parse_iso,
    parse_point,
    parse_record_offset,
    record_local_date,
    to_api_iso,
    week_bounds_for,
)

NY = ZoneInfo("America/New_York")
# A fixed "now": Friday 2026-06-12 10:30 in New York.
NOW = datetime(2026, 6, 12, 10, 30, tzinfo=NY)


def point(expr: str, *, end: bool = False) -> datetime:
    return parse_point(expr, tz=NY, end=end, now=NOW)


def test_today_and_yesterday():
    assert point("today").date() == date(2026, 6, 12)
    assert point("today").hour == 0
    assert point("today", end=True).hour == 23
    assert point("Yesterday").date() == date(2026, 6, 11)


def test_days_ago_and_last_n_days():
    assert point("7 days ago").date() == date(2026, 6, 5)
    assert point("2 weeks ago").date() == date(2026, 5, 29)
    # "last 7 days" includes today: starts 6 days back.
    assert point("last 7 days").date() == date(2026, 6, 6)
    assert point("last 7 days", end=True).date() == date(2026, 6, 12)


def test_week_and_month_expressions():
    assert point("this week").date() == date(2026, 6, 8)  # Monday
    assert point("last week").date() == date(2026, 6, 1)
    assert point("last week", end=True).date() == date(2026, 6, 7)  # Sunday
    assert point("last month").date() == date(2026, 5, 1)
    assert point("last month", end=True).date() == date(2026, 5, 31)
    assert point("2026-03").date() == date(2026, 3, 1)
    assert point("2026-03", end=True).date() == date(2026, 3, 31)


def test_iso_inputs():
    assert point("2026-06-01").date() == date(2026, 6, 1)
    assert point("2026-06-01", end=True).hour == 23
    parsed = point("2026-06-01T08:15:00Z")
    assert parsed == datetime(2026, 6, 1, 8, 15, tzinfo=timezone.utc)
    naive = point("2026-06-01T08:15:00")
    assert naive.tzinfo == NY


def test_invalid_expression_lists_supported_forms():
    with pytest.raises(WhoopError, match="Supported forms"):
        point("a fortnight hence")


def test_invalid_month_and_date_raise_whoop_errors():
    with pytest.raises(WhoopError, match="Invalid month"):
        point("2026-13")
    with pytest.raises(WhoopError, match="Supported forms"):
        point("2026-13-45")


def test_record_offset_parsing():
    assert parse_record_offset("+05:30") == timedelta(hours=5, minutes=30)
    assert parse_record_offset("-04:00") == timedelta(hours=-4)
    assert parse_record_offset("Z") == timedelta(0)
    assert parse_record_offset(None) == timedelta(0)


def test_record_local_date_crosses_midnight():
    # 20:00 UTC is already the next day in +05:30 (01:30).
    assert record_local_date("2026-06-10T20:00:00.000Z", "+05:30") == date(2026, 6, 11)
    # 02:25 UTC is still the previous evening in -05:00 (21:25).
    assert record_local_date("2022-04-24T02:25:44.774Z", "-05:00") == date(2022, 4, 23)


def test_api_iso_round_trip():
    moment = datetime(2026, 6, 1, 8, 15, 30, 123000, tzinfo=timezone.utc)
    assert to_api_iso(moment) == "2026-06-01T08:15:30.123Z"
    assert parse_iso(to_api_iso(moment)) == moment


def test_day_and_week_bounds():
    start, end = day_bounds(date(2026, 6, 12), NY)
    assert start.hour == 0 and end.hour == 23
    assert (end - start) < timedelta(days=1)
    monday, sunday = week_bounds_for(date(2026, 6, 12))
    assert monday == date(2026, 6, 8)
    assert sunday == date(2026, 6, 14)
