"""
Data model for the Step 3 trajectory predictor: RoadAwareVehicleState (Step 2)
→ a sequence of future positions along the actual road network.

Deliberately does not introduce a second "vehicle state" concept — everything
here is *derived from* app.services.osrm.models.RoadAwareVehicleState, never
a parallel representation of it.
"""
import enum
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel


class TrajectoryStatus(str, enum.Enum):
    VALID = "VALID"              # fresh telemetry, usable road context, full horizon predicted
    DEGRADED = "DEGRADED"        # still predicted, but telemetry aging / match quality low / geometry ran out early
    STALE = "STALE"              # telemetry too old to extrapolate motion from at all
    UNAVAILABLE = "UNAVAILABLE"  # no usable road context to predict along (Step 2 match failed/unavailable)


class ConfidenceLevel(str, enum.Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class AccelerationSource(str, enum.Enum):
    DEVICE = "DEVICE"             # reported directly by the vehicle's telemetry
    DERIVED = "DERIVED"           # computed from two consecutive speed samples
    ASSUMED_ZERO = "ASSUMED_ZERO"  # no reliable signal — constant-velocity fallback, not a guess


class TrajectoryPoint(BaseModel):
    t_s: float                     # seconds ahead of "now" (not the telemetry timestamp — see predictor.py)
    latitude: float
    longitude: float
    distance_along_road_m: float   # cumulative distance from the vehicle's current matched position
    speed_mps: float               # modelled speed at this point
    road_segment_id: Optional[str] = None  # which Step 2 segment this point falls on (current or an upcoming one)


class Trajectory(BaseModel):
    vehicle_id: str
    generated_at: datetime
    status: TrajectoryStatus
    confidence: float                     # 0..1 composite score — see predictor.py for the factors
    confidence_level: ConfidenceLevel
    reason: Optional[str] = None          # populated for UNAVAILABLE / notably DEGRADED cases

    telemetry_timestamp: datetime
    telemetry_age_s: float

    initial_speed_mps: float
    acceleration_mps2: float
    acceleration_source: AccelerationSource

    road_segment_id: Optional[str] = None
    road_direction: Optional[str] = None  # mirrors app.services.osrm.models.RoadDirection.value
    road_type: Optional[str] = None       # mirrors RoadContext.road_type — best-effort, often None (see Step 2)

    prediction_horizon_s: float           # the largest horizon actually configured (settings.TRAJECTORY_HORIZONS_S)
    truncated: bool = False               # True if known road geometry ran out before the full horizon
    points: List[TrajectoryPoint] = []
