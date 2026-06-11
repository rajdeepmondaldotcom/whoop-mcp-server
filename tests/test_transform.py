from datetime import datetime, timedelta, timezone

from conftest import BODY, PROFILE, make_cycle, make_recovery, make_sleep, make_workout
from whoop_mcp.transform import (
    fmt_duration,
    kj_to_kcal,
    ms_to_hours,
    prune,
    recovery_zone,
    transform_cycle,
    transform_profile,
    transform_recovery,
    transform_sleep,
    transform_workout,
)

UTC = timezone.utc


def test_fmt_duration():
    assert fmt_duration(27_415_794) == "7h 37m"
    assert fmt_duration(3_600_000) == "1h"
    assert fmt_duration(120_000) == "2m"
    assert fmt_duration(None) is None


def test_recovery_zone_thresholds():
    assert recovery_zone(67) == "green"
    assert recovery_zone(66.9) == "yellow"
    assert recovery_zone(34) == "yellow"
    assert recovery_zone(33.9) == "red"
    assert recovery_zone(None) is None


def test_prune_removes_empty_values():
    assert prune({"a": 1, "b": None, "c": {"d": None}, "e": {"f": 2}}) == {"a": 1, "e": {"f": 2}}


def test_transform_profile():
    out = transform_profile(PROFILE, BODY)
    assert out["name"] == "Ada Lovelace"
    assert out["body"]["height_cm"] == 170.0
    assert out["body"]["weight_kg"] == 62.5
    assert out["body"]["weight_lb"] == 137.8


def test_transform_cycle_localizes_and_converts():
    start = datetime(2026, 6, 10, 2, 25, tzinfo=UTC)
    cycle = make_cycle(7, start, None, offset="-05:00", strain=10.46, kilojoule=8288.297)
    out = transform_cycle(cycle)
    # 02:25 UTC with a -05:00 offset is the previous local evening.
    assert out["date"] == "2026-06-09"
    assert out["start"].endswith("-05:00")
    assert out["in_progress"] is True
    assert out["strain"] == 10.5
    assert out["calories"] == 1981  # 8288.297 kJ -> kcal


def test_transform_recovery_scored_and_pending():
    rec = make_recovery(7, "sid", created=datetime(2026, 6, 10, 12, tzinfo=UTC), score=72.4)
    out = transform_recovery(rec, date="2026-06-10")
    assert out["recovery_score"] == 72
    assert out["zone"] == "green"
    assert out["hrv_ms"] == 65.0
    assert "user_calibrating" not in out  # falsey values pruned

    pending = make_recovery(
        8, "sid2", created=datetime(2026, 6, 10, 12, tzinfo=UTC), score_state="PENDING_SCORE"
    )
    out = transform_recovery(pending)
    assert out["score_state"] == "PENDING_SCORE"
    assert "recovery_score" not in out
    assert "still scoring" in out["note"]


def test_transform_sleep_numbers():
    start = datetime(2026, 6, 9, 23, 30, tzinfo=UTC)
    end = datetime(2026, 6, 10, 7, 15, tzinfo=UTC)
    sleep = make_sleep(
        "sid",
        start,
        end,
        light_ms=14_905_851,
        sws_ms=6_630_370,
        rem_ms=5_879_573,
        awake_ms=1_403_507,
    )
    out = transform_sleep(sleep)
    assert out["date"] == "2026-06-10"
    assert out["nap"] is False
    # asleep = light + deep + rem = 27,415,794 ms ≈ 7.62 h
    assert out["duration"]["asleep_hours"] == 7.62
    assert out["duration"]["asleep"] == "7h 37m"
    assert out["duration"]["deep_sws"] == "1h 51m"  # 6,630,370 ms = 110.5 min
    assert out["sleep_cycles"] == 4
    # needed = 27,944,229 ms; debt = needed - asleep ≈ 9 min
    assert out["sleep_debt_minutes"] == 9
    assert out["performance_pct"] == 85
    assert out["bedtime"] == "2026-06-09T23:30+00:00"


def test_transform_workout_zones_and_distance():
    start = datetime(2026, 6, 10, 16, 0, tzinfo=UTC)
    end = start + timedelta(minutes=60)
    workout = make_workout("wid", start, end, sport="running", kilojoule=1569.34, distance_m=5000)
    out = transform_workout(workout)
    assert out["sport"] == "running"
    assert out["duration_minutes"] == 60
    assert out["calories"] == kj_to_kcal(1569.34) == 375
    assert out["distance_km"] == 5.0
    assert out["distance_miles"] == 3.11
    zones = out["heart_rate_zones"]
    assert zones["zone_2"]["minutes"] == 15
    assert zones["zone_2"]["pct"] == 25  # 15 of 60 minutes
    assert ms_to_hours(None) is None


def test_transform_workout_tolerates_legacy_zone_key():
    start = datetime(2026, 6, 10, 16, 0, tzinfo=UTC)
    workout = make_workout("wid", start, start + timedelta(minutes=30))
    workout["score"]["zone_duration"] = workout["score"].pop("zone_durations")
    out = transform_workout(workout)
    assert "zone_2" in out["heart_rate_zones"]
