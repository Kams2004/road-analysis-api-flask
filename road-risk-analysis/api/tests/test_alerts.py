"""
Step 5 (prevention & alert engine) scenarios. Fully deterministic — built
directly against app.services.alerts.engine with VehicleConflict fixtures;
no Redis/network involved (that's alert_service.py's orchestration concern,
live-verified separately, same pattern as Steps 2-4).
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import settings
from app.services.alerts import engine
from app.services.alerts.models import AlertLifecycleStatus, AlertTransition, AlertLevel, RecommendedAction
from app.services.collision.models import ConflictRiskLevel, ConflictStatus, VehicleConflict

NOW = datetime.now(timezone.utc)


def make_conflict(
    risk_level=ConflictRiskLevel.CRITICAL,
    ttc=2.0,
    distance=10.0,
    status=ConflictStatus.ACTIVE,
    confidence=0.9,
    vehicle_a_id="A",
    vehicle_b_id="B",
    **kw,
) -> VehicleConflict:
    return VehicleConflict(
        vehicle_a_id=vehicle_a_id,
        vehicle_b_id=vehicle_b_id,
        risk_level=risk_level,
        status=status,
        confidence=confidence,
        time_to_closest_approach_s=ttc,
        distance_at_closest_approach_m=distance,
        **kw,
    )


# ── Lifecycle: new -> unchanged -> escalate -> de-escalate -> resolve ──────

def test_new_conflict_is_detected():
    conflict = make_conflict(ttc=2.0)
    alert = engine.evaluate(conflict, previous=None, now=NOW)

    assert alert.status == AlertLifecycleStatus.DETECTED
    assert alert.transition == AlertTransition.NEW
    assert alert.level == AlertLevel.CRITICAL
    assert alert.conflict_id.startswith("CF-")


def test_same_level_next_tick_does_not_spam():
    conflict = make_conflict(ttc=2.0)
    first = engine.evaluate(conflict, previous=None, now=NOW)
    second = engine.evaluate(conflict, previous=first, now=NOW + timedelta(seconds=3))

    assert second.status == AlertLifecycleStatus.ACTIVE
    assert second.transition == AlertTransition.UNCHANGED
    assert second.conflict_id == first.conflict_id  # same ongoing conflict, not a new one


def test_escalation_from_warning_to_critical():
    warning_conflict = make_conflict(risk_level=ConflictRiskLevel.MODERATE, ttc=10.0)  # -> WARNING band
    first = engine.evaluate(warning_conflict, previous=None, now=NOW)
    assert first.level == AlertLevel.WARNING

    critical_conflict = make_conflict(risk_level=ConflictRiskLevel.CRITICAL, ttc=2.0)
    second = engine.evaluate(critical_conflict, previous=first, now=NOW + timedelta(seconds=3))

    assert second.level == AlertLevel.CRITICAL
    assert second.transition == AlertTransition.ESCALATED
    assert second.conflict_id == first.conflict_id
    assert second.first_detected_at == first.first_detected_at  # origin time preserved through escalation


def test_de_escalation_from_critical_to_warning():
    critical_conflict = make_conflict(risk_level=ConflictRiskLevel.CRITICAL, ttc=2.0)
    first = engine.evaluate(critical_conflict, previous=None, now=NOW)

    warning_conflict = make_conflict(risk_level=ConflictRiskLevel.MODERATE, ttc=10.0)
    second = engine.evaluate(warning_conflict, previous=first, now=NOW + timedelta(seconds=3))

    assert second.level == AlertLevel.WARNING
    assert second.transition == AlertTransition.DE_ESCALATED


def test_resolve_marks_terminal_state():
    conflict = make_conflict(ttc=2.0)
    active = engine.evaluate(conflict, previous=None, now=NOW)

    resolved = engine.resolve(active, now=NOW + timedelta(seconds=5))

    assert resolved.status == AlertLifecycleStatus.RESOLVED
    assert resolved.level == AlertLevel.SAFE
    assert resolved.transition == AlertTransition.RESOLVED
    assert resolved.conflict_id == active.conflict_id
    assert resolved.resolved_at is not None


def test_resolving_an_already_resolved_alert_does_not_spam():
    conflict = make_conflict(ttc=2.0)
    active = engine.evaluate(conflict, previous=None, now=NOW)
    resolved_once = engine.resolve(active, now=NOW + timedelta(seconds=5))

    resolved_again = engine.resolve(resolved_once, now=NOW + timedelta(seconds=10))

    assert resolved_again.transition == AlertTransition.UNCHANGED
    assert resolved_again.status == AlertLifecycleStatus.RESOLVED


# ── conflictId stability / renewal ──────────────────────────────────────────

def test_conflict_id_stable_across_many_unchanged_ticks():
    conflict = make_conflict(ttc=2.0)
    alert = engine.evaluate(conflict, previous=None, now=NOW)
    original_id = alert.conflict_id

    for i in range(5):
        alert = engine.evaluate(conflict, previous=alert, now=NOW + timedelta(seconds=3 * (i + 1)))

    assert alert.conflict_id == original_id


def test_new_conflict_after_resolution_gets_a_fresh_id():
    conflict = make_conflict(ttc=2.0)
    first = engine.evaluate(conflict, previous=None, now=NOW)
    resolved = engine.resolve(first, now=NOW + timedelta(seconds=5))

    # Same vehicle pair conflicts again later — engine.evaluate is called
    # fresh (as alert_service would, since the pair key's stored alert is
    # now RESOLVED and thus "not active" from evaluate's point of view).
    new_conflict_later = engine.evaluate(conflict, previous=resolved, now=NOW + timedelta(minutes=10))

    assert new_conflict_later.transition == AlertTransition.NEW
    assert new_conflict_later.conflict_id != first.conflict_id


# ── TTC banding + Step 4 risk floor ─────────────────────────────────────────

def test_ttc_bands_match_configured_thresholds():
    just_above_safe = make_conflict(risk_level=ConflictRiskLevel.LOW, ttc=settings.ALERT_SAFE_TTC_S + 1)
    assert engine.classify_level(just_above_safe) == AlertLevel.MONITOR

    warning = make_conflict(risk_level=ConflictRiskLevel.LOW, ttc=settings.ALERT_SAFE_TTC_S - 1)
    assert engine.classify_level(warning) == AlertLevel.WARNING

    high_warning = make_conflict(risk_level=ConflictRiskLevel.LOW, ttc=settings.ALERT_WARNING_TTC_S - 1)
    assert engine.classify_level(high_warning) == AlertLevel.HIGH_WARNING

    critical = make_conflict(risk_level=ConflictRiskLevel.LOW, ttc=settings.ALERT_HIGH_WARNING_TTC_S - 1)
    assert engine.classify_level(critical) == AlertLevel.CRITICAL


def test_risk_level_floor_prevents_understating_urgency():
    # A large/coincidental TTC (would be WARNING on its own) but Step 4
    # already says CRITICAL by distance — must not be downgraded.
    conflict = make_conflict(risk_level=ConflictRiskLevel.CRITICAL, ttc=settings.ALERT_SAFE_TTC_S - 1)
    assert engine.classify_level(conflict) == AlertLevel.CRITICAL


def test_no_risk_is_safe():
    conflict = make_conflict(risk_level=ConflictRiskLevel.NONE, ttc=None, distance=None)
    assert engine.classify_level(conflict) == AlertLevel.SAFE


# ── Recommended action mapping ──────────────────────────────────────────────

def test_recommended_action_matches_level():
    critical = engine.evaluate(make_conflict(ttc=2.0), previous=None, now=NOW)
    assert critical.recommended_action == RecommendedAction.EMERGENCY_ALERT

    warning = engine.evaluate(
        make_conflict(risk_level=ConflictRiskLevel.MODERATE, ttc=10.0), previous=None, now=NOW,
    )
    assert warning.recommended_action == RecommendedAction.ADVISE_CAUTION


# ── Degraded/stale conflicts still produce an alert, correctly flagged ─────

def test_degraded_conflict_flagged_as_telemetry_stale():
    conflict = make_conflict(ttc=2.0, status=ConflictStatus.DEGRADED)
    alert = engine.evaluate(conflict, previous=None, now=NOW)
    assert alert.telemetry_stale is True


def test_relative_motion_fields_passed_through():
    conflict = make_conflict(
        ttc=2.0,
        relative_speed_mps=1.5,
        closing_speed_mps=18.0,
        relative_heading_deg=176.0,
        road_type_a="residential",
    )
    alert = engine.evaluate(conflict, previous=None, now=NOW)

    assert alert.relative_speed_mps == 1.5
    assert alert.closing_speed_mps == 18.0
    assert alert.relative_heading_deg == 176.0
    assert alert.road_type == "residential"
