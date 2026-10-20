"""
Module 6 — detection state machine, confidence routing and hazard
assignment. Pure rules only (same convention as test_alerts.py): the DB
wrappers in detection_workflow / hazard_service are exercised live.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.services.detection_workflow import (
    TRANSITIONS, accept_target, initial_status, is_allowed, location_status_for, reopen_target, thresholds_for,
)
from app.services.hazard_service import aggregate, area_radius, hazard_code, pick_hazard
from app.services.settings_service import ValidationSettings

# ~111 m per 0.001° of latitude
YAOUNDE = (3.8480, 11.5021)


# ── state machine ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("frm,to,action", [
    ("location_pending", "pending", "location_fix"),
    ("location_pending", "rejected", "reject"),
    ("pending", "pending", "location_fix"),
    ("pending", "validated", "accept"),
    ("pending", "corrected", "correct"),
    ("pending", "corrected", "accept"),
    ("pending", "rejected", "reject"),
    ("validated", "resolved", "resolve"),
    ("corrected", "resolved", "resolve"),
    ("resolved", "validated", "unresolve"),
    ("validated", "pending", "reopen"),
    ("corrected", "location_pending", "reopen"),
    ("rejected", "pending", "reopen"),
    ("resolved", "pending", "reopen"),
])
def test_allowed_transitions(frm, to, action):
    assert is_allowed(frm, to, action)


@pytest.mark.parametrize("frm,to,action", [
    ("location_pending", "validated", "accept"),   # location must be confirmed first
    ("location_pending", "corrected", "correct"),
    ("validated", "rejected", "reject"),           # finalized: only reopen
    ("rejected", "validated", "accept"),
    ("pending", "resolved", "resolve"),
    ("rejected", "resolved", "resolve"),
    ("pending", "pending", "reopen"),              # nothing to reopen
    ("location_pending", "pending", "reopen"),
    ("validated", "pending", "location_fix"),      # location fixes only before the decision
    ("validated", "validated", "accept"),
    (None, "pending", "accept"),
])
def test_refused_transitions(frm, to, action):
    assert not is_allowed(frm, to, action)


def test_reopen_is_the_only_way_out_of_a_final_state():
    for (frm, to), actions in TRANSITIONS.items():
        if frm in ("validated", "corrected", "rejected") and to in ("pending", "location_pending"):
            assert actions == {"reopen"}


def test_reopen_target_depends_on_location():
    assert reopen_target("confirmed") == "pending"
    assert reopen_target("approximate") == "location_pending"
    assert reopen_target("missing") == "location_pending"
    assert reopen_target(None) == "location_pending"


def test_accept_after_manual_location_fix_counts_as_corrected():
    assert accept_target("ocr") == "validated"
    assert accept_target("interpolated") == "validated"   # proposed position accepted as-is
    for src in ("map_pin", "trajectory", "ocr_text"):
        assert accept_target(src) == "corrected"


# ── creation rules ───────────────────────────────────────────────────────────

def test_location_status_for():
    assert location_status_for(None, None, False) == ("missing", "none")
    assert location_status_for(3.8, None, False) == ("missing", "none")
    assert location_status_for(3.8, 11.5, True) == ("approximate", "interpolated")
    assert location_status_for(3.8, 11.5, False) == ("confirmed", "ocr")


def test_default_routing_rejects_low_confidence_only():
    rules = ValidationSettings()
    assert initial_status(0.29, "confirmed", "pothole", rules) == ("rejected", "auto_reject", "low_confidence")
    assert initial_status(0.29, "missing", "pothole", rules) == ("rejected", "auto_reject", "low_confidence")
    assert initial_status(0.30, "confirmed", "pothole", rules) == ("pending", "create", None)
    assert initial_status(0.99, "confirmed", "pothole", rules) == ("pending", "create", None)  # auto-accept off
    assert initial_status(0.99, "approximate", "pothole", rules) == ("location_pending", "create", None)
    assert initial_status(0.50, "missing", "pothole", rules) == ("location_pending", "create", None)


def test_auto_accept_needs_confirmed_location():
    rules = ValidationSettings(auto_accept_above=0.9)
    assert initial_status(0.9, "confirmed", "pothole", rules) == ("validated", "auto_accept", None)
    assert initial_status(0.95, "approximate", "pothole", rules) == ("location_pending", "create", None)
    assert initial_status(0.89, "confirmed", "pothole", rules) == ("pending", "create", None)


def test_type_overrides_win_and_none_inherits():
    rules = ValidationSettings(
        auto_reject_below=0.3, auto_accept_above=0.95,
        type_overrides={"traffic_sign": {"auto_reject_below": 0.5},
                        "speed_bump": {"auto_accept_above": 0.8}},
    )
    assert thresholds_for(rules, "traffic_sign") == (0.5, 0.95)
    assert thresholds_for(rules, "speed_bump") == (0.3, 0.8)
    assert thresholds_for(rules, "pothole") == (0.3, 0.95)
    assert initial_status(0.45, "confirmed", "traffic_sign", rules)[0] == "rejected"
    assert initial_status(0.45, "confirmed", "pothole", rules)[0] == "pending"
    assert initial_status(0.85, "confirmed", "speed_bump", rules)[0] == "validated"


def test_validation_settings_bounds():
    with pytest.raises(ValidationError):
        ValidationSettings(auto_reject_below=1.5)
    with pytest.raises(ValidationError):
        ValidationSettings(clip_seconds=0)
    with pytest.raises(ValidationError):
        ValidationSettings(auto_reject_below=0.5, auto_accept_above=0.4)
    assert ValidationSettings().clip_seconds == 5
    assert ValidationSettings().distinct_subtype_types == ["traffic_sign", "parked_vehicle"]


# ── hazard assignment ────────────────────────────────────────────────────────

def _area(name, lat, lon, area_km, radius_m, type_radius=None, active=True):
    return SimpleNamespace(name=name, center_lat=lat, center_lon=lon, area_radius_km=area_km,
                           cluster_radius_m=radius_m, type_radius=type_radius, is_active=active)


def _hazard(code, type, lat, lon, subtype=None):
    return SimpleNamespace(code=code, type=type, subtype=subtype, latitude=lat, longitude=lon)


def test_area_radius_smallest_matching_area_wins():
    lat, lon = YAOUNDE
    areas = [
        _area("Centre region", lat, lon, 100, 80),
        _area("Yaoundé", lat, lon, 15, 30, {"pothole": 20}),
        _area("Douala", 4.05, 9.70, 20, 25),
        _area("Disabled", lat, lon, 1, 5, active=False),
    ]
    assert area_radius(areas, lat + 0.01, lon, "pothole") == 20       # per-type override
    assert area_radius(areas, lat + 0.01, lon, "speed_bump") == 30
    assert area_radius(areas, lat + 0.5, lon, "pothole") == 80        # ~55 km out: region only
    assert area_radius(areas, 10.0, 14.0, "pothole") is None          # nowhere → global default


def test_pick_nearest_same_type_within_radius():
    lat, lon = YAOUNDE
    hazards = [
        _hazard("HZ-1", "pothole", lat + 0.0003, lon),      # ~33 m
        _hazard("HZ-2", "pothole", lat + 0.0001, lon),      # ~11 m
        _hazard("HZ-3", "speed_bump", lat, lon),            # other type
    ]
    assert pick_hazard(hazards, lat, lon, "pothole", None, 50, ["traffic_sign"]).code == "HZ-2"
    assert pick_hazard(hazards, lat, lon, "pothole", None, 5, ["traffic_sign"]) is None
    assert pick_hazard(hazards, lat, lon, "speed_bump", None, 50, ["traffic_sign"]).code == "HZ-3"
    assert pick_hazard(hazards, lat, lon, "traffic_sign", "stop", 50, ["traffic_sign"]) is None


def test_distinct_signs_close_together_stay_separate():
    """RRAP-06-011: a stop sign and a speed-limit sign 10 m apart are two hazards."""
    lat, lon = YAOUNDE
    hazards = [_hazard("HZ-STOP", "traffic_sign", lat, lon, "stop")]
    near = lat + 0.00009  # ~10 m
    assert pick_hazard(hazards, near, lon, "traffic_sign", "stop", 50, ["traffic_sign"]).code == "HZ-STOP"
    assert pick_hazard(hazards, near, lon, "traffic_sign", "speed_limit_50", 50, ["traffic_sign"]) is None
    # When the type is not listed, the subtype is ignored.
    assert pick_hazard(hazards, near, lon, "traffic_sign", "speed_limit_50", 50, []).code == "HZ-STOP"


def test_aggregate_members():
    t0 = datetime(2026, 10, 1, 8, 0)
    members = [
        SimpleNamespace(latitude=3.0, longitude=11.0, confidence=0.6, captured_at=t0, created_at=t0,
                        location_name="Rue A, Yaoundé", city="Yaoundé", country="Cameroon"),
        SimpleNamespace(latitude=3.002, longitude=11.002, confidence=0.9, captured_at=None,
                        created_at=t0 + timedelta(days=2), location_name=None, city=None, country=None),
    ]
    agg = aggregate(members)
    assert agg["latitude"] == pytest.approx(3.001)
    assert agg["longitude"] == pytest.approx(11.001)
    assert agg["detection_count"] == 2
    assert agg["avg_confidence"] == 0.75
    assert agg["max_confidence"] == 0.9
    assert agg["first_detected_at"] == t0
    assert agg["last_detected_at"] == t0 + timedelta(days=2)
    assert (agg["city"], agg["country"]) == ("Yaoundé", "Cameroon")


def test_hazard_code_format():
    assert hazard_code(1) == "HZ-000001"
    assert hazard_code(123456) == "HZ-123456"
