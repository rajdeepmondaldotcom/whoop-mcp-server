"""Composite views over WHOOP data: daily summaries, weekly reports, trends.

Everything here works on a :class:`Bundle` - one window of cycles,
recoveries, sleeps, and workouts fetched concurrently - and buckets records
onto calendar days using each record's own ``timezone_offset``. Recoveries
don't carry an offset, so they inherit the date of their cycle (falling back
to their UTC creation date when the cycle isn't in the window).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from whoop_mcp.analytics import (
    DayValue,
    acute_chronic_ratio,
    average,
    compare_metric,
    describe_correlation,
    describe_series,
)
from whoop_mcp.client import WhoopClient
from whoop_mcp.timeutil import parse_iso, record_local_date
from whoop_mcp.transform import (
    duration_between,
    fmt_duration,
    kj_to_kcal,
    ms_to_hours,
    ms_to_minutes,
    prune,
    recovery_zone,
    rounded,
    transform_cycle,
    transform_recovery,
    transform_sleep,
    transform_workout,
)

SCORED = "SCORED"


@dataclass
class Bundle:
    cycles: list[dict[str, Any]] = field(default_factory=list)
    recoveries: list[dict[str, Any]] = field(default_factory=list)
    sleeps: list[dict[str, Any]] = field(default_factory=list)
    workouts: list[dict[str, Any]] = field(default_factory=list)
    truncated: list[str] = field(default_factory=list)


async def fetch_bundle(
    client: WhoopClient,
    start: datetime,
    end: datetime,
    *,
    max_records: int = 250,
) -> Bundle:
    """Fetch all four record types for a window, concurrently."""
    (cycles, c_trunc), (recoveries, r_trunc), (sleeps, s_trunc), (workouts, w_trunc) = (
        await asyncio.gather(
            client.cycles(start, end, max_records),
            client.recoveries(start, end, max_records),
            client.sleeps(start, end, max_records),
            client.workouts(start, end, max_records),
        )
    )
    bundle = Bundle(cycles=cycles, recoveries=recoveries, sleeps=sleeps, workouts=workouts)
    for name, flag in (
        ("cycles", c_trunc),
        ("recoveries", r_trunc),
        ("sleeps", s_trunc),
        ("workouts", w_trunc),
    ):
        if flag:
            bundle.truncated.append(name)
    return bundle


# ------------------------------------------------------------- day bucketing


def cycle_date(cycle: dict[str, Any]) -> date | None:
    start = cycle.get("start")
    return record_local_date(start, cycle.get("timezone_offset")) if start else None


def sleep_date(sleep: dict[str, Any]) -> date | None:
    """A sleep belongs to the day you woke up (its local end date)."""
    anchor = sleep.get("end") or sleep.get("start")
    return record_local_date(anchor, sleep.get("timezone_offset")) if anchor else None


def workout_date(workout: dict[str, Any]) -> date | None:
    start = workout.get("start")
    return record_local_date(start, workout.get("timezone_offset")) if start else None


def recovery_day_points(
    recoveries: list[dict[str, Any]], cycles: list[dict[str, Any]]
) -> list[tuple[date, dict[str, Any]]]:
    """(local day, recovery) pairs, dated via the owning cycle, oldest first."""
    cycle_days = {c.get("id"): cycle_date(c) for c in cycles}
    points: list[tuple[date, dict[str, Any]]] = []
    for recovery in recoveries:
        day = cycle_days.get(recovery.get("cycle_id"))
        if day is None and recovery.get("created_at"):
            day = parse_iso(recovery["created_at"]).date()
        if day is not None:
            points.append((day, recovery))
    points.sort(key=lambda pair: pair[0])
    return points


def _asleep_ms(sleep: dict[str, Any]) -> float | None:
    stages = (sleep.get("score") or {}).get("stage_summary") or {}
    parts = [
        stages.get("total_light_sleep_time_milli"),
        stages.get("total_slow_wave_sleep_time_milli"),
        stages.get("total_rem_sleep_time_milli"),
    ]
    present = [p for p in parts if p is not None]
    return sum(present) if present else None


def _sleep_debt_minutes(sleep: dict[str, Any]) -> int | None:
    score = sleep.get("score") or {}
    needed = score.get("sleep_needed") or {}
    if not needed:
        return None
    total_needed = sum(
        needed.get(key, 0)
        for key in (
            "baseline_milli",
            "need_from_sleep_debt_milli",
            "need_from_recent_strain_milli",
            "need_from_recent_nap_milli",
        )
    )
    asleep = _asleep_ms(sleep)
    if asleep is None:
        return None
    return ms_to_minutes(total_needed - asleep)


# ------------------------------------------------------------ daily summary


def build_daily_summary(bundle: Bundle, day: date, *, today: date) -> dict[str, Any]:
    notes: list[str] = []

    cycle = next((c for c in bundle.cycles if cycle_date(c) == day), None)
    recovery = None
    if cycle is not None:
        recovery = next(
            (r for r in bundle.recoveries if r.get("cycle_id") == cycle.get("id")), None
        )
    if recovery is None:
        recovery = next(
            (r for d, r in recovery_day_points(bundle.recoveries, bundle.cycles) if d == day),
            None,
        )

    day_sleeps = [s for s in bundle.sleeps if sleep_date(s) == day]
    main_sleeps = [s for s in day_sleeps if not s.get("nap")]
    main_sleep = max(
        main_sleeps,
        key=lambda s: duration_between(s.get("start"), s.get("end")) or 0,
        default=None,
    )
    naps = [s for s in day_sleeps if s.get("nap")]

    day_workouts = sorted(
        (w for w in bundle.workouts if workout_date(w) == day),
        key=lambda w: w.get("start") or "",
    )

    if day == today and recovery is None:
        notes.append(
            "Today's recovery is not available yet - WHOOP scores it after you wake up "
            "and sync."
        )
    if day == today and cycle is not None and cycle.get("end") is None:
        notes.append("Today's cycle is still in progress; strain will keep rising until sleep.")
    for record, label in ((recovery, "recovery"), (main_sleep, "sleep"), (cycle, "cycle")):
        if record is not None and record.get("score_state") == "PENDING_SCORE":
            notes.append(f"The {label} record is still being scored by WHOOP.")

    summary = _summary_line(recovery, main_sleep, cycle, day_workouts)

    return prune(
        {
            "date": str(day),
            "summary": summary,
            "recovery": transform_recovery(recovery, date=str(day)) if recovery else None,
            "sleep": transform_sleep(main_sleep) if main_sleep else None,
            "naps": [transform_sleep(nap) for nap in naps] or None,
            "cycle": transform_cycle(cycle) if cycle else None,
            "workouts": [transform_workout(w) for w in day_workouts] or None,
            "notes": notes or None,
        }
    )


def _summary_line(
    recovery: dict[str, Any] | None,
    sleep: dict[str, Any] | None,
    cycle: dict[str, Any] | None,
    workouts: list[dict[str, Any]],
) -> str:
    parts: list[str] = []
    if recovery and (recovery.get("score") or {}).get("recovery_score") is not None:
        score = recovery["score"]["recovery_score"]
        parts.append(f"Recovery {round(score)}% ({recovery_zone(score)})")
    if sleep:
        asleep = _asleep_ms(sleep)
        performance = (sleep.get("score") or {}).get("sleep_performance_percentage")
        if asleep is not None:
            text = f"Sleep {fmt_duration(asleep)}"
            if performance is not None:
                text += f" ({round(performance)}%)"
            parts.append(text)
    if cycle and (cycle.get("score") or {}).get("strain") is not None:
        parts.append(f"Strain {round(cycle['score']['strain'], 1)}")
    if workouts:
        sports = ", ".join(sorted({w.get("sport_name") or "workout" for w in workouts}))
        parts.append(f"{len(workouts)} workout{'s' if len(workouts) != 1 else ''} ({sports})")
    return " · ".join(parts) if parts else "No WHOOP data recorded for this day."


# ------------------------------------------------------------ weekly report


def build_weekly_report(
    bundle: Bundle, monday: date, sunday: date, *, today: date
) -> dict[str, Any]:
    recovery_by_day = dict(recovery_day_points(bundle.recoveries, bundle.cycles))
    cycles_by_day = {cycle_date(c): c for c in bundle.cycles}
    sleeps_by_day: dict[date | None, dict[str, Any]] = {}
    for sleep in bundle.sleeps:
        if sleep.get("nap"):
            continue
        day = sleep_date(sleep)
        existing = sleeps_by_day.get(day)
        if existing is None or (duration_between(sleep.get("start"), sleep.get("end")) or 0) > (
            duration_between(existing.get("start"), existing.get("end")) or 0
        ):
            sleeps_by_day[day] = sleep

    days = []
    cursor = monday
    while cursor <= sunday:
        recovery = recovery_by_day.get(cursor)
        recovery_score = ((recovery or {}).get("score") or {}).get("recovery_score")
        sleep = sleeps_by_day.get(cursor)
        cycle = cycles_by_day.get(cursor)
        strain = ((cycle or {}).get("score") or {}).get("strain")
        day_workouts = [w for w in bundle.workouts if workout_date(w) == cursor]
        days.append(
            prune(
                {
                    "date": str(cursor),
                    "weekday": cursor.strftime("%a"),
                    "recovery": rounded(recovery_score, 0),
                    "zone": recovery_zone(recovery_score),
                    "sleep_hours": ms_to_hours(_asleep_ms(sleep)) if sleep else None,
                    "sleep_performance": rounded(
                        ((sleep or {}).get("score") or {}).get("sleep_performance_percentage"), 0
                    ),
                    "strain": rounded(strain),
                    "workouts": [w.get("sport_name") or "workout" for w in day_workouts] or None,
                }
            )
        )
        cursor += timedelta(days=1)

    week_workouts = [
        w for w in bundle.workouts if (d := workout_date(w)) and monday <= d <= sunday
    ]
    total_workout_minutes = sum(
        m
        for w in week_workouts
        if (m := ms_to_minutes(duration_between(w.get("start"), w.get("end")))) is not None
    )
    week_cycles = [
        c
        for c in bundle.cycles
        if (d := cycle_date(c)) and monday <= d <= sunday and c.get("score_state") == SCORED
    ]

    averages = prune(
        {
            "recovery": average([d.get("recovery") for d in days]),
            "sleep_hours": average([d.get("sleep_hours") for d in days]),
            "sleep_performance": average([d.get("sleep_performance") for d in days]),
            "strain": average([d.get("strain") for d in days]),
        }
    )
    totals = prune(
        {
            "workouts": len(week_workouts),
            "workout_minutes": total_workout_minutes,
            "calories": sum(
                kcal
                for c in week_cycles
                if (kcal := kj_to_kcal((c.get("score") or {}).get("kilojoule"))) is not None
            )
            or None,
        }
    )

    notes = []
    if sunday >= today:
        notes.append("This week is still in progress; averages cover the days so far.")
    if bundle.truncated:
        notes.append(f"Some collections were truncated: {', '.join(bundle.truncated)}.")

    return prune(
        {
            "week_start": str(monday),
            "week_end": str(sunday),
            "days": days,
            "averages": averages,
            "totals": totals,
            "notes": notes or None,
        }
    )


# ------------------------------------------------------------------- trends


def _period(start: date, end: date) -> dict[str, str]:
    return {"start": str(start), "end": str(end)}


def recovery_trends(bundle: Bundle, start: date, end: date) -> dict[str, Any]:
    points = recovery_day_points(bundle.recoveries, bundle.cycles)
    scored = [
        (day, r["score"])
        for day, r in points
        if start <= day <= end and r.get("score_state") == SCORED and r.get("score")
    ]
    calibrating = sum(1 for _, s in scored if s.get("user_calibrating"))

    def series(key: str) -> list[DayValue]:
        return [DayValue(day, s[key]) for day, s in scored if s.get(key) is not None]

    daily = [
        prune(
            {
                "date": str(day),
                "recovery": rounded(s.get("recovery_score"), 0),
                "zone": recovery_zone(s.get("recovery_score")),
                "hrv_ms": rounded(s.get("hrv_rmssd_milli")),
                "rhr": rounded(s.get("resting_heart_rate"), 0),
            }
        )
        for day, s in scored
    ]
    notes = []
    if calibrating:
        notes.append(f"{calibrating} day(s) were recorded while WHOOP was still calibrating.")
    if bundle.truncated:
        notes.append(f"Some collections were truncated: {', '.join(bundle.truncated)}.")
    return prune(
        {
            "period": _period(start, end),
            "recovery_score": describe_series("recovery_score", series("recovery_score")),
            "hrv_ms": describe_series("hrv_ms", series("hrv_rmssd_milli")),
            "resting_heart_rate": describe_series(
                "resting_heart_rate", series("resting_heart_rate")
            ),
            "daily": daily or None,
            "notes": notes or None,
        }
    )


def sleep_trends(bundle: Bundle, start: date, end: date) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    hours_series: list[DayValue] = []
    perf_series: list[DayValue] = []
    eff_series: list[DayValue] = []
    cons_series: list[DayValue] = []
    debt_series: list[DayValue] = []
    nap_count = 0

    for sleep in sorted(bundle.sleeps, key=lambda s: s.get("end") or ""):
        day = sleep_date(sleep)
        if day is None or not (start <= day <= end):
            continue
        if sleep.get("nap"):
            nap_count += 1
            continue
        if sleep.get("score_state") != SCORED or not sleep.get("score"):
            continue
        score = sleep["score"]
        hours = ms_to_hours(_asleep_ms(sleep))
        debt = _sleep_debt_minutes(sleep)
        if hours is not None:
            hours_series.append(DayValue(day, hours))
        for value, series in (
            (score.get("sleep_performance_percentage"), perf_series),
            (score.get("sleep_efficiency_percentage"), eff_series),
            (score.get("sleep_consistency_percentage"), cons_series),
        ):
            if value is not None:
                series.append(DayValue(day, value))
        if debt is not None:
            debt_series.append(DayValue(day, debt))
        rows.append(
            prune(
                {
                    "date": str(day),
                    "hours": hours,
                    "performance": rounded(score.get("sleep_performance_percentage"), 0),
                    "efficiency": rounded(score.get("sleep_efficiency_percentage"), 0),
                    "consistency": rounded(score.get("sleep_consistency_percentage"), 0),
                    "debt_minutes": debt,
                }
            )
        )

    notes = []
    if nap_count:
        notes.append(f"{nap_count} nap(s) in this window are excluded from nightly averages.")
    if bundle.truncated:
        notes.append(f"Some collections were truncated: {', '.join(bundle.truncated)}.")
    return prune(
        {
            "period": _period(start, end),
            "sleep_hours": describe_series("sleep_hours", hours_series),
            "performance_pct": describe_series("sleep_performance_pct", perf_series),
            "efficiency_pct": describe_series("sleep_efficiency_pct", eff_series),
            "consistency_pct": describe_series("sleep_consistency_pct", cons_series),
            "debt_minutes": describe_series("sleep_debt_minutes", debt_series),
            "naps": nap_count or None,
            "daily": rows or None,
            "notes": notes or None,
        }
    )


def strain_trends(bundle: Bundle, start: date, end: date, *, today: date) -> dict[str, Any]:
    strain_by_day: dict[date, float] = {}
    calories_series: list[DayValue] = []
    for cycle in bundle.cycles:
        day = cycle_date(cycle)
        if day is None or not (start <= day <= end) or cycle.get("score_state") != SCORED:
            continue
        score = cycle.get("score") or {}
        if score.get("strain") is not None:
            strain_by_day[day] = score["strain"]
        kcal = kj_to_kcal(score.get("kilojoule"))
        if kcal is not None:
            calories_series.append(DayValue(day, kcal))

    strain_series = [DayValue(day, value) for day, value in sorted(strain_by_day.items())]

    by_sport: dict[str, dict[str, Any]] = {}
    for workout in bundle.workouts:
        day = workout_date(workout)
        if day is None or not (start <= day <= end):
            continue
        sport = workout.get("sport_name") or "workout"
        entry = by_sport.setdefault(
            sport, {"count": 0, "total_minutes": 0, "total_calories": 0, "strains": []}
        )
        entry["count"] += 1
        minutes = ms_to_minutes(duration_between(workout.get("start"), workout.get("end")))
        if minutes:
            entry["total_minutes"] += minutes
        score = workout.get("score") or {}
        kcal = kj_to_kcal(score.get("kilojoule"))
        if kcal:
            entry["total_calories"] += kcal
        if score.get("strain") is not None:
            entry["strains"].append(score["strain"])

    sports = {
        sport: prune(
            {
                "count": entry["count"],
                "total_minutes": entry["total_minutes"] or None,
                "total_calories": entry["total_calories"] or None,
                "avg_strain": average(entry["strains"]),
            }
        )
        for sport, entry in sorted(
            by_sport.items(), key=lambda item: item[1]["count"], reverse=True
        )
    }

    window_days = (end - start).days + 1
    load = acute_chronic_ratio(strain_by_day, today)
    notes = []
    if load is None and window_days >= 28:
        notes.append("Not enough scored days for an acute:chronic load ratio.")
    elif load is None:
        notes.append("Acute:chronic load ratio needs a window of at least 28 days of data.")
    if bundle.truncated:
        notes.append(f"Some collections were truncated: {', '.join(bundle.truncated)}.")

    daily = [
        {"date": str(point.day), "strain": rounded(point.value)} for point in strain_series
    ]
    return prune(
        {
            "period": _period(start, end),
            "day_strain": describe_series("strain", strain_series),
            "daily_calories": describe_series("calories", calories_series),
            "training_load": load,
            "workouts": prune(
                {
                    "total": sum(entry["count"] for entry in by_sport.values()),
                    "by_sport": sports or None,
                }
            ),
            "daily": daily or None,
            "notes": notes or None,
        }
    )


# --------------------------------------------------------- period comparison


def aggregate_period(bundle: Bundle, start: date, end: date) -> dict[str, Any]:
    recovery_points = [
        (day, r["score"])
        for day, r in recovery_day_points(bundle.recoveries, bundle.cycles)
        if start <= day <= end and r.get("score_state") == SCORED and r.get("score")
    ]
    sleeps = [
        s
        for s in bundle.sleeps
        if not s.get("nap")
        and s.get("score_state") == SCORED
        and s.get("score")
        and (d := sleep_date(s))
        and start <= d <= end
    ]
    cycles = [
        c
        for c in bundle.cycles
        if c.get("score_state") == SCORED
        and c.get("score")
        and (d := cycle_date(c))
        and start <= d <= end
    ]
    workouts = [w for w in bundle.workouts if (d := workout_date(w)) and start <= d <= end]

    return prune(
        {
            "period": _period(start, end),
            "days_with_recovery": len(recovery_points),
            "recovery_score": average([s.get("recovery_score") for _, s in recovery_points]),
            "hrv_ms": average([s.get("hrv_rmssd_milli") for _, s in recovery_points]),
            "resting_heart_rate": average(
                [s.get("resting_heart_rate") for _, s in recovery_points]
            ),
            "sleep_hours": average([ms_to_hours(_asleep_ms(s)) for s in sleeps]),
            "sleep_performance_pct": average(
                [s["score"].get("sleep_performance_percentage") for s in sleeps]
            ),
            "strain": average([c["score"].get("strain") for c in cycles]),
            "daily_calories": average(
                [kj_to_kcal(c["score"].get("kilojoule")) for c in cycles]
            ),
            "workouts": len(workouts),
        }
    )


COMPARISON_METRICS = (
    "recovery_score",
    "hrv_ms",
    "resting_heart_rate",
    "sleep_hours",
    "sleep_performance_pct",
    "strain",
    "daily_calories",
    "workouts",
)


def compare_aggregates(agg_a: dict[str, Any], agg_b: dict[str, Any]) -> dict[str, Any]:
    comparison = {}
    for metric in COMPARISON_METRICS:
        result = compare_metric(metric, agg_a.get(metric), agg_b.get(metric))
        if result is not None:
            comparison[metric] = result
    return comparison


# --------------------------------------------------------------- daily table


def _daily_metric_table(bundle: Bundle, start: date, end: date) -> dict[date, dict[str, Any]]:
    """One row per day joining recovery, sleep, and strain - the substrate for
    correlations, records, and the health overview."""
    table: dict[date, dict[str, Any]] = {}

    def row(day: date) -> dict[str, Any]:
        return table.setdefault(day, {})

    for day, recovery in recovery_day_points(bundle.recoveries, bundle.cycles):
        if not (start <= day <= end) or recovery.get("score_state") != SCORED:
            continue
        score = recovery.get("score") or {}
        row(day).update(
            recovery=score.get("recovery_score"),
            hrv=score.get("hrv_rmssd_milli"),
            rhr=score.get("resting_heart_rate"),
        )
    for sleep in bundle.sleeps:
        day = sleep_date(sleep)
        if (
            day is None
            or not (start <= day <= end)
            or sleep.get("nap")
            or sleep.get("score_state") != SCORED
        ):
            continue
        score = sleep.get("score") or {}
        hours = ms_to_hours(_asleep_ms(sleep))
        existing = row(day)
        if existing.get("sleep_hours") is None or (hours or 0) > existing["sleep_hours"]:
            existing.update(
                sleep_hours=hours,
                sleep_performance=score.get("sleep_performance_percentage"),
                sleep_consistency=score.get("sleep_consistency_percentage"),
                sleep_id=sleep.get("id"),
            )
    for cycle in bundle.cycles:
        day = cycle_date(cycle)
        if day is None or not (start <= day <= end) or cycle.get("score_state") != SCORED:
            continue
        score = cycle.get("score") or {}
        row(day).update(
            strain=score.get("strain"), calories=kj_to_kcal(score.get("kilojoule"))
        )
    for workout in bundle.workouts:
        day = workout_date(workout)
        if day is None or not (start <= day <= end):
            continue
        row(day)["workouts"] = row(day).get("workouts", 0) + 1
    return dict(sorted(table.items()))


# -------------------------------------------------------------- correlations


def build_correlations(bundle: Bundle, start: date, end: date) -> dict[str, Any]:
    """How the user's behaviors and physiology move together, day to day."""
    table = _daily_metric_table(bundle, start, end)
    days = sorted(table)

    def pairs(metric_x: str, metric_y: str, *, lag_days: int = 0) -> list[tuple[float, float]]:
        out = []
        for day in days:
            other = day + timedelta(days=lag_days)
            x = table.get(day, {}).get(metric_x)
            y = table.get(other, {}).get(metric_y)
            if x is not None and y is not None:
                out.append((float(x), float(y)))
        return out

    correlations = [
        describe_correlation(
            "day strain", "next-morning recovery", pairs("strain", "recovery", lag_days=1)
        ),
        describe_correlation(
            "sleep duration (hours)", "same-morning recovery", pairs("sleep_hours", "recovery")
        ),
        describe_correlation(
            "sleep consistency", "recovery", pairs("sleep_consistency", "recovery")
        ),
        describe_correlation(
            "day strain", "that night's sleep duration", pairs("strain", "sleep_hours", lag_days=1)
        ),
        describe_correlation("HRV", "recovery", pairs("hrv", "recovery")),
    ]
    notes = [
        "Correlation is not causation; treat these as hypotheses to test.",
    ]
    if bundle.truncated:
        notes.append(f"Some collections were truncated: {', '.join(bundle.truncated)}.")
    return prune(
        {
            "period": _period(start, end),
            "days_analyzed": len(days),
            "correlations": [c for c in correlations if c is not None],
            "notes": notes,
        }
    )


