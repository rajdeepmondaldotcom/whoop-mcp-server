from datetime import date, datetime, timedelta, timezone

from conftest import make_cycle, make_recovery, make_sleep, make_workout
from whoop_mcp.summaries import (
    Bundle,
    aggregate_period,
    build_daily_summary,
    build_weekly_report,
    compare_aggregates,
    recovery_day_points,
)

UTC = timezone.utc


def day_bundle(day: date, *, with_workout: bool = True, nap: bool = False) -> Bundle:
    wake = datetime.combine(day, datetime.min.time(), tzinfo=UTC).replace(hour=7)
    bundle = Bundle(
        cycles=[make_cycle(1, wake, wake + timedelta(days=1), strain=12.0)],
        sleeps=[make_sleep("s-main", wake - timedelta(hours=8), wake, cycle_id=1)],
        recoveries=[make_recovery(1, "s-main", created=wake + timedelta(minutes=15), score=81.0)],
    )
    if nap:
        bundle.sleeps.append(
            make_sleep(
                "s-nap",
                wake + timedelta(hours=7),
                wake + timedelta(hours=7, minutes=40),
                nap=True,
            )
        )
    if with_workout:
        bundle.workouts.append(
            make_workout("w-1", wake + timedelta(hours=9), wake + timedelta(hours=10))
        )
    return bundle


def test_recovery_day_points_joins_cycle_dates():
    day = date(2026, 6, 10)
    bundle = day_bundle(day)
    points = recovery_day_points(bundle.recoveries, bundle.cycles)
    assert points[0][0] == day


def test_recovery_day_points_falls_back_to_created_at():
    rec = make_recovery(99, "sid", created=datetime(2026, 6, 11, 12, tzinfo=UTC))
    points = recovery_day_points([rec], [])
    assert points[0][0] == date(2026, 6, 11)


def test_daily_summary_assembles_everything():
    day = date(2026, 6, 10)
    out = build_daily_summary(day_bundle(day, nap=True), day, today=date(2026, 6, 12))
    assert out["date"] == "2026-06-10"
    assert out["recovery"]["recovery_score"] == 81
    assert out["sleep"]["nap"] is False
    assert len(out["naps"]) == 1
    assert len(out["workouts"]) == 1
    assert "Recovery 81% (green)" in out["summary"]
    assert "Strain 12" in out["summary"]
    assert "running" in out["summary"]
    assert "notes" not in out


def test_daily_summary_today_without_recovery_notes_it():
    day = date(2026, 6, 12)
    bundle = Bundle()
    out = build_daily_summary(bundle, day, today=day)
    assert "not available yet" in out["notes"][0]
    assert out["summary"] == "No WHOOP data recorded for this day."


def test_weekly_report_grid_and_averages():
    monday = date(2026, 6, 1)
    bundle = Bundle()
    for i in range(7):
        day = monday + timedelta(days=i)
        wake = datetime.combine(day, datetime.min.time(), tzinfo=UTC).replace(hour=7)
        cycle_id = 100 + i
        bundle.cycles.append(
            make_cycle(cycle_id, wake, wake + timedelta(days=1), strain=10.0 + i)
        )
        bundle.sleeps.append(
            make_sleep(f"s-{i}", wake - timedelta(hours=8), wake, cycle_id=cycle_id)
        )
        bundle.recoveries.append(
            make_recovery(
                cycle_id, f"s-{i}", created=wake + timedelta(minutes=10), score=60.0 + i * 2
            )
        )
    bundle.workouts.append(
        make_workout(
            "w-1",
            datetime(2026, 6, 3, 16, tzinfo=UTC),
            datetime(2026, 6, 3, 17, tzinfo=UTC),
            sport="tennis",
        )
    )
    out = build_weekly_report(bundle, monday, monday + timedelta(days=6), today=date(2026, 6, 12))
    assert out["week_start"] == "2026-06-01"
    assert len(out["days"]) == 7
    assert out["days"][0]["weekday"] == "Mon"
    assert out["days"][2]["workouts"] == ["tennis"]
    assert out["averages"]["recovery"] == 66.0  # mean of 60..72
    assert out["averages"]["strain"] == 13.0  # mean of 10..16
    assert out["totals"]["workouts"] == 1
    assert "notes" not in out  # completed week, nothing truncated


def test_aggregate_and_compare_periods():
    week_a_monday = date(2026, 6, 1)
    week_b_monday = date(2026, 6, 8)

    def week_bundle(monday: date, recovery: float, strain: float) -> Bundle:
        bundle = Bundle()
        for i in range(7):
            day = monday + timedelta(days=i)
            wake = datetime.combine(day, datetime.min.time(), tzinfo=UTC).replace(hour=7)
            cycle_id = hash((monday, i)) % 10_000
            bundle.cycles.append(
                make_cycle(cycle_id, wake, wake + timedelta(days=1), strain=strain)
            )
            bundle.sleeps.append(
                make_sleep(f"s-{monday}-{i}", wake - timedelta(hours=8), wake, cycle_id=cycle_id)
            )
            bundle.recoveries.append(
                make_recovery(
                    cycle_id, f"s-{monday}-{i}", created=wake, score=recovery, rhr=52.0
                )
            )
        return bundle

    agg_a = aggregate_period(
        week_bundle(week_a_monday, 60.0, 10.0), week_a_monday, week_a_monday + timedelta(days=6)
    )
    agg_b = aggregate_period(
        week_bundle(week_b_monday, 72.0, 14.0), week_b_monday, week_b_monday + timedelta(days=6)
    )
    assert agg_a["recovery_score"] == 60.0
    assert agg_b["recovery_score"] == 72.0

    comparison = compare_aggregates(agg_a, agg_b)
    assert comparison["recovery_score"]["assessment"] == "improved"
    assert comparison["recovery_score"]["change_pct"] == 20.0
    assert comparison["strain"]["assessment"] == "increased"
