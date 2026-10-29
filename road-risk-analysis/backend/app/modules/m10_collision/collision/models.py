"""
Data model for Step 4 — the conflict/collision-risk engine: two vehicles'
predicted trajectories (Step 3) in, a pairwise risk assessment out.

Scope, deliberately: this computes and exposes *what the risk is*, nothing
more. No driver alerts, no push notifications, no mobile UI, no ML — same
staged discipline Steps 2 and 3 held themselves to ("do not implement X
yet"). Turning a CRITICAL conflict into something a driver actually sees is
a later step's job; this one's job is to make that judgment available
through a clean API for whatever consumes it next.
"""
import enum
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from app.modules.m10_collision.collision.context import ContextReason


class ConflictRiskLevel(str, enum.Enum):
    """Risk of a vehicle pair, combining distance and time at the closest
    approach: `NONE` < `LOW` < `MODERATE` < `HIGH` < `CRITICAL` (thresholds in
    Platform settings → collision)."""
    NONE = "NONE"          # not worth reporting — no shared close approach within the horizon
    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ConflictStatus(str, enum.Enum):
    """Quality of the assessment: `ACTIVE` (both trajectories usable),
    `DEGRADED` (assessed with low-confidence data), `UNAVAILABLE` (could not be
    assessed)."""
    ACTIVE = "ACTIVE"            # both trajectories usable, risk genuinely assessed
    DEGRADED = "DEGRADED"        # assessed, but one or both trajectories are low-confidence/degraded
    UNAVAILABLE = "UNAVAILABLE"  # couldn't assess at all (no usable trajectory on one or both sides)


class VehicleConflict(BaseModel):
    """Risk assessment of one pair of vehicles from their predicted trajectories."""
    vehicle_a_id: str = Field(..., description="The vehicle the report is about.")
    vehicle_b_id: str = Field(..., description="The other vehicle.")

    risk_level: ConflictRiskLevel = Field(..., description="`NONE`, `LOW`, `MODERATE`, `HIGH` or `CRITICAL`.")
    status: ConflictStatus = Field(..., description="`ACTIVE`, `DEGRADED` or `UNAVAILABLE`.")
    confidence: float = Field(..., description=(  # 0..1 — the lower of the two trajectories' confidences, see detector.py
        "Confidence 0–1: the lower of the two trajectories' confidences."))

    # Closest-approach estimate, from a locally-linear (constant-velocity per
    # trajectory-sample-interval) kinematic projection — see detector.py's
    # module docstring for why this is more than just "nearest of the
    # discrete horizon samples".
    time_to_closest_approach_s: Optional[float] = Field(None, description="Time until the closest approach, seconds.")
    distance_at_closest_approach_m: Optional[float] = Field(None, description=(
        "Predicted distance between the vehicles at the closest approach, metres."))
    conflict_point: Optional[Tuple[float, float]] = Field(None, description=(  # (lat, lon), approximate
        "Approximate `[latitude, longitude]` of the closest approach (WGS84 degrees)."))

    same_current_segment: bool = Field(False, description=(  # both vehicles currently matched to the same Step 2 segment
        "Both vehicles are currently matched to the same road segment."))
    segment_overlap_ahead: bool = Field(False, description=(  # either vehicle's predicted path crosses a segment the other's does too
        "The predicted paths share a road segment ahead."))

    # Relative motion at the winning interval — free byproducts of the CPA
    # calculation already done for distance/TTC (see detector._cpa_in_interval),
    # exposed here so Step 5 doesn't need to re-derive them from scratch.
    relative_speed_mps: Optional[float] = Field(None, description=(  # |speed_a - speed_b| at t=0 (simple magnitude difference)
        "|speed A − speed B| now, m/s."))
    closing_speed_mps: Optional[float] = Field(None, description=(  # magnitude of relative velocity over the winning CPA interval
        "Magnitude of the relative velocity around the closest approach, m/s."))
    relative_heading_deg: Optional[float] = Field(None, description=(  # 0=same direction, 180=head-on, ~90=crossing
        "Angle between headings, degrees: 0 = same direction, ~90 = crossing, 180 = head-on."))

    # Encounter context (see detector.assess_conflict): why this pair is or
    # isn't a genuine conflict.
    encounter: Optional[str] = Field(None, description="`head_on`, `crossing`, `same_direction` or `stationary` (one vehicle stopped: its heading is ignored).")  # head_on | crossing | same_direction | stationary
    road_relation: Optional[str] = Field(None, description=(  # same_road | connected | unrelated | unknown
        "`same_road`, `connected`, `unrelated` or `unknown`."))
    lateral_separation_m: Optional[float] = Field(None, description=(  # lane separation estimated from raw GPS vs road centreline
        "Lateral (lane) separation estimated from GPS vs the road centreline, metres."))
    separating: bool = Field(False, description=(  # range not decreasing / CPA not in the future
        "The vehicles are moving apart (closest approach not in the future)."))
    clear_immediately: bool = Field(False, description=(  # NONE for a structural reason (separating, passing, unrelated road)
        "Risk is `NONE` for a structural reason (separating, passing in another lane, unrelated road); "
        "any alert for the pair is resolved at once."))

    road_type_a: Optional[str] = Field(None, description="Road class of vehicle A, best-effort (often null).")  # mirrors Trajectory.road_type — best-effort, often None (see Step 2)
    road_type_b: Optional[str] = Field(None, description="Road class of vehicle B, best-effort (often null).")

    reason: Optional[str] = Field(None, description=(  # populated for DEGRADED/UNAVAILABLE, or notable NONE results
        "Explanation, for `DEGRADED` / `UNAVAILABLE` or notable `NONE` results."))

    # Layer 1 detail + layer 2 (context) — see detector.apply_context.
    physical_risk_level: Optional[ConflictRiskLevel] = Field(None, description=(
        "Risk from physics alone (distance, time, braking), before the context; `risk_level` is never "
        "lower and is above `NONE` only if this is."))
    required_decel_mps2: Optional[float] = Field(None, description=(
        "Closing on a stopped vehicle: deceleration needed to stop short of it, m/s² (99 = already too close)."))
    context_index: float = Field(0.0, description="Context Risk Index at the conflict point, 0–1.")
    context_level_boost: int = Field(0, description="Levels added by a high context index (0 or 1).")
    context_reasons: List[ContextReason] = Field([], description="Why the context index is what it is, largest first.")
    context_factors: Dict[str, float] = Field({}, description="Value (0–1) of each context factor.")
    telemetry_timestamp: Optional[datetime] = Field(None, description=(
        "Newest telemetry timestamp of the two vehicles (detection latency is measured from it)."))


class VehicleConflictReport(BaseModel):
    """Conflicts of one vehicle with the vehicles around it."""
    vehicle_id: str = Field(..., description="The vehicle the report is about.")
    generated_at: datetime = Field(..., description="When the assessment was computed (UTC).")
    highest_risk: ConflictRiskLevel = Field(ConflictRiskLevel.NONE, description="Worst `risk_level` among `conflicts`.")
    conflicts: List[VehicleConflict] = Field([], description=(  # sorted worst-risk-first; only NONE-risk entries are excluded
        "Conflicts with a risk above `NONE`, worst first."))
    # Other vehicles assessed this tick as structurally safe (separating,
    # passing in another lane, unrelated road) — Step 5 resolves their
    # alerts immediately instead of waiting out the resolve hysteresis.
    cleared_vehicle_ids: List[str] = Field([], description=(
        "Nearby vehicles assessed as structurally safe this time (separating, passing in another lane, "
        "unrelated road)."))
