"""Demo mode: a realistic, fully offline WHOOP account.

``whoop-mcp serve --demo`` (or ``WHOOP_MCP_DEMO=1``) serves 150 days of
generated data through the exact same client/transform/analytics pipeline as
real data - no WHOOP account, no developer app, no OAuth. It exists so
anyone can try the server in 30 seconds before committing to setup.

The dataset is deterministic for a given calendar day and deliberately
patterned so the analysis tools have something true to find:

* a 4-week recovery cycle plus weekly training periodization
  (hard Tue/Thu/Sat, rest Sun),
* big-strain days are followed by a recovery dip the next morning
  (so `get_correlations` finds a real negative correlation),
* weekends sleep later and longer (visible in consistency),
* an occasional terrible night and an occasional nap,
* a one-week trip in a different timezone (exercises day bucketing),
* the first days are marked "calibrating", today's cycle is in progress.
"""

from __future__ import annotations

import math
import random
import urllib.parse
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import httpx

from whoop_mcp.timeutil import parse_iso

HOME_OFFSET = "-04:00"
TRIP_OFFSET = "+02:00"
TRIP_DAYS_FROM_TODAY = range(35, 42)  # a week abroad, ~5 weeks ago

DEMO_PROFILE = {
    "user_id": 1000001,
    "email": "demo@whoop-mcp.example",
    "first_name": "Demo",
    "last_name": "Athlete",
}
DEMO_BODY = {"height_meter": 1.78, "weight_kilogram": 74.5, "max_heart_rate": 192}

_SPORT_ROTATION = ("running", "weightlifting", "cycling", "tennis")


def _offset_delta(offset: str) -> timedelta:
    sign = 1 if offset.startswith("+") else -1
    hours, minutes = offset[1:].split(":")
    return sign * timedelta(hours=int(hours), minutes=int(minutes))


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _sleep_uuid(index: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"whoop-mcp-demo-sleep-{index}"))


def _workout_uuid(index: int, slot: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"whoop-mcp-demo-workout-{index}-{slot}"))


