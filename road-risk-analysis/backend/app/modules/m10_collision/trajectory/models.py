"""
Data model for the Step 3 trajectory predictor: RoadAwareVehicleState (Step 2)
→ a sequence of future positions along the actual road network.

Deliberately does not introduce a second "vehicle state" concept — everything
here is *derived from* app.modules.m03_maps.osrm.models.RoadAwareVehicleState, never
a parallel representation of it.
"""
import enum
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class TrajectoryStatus(str, enum.Enum):
    """`VALID` (fresh telemetry, full horizon), `DEGRADED` (predicted from aging
    telemetry, a poor map match or truncated geometry), `STALE` (telemetry too
    old to extrapolate), `UNAVAILABLE` (no road context to predict along)."""
    VALID = "VALID"              # fresh telemetry, usable road context, full horizon predicted
    DEGRADED = "DEGRADED"        # still predicted, but telemetry aging / match quality low / geometry ran out early
    STALE = "STALE"              # telemetry too old to extrapolate motion from at all
    UNAVAILABLE = "UNAVAILABLE"  # no usable road context to predict along (Step 2 match failed/unavailable)


class ConfidenceLevel(str, enum.Enum):
    """Banded `confidence`: `HIGH`, `MEDIUM` or `LOW`."""
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class AccelerationSource(str, enum.Enum):
    """Where the acceleration used for the prediction comes from: `DEVICE`
    (reported in the ping), `DERIVED` (from two consecutive speeds) or
    `ASSUMED_ZERO` (no reliable signal — constant speed)."""
    DEVICE = "DEVICE"             # reported directly by the vehicle's telemetry
    DERIVED = "DERIVED"           # computed from two consecutive speed samples
    ASSUMED_ZERO = "ASSUMED_ZERO"  # no reliable signal — constant-velocity fallback, not a guess


class TrajectoryPoint(BaseModel):
    """One predicted future position."""
    t_s: float = Field(..., description=(  # seconds ahead of "now" (not the telemetry timestamp — see predictor.py)
        "Seconds ahead of now (not of the telemetry timestamp)."), examples=[3.0])
    latitude: float = Field(..., description="WGS84 latitude (degrees).")
    longitude: float = Field(..., description="WGS84 longitude (degrees).")
    distance_along_road_m: float = Field(..., description=(  # cumulative distance from the vehicle's current matched position
        "Distance along the road from the vehicle's current matched position, metres."))
    speed_mps: float = Field(..., description="Modelled speed at this point, m/s.")  # modelled speed at this point
    road_segment_id: Optional[str] = Field(None, description=(  # which Step 2 segment this point falls on (current or an upcoming one)
        "Road segment this point falls on (current or upcoming)."))


class Trajectory(BaseModel):
    """Short-horizon prediction of a vehicle's future positions along the road network."""
    vehicle_id: str = Field(..., description="Vehicle id.")
    generated_at: datetime = Field(..., description="When the prediction was computed (UTC).")
    status: TrajectoryStatus = Field(..., description="`VALID`, `DEGRADED`, `STALE` or `UNAVAILABLE`.")
    confidence: float = Field(..., description=(  # 0..1 composite score — see predictor.py for the factors
        "Composite confidence 0–1 (telemetry age, match quality, GPS accuracy …)."))
    confidence_level: ConfidenceLevel = Field(..., description="`HIGH`, `MEDIUM` or `LOW`.")
    reason: Optional[str] = Field(None, description=(  # populated for UNAVAILABLE / notably DEGRADED cases
        "Explanation for `UNAVAILABLE` / notable `DEGRADED` results."))

    telemetry_timestamp: datetime = Field(..., description="Timestamp of the telemetry the prediction starts from.")
    telemetry_age_s: float = Field(..., description="Age of that telemetry when predicted, seconds.")

    initial_speed_mps: float = Field(..., description="Starting speed, m/s.")
    acceleration_mps2: float = Field(..., description="Acceleration applied, m/s².")
    acceleration_source: AccelerationSource = Field(..., description="`DEVICE`, `DERIVED` or `ASSUMED_ZERO`.")

    road_segment_id: Optional[str] = Field(None, description="Current road segment id.")
    road_direction: Optional[str] = Field(None, description=(  # mirrors app.modules.m03_maps.osrm.models.RoadDirection.value
        "Direction of travel on the segment: `FORWARD`, `REVERSE` or `UNKNOWN`."))
    road_type: Optional[str] = Field(None, description="Road class, best-effort (often null).")  # mirrors RoadContext.road_type — best-effort, often None (see Step 2)

    # Road/lane context for the collision detector (Step 4) — all optional so
    # older cached trajectories still parse.
    road_name: Optional[str] = Field(None, description="Current road name.")
    match_quality: Optional[str] = Field(None, description=(  # mirrors MapMatchQuality.value
        "Map-match quality: `GOOD`, `DEGRADED`, `FAILED`, `UNAVAILABLE` or `STALE`."))
    matched_latitude: Optional[float] = Field(None, description=(  # current position on the road centreline
        "Current position snapped to the road centreline, latitude (degrees)."))
    matched_longitude: Optional[float] = Field(None, description=(
        "Current position snapped to the road centreline, longitude (degrees)."))
    travel_bearing_deg: Optional[float] = Field(None, description=(  # direction of travel along the road at the current position
        "Direction of travel along the road, degrees clockwise from north."))
    lateral_offset_m: Optional[float] = Field(None, description=(  # signed raw-GPS offset from the centreline, + = right of travel
        "Signed GPS offset from the centreline, metres (+ = right of the direction of travel)."))
    gps_accuracy_m: Optional[float] = Field(None, description=(
        "GPS horizontal accuracy, metres (`collision.assumed_accuracy_m` when the ping carried none)."))
    gps_accuracy_assumed: bool = Field(False, description="The ping carried no accuracy; `gps_accuracy_m` is assumed.")

    prediction_horizon_s: float = Field(..., description=(  # the largest horizon actually configured (settings.TRAJECTORY_HORIZONS_S)
        "Largest configured prediction horizon, seconds (`TRAJECTORY_HORIZONS_S`)."))
    truncated: bool = Field(False, description=(  # True if known road geometry ran out before the full horizon
        "Known road geometry ran out before the full horizon."))
    points: List[TrajectoryPoint] = Field([], description="Predicted positions, in time order.")
