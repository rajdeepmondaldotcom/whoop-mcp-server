"""Transforms from raw WHOOP API records into clean, LLM-friendly shapes.

WHOOP's wire format is verbose (millisecond durations, kilojoules, repeated
bookkeeping fields). These functions reshape records so a model gets exactly
the numbers a coach would care about — hours of sleep, calories, heart-rate
zones as minutes and percentages — while keeping ids and ISO timestamps so
every value stays traceable. Timestamps are rendered in the record's own
timezone, which is where the user actually was.
"""

from __future__ import annotations

from typing import Any

from whoop_mcp.timeutil import local_iso, parse_iso, record_local_date

MS_PER_MINUTE = 60_000
MS_PER_HOUR = 3_600_000
KCAL_PER_KJ = 0.239006
MILES_PER_KM = 0.621371

RECOVERY_GREEN = 67
RECOVERY_YELLOW = 34

ZONE_LABELS = (
    ("zone_zero_milli", "zone_0_rest"),
    ("zone_one_milli", "zone_1"),
    ("zone_two_milli", "zone_2"),
    ("zone_three_milli", "zone_3"),
    ("zone_four_milli", "zone_4"),
    ("zone_five_milli", "zone_5_max"),
)


def fmt_duration(ms: float | None) -> str | None:
    if ms is None:
        return None
    total_minutes = int(round(ms / MS_PER_MINUTE))
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def ms_to_hours(ms: float | None) -> float | None:
    return None if ms is None else round(ms / MS_PER_HOUR, 2)


def ms_to_minutes(ms: float | None) -> int | None:
    return None if ms is None else int(round(ms / MS_PER_MINUTE))


def kj_to_kcal(kj: float | None) -> int | None:
    return None if kj is None else int(round(kj * KCAL_PER_KJ))


def rounded(value: float | None, digits: int = 1) -> float | int | None:
    if value is None:
        return None
    return int(round(value)) if digits == 0 else round(value, digits)


def recovery_zone(score: float | None) -> str | None:
    if score is None:
        return None
    if score >= RECOVERY_GREEN:
        return "green"
    if score >= RECOVERY_YELLOW:
        return "yellow"
    return "red"


def prune(mapping: dict[str, Any]) -> dict[str, Any]:
    """Drop None values (and empty dicts) to keep tool output compact."""
    cleaned: dict[str, Any] = {}
    for key, value in mapping.items():
        if value is None:
            continue
        if isinstance(value, dict):
            value = prune(value)
            if not value:
                continue
        cleaned[key] = value
    return cleaned


def duration_between(start_iso: str | None, end_iso: str | None) -> float | None:
    """Elapsed milliseconds between two API timestamps."""
    if not start_iso or not end_iso:
        return None
    return (parse_iso(end_iso) - parse_iso(start_iso)).total_seconds() * 1000


# ----------------------------------------------------------------- profiles


