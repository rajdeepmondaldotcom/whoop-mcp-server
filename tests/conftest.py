"""Shared fixtures: realistic sample records and a fake WHOOP API.

The fake API is an httpx.MockTransport handler that mimics WHOOP v2's
behavior closely enough to exercise the client end-to-end: bearer-token
checks, start/end filtering, newest-first ordering, limit + nextToken
pagination, and by-id lookups with 404s.
"""

from __future__ import annotations

import json
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from whoop_mcp.client import WhoopClient
from whoop_mcp.timeutil import parse_iso


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ------------------------------------------------------------ record makers


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def make_cycle(
    cycle_id: int,
    start: datetime,
    end: datetime | None,
    *,
    offset: str = "+00:00",
    strain: float = 10.5,
    kilojoule: float = 8000.0,
    score_state: str = "SCORED",
) -> dict[str, Any]:
    return {
        "id": cycle_id,
        "user_id": 4242,
        "created_at": iso(start),
        "updated_at": iso(end or start),
        "start": iso(start),
        "end": iso(end) if end else None,
        "timezone_offset": offset,
        "score_state": score_state,
        "score": (
            {
                "strain": strain,
                "kilojoule": kilojoule,
                "average_heart_rate": 68,
                "max_heart_rate": 142,
            }
            if score_state == "SCORED"
            else None
        ),
    }


def make_recovery(
    cycle_id: int,
    sleep_id: str,
    *,
    created: datetime,
    score: float = 75.0,
    hrv: float = 65.0,
    rhr: float = 52.0,
    score_state: str = "SCORED",
    calibrating: bool = False,
) -> dict[str, Any]:
    return {
        "cycle_id": cycle_id,
        "sleep_id": sleep_id,
        "user_id": 4242,
        "created_at": iso(created),
        "updated_at": iso(created),
        "score_state": score_state,
        "score": (
            {
                "user_calibrating": calibrating,
                "recovery_score": score,
                "resting_heart_rate": rhr,
                "hrv_rmssd_milli": hrv,
                "spo2_percentage": 96.2,
                "skin_temp_celsius": 33.7,
            }
            if score_state == "SCORED"
            else None
        ),
    }


def make_sleep(
    sleep_id: str,
    start: datetime,
    end: datetime,
    *,
    cycle_id: int | None = None,
    offset: str = "+00:00",
    nap: bool = False,
    light_ms: int = 14_400_000,
    sws_ms: int = 6_300_000,
    rem_ms: int = 5_400_000,
    awake_ms: int = 1_500_000,
    performance: float = 85.0,
    efficiency: float = 91.0,
    consistency: float = 78.0,
    score_state: str = "SCORED",
) -> dict[str, Any]:
    in_bed = light_ms + sws_ms + rem_ms + awake_ms
    return {
        "id": sleep_id,
        "cycle_id": cycle_id,
        "v1_id": 999,
        "user_id": 4242,
        "created_at": iso(end),
        "updated_at": iso(end),
        "start": iso(start),
        "end": iso(end),
        "timezone_offset": offset,
        "nap": nap,
        "score_state": score_state,
        "score": (
            {
                "stage_summary": {
                    "total_in_bed_time_milli": in_bed,
                    "total_awake_time_milli": awake_ms,
                    "total_no_data_time_milli": 0,
                    "total_light_sleep_time_milli": light_ms,
                    "total_slow_wave_sleep_time_milli": sws_ms,
                    "total_rem_sleep_time_milli": rem_ms,
                    "sleep_cycle_count": 4,
                    "disturbance_count": 9,
                },
                "sleep_needed": {
                    "baseline_milli": 27_395_716,
                    "need_from_sleep_debt_milli": 352_230,
                    "need_from_recent_strain_milli": 208_595,
                    "need_from_recent_nap_milli": -12_312,
                },
                "respiratory_rate": 16.1,
                "sleep_performance_percentage": performance,
                "sleep_consistency_percentage": consistency,
                "sleep_efficiency_percentage": efficiency,
            }
            if score_state == "SCORED"
            else None
        ),
    }


