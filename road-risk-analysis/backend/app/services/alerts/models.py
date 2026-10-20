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

from pydantic import BaseModel, Field


class AlertLevel(str, enum.Enum):
    """Alert severity, in increasing order: `SAFE` < `MONITOR` < `WARNING` <
    `HIGH_WARNING` < `CRITICAL`. Direct mapping of the conflict's risk level
    (NONE→SAFE, LOW→MONITOR, MODERATE→WARNING, HIGH→HIGH_WARNING,
    CRITICAL→CRITICAL), which already combines distance and time to the
    closest approach — see engine.classify_level."""
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
    """What the driver should be told, one per level: SAFE→`NONE`,
    MONITOR→`MONITOR`, WARNING→`ADVISE_CAUTION`,
    HIGH_WARNING→`IMMEDIATE_DRIVER_WARNING`, CRITICAL→`EMERGENCY_ALERT`."""
    NONE = "NONE"
    MONITOR = "MONITOR"
    ADVISE_CAUTION = "ADVISE_CAUTION"
    IMMEDIATE_DRIVER_WARNING = "IMMEDIATE_DRIVER_WARNING"
    EMERGENCY_ALERT = "EMERGENCY_ALERT"


class ConflictAlert(BaseModel):
    """A stateful collision alert between two vehicles — one evolving record per
    conflict (shared by both vehicles), not a fresh warning per ping.

    **Levels**: `SAFE` < `MONITOR` < `WARNING` < `HIGH_WARNING` < `CRITICAL`.
    **Lifecycle** (`status`): `DETECTED` (first tick above SAFE) → `ACTIVE`
    (ongoing, may escalate / de-escalate) → `RESOLVED` (terminal).
    `transition` says what changed on the latest evaluation; consumers notify on
    `NEW` / `ESCALATED`. De-escalation is delayed by a hysteresis
    (`pending_level` / `pending_since`)."""
    conflict_id: str = Field(..., description=(  # stable for the life of one conflict — see engine._new_conflict_id
        "Stable id for the life of this conflict."), examples=["CF-20261015-3fa2b9c1"])
    vehicle_a_id: str = Field(..., description="First vehicle of the pair.")
    vehicle_b_id: str = Field(..., description="Second vehicle of the pair.")

    status: AlertLifecycleStatus = Field(..., description="Lifecycle: `DETECTED`, `ACTIVE` or `RESOLVED`.")
    level: AlertLevel = Field(..., description="Current severity (`SAFE` … `CRITICAL`).")
    transition: AlertTransition = Field(..., description=(
        "Change on the latest evaluation: `NEW`, `ESCALATED`, `DE_ESCALATED`, `UNCHANGED` or `RESOLVED`."))

    time_to_conflict_s: Optional[float] = Field(None, description="Predicted time until the closest approach, seconds.")
    minimum_predicted_distance_m: Optional[float] = Field(None, description=(
        "Predicted distance between the vehicles at the closest approach, metres."))
    conflict_location: Optional[Tuple[float, float]] = Field(None, description=(
        "Approximate `[latitude, longitude]` of the closest approach (WGS84 degrees)."))

    relative_speed_mps: Optional[float] = Field(None, description="Difference of the two speeds, m/s.")
    closing_speed_mps: Optional[float] = Field(None, description="Speed at which the vehicles approach each other, m/s.")
    relative_heading_deg: Optional[float] = Field(None, description=(
        "Angle between the headings, degrees: 0 = same direction, ~90 = crossing, 180 = head-on."))

    road_type: Optional[str] = Field(None, description="Road class, best-effort (often null).")  # best-effort, often None — see Step 2
    encounter: Optional[str] = Field(None, description="`head_on`, `crossing` or `same_direction`.")  # head_on | crossing | same_direction
    road_relation: Optional[str] = Field(None, description=(  # same_road | connected | unrelated | unknown
        "`same_road`, `connected`, `unrelated` or `unknown`."))
    same_current_segment: bool = Field(False, description="Both vehicles are on the same road segment now.")
    segment_overlap_ahead: bool = Field(False, description="Their predicted paths share a road segment ahead.")

    map_match_confidence: float = Field(0.0, description=(  # Step 2/3-derived confidence of the underlying conflict assessment
        "Confidence (0–1) of the underlying conflict assessment (map matching + prediction)."))
    telemetry_stale: bool = Field(False, description=(  # true if either side's trajectory was DEGRADED/STALE when assessed
        "Either vehicle's trajectory was `DEGRADED` or `STALE` when assessed."))

    reason: str = Field(..., description="Human-readable explanation of the level.")
    recommended_action: RecommendedAction = Field(..., description=(
        "`NONE`, `MONITOR`, `ADVISE_CAUTION`, `IMMEDIATE_DRIVER_WARNING` or `EMERGENCY_ALERT`."))

    # De-escalation hysteresis (engine.evaluate): a lower level observed
    # since `pending_since` — the alert only drops to it once it has
    # persisted for the configured deescalate_after_s.
    pending_level: Optional[AlertLevel] = Field(None, description=(
        "Lower level observed since `pending_since`; the alert drops to it once it has persisted for the "
        "configured de-escalation delay."))
    pending_since: Optional[datetime] = Field(None, description="Since when `pending_level` has been observed (UTC).")

    first_detected_at: datetime = Field(..., description="When the conflict was first detected (UTC).")
    last_updated_at: datetime = Field(..., description="Time of the latest evaluation (UTC).")
    resolved_at: Optional[datetime] = Field(None, description="When the alert was resolved (UTC).")
