"""Server-side analytics over WHOOP records.

Doing the statistics here instead of handing the model raw records buys two
things: far fewer tokens, and arithmetic that is actually correct. Two rules
this module is strict about:

* Series are always sorted by date before any trend math. WHOOP collections
  arrive newest-first; regressing over arrival order silently flips every
  trend direction.
* Direction labels respect metric polarity. A rising HRV is improvement; a
  rising resting heart rate is not. Strain is neutral — more is not better
  or worse, so it gets "increasing/decreasing" rather than a judgment.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

# +1: higher is better, -1: lower is better, 0: neutral (no value judgment)
METRIC_POLARITY: dict[str, int] = {
    "recovery_score": 1,
    "hrv_ms": 1,
    "resting_heart_rate": -1,
    "sleep_hours": 1,
    "sleep_performance_pct": 1,
    "sleep_efficiency_pct": 1,
    "sleep_consistency_pct": 1,
    "sleep_debt_minutes": -1,
    "strain": 0,
    "calories": 0,
    "workouts": 0,
}

STABLE_THRESHOLD = 0.05  # relative change across the window below this = "stable"


@dataclass(frozen=True)
class DayValue:
    day: date
    value: float


def linear_regression(points: Sequence[tuple[float, float]]) -> tuple[float, float, float]:
    """Closed-form least squares. Returns (slope, intercept, r_squared)."""
    n = len(points)
    if n < 2:
        return 0.0, points[0][1] if points else 0.0, 0.0
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    ss_xx = sum((x - mean_x) ** 2 for x in xs)
    ss_xy = sum((x - mean_x) * (y - mean_y) for x, y in points)
    if ss_xx == 0:
        return 0.0, mean_y, 0.0
    slope = ss_xy / ss_xx
    intercept = mean_y - slope * mean_x
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    if ss_tot == 0:
        return slope, intercept, 1.0
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in points)
    return slope, intercept, max(0.0, 1.0 - ss_res / ss_tot)


def describe_series(metric: str, series: list[DayValue]) -> dict[str, Any] | None:
    """Stats + trend direction + anomalies for one metric's daily series."""
    cleaned = sorted((p for p in series if p.value is not None), key=lambda p: p.day)
    if not cleaned:
        return None
    values = [p.value for p in cleaned]
    summary: dict[str, Any] = {
        "days_with_data": len(values),
        "average": round(statistics.fmean(values), 2),
        "median": round(statistics.median(values), 2),
        "min": round(min(values), 2),
        "max": round(max(values), 2),
    }
    if len(values) >= 2:
        summary["stdev"] = round(statistics.stdev(values), 2)

    if len(cleaned) >= 3:
        origin = cleaned[0].day
        points = [((p.day - origin).days, p.value) for p in cleaned]
        slope, _, r_squared = linear_regression(points)
        span_days = max(points[-1][0], 1)
        summary["trend"] = {
            "direction": _direction(metric, slope, span_days, summary["average"]),
            "slope_per_day": round(slope, 4),
            "change_over_period": round(slope * span_days, 2),
            "confidence": _confidence(r_squared),
            "r_squared": round(r_squared, 3),
        }

    anomalies = _anomalies(cleaned)
    if anomalies:
        summary["unusual_days"] = anomalies
    return summary


def _direction(metric: str, slope: float, span_days: int, mean: float) -> str:
    polarity = METRIC_POLARITY.get(metric, 0)
    change = slope * span_days
    if mean:
        relative = abs(change) / abs(mean)
    else:
        # Series averaging ~0 (e.g. sleep debt): any real slope is a trend.
        relative = float("inf") if change else 0.0
    if relative < STABLE_THRESHOLD:
        return "stable"
    rising = slope > 0
    if polarity == 0:
        return "increasing" if rising else "decreasing"
    improving = (polarity > 0) == rising
    return "improving" if improving else "declining"


def _confidence(r_squared: float) -> str:
    if r_squared >= 0.6:
        return "high"
    if r_squared >= 0.3:
        return "medium"
    return "low"


def _anomalies(series: list[DayValue], sigma: float = 2.0) -> list[dict[str, Any]]:
    if len(series) < 5:
        return []
    values = [p.value for p in series]
    mean = statistics.fmean(values)
    stdev = statistics.stdev(values)
    if stdev == 0:
        return []
    flagged = [
        {
            "date": str(p.day),
            "value": round(p.value, 2),
            "deviation": f"{'+' if p.value > mean else '-'}{abs(p.value - mean) / stdev:.1f}σ",
        }
        for p in series
        if abs(p.value - mean) > sigma * stdev
    ]
    return flagged[:10]


def average(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return round(statistics.fmean(present), 2) if present else None


def acute_chronic_ratio(daily_strain: dict[date, float], today: date) -> dict[str, Any] | None:
    """Acute (7-day) vs chronic (28-day) average daily strain.

    A common overtraining heuristic: ratios well above ~1.3 mean the recent
    week is much harder than the body is conditioned for.
    """
    last_7 = [v for d, v in daily_strain.items() if 0 <= (today - d).days < 7]
    last_28 = [v for d, v in daily_strain.items() if 0 <= (today - d).days < 28]
    if len(last_28) < 14 or not last_7:
        return None
    acute = statistics.fmean(last_7)
    chronic = statistics.fmean(last_28)
    if chronic == 0:
        return None
    ratio = acute / chronic
    if ratio >= 1.5:
        note = "much higher load than usual — elevated injury/overreach risk"
    elif ratio >= 1.2:
        note = "training load is ramping up faster than your 4-week base"
    elif ratio <= 0.8:
        note = "lighter week than your recent base — good for recovery, watch detraining"
    else:
        note = "training load is balanced against your 4-week base"
    return {
        "acute_7d_avg_strain": round(acute, 2),
        "chronic_28d_avg_strain": round(chronic, 2),
        "ratio": round(ratio, 2),
        "interpretation": note,
    }


def compare_metric(
    metric: str, value_a: float | None, value_b: float | None, *, deadband: float = 0.03
) -> dict[str, Any] | None:
    """Compare metric between period A (earlier/baseline) and period B (later)."""
    if value_a is None or value_b is None:
        return None
    change = value_b - value_a
    pct = (change / abs(value_a) * 100) if value_a else None
    polarity = METRIC_POLARITY.get(metric, 0)
    if change == 0 or (pct is not None and abs(pct) <= deadband * 100):
        assessment = "unchanged"
    elif polarity == 0:
        assessment = "increased" if change > 0 else "decreased"
    else:
        assessment = "improved" if (change > 0) == (polarity > 0) else "declined"
    return {
        "period_a": value_a,
        "period_b": value_b,
        "change": round(change, 2),
        "change_pct": round(pct, 1) if pct is not None else None,
        "assessment": assessment,
    }