def make_workout(
    workout_id: str,
    start: datetime,
    end: datetime,
    *,
    offset: str = "+00:00",
    sport: str = "running",
    strain: float = 12.3,
    kilojoule: float = 1569.34,
    distance_m: float | None = 5000.0,
    score_state: str = "SCORED",
) -> dict[str, Any]:
    return {
        "id": workout_id,
        "v1_id": 1043,
        "user_id": 4242,
        "created_at": iso(end),
        "updated_at": iso(end),
        "start": iso(start),
        "end": iso(end),
        "timezone_offset": offset,
        "sport_name": sport,
        "sport_id": 1,
        "score_state": score_state,
        "score": (
            {
                "strain": strain,
                "average_heart_rate": 142,
                "max_heart_rate": 171,
                "kilojoule": kilojoule,
                "percent_recorded": 100.0,
                "distance_meter": distance_m,
                "altitude_gain_meter": 46.6,
                "altitude_change_meter": -0.78,
                "zone_durations": {
                    "zone_zero_milli": 300_000,
                    "zone_one_milli": 600_000,
                    "zone_two_milli": 900_000,
                    "zone_three_milli": 900_000,
                    "zone_four_milli": 600_000,
                    "zone_five_milli": 300_000,
                },
            }
            if score_state == "SCORED"
            else None
        ),
    }


PROFILE = {
    "user_id": 4242,
    "email": "ada@example.com",
    "first_name": "Ada",
    "last_name": "Lovelace",
}
BODY = {"height_meter": 1.7, "weight_kilogram": 62.5, "max_heart_rate": 195}


# ------------------------------------------------------------- fake WHOOP API