# ---------------------------------------------------------- personal records


def build_personal_records(bundle: Bundle, start: date, end: date) -> dict[str, Any]:
    """Bests, worsts, streaks, and totals across the window."""
    table = _daily_metric_table(bundle, start, end)

    def extreme(metric: str, *, best: bool) -> dict[str, Any] | None:
        rows = [(d, r[metric]) for d, r in table.items() if r.get(metric) is not None]
        if not rows:
            return None
        day, value = (max if best else min)(rows, key=lambda pair: pair[1])
        return {"date": str(day), "value": rounded(value, 1)}

    # Green-recovery streaks (>= 67%) - strictly consecutive calendar days; a
    # day without recovery data breaks the streak rather than bridging it.
    longest_streak = 0
    streak = 0
    previous_day: date | None = None
    last_recovery_day: date | None = None
    for day in sorted(table):
        recovery = table[day].get("recovery")
        if recovery is None:
            continue
        if recovery >= 67:
            adjacent = previous_day is not None and (day - previous_day).days == 1
            streak = streak + 1 if (adjacent and streak > 0) else 1
            longest_streak = max(longest_streak, streak)
        else:
            streak = 0
        previous_day = day
        last_recovery_day = day
    # "Current" only counts if the run reaches the end of the window (allowing
    # one day of slack for a not-yet-scored morning).
    current_streak = (
        streak if last_recovery_day is not None and (end - last_recovery_day).days <= 1 else 0
    )

    best_workout = None
    scored_workouts = [
        w
        for w in bundle.workouts
        if w.get("score_state") == SCORED
        and (w.get("score") or {}).get("strain") is not None
        and (d := workout_date(w))
        and start <= d <= end
    ]
    if scored_workouts:
        top = max(scored_workouts, key=lambda w: w["score"]["strain"])
        best_workout = {
            "sport": top.get("sport_name") or "workout",
            "date": str(workout_date(top)),
            "strain": rounded(top["score"]["strain"]),
            "calories": kj_to_kcal(top["score"].get("kilojoule")),
            "id": top.get("id"),
        }

    total_distance_m = sum(
        (w.get("score") or {}).get("distance_meter") or 0 for w in scored_workouts
    )
    totals = prune(
        {
            "days_with_data": len(table),
            "workouts": len(
                [w for w in bundle.workouts if (d := workout_date(w)) and start <= d <= end]
            ),
            "calories": sum(r.get("calories") or 0 for r in table.values()) or None,
            "distance_km": rounded(total_distance_m / 1000, 1) if total_distance_m else None,
        }
    )

    return prune(
        {
            "period": _period(start, end),
            "best_recovery": extreme("recovery", best=True),
            "worst_recovery": extreme("recovery", best=False),
            "highest_hrv": extreme("hrv", best=True),
            "lowest_rhr": extreme("rhr", best=False),
            "longest_sleep_hours": extreme("sleep_hours", best=True),
            "highest_strain_day": extreme("strain", best=True),
            "biggest_workout": best_workout,
            "green_streak": {
                "current_days": current_streak,
                "longest_days": longest_streak,
            },
            "totals": totals,
        }
    )