def transform_profile(profile: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    height_m = body.get("height_meter")
    weight_kg = body.get("weight_kilogram")
    out = {
        "user_id": profile.get("user_id"),
        "name": " ".join(
            part for part in (profile.get("first_name"), profile.get("last_name")) if part
        )
        or None,
        "email": profile.get("email"),
        "body": {
            "height_cm": rounded(height_m * 100, 1) if height_m is not None else None,
            "height_meter": height_m,
            "weight_kg": rounded(weight_kg, 1),
            "weight_lb": rounded(weight_kg * 2.20462, 1) if weight_kg is not None else None,
            "max_heart_rate": body.get("max_heart_rate"),
        },
    }
    return prune(out)


# ------------------------------------------------------------------- cycles


def transform_cycle(cycle: dict[str, Any]) -> dict[str, Any]:
    offset = cycle.get("timezone_offset")
    score = cycle.get("score") or {}
    start = cycle.get("start")
    end = cycle.get("end")
    out = {
        "id": cycle.get("id"),
        "date": str(record_local_date(start, offset)) if start else None,
        "start": local_iso(start, offset),
        "end": local_iso(end, offset),
        "in_progress": end is None,
        "score_state": cycle.get("score_state"),
        "strain": rounded(score.get("strain")),
        "calories": kj_to_kcal(score.get("kilojoule")),
        "average_heart_rate": score.get("average_heart_rate"),
        "max_heart_rate": score.get("max_heart_rate"),
    }
    if cycle.get("score_state") != "SCORED":
        out["note"] = _score_note(cycle.get("score_state"))
    return prune(out)


# ---------------------------------------------------------------- recovery


def transform_recovery(recovery: dict[str, Any], *, date: str | None = None) -> dict[str, Any]:
    score = recovery.get("score") or {}
    recovery_score = score.get("recovery_score")
    out = {
        "cycle_id": recovery.get("cycle_id"),
        "sleep_id": recovery.get("sleep_id"),
        "date": date,
        "score_state": recovery.get("score_state"),
        "recovery_score": rounded(recovery_score, 0),
        "zone": recovery_zone(recovery_score),
        "hrv_ms": rounded(score.get("hrv_rmssd_milli")),
        "resting_heart_rate": rounded(score.get("resting_heart_rate"), 0),
        "spo2_percentage": rounded(score.get("spo2_percentage")),
        "skin_temp_celsius": rounded(score.get("skin_temp_celsius")),
        "user_calibrating": score.get("user_calibrating") or None,
    }
    if recovery.get("score_state") != "SCORED":
        out["note"] = _score_note(recovery.get("score_state"))
    return prune(out)


# ------------------------------------------------------------------- sleep


def transform_sleep(sleep: dict[str, Any]) -> dict[str, Any]:
    offset = sleep.get("timezone_offset")
    score = sleep.get("score") or {}
    stages = score.get("stage_summary") or {}
    needed = score.get("sleep_needed") or {}

    light = stages.get("total_light_sleep_time_milli")
    deep = stages.get("total_slow_wave_sleep_time_milli")
    rem = stages.get("total_rem_sleep_time_milli")
    awake = stages.get("total_awake_time_milli")
    in_bed = stages.get("total_in_bed_time_milli")
    asleep = sum(v for v in (light, deep, rem) if v is not None) or None

    needed_total = (
        sum(
            needed.get(key, 0)
            for key in (
                "baseline_milli",
                "need_from_sleep_debt_milli",
                "need_from_recent_strain_milli",
                "need_from_recent_nap_milli",
            )
        )
        if needed
        else None
    )
    debt_minutes = (
        ms_to_minutes(needed_total - asleep) if needed_total is not None and asleep else None
    )

    end = sleep.get("end")
    out = {
        "id": sleep.get("id"),
        "cycle_id": sleep.get("cycle_id"),
        "date": str(record_local_date(end, offset)) if end else None,
        "nap": sleep.get("nap", False),
        "bedtime": local_iso(sleep.get("start"), offset),
        "waketime": local_iso(end, offset),
        "score_state": sleep.get("score_state"),
        "duration": prune(
            {
                "asleep": fmt_duration(asleep),
                "asleep_hours": ms_to_hours(asleep),
                "in_bed": fmt_duration(in_bed),
                "awake": fmt_duration(awake),
                "light": fmt_duration(light),
                "deep_sws": fmt_duration(deep),
                "rem": fmt_duration(rem),
            }
        ),
        "sleep_cycles": stages.get("sleep_cycle_count"),
        "disturbances": stages.get("disturbance_count"),
        "respiratory_rate": rounded(score.get("respiratory_rate")),
        "performance_pct": rounded(score.get("sleep_performance_percentage"), 0),
        "efficiency_pct": rounded(score.get("sleep_efficiency_percentage")),
        "consistency_pct": rounded(score.get("sleep_consistency_percentage"), 0),
        "sleep_needed": fmt_duration(needed_total),
        "sleep_debt_minutes": debt_minutes,
    }
    if sleep.get("score_state") != "SCORED":
        out["note"] = _score_note(sleep.get("score_state"))
    return prune(out)


# ----------------------------------------------------------------- workout


def transform_workout(workout: dict[str, Any]) -> dict[str, Any]:
    offset = workout.get("timezone_offset")
    score = workout.get("score") or {}
    start = workout.get("start")
    duration_ms = duration_between(start, workout.get("end"))

    # v2 spells it `zone_durations`; tolerate the old singular spelling too.
    zones_raw = score.get("zone_durations") or score.get("zone_duration") or {}
    zones: dict[str, Any] = {}
    for key, label in ZONE_LABELS:
        ms = zones_raw.get(key)
        if ms is None:
            continue
        zones[label] = {
            "minutes": ms_to_minutes(ms),
            "pct": int(round(ms / duration_ms * 100)) if duration_ms else None,
        }

    distance_m = score.get("distance_meter")
    out = {
        "id": workout.get("id"),
        "sport": workout.get("sport_name") or _legacy_sport(workout.get("sport_id")),
        "date": str(record_local_date(start, offset)) if start else None,
        "start": local_iso(start, offset),
        "end": local_iso(workout.get("end"), offset),
        "duration": fmt_duration(duration_ms),
        "duration_minutes": ms_to_minutes(duration_ms),
        "score_state": workout.get("score_state"),
        "strain": rounded(score.get("strain")),
        "calories": kj_to_kcal(score.get("kilojoule")),
        "average_heart_rate": score.get("average_heart_rate"),
        "max_heart_rate": score.get("max_heart_rate"),
        "distance_km": rounded(distance_m / 1000, 2) if distance_m is not None else None,
        "distance_miles": (
            rounded(distance_m / 1000 * MILES_PER_KM, 2) if distance_m is not None else None
        ),
        "elevation_gain_m": rounded(score.get("altitude_gain_meter"), 0),
        "percent_recorded": rounded(score.get("percent_recorded"), 0),
        "heart_rate_zones": prune(zones) or None,
    }
    if workout.get("score_state") != "SCORED":
        out["note"] = _score_note(workout.get("score_state"))
    return prune(out)


def _legacy_sport(sport_id: Any) -> str | None:
    return f"sport_id:{sport_id}" if sport_id is not None else None


def _score_note(state: str | None) -> str:
    if state == "PENDING_SCORE":
        return "WHOOP is still scoring this record; check back shortly."
    if state == "UNSCORABLE":
        return "WHOOP could not score this record (insufficient data)."
    return "No score available."