class FakeWhoop:
    """In-memory WHOOP API v2 served through httpx.MockTransport."""

    def __init__(self) -> None:
        self.cycles: list[dict[str, Any]] = []
        self.recoveries: list[dict[str, Any]] = []
        self.sleeps: list[dict[str, Any]] = []
        self.workouts: list[dict[str, Any]] = []
        self.requests: list[str] = []
        self.expected_token = "test-token"

    # -- data loading

    def seed_days(self, days: int, *, today: date | None = None) -> None:
        """Generate `days` realistic days of history ending today (UTC dates)."""
        today = today or datetime.now(timezone.utc).date()
        for i in range(days):
            day = today - timedelta(days=days - 1 - i)
            wake = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).replace(hour=7)
            bed_prev = wake - timedelta(hours=7, minutes=45)
            cycle_id = 1000 + i
            sleep_id = f"5e000000-0000-0000-0000-{i:012d}"
            is_today = day == today
            self.cycles.append(
                make_cycle(
                    cycle_id,
                    wake,
                    None if is_today else wake + timedelta(days=1),
                    strain=8.0 + (i % 7),
                    kilojoule=7000 + 150 * (i % 5),
                )
            )
            self.sleeps.append(
                make_sleep(
                    sleep_id,
                    bed_prev,
                    wake,
                    cycle_id=cycle_id,
                    performance=80.0 + (i % 15),
                    light_ms=14_400_000 + (i % 4) * 600_000,
                )
            )
            self.recoveries.append(
                make_recovery(
                    cycle_id,
                    sleep_id,
                    created=wake + timedelta(minutes=20),
                    score=60.0 + (i * 7) % 35,
                    hrv=55.0 + (i * 3) % 25,
                    rhr=50.0 + (i % 6),
                )
            )
            if i % 2 == 0:
                self.workouts.append(
                    make_workout(
                        f"a0000000-0000-0000-0000-{i:012d}",
                        wake + timedelta(hours=9),
                        wake + timedelta(hours=9, minutes=50),
                        sport="running" if i % 4 == 0 else "cycling",
                        strain=10.0 + (i % 6),
                    )
                )

    # -- request handling

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(f"{request.method} {request.url.path}?{request.url.query.decode()}")
        if request.headers.get("Authorization") != f"Bearer {self.expected_token}":
            return httpx.Response(401, json={"error": "invalid_token"})

        path = request.url.path
        params = dict(urllib.parse.parse_qsl(request.url.query.decode()))

        if path == "/developer/v2/user/profile/basic":
            return httpx.Response(200, json=PROFILE)
        if path == "/developer/v2/user/measurement/body":
            return httpx.Response(200, json=BODY)
        if path == "/developer/v2/cycle":
            return self._collection(self.cycles, params, sort_key="start")
        if path == "/developer/v2/recovery":
            return self._collection(self.recoveries, params, sort_key="created_at")
        if path == "/developer/v2/activity/sleep":
            return self._collection(self.sleeps, params, sort_key="start")
        if path == "/developer/v2/activity/workout":
            return self._collection(self.workouts, params, sort_key="start")

        for prefix, records, key in (
            ("/developer/v2/activity/sleep/", self.sleeps, "id"),
            ("/developer/v2/activity/workout/", self.workouts, "id"),
        ):
            if path.startswith(prefix):
                ident = path[len(prefix) :]
                found = next((r for r in records if str(r[key]) == ident), None)
                return (
                    httpx.Response(200, json=found)
                    if found
                    else httpx.Response(404, json={"message": "not found"})
                )

        if path.startswith("/developer/v2/cycle/"):
            parts = path[len("/developer/v2/cycle/") :].split("/")
            cycle = next((c for c in self.cycles if str(c["id"]) == parts[0]), None)
            if cycle is None:
                return httpx.Response(404, json={"message": "not found"})
            if len(parts) == 1:
                return httpx.Response(200, json=cycle)
            if parts[1] == "recovery":
                rec = next((r for r in self.recoveries if r["cycle_id"] == cycle["id"]), None)
                return (
                    httpx.Response(200, json=rec)
                    if rec
                    else httpx.Response(404, json={"message": "no recovery"})
                )
            if parts[1] == "sleep":
                slp = next((s for s in self.sleeps if s.get("cycle_id") == cycle["id"]), None)
                return (
                    httpx.Response(200, json=slp)
                    if slp
                    else httpx.Response(404, json={"message": "no sleep"})
                )

        return httpx.Response(404, json={"message": f"unhandled path {path}"})

    def _collection(
        self, records: list[dict[str, Any]], params: dict[str, str], *, sort_key: str
    ) -> httpx.Response:
        items = sorted(records, key=lambda r: r[sort_key], reverse=True)
        if start := params.get("start"):
            cutoff = parse_iso(start)
            items = [r for r in items if parse_iso(r[sort_key]) >= cutoff]
        if end := params.get("end"):
            cutoff = parse_iso(end)
            items = [r for r in items if parse_iso(r[sort_key]) <= cutoff]
        limit = min(int(params.get("limit", 10)), 25)
        offset = int(params.get("nextToken", 0))
        page = items[offset : offset + limit]
        next_token = str(offset + limit) if offset + limit < len(items) else None
        return httpx.Response(200, json={"records": page, "next_token": next_token})


class StaticTokens:
    """TokenProvider stub; rotates the token on forced refresh."""

    def __init__(self, token: str = "test-token") -> None:
        self.token = token
        self.refreshes = 0

    async def get_access_token(self, *, force_refresh: bool = False) -> str:
        if force_refresh:
            self.refreshes += 1
            self.token = "test-token"
        return self.token


@pytest.fixture
def fake_whoop() -> FakeWhoop:
    return FakeWhoop()


@pytest.fixture
def whoop_client(fake_whoop: FakeWhoop) -> WhoopClient:
    return WhoopClient(
        StaticTokens(),
        transport=httpx.MockTransport(fake_whoop.handler),
        cache_ttl=300.0,
    )


def result_json(result: Any) -> dict[str, Any]:
    """Extract the structured payload from a CallToolResult."""
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads(result.content[0].text)
