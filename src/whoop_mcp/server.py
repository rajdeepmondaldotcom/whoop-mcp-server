"""The WHOOP MCP server.

Tool design notes:

* Every tool is read-only and annotated as such.
* Outputs are compact, transformed dicts (see :mod:`whoop_mcp.transform`)
  rather than raw API passthrough — friendlier for models and much cheaper
  in tokens. Returning dicts also gives MCP clients structured content.
* ``search`` and ``fetch`` implement the contract ChatGPT connectors
  require, mapping queries onto WHOOP records and day summaries.
* Date parameters accept human expressions ("today", "yesterday",
  "7 days ago", "last week", "2026-06-01", full ISO timestamps).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from whoop_mcp import oauth
from whoop_mcp.client import WhoopClient
from whoop_mcp.config import Settings, load_settings
from whoop_mcp.errors import ApiError, WhoopError
from whoop_mcp.summaries import (
    aggregate_period,
    build_daily_summary,
    build_weekly_report,
    compare_aggregates,
    cycle_date,
    fetch_bundle,
    recovery_day_points,
    recovery_trends,
    sleep_date,
    sleep_trends,
    strain_trends,
    workout_date,
)
from whoop_mcp.timeutil import day_bounds, parse_point, resolve_tz, week_bounds_for
from whoop_mcp.tokens import TokenManager, TokenStore
from whoop_mcp.transform import (
    duration_between,
    fmt_duration,
    ms_to_hours,
    prune,
    recovery_zone,
    rounded,
    transform_cycle,
    transform_profile,
    transform_recovery,
    transform_sleep,
    transform_workout,
)

logger = logging.getLogger(__name__)

DATE_FORMS = (
    "today | yesterday | N days ago | last N days | this/last week | "
    "this/last month | YYYY-MM-DD | ISO datetime"
)

INSTRUCTIONS = f"""Personal WHOOP health data: recovery, sleep, strain, and workouts.

Key concepts:
- Recovery (0-100%): how ready the body is today. green >=67, yellow 34-66, red <34.
- Strain (0-21, logarithmic): cardiovascular load. Day strain accumulates across a cycle.
- HRV (ms) and resting heart rate (bpm) drive recovery; higher HRV and lower RHR are better.
- A "cycle" is WHOOP's wake-to-wake physiological day. Recovery is scored after each sleep.

Usage:
- Start with get_daily_summary for "how am I doing" questions; it combines recovery,
  sleep, strain, and workouts for one day.
- Use the trends tools (get_recovery_trends / get_sleep_trends / get_strain_trends)
  for patterns over weeks, and compare_periods for before/after questions.
- Date parameters accept: {DATE_FORMS}.