def build_demo_dataset(days: int = 150, today: date | None = None) -> dict[str, Any]:
    """Generate the full demo account. Deterministic for a given `today`."""
    today = today or datetime.now(timezone.utc).astimezone().date()
    rng = random.Random(20260612)

    cycles: list[dict[str, Any]] = []
    recoveries: list[dict[str, Any]] = []
    sleeps: list[dict[str, Any]] = []
    workouts: list[dict[str, Any]] = []
    sleep_index: dict[str, int] = {}

    previous_strain = 8.0
    for i in range(days):
        day = today - timedelta(days=days - 1 - i)
        days_from_today = (today - day).days
        offset = TRIP_OFFSET if days_from_today in TRIP_DAYS_FROM_TODAY else HOME_OFFSET
        tz = timezone(_offset_delta(offset))
        weekday = day.weekday()
        is_weekend = weekday >= 5
        is_today = day == today
        bad_night = i % 17 == 5
        calibrating = i < 3

        # --- sleep (the night ending this morning) -----------------------
        wake_clock = time(7, 35) if is_weekend else time(6, 40)
        wake = datetime.combine(day, wake_clock, tzinfo=tz) + timedelta(
            minutes=rng.randint(-15, 20)
        )
        sleep_hours = 4.6 if bad_night else rng.uniform(6.7, 8.2) + (0.5 if is_weekend else 0)
        bedtime = wake - timedelta(hours=sleep_hours / 0.93)  # in-bed > asleep
        asleep_ms = int(sleep_hours * 3_600_000)
        in_bed_ms = int(asleep_ms / 0.93)
        light_ms = int(asleep_ms * 0.55)
        deep_ms = int(asleep_ms * 0.22)
        rem_ms = asleep_ms - light_ms - deep_ms
        performance = 38.0 if bad_night else min(96.0, 52 + sleep_hours * 5 + rng.uniform(-4, 4))
        consistency = max(35.0, 82 - (12 if is_weekend else 0) + rng.uniform(-6, 6))
        efficiency = 93.0 + rng.uniform(-3, 3)

        sleep_id = _sleep_uuid(i)
        sleep_index[sleep_id] = i
        cycle_id = 10_000 + i
        sleeps.append(
            {
                "id": sleep_id,
                "cycle_id": cycle_id,
                "v1_id": 90_000 + i,
                "user_id": DEMO_PROFILE["user_id"],
                "created_at": _iso(wake),
                "updated_at": _iso(wake),
                "start": _iso(bedtime),
                "end": _iso(wake),
                "timezone_offset": offset,
                "nap": False,
                "score_state": "SCORED",
                "score": {
                    "stage_summary": {
                        "total_in_bed_time_milli": in_bed_ms,
                        "total_awake_time_milli": in_bed_ms - asleep_ms,
                        "total_no_data_time_milli": 0,
                        "total_light_sleep_time_milli": light_ms,
                        "total_slow_wave_sleep_time_milli": deep_ms,
                        "total_rem_sleep_time_milli": rem_ms,
                        "sleep_cycle_count": max(2, int(sleep_hours // 1.6)),
                        "disturbance_count": rng.randint(4, 14) + (9 if bad_night else 0),
                    },
                    "sleep_needed": {
                        "baseline_milli": 27_600_000,
                        "need_from_sleep_debt_milli": 900_000 if bad_night else 300_000,
                        "need_from_recent_strain_milli": int(previous_strain * 30_000),
                        "need_from_recent_nap_milli": 0,
                    },
                    "respiratory_rate": round(15.2 + rng.uniform(-0.7, 0.7), 1),
                    "sleep_performance_percentage": round(performance, 1),
                    "sleep_consistency_percentage": round(consistency, 1),
                    "sleep_efficiency_percentage": round(efficiency, 1),
                },
            }
        )

        # --- recovery (scored on waking) ----------------------------------
        recovery = 70 + 9 * math.sin(2 * math.pi * i / 28) + rng.uniform(-7, 7)
        if previous_strain > 14:
            recovery -= 18  # the pattern get_correlations should find
        if bad_night:
            recovery = rng.uniform(22, 33)
        recovery = max(2.0, min(98.0, recovery))
        hrv = max(28.0, 33 + recovery * 0.55 + rng.uniform(-5, 5))
        rhr = max(42.0, 64 - recovery * 0.13 + rng.uniform(-2, 2))
        recoveries.append(
            {
                "cycle_id": cycle_id,
                "sleep_id": sleep_id,
                "user_id": DEMO_PROFILE["user_id"],
                "created_at": _iso(wake + timedelta(minutes=18)),
                "updated_at": _iso(wake + timedelta(minutes=18)),
                "score_state": "SCORED",
                "score": {
                    "user_calibrating": calibrating,
                    "recovery_score": round(recovery, 1),
                    "resting_heart_rate": round(rhr, 1),
                    "hrv_rmssd_milli": round(hrv, 2),
                    "spo2_percentage": round(rng.uniform(94.5, 98.5), 1),
                    "skin_temp_celsius": round(33.4 + rng.uniform(-0.5, 0.5), 1),
                },
            }
        )

        # --- workouts + day strain ----------------------------------------
        strain_by_weekday = {0: 9.0, 1: 14.6, 2: 8.2, 3: 15.4, 4: 10.1, 5: 16.3, 6: 5.4}
        day_strain = max(1.0, strain_by_weekday[weekday] + rng.uniform(-1.4, 1.4))
        day_workouts: list[tuple[str, float, float, float | None]] = []
        if weekday in (1, 3):  # Tue / Thu
            sport = _SPORT_ROTATION[(i // 2) % 2]  # running / weightlifting
            distance = 8_000.0 if sport == "running" else None
            day_workouts.append((sport, 50 + rng.uniform(-8, 10), day_strain * 0.72, distance))
        elif weekday == 5:  # Saturday: long ride, sometimes tennis too
            day_workouts.append(("cycling", 95 + rng.uniform(-15, 20), day_strain * 0.78, 42_000.0))
            if (i // 7) % 3 == 0:
                day_workouts.append(("tennis", 55.0, 6.5, None))
        elif weekday == 0 and i % 4 == 0:  # occasional Monday recovery jog
            day_workouts.append(("running", 32.0, 7.8, 5_200.0))

        for slot, (sport, minutes, workout_strain, distance) in enumerate(day_workouts):
            start_at = wake + timedelta(hours=10, minutes=30 * slot + rng.randint(0, 40))
            end_at = start_at + timedelta(minutes=minutes)
            zone_total_ms = int(minutes * 60_000)
            hard = workout_strain >= 12
            zone_split = (
                (0.04, 0.16, 0.30, 0.28, 0.16, 0.06)
                if hard
                else (0.12, 0.30, 0.34, 0.16, 0.06, 0.02)
            )
            workouts.append(
                {
                    "id": _workout_uuid(i, slot),
                    "v1_id": 70_000 + i * 4 + slot,
                    "user_id": DEMO_PROFILE["user_id"],
                    "created_at": _iso(end_at),
                    "updated_at": _iso(end_at),
                    "start": _iso(start_at),
                    "end": _iso(end_at),
                    "timezone_offset": offset,
                    "sport_name": sport,
                    "sport_id": 1,
                    "score_state": "SCORED",
                    "score": {
                        "strain": round(workout_strain, 2),
                        "average_heart_rate": int(118 + workout_strain * 3.4),
                        "max_heart_rate": int(150 + workout_strain * 2.4),
                        "kilojoule": round(minutes * 38 + workout_strain * 90, 1),
                        "percent_recorded": 100.0,
                        "distance_meter": distance,
                        "altitude_gain_meter": round(rng.uniform(8, 240), 1) if distance else None,
                        "altitude_change_meter": round(rng.uniform(-4, 4), 2) if distance else None,
                        "zone_durations": {
                            "zone_zero_milli": int(zone_total_ms * zone_split[0]),
                            "zone_one_milli": int(zone_total_ms * zone_split[1]),
                            "zone_two_milli": int(zone_total_ms * zone_split[2]),
                            "zone_three_milli": int(zone_total_ms * zone_split[3]),
                            "zone_four_milli": int(zone_total_ms * zone_split[4]),
                            "zone_five_milli": int(zone_total_ms * zone_split[5]),
                        },
                    },
                }
            )

        # --- naps -----------------------------------------------------------
        if i % 11 == 7:
            nap_start = wake + timedelta(hours=7)
            nap_id = _sleep_uuid(10_000 + i)
            sleep_index[nap_id] = i
            sleeps.append(
                {
                    "id": nap_id,
                    "cycle_id": None,
                    "v1_id": None,
                    "user_id": DEMO_PROFILE["user_id"],
                    "created_at": _iso(nap_start + timedelta(minutes=35)),
                    "updated_at": _iso(nap_start + timedelta(minutes=35)),
                    "start": _iso(nap_start),
                    "end": _iso(nap_start + timedelta(minutes=35)),
                    "timezone_offset": offset,
                    "nap": True,
                    "score_state": "SCORED",
                    "score": {
                        "stage_summary": {
                            "total_in_bed_time_milli": 2_100_000,
                            "total_awake_time_milli": 180_000,
                            "total_no_data_time_milli": 0,
                            "total_light_sleep_time_milli": 1_500_000,
                            "total_slow_wave_sleep_time_milli": 420_000,
                            "total_rem_sleep_time_milli": 0,
                            "sleep_cycle_count": 0,
                            "disturbance_count": 1,
                        },
                        "sleep_needed": None,
                        "respiratory_rate": 15.0,
                        "sleep_performance_percentage": None,
                        "sleep_consistency_percentage": None,
                        "sleep_efficiency_percentage": 91.0,
                    },
                }
            )

        # --- the physiological day (cycle) ----------------------------------
        next_wake = wake + timedelta(hours=23, minutes=rng.randint(30, 90))
        cycles.append(
            {
                "id": cycle_id,
                "user_id": DEMO_PROFILE["user_id"],
                "created_at": _iso(wake),
                "updated_at": _iso(next_wake if not is_today else wake),
                "start": _iso(wake),
                "end": None if is_today else _iso(next_wake),
                "timezone_offset": offset,
                "score_state": "SCORED",
                "score": {
                    "strain": round(day_strain, 2),
                    "kilojoule": round(7_400 + day_strain * 430 + rng.uniform(-300, 300), 1),
                    "average_heart_rate": int(60 + day_strain * 1.7),
                    "max_heart_rate": int(132 + day_strain * 2.6),
                },
            }
        )
        previous_strain = day_strain

    return {
        "cycles": cycles,
        "recoveries": recoveries,
        "sleeps": sleeps,
        "workouts": workouts,
        "sleep_index": sleep_index,
    }


def _build_stream(sleep: dict[str, Any], index: int) -> dict[str, Any]:
    """Deterministic per-minute overnight stream for one demo sleep."""
    rng = random.Random(7_000 + index)
    start = parse_iso(sleep["start"])
    end = parse_iso(sleep["end"])
    total_minutes = max(int((end - start).total_seconds() // 60), 10)
    points = []
    for minute in range(total_minutes):
        progress = minute / total_minutes
        # U-shaped heart rate: settles fast, bottoms out ~40% in, rises at dawn.
        depth = math.sin(math.pi * min(progress / 0.8, 1.0)) ** 1.5
        hr = 60 - 14 * depth + rng.uniform(-1.5, 1.5)
        points.append(
            {
                "timestamp": _iso(start + timedelta(minutes=minute)),
                "hr": round(max(hr, 41)),
                "skin_temp": round(33.1 + 0.5 * progress + rng.uniform(-0.08, 0.08), 2),
                "board_temp": 30.2,
                "battery_temp": 29.8,
                "is_sleeping": 4 < minute < total_minutes - 2,
                "is_charging": False,
            }
        )
    return {"stream": points, "algorithm_version": "demo-1"}


class DemoWhoop:
    """Serves the demo dataset with real WHOOP API v2 semantics."""

    def __init__(self, dataset: dict[str, Any] | None = None) -> None:
        self.data = dataset or build_demo_dataset()

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = dict(urllib.parse.parse_qsl(request.url.query.decode()))

        if path == "/developer/v2/user/profile/basic":
            return httpx.Response(200, json=DEMO_PROFILE)
        if path == "/developer/v2/user/measurement/body":
            return httpx.Response(200, json=DEMO_BODY)
        if path == "/developer/v2/user/access" and request.method == "DELETE":
            return httpx.Response(204)
        if path == "/developer/v2/cycle":
            return self._collection(self.data["cycles"], params, "start")
        if path == "/developer/v2/recovery":
            return self._collection(self.data["recoveries"], params, "created_at")
        if path == "/developer/v2/activity/sleep":
            return self._collection(self.data["sleeps"], params, "start")
        if path == "/developer/v2/activity/workout":
            return self._collection(self.data["workouts"], params, "start")

        if path.startswith("/developer/v2/activity/sleep/") and path.endswith("/stream"):
            ident = path[len("/developer/v2/activity/sleep/") : -len("/stream")]
            sleep = next((s for s in self.data["sleeps"] if s["id"] == ident), None)
            if sleep is None:
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(
                200, json=_build_stream(sleep, self.data["sleep_index"].get(ident, 0))
            )
        if path.startswith("/developer/v2/activity/sleep/"):
            ident = path[len("/developer/v2/activity/sleep/") :]
            found = next((s for s in self.data["sleeps"] if s["id"] == ident), None)
            return self._maybe(found)
        if path.startswith("/developer/v2/activity/workout/"):
            ident = path[len("/developer/v2/activity/workout/") :]
            found = next((w for w in self.data["workouts"] if w["id"] == ident), None)
            return self._maybe(found)
        if path.startswith("/developer/v2/cycle/"):
            parts = path[len("/developer/v2/cycle/") :].split("/")
            cycle = next((c for c in self.data["cycles"] if str(c["id"]) == parts[0]), None)
            if cycle is None:
                return httpx.Response(404, json={"message": "not found"})
            if len(parts) == 1:
                return httpx.Response(200, json=cycle)
            if parts[1] == "recovery":
                found = next(
                    (r for r in self.data["recoveries"] if r["cycle_id"] == cycle["id"]), None
                )
                return self._maybe(found)
            if parts[1] == "sleep":
                found = next(
                    (s for s in self.data["sleeps"] if s.get("cycle_id") == cycle["id"]), None
                )
                return self._maybe(found)
        return httpx.Response(404, json={"message": f"unhandled demo path {path}"})

    @staticmethod
    def _maybe(record: dict[str, Any] | None) -> httpx.Response:
        if record is None:
            return httpx.Response(404, json={"message": "not found"})
        return httpx.Response(200, json=record)

    @staticmethod
    def _collection(
        records: list[dict[str, Any]], params: dict[str, str], sort_key: str
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


class DemoTokens:
    """Token provider for demo mode - no OAuth anywhere."""

    async def get_access_token(
        self, *, force_refresh: bool = False, rejected: str | None = None
    ) -> str:
        return "demo-token"
