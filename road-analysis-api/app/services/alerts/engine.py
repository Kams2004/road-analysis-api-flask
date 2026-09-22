"""
Step 5 core: (Step 4 VehicleConflict, previous ConflictAlert or None, now) ->
new ConflictAlert, plus the standalone resolution path for a pair that has
stopped appearing in either vehicle's conflict list at all. Pure, no I/O —
the stateful parts (reading/writing the previous alert, tracking which
pairs were active last tick so a disappearance can be detected) live in
app/services/alert_service.py.

In normal operation this is only ever called with conflicts Step 4 already
filtered to a non-NONE risk_level (see collision_service.compute_and_store),
so the "still safe, never alerted" case below is a defensive/degenerate
path, not the common one — it exists so this function is correct on its own
terms without relying on callers to pre-filter perfectly.
"""
import uuid
from datetime import datetime
from typing import Optional

from app.core.config import settings
from app.services.alerts.models import (
    AlertLevel,
    AlertLifecycleStatus,
    AlertTransition,
    ConflictAlert,
    RecommendedAction,
)
from app.services.collision.models import ConflictRiskLevel, ConflictStatus, VehicleConflict

LEVEL_SEVERITY = {
    AlertLevel.SAFE: 0,
    AlertLevel.MONITOR: 1,
    AlertLevel.WARNING: 2,
    AlertLevel.HIGH_WARNING: 3,
    AlertLevel.CRITICAL: 4,
}

_RECOMMENDED_ACTION = {
    AlertLevel.SAFE: RecommendedAction.NONE,
    AlertLevel.MONITOR: RecommendedAction.MONITOR,
    AlertLevel.WARNING: RecommendedAction.ADVISE_CAUTION,
    AlertLevel.HIGH_WARNING: RecommendedAction.IMMEDIATE_DRIVER_WARNING,
    AlertLevel.CRITICAL: RecommendedAction.EMERGENCY_ALERT,
}

# Step 4's own risk_level acts as a floor so the two classification layers
# never disagree in a way that understates urgency.
_RISK_LEVEL_FLOOR = {
    ConflictRiskLevel.NONE: AlertLevel.SAFE,
    ConflictRiskLevel.LOW: AlertLevel.MONITOR,
    ConflictRiskLevel.MODERATE: AlertLevel.WARNING,
    ConflictRiskLevel.HIGH: AlertLevel.HIGH_WARNING,
    ConflictRiskLevel.CRITICAL: AlertLevel.CRITICAL,
}


def classify_level(conflict: VehicleConflict) -> AlertLevel:
    """Primarily TTC-banded, per spec — with Step 4's distance-derived
    risk_level as a floor (see module docstring)."""
    floor = _RISK_LEVEL_FLOOR.get(conflict.risk_level, AlertLevel.SAFE)

    ttc = conflict.time_to_closest_approach_s
    if conflict.risk_level == ConflictRiskLevel.NONE or ttc is None:
        return floor

    if ttc <= settings.ALERT_HIGH_WARNING_TTC_S:
        ttc_level = AlertLevel.CRITICAL
    elif ttc <= settings.ALERT_WARNING_TTC_S:
        ttc_level = AlertLevel.HIGH_WARNING
    elif ttc <= settings.ALERT_SAFE_TTC_S:
        ttc_level = AlertLevel.WARNING
    else:
        ttc_level = AlertLevel.MONITOR

    return ttc_level if LEVEL_SEVERITY[ttc_level] >= LEVEL_SEVERITY[floor] else floor


def _new_conflict_id(now: datetime) -> str:
    return f"CF-{now.strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"


def _reason_text(conflict: VehicleConflict, level: AlertLevel) -> str:
    if level == AlertLevel.SAFE:
        return "no significant predicted conflict"
    parts = []
    if conflict.segment_overlap_ahead or conflict.same_current_segment:
        parts.append("shared road segment")
    heading = conflict.relative_heading_deg
    if heading is not None and heading >= 135:
        parts.append("opposing trajectories predicted to converge")
    elif heading is not None and 45 <= heading < 135:
        parts.append("crossing trajectories predicted to converge")
    if not parts:
        parts.append("predicted trajectories converge within the prediction horizon")
    return "; ".join(parts).capitalize()


def evaluate(
    conflict: VehicleConflict,
    previous: Optional[ConflictAlert],
    now: datetime,
) -> ConflictAlert:
    """Given the current Step 4 assessment for a pair and its previous alert
    state (None if this pair has never been alerted), decide the new alert
    record and what changed this tick."""
    level = classify_level(conflict)
    was_active = previous is not None and previous.status != AlertLifecycleStatus.RESOLVED

    if level == AlertLevel.SAFE:
        status = AlertLifecycleStatus.RESOLVED
        transition = AlertTransition.RESOLVED if was_active else AlertTransition.UNCHANGED
    elif not was_active:
        status = AlertLifecycleStatus.DETECTED
        transition = AlertTransition.NEW
    else:
        status = AlertLifecycleStatus.ACTIVE
        prev_severity = LEVEL_SEVERITY[previous.level]
        cur_severity = LEVEL_SEVERITY[level]
        if cur_severity > prev_severity:
            transition = AlertTransition.ESCALATED
        elif cur_severity < prev_severity:
            transition = AlertTransition.DE_ESCALATED
        else:
            transition = AlertTransition.UNCHANGED

    conflict_id = previous.conflict_id if was_active and previous is not None else _new_conflict_id(now)
    first_detected_at = previous.first_detected_at if was_active and previous is not None else now
    resolved_at = now if status == AlertLifecycleStatus.RESOLVED else (
        previous.resolved_at if previous is not None else None
    )

    return ConflictAlert(
        conflict_id=conflict_id,
        vehicle_a_id=conflict.vehicle_a_id,
        vehicle_b_id=conflict.vehicle_b_id,
        status=status,
        level=level,
        transition=transition,
        time_to_conflict_s=conflict.time_to_closest_approach_s,
        minimum_predicted_distance_m=conflict.distance_at_closest_approach_m,
        conflict_location=conflict.conflict_point,
        relative_speed_mps=conflict.relative_speed_mps,
        closing_speed_mps=conflict.closing_speed_mps,
        relative_heading_deg=conflict.relative_heading_deg,
        road_type=conflict.road_type_a or conflict.road_type_b,
        same_current_segment=conflict.same_current_segment,
        segment_overlap_ahead=conflict.segment_overlap_ahead,
        map_match_confidence=conflict.confidence,
        telemetry_stale=(conflict.status == ConflictStatus.DEGRADED),
        reason=_reason_text(conflict, level),
        recommended_action=_RECOMMENDED_ACTION[level],
        first_detected_at=first_detected_at,
        last_updated_at=now,
        resolved_at=resolved_at,
    )


def resolve(previous: ConflictAlert, now: datetime) -> ConflictAlert:
    """A pair that was previously alerted no longer appears in either
    vehicle's current conflict list at all (trajectories diverged, one
    vehicle dropped off, etc.) — explicit resolution, since there's no fresh
    VehicleConflict to derive one from."""
    if previous.status == AlertLifecycleStatus.RESOLVED:
        return previous.model_copy(update={"transition": AlertTransition.UNCHANGED, "last_updated_at": now})
    return previous.model_copy(update={
        "status": AlertLifecycleStatus.RESOLVED,
        "level": AlertLevel.SAFE,
        "transition": AlertTransition.RESOLVED,
        "reason": "trajectories no longer predicted to conflict",
        "recommended_action": RecommendedAction.NONE,
        "last_updated_at": now,
        "resolved_at": now,
    })
