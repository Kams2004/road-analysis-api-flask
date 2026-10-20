"""GET /stats/dashboard — pure helpers: period windows, trend buckets and
the hazard risk-level rule (no database)."""
from datetime import datetime

from app.modules.m02_dashboard.routes.stats import bucket_floor, bucket_starts, hazard_risk_level, period_window


NOW = datetime(2026, 10, 14, 15, 30)   # a Wednesday


def test_period_window_previous_is_same_length():
    start, prev, unit = period_window("7d", NOW)
    assert (NOW - start) == (start - prev)
    assert unit == "day"
    assert period_window("90d", NOW)[2] == "week"
    assert period_window("12m", NOW)[2] == "month"


def test_bucket_floor_matches_date_trunc():
    assert bucket_floor(NOW, "day") == datetime(2026, 10, 14)
    assert bucket_floor(NOW, "week") == datetime(2026, 10, 12)   # Monday
    assert bucket_floor(NOW, "month") == datetime(2026, 10, 1)


def test_bucket_starts_cover_window():
    start, _, unit = period_window("7d", NOW)
    days = bucket_starts(start, NOW, unit)
    assert len(days) == 8 and days[0] == datetime(2026, 10, 7) and days[-1] == datetime(2026, 10, 14)
    start, _, unit = period_window("12m", NOW)
    months = bucket_starts(start, NOW, unit)
    assert months[0] == datetime(2025, 10, 1) and months[-1] == datetime(2026, 10, 1)
    assert len(months) == 13
    start, _, unit = period_window("90d", NOW)
    weeks = bucket_starts(start, NOW, unit)
    assert all(w.weekday() == 0 for w in weeks)


def test_hazard_risk_level_rule():
    assert hazard_risk_level(5, 0.4) == "very_high"
    assert hazard_risk_level(3, 0.9) == "very_high"
    assert hazard_risk_level(3, 0.5) == "high"
    assert hazard_risk_level(1, 0.82) == "high"
    assert hazard_risk_level(2, 0.3) == "medium"
    assert hazard_risk_level(1, 0.65) == "medium"
    assert hazard_risk_level(1, 0.4) == "low"
    assert hazard_risk_level(0, None) == "low"
