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
from typing import List, Optional, Tuple

from pydantic import BaseModel


class ConflictRiskLevel(str, enum.Enum):
    NONE = "NONE"          # not worth reporting — no shared close approach within the horizon
    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ConflictStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"            # both trajectories usable, risk genuinely assessed
    DEGRADED = "DEGRADED"        # assessed, but one or both trajectories are low-confidence/degraded
    UNAVAILABLE = "UNAVAILABLE"  # couldn't assess at all (no usable trajectory on one or both sides)


class VehicleConflict(BaseModel):
    vehicle_a_id: str
    vehicle_b_id: str

    risk_level: ConflictRiskLevel
    status: ConflictStatus
    confidence: float  # 0..1 — the lower of the two trajectories' confidences, see detector.py

    # Closest-approach estimate, from a locally-linear (constant-velocity per
    # trajectory-sample-interval) kinematic projection — see detector.py's
    # module docstring for why this is more than just "nearest of the
    # discrete horizon samples".
    time_to_closest_approach_s: Optional[float] = None
    distance_at_closest_approach_m: Optional[float] = None
    conflict_point: Optional[Tuple[float, float]] = None  # (lat, lon), approximate

    same_current_segment: bool = False   # both vehicles currently matched to the same Step 2 segment
    segment_overlap_ahead: bool = False  # either vehicle's predicted path crosses a segment the other's does too

    # Relative motion at the winning interval — free byproducts of the CPA
    # calculation already done for distance/TTC (see detector._cpa_in_interval),
    # exposed here so Step 5 doesn't need to re-derive them from scratch.
    relative_speed_mps: Optional[float] = None   # |speed_a - speed_b| at t=0 (simple magnitude difference)
    closing_speed_mps: Optional[float] = None    # magnitude of relative velocity over the winning CPA interval
    relative_heading_deg: Optional[float] = None  # 0=same direction, 180=head-on, ~90=crossing

    road_type_a: Optional[str] = None  # mirrors Trajectory.road_type — best-effort, often None (see Step 2)
    road_type_b: Optional[str] = None

    reason: Optional[str] = None  # populated for DEGRADED/UNAVAILABLE, or notable NONE results


class VehicleConflictReport(BaseModel):
    vehicle_id: str
    generated_at: datetime
    highest_risk: ConflictRiskLevel = ConflictRiskLevel.NONE
    conflicts: List[VehicleConflict] = []  # sorted worst-risk-first; only NONE-risk entries are excluded
