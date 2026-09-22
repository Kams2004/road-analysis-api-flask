"""
Data model for Step 5 — the collision prevention & alert engine: turns a
Step 4 VehicleConflict *snapshot* into a *stateful* alert with a lifecycle,
so a consumer gets one evolving event per conflict instead of an
indistinguishable fresh warning on every single ping.

Scope, deliberately: this decides WHAT the alert is and WHETHER it changed
enough to be worth telling someone about — it does not decide HOW a human
finds out (mobile push, SMS, a BeSafe device beep, a fleet dashboard...).
That is a NotificationAdapter's job (see notifier.py), kept behind an
interface on purpose so the collision/alert engine never depends on the
mobile app or any specific delivery channel — only a logging adapter exists
here; wiring a real one (Expo push, BeSafe, etc.) is future work.
"""
import enum
from datetime import datetime
from typing import Optional, Tuple

from pydantic import BaseModel


class AlertLevel(str, enum.Enum):
    """Severity — primarily TTC-banded (settings.ALERT_*_TTC_S) with Step 4's
    distance-based risk_level as a floor, see engine.classify_level."""
    SAFE = "SAFE"
    MONITOR = "MONITOR"
    WARNING = "WARNING"
    HIGH_WARNING = "HIGH_WARNING"
    CRITICAL = "CRITICAL"


class AlertLifecycleStatus(str, enum.Enum):
    """Where this specific conflictId is in its life — independent of how
    severe it currently is (see `level` for that)."""
    DETECTED = "DETECTED"  # first tick this pair crossed above SAFE
    ACTIVE = "ACTIVE"      # ongoing; may be escalating or de-escalating between ticks
    RESOLVED = "RESOLVED"  # the pair no longer conflicts — terminal


class AlertTransition(str, enum.Enum):
    """What changed on *this specific tick* — the signal a
    NotificationAdapter should actually act on, so an ongoing, unchanged
    alert doesn't get re-pushed to a driver every few seconds."""
    NEW = "NEW"
    ESCALATED = "ESCALATED"
    DE_ESCALATED = "DE_ESCALATED"
    UNCHANGED = "UNCHANGED"
    RESOLVED = "RESOLVED"


class RecommendedAction(str, enum.Enum):
    NONE = "NONE"
    MONITOR = "MONITOR"
    ADVISE_CAUTION = "ADVISE_CAUTION"
    IMMEDIATE_DRIVER_WARNING = "IMMEDIATE_DRIVER_WARNING"
    EMERGENCY_ALERT = "EMERGENCY_ALERT"


class ConflictAlert(BaseModel):
    conflict_id: str  # stable for the life of one conflict — see engine._new_conflict_id
    vehicle_a_id: str
    vehicle_b_id: str

    status: AlertLifecycleStatus
    level: AlertLevel
    transition: AlertTransition

    time_to_conflict_s: Optional[float] = None
    minimum_predicted_distance_m: Optional[float] = None
    conflict_location: Optional[Tuple[float, float]] = None

    relative_speed_mps: Optional[float] = None
    closing_speed_mps: Optional[float] = None
    relative_heading_deg: Optional[float] = None

    road_type: Optional[str] = None       # best-effort, often None — see Step 2
    same_current_segment: bool = False
    segment_overlap_ahead: bool = False

    map_match_confidence: float = 0.0  # Step 2/3-derived confidence of the underlying conflict assessment
    telemetry_stale: bool = False      # true if either side's trajectory was DEGRADED/STALE when assessed

    reason: str
    recommended_action: RecommendedAction

    first_detected_at: datetime
    last_updated_at: datetime
    resolved_at: Optional[datetime] = None
