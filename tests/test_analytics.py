import random
from datetime import date, timedelta

import pytest

from whoop_mcp.analytics import (
    DayValue,
    acute_chronic_ratio,
    compare_metric,
    describe_series,
    linear_regression,
)

D0 = date(2026, 5, 1)


def series(values: list[float]) -> list[DayValue]:
    return [DayValue(D0 + timedelta(days=i), v) for i, v in enumerate(values)]


def test_linear_regression_exact_fit():
    slope, intercept, r2 = linear_regression([(0, 1.0), (1, 3.0), (2, 5.0)])
    assert slope == 2.0
    assert intercept == 1.0
    assert r2 == 1.0


def test_linear_regression_degenerate_cases():
    assert linear_regression([]) == (0.0, 0.0, 0.0)
    assert linear_regression([(0, 5.0)]) == (0.0, 5.0, 0.0)
    slope, _, _ = linear_regression([(0, 1.0), (0, 2.0)])
    assert slope == 0.0


def test_rising_recovery_is_improving():
    out = describe_series("recovery_score", series([50, 55, 60, 65, 70, 75, 80]))
    assert out["trend"]["direction"] == "improving"
    assert out["trend"]["confidence"] == "high"


def test_rising_resting_heart_rate_is_declining():
    # Same rising shape, but for RHR higher is worse - polarity must flip it.
    out = describe_series("resting_heart_rate", series([50, 52, 54, 56, 58, 60, 62]))
    assert out["trend"]["direction"] == "declining"


def test_strain_is_neutral_language():
    out = describe_series("strain", series([8, 9, 10, 11, 12, 13, 14]))
    assert out["trend"]["direction"] == "increasing"


def test_input_order_does_not_change_trend():
    # WHOOP returns newest-first; a regression over arrival order would flip
    # the sign. describe_series must sort by date first.
    points = series([50, 55, 60, 65, 70, 75, 80])
    shuffled = points[::-1]
    random.Random(7).shuffle(shuffled)
    ordered = describe_series("recovery_score", points)
    scrambled = describe_series("recovery_score", shuffled)
    assert scrambled == ordered
    assert scrambled["trend"]["direction"] == "improving"


def test_flat_series_is_stable():
    out = describe_series("recovery_score", series([70, 71, 70, 69, 70, 71, 70]))
    assert out["trend"]["direction"] == "stable"


def test_anomaly_detection():
    values = [70.0] * 10 + [20.0] + [70.0] * 10
    out = describe_series("recovery_score", series(values))
    unusual = out["unusual_days"]
    assert len(unusual) == 1
    assert unusual[0]["value"] == 20.0
    assert unusual[0]["deviation"].startswith("-")


def test_describe_series_empty_and_single():
    assert describe_series("recovery_score", []) is None
    out = describe_series("recovery_score", series([66]))
    assert out["average"] == 66
    assert "trend" not in out


def test_acute_chronic_ratio():
    today = date(2026, 6, 12)
    daily = {today - timedelta(days=i): 10.0 for i in range(28)}
    for i in range(7):  # much harder recent week
        daily[today - timedelta(days=i)] = 16.0
    out = acute_chronic_ratio(daily, today)
    assert out["acute_7d_avg_strain"] == 16.0
    assert out["ratio"] > 1.3
    assert "risk" in out["interpretation"] or "ramping" in out["interpretation"]


def test_acute_chronic_needs_enough_data():
    today = date(2026, 6, 12)
    daily = {today - timedelta(days=i): 10.0 for i in range(10)}
    assert acute_chronic_ratio(daily, today) is None


def test_compare_metric_polarity_and_deadband():
    out = compare_metric("resting_heart_rate", 50.0, 55.0)
    assert out["assessment"] == "declined"  # RHR up = worse
    out = compare_metric("recovery_score", 50.0, 55.0)
    assert out["assessment"] == "improved"
    out = compare_metric("recovery_score", 50.0, 50.5)
    assert out["assessment"] == "unchanged"
    out = compare_metric("strain", 10.0, 14.0)
    assert out["assessment"] == "increased"
    assert compare_metric("strain", None, 14.0) is None


def test_pearson_known_values():
    from whoop_mcp.analytics import pearson

    assert pearson([(1, 2), (2, 4), (3, 6)]) == pytest.approx(1.0)
    assert pearson([(1, 6), (2, 4), (3, 2)]) == pytest.approx(-1.0)
    assert pearson([(1, 1)]) is None
    assert pearson([(1, 5), (2, 5), (3, 5)]) is None  # no variance in y


def test_describe_correlation_labels():
    from whoop_mcp.analytics import describe_correlation

    pairs = [(float(i), float(i) * 2 + (i % 3)) for i in range(20)]
    out = describe_correlation("strain", "recovery", pairs)
    assert out["strength"] == "strong"
    assert out["r"] > 0.9
    assert "higher recovery" in out["interpretation"]

    sparse = describe_correlation("a", "b", [(1.0, 2.0)] * 3)
    assert "Not enough" in sparse["note"]


def test_compare_metric_zero_baseline():
    # 0 → 0 must be "unchanged", not a direction (pct is undefined at 0).
    assert compare_metric("workouts", 0, 0)["assessment"] == "unchanged"
    assert compare_metric("strain", 0.0, 0.0)["assessment"] == "unchanged"
    # 0 → something still gets a direction even though pct is undefined.
    out = compare_metric("workouts", 0, 5)
    assert out["assessment"] == "increased"
    assert out["change_pct"] is None