# ------------------------------------------------------------ health overview


def build_health_overview(
    bundle: Bundle, start: date, end: date, *, today: date
) -> dict[str, Any]:
    """The everything-at-once view: where you stand now, how the window
    trended, training load, records, and the strongest correlations."""
    latest = build_daily_summary(bundle, today, today=today)
    recovery = recovery_trends(bundle, start, end)
    sleep = sleep_trends(bundle, start, end)
    strain = strain_trends(bundle, start, end, today=today)

    def compact(trend: dict[str, Any] | None, *keys: str) -> dict[str, Any] | None:
        if not trend:
            return None
        out: dict[str, Any] = {}
        for key in keys:
            block = trend.get(key)
            if not isinstance(block, dict):
                continue
            entry = {"average": block.get("average")}
            if "trend" in block:
                entry["direction"] = block["trend"]["direction"]
            out[key] = entry
        return out or None

    return prune(
        {
            "period": _period(start, end),
            "today": latest,
            "trends": {
                "recovery": compact(
                    recovery, "recovery_score", "hrv_ms", "resting_heart_rate"
                ),
                "sleep": compact(
                    sleep, "sleep_hours", "performance_pct", "consistency_pct", "debt_minutes"
                ),
                "strain": compact(strain, "day_strain", "daily_calories"),
            },
            "training_load": strain.get("training_load"),
            "workouts_by_sport": (strain.get("workouts") or {}).get("by_sport"),
            "records": build_personal_records(bundle, start, end),
            "correlations": build_correlations(bundle, start, end).get("correlations"),
            "notes": [
                "Use get_recovery_trends / get_sleep_trends / get_strain_trends for the "
                "full daily tables behind these numbers."
            ],
        }
    )