If a tool reports that authorization is required, tell the user to run
`whoop-mcp auth` in a terminal and retry."""

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

MAX_TREND_DAYS = 180
MAX_COMPARE_DAYS = 120


@dataclass
class _AppState:
    settings: Settings | None = None
    client: WhoopClient | None = None
    tz: tzinfo | None = None


_state = _AppState()
_client_lock = asyncio.Lock()


def get_settings() -> Settings:
    if _state.settings is None:
        _state.settings = load_settings()
    return _state.settings


def get_tz() -> tzinfo:
    if _state.tz is None:
        _state.tz = resolve_tz(get_settings().timezone)
    return _state.tz


async def get_client() -> WhoopClient:
    if _state.client is None:
        async with _client_lock:
            if _state.client is None:
                settings = get_settings()
                manager = TokenManager(
                    TokenStore(settings.tokens_path),
                    lambda token: oauth.refresh_token(settings, token),
                    static_access_token=settings.static_access_token,
                )
                _state.client = WhoopClient(
                    manager,
                    timeout=settings.request_timeout,
                    cache_ttl=settings.cache_ttl,
                )
    return _state.client


def configure_for_testing(client: WhoopClient, tz: tzinfo | None = None) -> None:
    """Inject a client (and optionally a timezone) — used by the test suite."""
    _state.client = client
    if tz is not None:
        _state.tz = tz


async def reset_state() -> None:
    if _state.client is not None:
        await _state.client.aclose()
    _state.settings = None
    _state.client = None
    _state.tz = None


@asynccontextmanager
async def _lifespan(_server: FastMCP):
    try:
        yield None
    finally:
        if _state.client is not None:
            await _state.client.aclose()
            _state.client = None


mcp = FastMCP(
    "whoop",
    instructions=INSTRUCTIONS,
    lifespan=_lifespan,
)


# ----------------------------------------------------------------- helpers


def _today() -> date:
    return datetime.now(get_tz()).date()


def _parse_window(
    start: str | None, end: str | None, *, default_days: int
) -> tuple[datetime | None, datetime | None]:
    tz = get_tz()
    start_dt = parse_point(start, tz=tz) if start else None
    end_dt = parse_point(end, tz=tz, end=True) if end else None
    if start_dt is None and end_dt is None and default_days:
        start_dt = day_bounds(_today() - timedelta(days=default_days - 1), tz)[0]
    if start_dt is not None and end_dt is not None and end_dt < start_dt:
        raise WhoopError(f"end ({end}) is before start ({start}).")
    return start_dt, end_dt


async def _daily(day: date) -> dict[str, Any]:
    tz = get_tz()
    start_dt, end_dt = day_bounds(day, tz)
    bundle = await fetch_bundle(
        await get_client(),
        start_dt - timedelta(days=1),
        end_dt + timedelta(days=1),
        max_records=60,
    )
    return build_daily_summary(bundle, day, today=_today())


async def _trend_bundle(days: int):
    days = max(7, min(days, MAX_TREND_DAYS))
    tz = get_tz()
    today = _today()
    start_day = today - timedelta(days=days - 1)
    query_start = day_bounds(start_day, tz)[0] - timedelta(days=1)
    query_end = day_bounds(today, tz)[1] + timedelta(days=1)
    bundle = await fetch_bundle(
        await get_client(),
        query_start,
        query_end,
        max_records=min(days * 4 + 20, 500),
    )
    return bundle, start_day, today


def _collection_result(
    records: list[dict[str, Any]], truncated: bool, note: str | None = None
) -> dict[str, Any]:
    result: dict[str, Any] = {"count": len(records), "records": records}
    if truncated:
        result["truncated"] = True
        result["note"] = (
            "More records exist than the limit allowed; narrow the date range or "
            "raise `limit`."
        )
    if note:
        result["note"] = f"{result.get('note', '')} {note}".strip()
    return result


def _clamp_limit(limit: int) -> int:
    return max(1, min(int(limit), 500))


# ------------------------------------------------------------------- tools


@mcp.tool(
    title="WHOOP profile",
    annotations=READ_ONLY,
)
async def get_profile() -> dict[str, Any]:
    """Get the user's WHOOP profile and body measurements (name, email, height,
    weight, max heart rate)."""
    client = await get_client()
    profile, body = await asyncio.gather(client.profile(), client.body_measurement())
    return transform_profile(profile or {}, body or {})


@mcp.tool(
    title="Daily summary",
    annotations=READ_ONLY,
)
async def get_daily_summary(day: str = "today") -> dict[str, Any]:
    """One day of WHOOP data in a single call: recovery score, sleep, day strain,
    and workouts, with a one-line summary. The best first tool for "how am I
    doing" or "how did I sleep" questions.

    `day` accepts: today | yesterday | N days ago | YYYY-MM-DD.
    """
    target = parse_point(day, tz=get_tz()).date()
    return await _daily(target)


@mcp.tool(
    title="Weekly report",
    annotations=READ_ONLY,
)
async def get_weekly_report(week_of: str = "this week") -> dict[str, Any]:
    """Monday-to-Sunday report: a per-day grid of recovery, sleep, and strain,
    plus weekly averages and workout totals.

    `week_of` accepts: this week | last week | YYYY-MM-DD (any day in the
    target week).
    """
    tz = get_tz()
    anchor = parse_point(week_of, tz=tz).date()
    monday, sunday = week_bounds_for(anchor)
    query_start = day_bounds(monday, tz)[0] - timedelta(days=1)
    query_end = day_bounds(sunday, tz)[1] + timedelta(days=1)
    bundle = await fetch_bundle(await get_client(), query_start, query_end, max_records=150)
    return build_weekly_report(bundle, monday, sunday, today=_today())


@mcp.tool(
    title="List recoveries",
    annotations=READ_ONLY,
)
async def get_recoveries(
    start: str | None = None, end: str | None = None, limit: int = 10
) -> dict[str, Any]:
    """List recovery records (recovery %, HRV, resting heart rate, SpO2, skin
    temperature), newest first.

    Defaults to the last 14 days when no range is given. `start`/`end` accept:
    today | yesterday | N days ago | last week | YYYY-MM-DD | ISO datetime.
    """
    start_dt, end_dt = _parse_window(start, end, default_days=14)
    client = await get_client()
    records, truncated = await client.recoveries(start_dt, end_dt, _clamp_limit(limit))
    cycles, _ = await client.cycles(start_dt, end_dt, _clamp_limit(limit) * 2)
    cycle_days = {c.get("id"): cycle_date(c) for c in cycles}
    transformed = [
        transform_recovery(
            r, date=str(d) if (d := cycle_days.get(r.get("cycle_id"))) else None
        )
        for r in records
    ]
    return _collection_result(transformed, truncated)


@mcp.tool(
    title="List sleeps",
    annotations=READ_ONLY,
)
async def get_sleeps(
    start: str | None = None,
    end: str | None = None,
    include_naps: bool = True,
    limit: int = 10,
) -> dict[str, Any]:
    """List sleep records (duration, stages, efficiency, performance, sleep debt),
    newest first.

    Defaults to the last 14 days when no range is given. `start`/`end` accept:
    today | yesterday | N days ago | last week | YYYY-MM-DD | ISO datetime.
    """
    start_dt, end_dt = _parse_window(start, end, default_days=14)
    client = await get_client()
    limit = _clamp_limit(limit)
    fetch_max = _clamp_limit(limit if include_naps else limit * 3)
    records, truncated = await client.sleeps(start_dt, end_dt, fetch_max)
    note = None
    if not include_naps:
        records = [r for r in records if not r.get("nap")]
        if truncated and len(records) < limit:
            note = (
                "The nap filter scanned only the newest records and may have missed "
                "older main sleeps — narrow the date range for complete results."
            )
    records = records[:limit]
    return _collection_result([transform_sleep(r) for r in records], truncated, note)


@mcp.tool(
    title="Sleep by id",
    annotations=READ_ONLY,
)
async def get_sleep(sleep_id: str) -> dict[str, Any]:
    """Get one sleep record by its UUID (from get_sleeps or a recovery's sleep_id)."""
    client = await get_client()
    return transform_sleep(await client.sleep(sleep_id))


@mcp.tool(
    title="List workouts",
    annotations=READ_ONLY,
)
async def get_workouts(
    start: str | None = None,
    end: str | None = None,
    sport: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """List workouts (sport, strain, calories, heart rate, distance, HR zones),
    newest first. Optionally filter by sport name (substring match, e.g.
    "run").

    Defaults to the last 30 days when no range is given. `start`/`end` accept:
    today | yesterday | N days ago | last week | YYYY-MM-DD | ISO datetime.
    """
    start_dt, end_dt = _parse_window(start, end, default_days=30)
    client = await get_client()
    limit = _clamp_limit(limit)
    fetch_max = _clamp_limit(limit * 4 if sport else limit)
    records, truncated = await client.workouts(start_dt, end_dt, fetch_max)
    note = None
    if sport:
        needle = sport.strip().lower()
        records = [r for r in records if needle in str(r.get("sport_name", "")).lower()]
        note = f"Filtered to sports matching {sport!r}."
        if truncated and len(records) < limit:
            note += (
                " The filter scanned only the newest records and may have missed older "
                "matches — narrow the date range for complete results."
            )
    records = records[:limit]
    return _collection_result([transform_workout(r) for r in records], truncated, note)


@mcp.tool(
    title="Workout by id",
    annotations=READ_ONLY,
)
async def get_workout(workout_id: str) -> dict[str, Any]:
    """Get one workout by its UUID (from get_workouts)."""
    client = await get_client()
    return transform_workout(await client.workout(workout_id))


@mcp.tool(
    title="List cycles",
    annotations=READ_ONLY,
)
async def get_cycles(
    start: str | None = None, end: str | None = None, limit: int = 10
) -> dict[str, Any]:
    """List physiological cycles (WHOOP's wake-to-wake "days"): day strain,
    calories, heart rate. Newest first; the newest cycle is usually still in
    progress.

    Defaults to the last 14 days when no range is given. `start`/`end` accept:
    today | yesterday | N days ago | last week | YYYY-MM-DD | ISO datetime.
    """
    start_dt, end_dt = _parse_window(start, end, default_days=14)
    client = await get_client()
    records, truncated = await client.cycles(start_dt, end_dt, _clamp_limit(limit))
    return _collection_result([transform_cycle(r) for r in records], truncated)


@mcp.tool(
    title="Cycle by id",
    annotations=READ_ONLY,
)
async def get_cycle(
    cycle_id: int, include_recovery: bool = True, include_sleep: bool = True
) -> dict[str, Any]:
    """Get one cycle by id, optionally with the recovery and sleep that belong
    to it (WHOOP links each recovery and primary sleep to a cycle)."""
    client = await get_client()

    async def _optional(coro) -> dict[str, Any] | None:
        try:
            return await coro
        except ApiError as exc:
            if exc.status_code == 404:
                return None
            raise

    cycle_raw, recovery_raw, sleep_raw = await asyncio.gather(
        client.cycle(cycle_id),
        _optional(client.cycle_recovery(cycle_id)) if include_recovery else _noop(),
        _optional(client.cycle_sleep(cycle_id)) if include_sleep else _noop(),
    )
    result = {"cycle": transform_cycle(cycle_raw)}
    if include_recovery:
        result["recovery"] = transform_recovery(recovery_raw) if recovery_raw else None
    if include_sleep:
        result["sleep"] = transform_sleep(sleep_raw) if sleep_raw else None
    return prune(result) or {"cycle": None}


async def _noop() -> None:
    return None


@mcp.tool(
    title="Recovery trends",
    annotations=READ_ONLY,
)
async def get_recovery_trends(days: int = 30) -> dict[str, Any]:
    """Recovery trends over a window (7-180 days): statistics, trend direction,
    and unusual days for recovery %, HRV, and resting heart rate, plus a daily
    table. Trend directions account for metric polarity (rising HRV is good,
    rising resting heart rate is not)."""
    bundle, start_day, today = await _trend_bundle(days)
    return recovery_trends(bundle, start_day, today)


@mcp.tool(
    title="Sleep trends",
    annotations=READ_ONLY,
)
async def get_sleep_trends(days: int = 30) -> dict[str, Any]:
    """Sleep trends over a window (7-180 days): hours slept, performance,
    efficiency, consistency, and sleep debt — statistics, trend directions,
    unusual nights, and a nightly table. Naps are counted but excluded from
    nightly averages."""
    bundle, start_day, today = await _trend_bundle(days)
    return sleep_trends(bundle, start_day, today)


@mcp.tool(
    title="Strain & training load",
    annotations=READ_ONLY,
)
async def get_strain_trends(days: int = 30) -> dict[str, Any]:
    """Strain and training-load trends over a window (7-180 days): daily strain
    statistics, calories, workout totals by sport, and the acute:chronic load
    ratio (7-day vs 28-day average strain) when enough data exists."""
    bundle, start_day, today = await _trend_bundle(days)
    return strain_trends(bundle, start_day, today, today=today)


@mcp.tool(
    title="Compare periods",
    annotations=READ_ONLY,
)
async def compare_periods(
    period_a_start: str,
    period_a_end: str,
    period_b_start: str,
    period_b_end: str,
) -> dict[str, Any]:
    """Compare two date ranges across recovery, HRV, resting heart rate, sleep,
    strain, calories, and workout count — with per-metric change, percent
    change, and an improved/declined/unchanged assessment. Period A is the
    baseline; period B is compared against it.

    All four arguments accept: today | yesterday | N days ago | last week |
    this month | YYYY-MM-DD | ISO datetime. Example: compare "last month" to
    "this month" by using the same expression for a period's start and end.
    """
    tz = get_tz()
    a_start = parse_point(period_a_start, tz=tz).date()
    a_end = parse_point(period_a_end, tz=tz, end=True).date()
    b_start = parse_point(period_b_start, tz=tz).date()
    b_end = parse_point(period_b_end, tz=tz, end=True).date()
    for label, (p_start, p_end) in {"A": (a_start, a_end), "B": (b_start, b_end)}.items():
        if p_end < p_start:
            raise WhoopError(f"Period {label} ends before it starts.")
        if (p_end - p_start).days > MAX_COMPARE_DAYS:
            raise WhoopError(f"Period {label} is longer than {MAX_COMPARE_DAYS} days.")

    client = await get_client()
    tz_bounds = lambda d, end: day_bounds(d, tz)[1 if end else 0]  # noqa: E731
    bundle_a, bundle_b = await asyncio.gather(
        fetch_bundle(
            client,
            tz_bounds(a_start, False) - timedelta(days=1),
            tz_bounds(a_end, True) + timedelta(days=1),
            max_records=500,
        ),
        fetch_bundle(
            client,
            tz_bounds(b_start, False) - timedelta(days=1),
            tz_bounds(b_end, True) + timedelta(days=1),
            max_records=500,
        ),
    )
    agg_a = aggregate_period(bundle_a, a_start, a_end)
    agg_b = aggregate_period(bundle_b, b_start, b_end)
    result = {
        "period_a": agg_a,
        "period_b": agg_b,
        "comparison": compare_aggregates(agg_a, agg_b),
    }
    truncated = sorted({*bundle_a.truncated, *bundle_b.truncated})
    if truncated:
        result["notes"] = [
            f"Some collections hit the record cap and were truncated: {', '.join(truncated)}; "
            "averages may be based on partial data."
        ]
    return result


# -------------------------------------------- ChatGPT connector compatibility


_TYPE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "sleep": ("sleep", "slept", "nap", "bed", "insomnia", "rem", "snore"),
    "recovery": (
        "recovery",
        "recover",
        "hrv",
        "readiness",
        "resting heart",
        "rhr",
        "spo2",
        "heart rate variability",
    ),
    "workout": (
        "workout",
        "run",
        "ride",
        "bike",
        "cycling",
        "swim",
        "training",
        "exercise",
        "gym",
        "sport",
        "activity",
        "yoga",
        "walk",
        "hike",
        "lift",
        "strength",
        "tennis",
        "golf",
        "soccer",
        "basketball",
        "row",
        "climb",
    ),
    "strain": ("strain", "load", "calorie", "effort"),
}

_LAST_N = re.compile(r"(?:last|past)\s+(\d{1,3})\s+(day|week|month)s?")
_ISO_DAY = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


def _search_window(query: str) -> tuple[date, date]:
    tz = get_tz()
    today = _today()
    if match := _ISO_DAY.search(query):
        try:
            day = date.fromisoformat(match.group(1))
        except ValueError:
            pass  # looked like a date but isn't one; try the other patterns
        else:
            return day, day
    if match := _LAST_N.search(query):
        count = max(int(match.group(1)), 1)
        unit_days = {"day": 1, "week": 7, "month": 30}[match.group(2)]
        span = min(count * unit_days, 90)
        return today - timedelta(days=span - 1), today
    for phrase in ("last week", "this week", "last month", "this month"):
        if phrase in query:
            start = parse_point(phrase, tz=tz).date()
            end = parse_point(phrase, tz=tz, end=True).date()
            return start, min(end, today)
    if "yesterday" in query:
        day = today - timedelta(days=1)
        return day, day
    if "today" in query:
        return today, today
    return today - timedelta(days=13), today


@mcp.tool(
    title="Search WHOOP data",
    annotations=READ_ONLY,
)
async def search(query: str) -> dict[str, Any]:
    """Search WHOOP data with a natural-language query (e.g. "sleep last week",
    "runs this month", "recovery yesterday"). Returns matching day summaries,
    workouts, and sleeps as documents whose ids can be passed to `fetch`."""
    text = query.lower().strip()
    types = {t for t, keywords in _TYPE_KEYWORDS.items() if any(k in text for k in keywords)}
    start_day, end_day = _search_window(text)

    tz = get_tz()
    bundle = await fetch_bundle(
        await get_client(),
        day_bounds(start_day, tz)[0] - timedelta(days=1),
        day_bounds(end_day, tz)[1] + timedelta(days=1),
        max_records=min(((end_day - start_day).days + 1) * 4 + 20, 400),
    )

    results: list[dict[str, str]] = []

    if not types or types & {"recovery", "strain", "sleep"}:
        recovery_by_day = {
            d: r for d, r in recovery_day_points(bundle.recoveries, bundle.cycles)
        }
        cycles_by_day = {cycle_date(c): c for c in bundle.cycles}
        sleep_by_day: dict[Any, dict[str, Any]] = {}
        for s in bundle.sleeps:
            if not s.get("nap"):
                sleep_by_day.setdefault(sleep_date(s), s)
        cursor = end_day
        while cursor >= start_day and len(results) < 20:
            pieces = []
            recovery = recovery_by_day.get(cursor)
            score = ((recovery or {}).get("score") or {}).get("recovery_score")
            if score is not None:
                pieces.append(f"Recovery {round(score)}% ({recovery_zone(score)})")
            sleep = sleep_by_day.get(cursor)
            if sleep is not None:
                hours = ms_to_hours(
                    sum(
                        ((sleep.get("score") or {}).get("stage_summary") or {}).get(k) or 0
                        for k in (
                            "total_light_sleep_time_milli",
                            "total_slow_wave_sleep_time_milli",
                            "total_rem_sleep_time_milli",
                        )
                    )
                )
                if hours:
                    pieces.append(f"Sleep {hours}h")
            strain = ((cycles_by_day.get(cursor) or {}).get("score") or {}).get("strain")
            if strain is not None:
                pieces.append(f"Strain {round(strain, 1)}")
            if pieces:
                results.append(
                    {
                        "id": f"day:{cursor.isoformat()}",
                        "title": f"{cursor.strftime('%a %b %d %Y')} — {' · '.join(pieces)}",
                        "url": f"https://app.whoop.com/#whoop-mcp/day/{cursor.isoformat()}",
                    }
                )
            cursor -= timedelta(days=1)

    if not types or "workout" in types:
        for workout in bundle.workouts[:10]:
            day = workout_date(workout)
            if day is None or not (start_day <= day <= end_day):
                continue
            sport = workout.get("sport_name") or "Workout"
            duration = fmt_duration(duration_between(workout.get("start"), workout.get("end")))
            strain = ((workout.get("score") or {}).get("strain"))
            bits = [b for b in (duration, f"strain {rounded(strain)}" if strain else None) if b]
            results.append(
                {
                    "id": f"workout:{workout.get('id')}",
                    "title": f"{sport.title()} — {day.strftime('%a %b %d %Y')}"
                    + (f" — {' · '.join(bits)}" if bits else ""),
                    "url": f"https://app.whoop.com/#whoop-mcp/workout/{workout.get('id')}",
                }
            )

    if "sleep" in types:
        for sleep in bundle.sleeps[:10]:
            day = sleep_date(sleep)
            if day is None or not (start_day <= day <= end_day):
                continue
            kind = "Nap" if sleep.get("nap") else "Sleep"
            duration = fmt_duration(duration_between(sleep.get("start"), sleep.get("end")))
            results.append(
                {
                    "id": f"sleep:{sleep.get('id')}",
                    "title": f"{kind} — {day.strftime('%a %b %d %Y')}"
                    + (f" — {duration} in bed" if duration else ""),
                    "url": f"https://app.whoop.com/#whoop-mcp/sleep/{sleep.get('id')}",
                }
            )

    return {"results": results[:30]}


@mcp.tool(
    title="Fetch WHOOP document",
    annotations=READ_ONLY,
)
async def fetch(id: str) -> dict[str, Any]:
    """Fetch the full document for an id returned by `search`. Supported id
    forms: day:YYYY-MM-DD, sleep:<uuid>, workout:<uuid>, cycle:<int>,
    recovery:<cycle_int>, profile."""
    kind, _, rest = id.strip().partition(":")
    client = await get_client()
    url = f"https://app.whoop.com/#whoop-mcp/{kind}/{rest}" if rest else "https://app.whoop.com"

    def bad_id(reason: str) -> WhoopError:
        return WhoopError(
            f"Invalid document id {id!r} ({reason}). Expected day:YYYY-MM-DD, "
            "sleep:<uuid>, workout:<uuid>, cycle:<int>, recovery:<cycle_int>, or profile."
        )

    if kind == "profile":
        profile, body = await asyncio.gather(client.profile(), client.body_measurement())
        document: dict[str, Any] = transform_profile(profile or {}, body or {})
        title = "WHOOP profile"
        metadata = {"type": "profile"}
    elif kind == "day":
        try:
            day = date.fromisoformat(rest)
        except ValueError:
            raise bad_id("the date must be YYYY-MM-DD") from None
        document = await _daily(day)
        title = f"WHOOP day summary — {day.strftime('%a %b %d %Y')}"
        metadata = {"type": "day_summary", "date": rest}
    elif kind == "sleep":
        document = transform_sleep(await client.sleep(rest))
        title = f"Sleep — {document.get('date', rest)}"
        metadata = {"type": "sleep", "date": str(document.get("date", ""))}
    elif kind == "workout":
        document = transform_workout(await client.workout(rest))
        sport = str(document.get("sport", "workout")).title()
        title = f"{sport} — {document.get('date', rest)}"
        metadata = {"type": "workout", "date": str(document.get("date", ""))}
    elif kind in ("cycle", "recovery"):
        try:
            cycle_id = int(rest)
        except ValueError:
            raise bad_id("the cycle id must be an integer") from None
        if kind == "cycle":
            document = transform_cycle(await client.cycle(cycle_id))
            title = f"Cycle — {document.get('date', rest)}"
            metadata = {"type": "cycle", "date": str(document.get("date", ""))}
        else:
            document = transform_recovery(await client.cycle_recovery(cycle_id))
            title = f"Recovery — cycle {rest}"
            metadata = {"type": "recovery", "cycle_id": rest}
    else:
        raise bad_id("unknown document type")

    return {
        "id": id,
        "title": title,
        "text": json.dumps(document, indent=2),
        "url": url,
        "metadata": metadata,
    }


# --------------------------------------------------------------- resources


@mcp.resource("whoop://profile", mime_type="application/json")
async def resource_profile() -> str:
    """The user's WHOOP profile and body measurements."""
    client = await get_client()
    profile, body = await asyncio.gather(client.profile(), client.body_measurement())
    return json.dumps(transform_profile(profile or {}, body or {}), indent=2)


@mcp.resource("whoop://summary/today", mime_type="application/json")
async def resource_today() -> str:
    """Today's combined recovery / sleep / strain / workout summary."""
    return json.dumps(await _daily(_today()), indent=2)


@mcp.resource("whoop://recovery/latest", mime_type="application/json")
async def resource_latest_recovery() -> str:
    """The most recent recovery score."""
    client = await get_client()
    records, _ = await client.recoveries(max_records=1)
    if not records:
        return json.dumps({"note": "No recovery records found."})
    return json.dumps(transform_recovery(records[0]), indent=2)


@mcp.resource("whoop://sleep/latest", mime_type="application/json")
async def resource_latest_sleep() -> str:
    """The most recent sleep record."""
    client = await get_client()
    records, _ = await client.sleeps(max_records=1)
    if not records:
        return json.dumps({"note": "No sleep records found."})
    return json.dumps(transform_sleep(records[0]), indent=2)


# ----------------------------------------------------------------- prompts


@mcp.prompt(title="Morning readiness check")
def morning_readiness() -> str:
    """Assess today's recovery and plan the day around it."""
    return (
        "Check my WHOOP data for this morning. Call get_daily_summary for today, "
        "then: 1) interpret my recovery score, HRV, and resting heart rate against "
        "my recent baseline (get_recovery_trends with days=14 if useful), 2) assess "
        "last night's sleep quality and any sleep debt, and 3) recommend how hard I "
        "should push today — training intensity, and one concrete thing to do for "
        "recovery. Be direct and specific, not generic."
    )


@mcp.prompt(title="Weekly review")
def weekly_review() -> str:
    """Review the last week of recovery, sleep, strain, and training."""
    return (
        "Give me a WHOOP weekly review. Call get_weekly_report for last week and "
        "compare_periods between the week before last and last week. Summarize: "
        "wins, concerns, the strongest pattern you see (e.g. which behaviors "
        "preceded my best recovery days), and 2-3 specific experiments for next "
        "week. Use the daily grid to point at concrete days."
    )


@mcp.prompt(title="Sleep coaching")
def sleep_coach(days: str = "14") -> str:
    """Analyze recent sleep and get specific recommendations."""
    return (
        f"Act as my sleep coach. Call get_sleep_trends with days={days} and "
        "get_daily_summary for today. Analyze duration vs my sleep need, "
        "efficiency, consistency (bed/wake time regularity), and debt. Identify "
        "my worst nights and what they had in common, then give me a prioritized, "
        "specific action list — times, not platitudes."
    )


@mcp.prompt(title="Training load check")
def training_planner(days: str = "30") -> str:
    """Evaluate training load and plan the next block."""
    return (
        f"Review my training load. Call get_strain_trends with days={days} and "
        "get_recovery_trends with the same window. Evaluate my acute:chronic "
        "ratio, how my recovery responds to high-strain days (look for lag "
        "patterns in the daily tables), and whether my load is trending "
        "sustainably. Then propose next week's training structure day by day."
    )


def run(transport: str = "stdio", host: str | None = None, port: int | None = None) -> None:
    """Entry point used by the CLI."""
    if host:
        mcp.settings.host = host
    if port:
        mcp.settings.port = port
    mcp.run(transport=transport)  # type: ignore[arg-type]
