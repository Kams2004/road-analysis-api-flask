"""
Data model for the OSRM road-intelligence layer (collision-prevention Step 2).

Design decision — road_segment_id is NOT an OSRM/OSM identifier
─────────────────────────────────────────────────────────────────
OSRM's public HTTP API does not stably expose OSM way ids for a matched
step (only optional internal node ids via `annotations=true`, which are tied
to the specific compiled `.osrm` graph and can shift when the underlying OSM
extract is rebuilt). Treating those as a durable business key would silently
break every time the road dataset is refreshed. Instead `road_segment_id`
here is a deterministic hash of (road name, rounded start/end coordinates of
the matched step) — stable across requests against the *same* road dataset,
good enough for Step 2/3 to group or deduplicate a "segment" by, without
committing to OSRM internals. If a durable, dataset-version-independent
segment registry is ever needed, it should be built as its own abstraction
on top of this (e.g. backed by OSM way ids captured at import time), not by
leaning on whatever OSRM happens to return over HTTP.

Design decision — "segment" is a route window, not junction-to-junction
─────────────────────────────────────────────────────────────────
OSRM's HTTP API has no endpoint that returns "the road between the two
nearest junctions." What it does return, via /route with `steps=true`, is a
sequence of steps — each step is one contiguous stretch with a single road
name/bearing, which OSRM itself breaks at junctions/turns/name changes. We
fetch a bounded window (OSRM_SEGMENT_WINDOW_BEHIND_M/AHEAD_M) of route
geometry centred on the map-matched point and treat the step containing that
point as "the current segment," with the following steps in the window as
"upcoming segments." This is a pragmatic approximation, not true OSM
topology — documented here rather than left implicit.
"""
import enum
import hashlib
from datetime import datetime
from typing import List, Optional, Tuple

from pydantic import BaseModel, Field


class MapMatchQuality(str, enum.Enum):
    """`GOOD` (confident match), `DEGRADED` (low confidence / large offset),
    `FAILED` (no usable match), `UNAVAILABLE` (OSRM unreachable), `STALE`
    (telemetry too old to be a current position)."""
    GOOD = "GOOD"                # high-confidence match, safe to use directly
    DEGRADED = "DEGRADED"        # matched, but low confidence / large GPS-to-road offset
    FAILED = "FAILED"            # OSRM responded but could not produce a usable match
    UNAVAILABLE = "UNAVAILABLE"  # OSRM could not be reached / timed out
    STALE = "STALE"              # telemetry is too old to treat as a current position


class RoadDirection(str, enum.Enum):
    """Direction of travel relative to the road segment's geometry:
    `FORWARD`, `REVERSE` or `UNKNOWN`."""
    FORWARD = "FORWARD"
    REVERSE = "REVERSE"
    UNKNOWN = "UNKNOWN"


class RoadGeometry(BaseModel):
    """GeoJSON LineString — the format the rest of this project already uses
    for route geometry (see AlongRouteQueryIn / the mobile app's OSRM usage)."""
    type: str = Field("LineString", description="Always `LineString`.")
    coordinates: List[Tuple[float, float]] = Field(..., description=(  # [lon, lat] pairs, GeoJSON order
        "`[longitude, latitude]` pairs (GeoJSON order, WGS84 degrees)."))


class UpcomingSegment(BaseModel):
    """A road segment ahead of the vehicle on its matched route window."""
    road_segment_id: str = Field(..., description="Segment id (deterministic hash, stable for a road dataset).")
    road_name: Optional[str] = Field(None, description="Road name.")
    distance_from_vehicle_m: float = Field(..., description="Distance from the vehicle to the segment start, metres.")
    length_m: float = Field(..., description="Segment length, metres.")
    geometry: RoadGeometry = Field(..., description="Segment geometry.")


class RoadContext(BaseModel):
    """The road segment a vehicle is matched to. A "segment" is one OSRM route
    step (a stretch with a single name/bearing), not a junction-to-junction
    OSM way."""
    road_segment_id: str = Field(..., description="Segment id (deterministic hash, stable for a road dataset).")
    road_name: Optional[str] = Field(None, description="Road name.")
    road_type: Optional[str] = Field(None, description="Road class, best-effort (often null).")  # best-effort; not always exposed by the OSRM profile in use

    direction: RoadDirection = Field(..., description="`FORWARD`, `REVERSE` or `UNKNOWN`.")
    road_bearing_deg: Optional[float] = Field(None, description="Bearing of the road at the matched point, degrees clockwise from north.")

    matched_latitude: float = Field(..., description="Position snapped to the road, latitude (degrees).")
    matched_longitude: float = Field(..., description="Position snapped to the road, longitude (degrees).")
    raw_to_matched_distance_m: float = Field(..., description="Distance between the GPS fix and the snapped position, metres.")

    distance_along_segment_m: float = Field(..., description="Distance from the segment start to the vehicle, metres.")
    distance_to_segment_end_m: float = Field(..., description="Distance from the vehicle to the segment end, metres.")
    segment_length_m: float = Field(..., description="Segment length, metres.")

    geometry: RoadGeometry = Field(..., description="Segment geometry.")
    upcoming_segments: List[UpcomingSegment] = Field([], description="Next segments ahead, nearest first.")


class TelemetrySnapshot(BaseModel):
    """The telemetry the road context was computed from."""
    vehicle_id: str = Field(..., description="Vehicle id.")
    latitude: float = Field(..., description="WGS84 latitude (degrees).")
    longitude: float = Field(..., description="WGS84 longitude (degrees).")
    speed_mps: Optional[float] = Field(None, description="Speed, m/s.")
    heading: Optional[float] = Field(None, description="Heading, degrees clockwise from north (0–360).")
    accuracy_m: Optional[float] = Field(None, description="GPS horizontal accuracy, metres.")
    acceleration_mps2: Optional[float] = Field(None, description=(  # device-reported; Step 3 falls back to a derived estimate if absent
        "Device-reported acceleration, m/s² (null if not measured)."))
    timestamp: datetime = Field(..., description="Timestamp of the fix.")


class RoadAwareVehicleState(BaseModel):
    """A live vehicle's latest telemetry together with its map-matched road context."""
    vehicle_id: str = Field(..., description="Vehicle id.")
    telemetry: TelemetrySnapshot = Field(..., description="Telemetry the match was computed from.")
    road_context: Optional[RoadContext] = Field(None, description="Matched road context; null when the match failed.")
    match_quality: MapMatchQuality = Field(..., description="`GOOD`, `DEGRADED`, `FAILED`, `UNAVAILABLE` or `STALE`.")
    match_confidence: Optional[float] = Field(None, description="OSRM match confidence, 0–1.")
    match_error: Optional[str] = Field(None, description="Why matching failed, if it did.")
    osrm_latency_ms: Optional[float] = Field(None, description="OSRM response time, milliseconds.")
    computed_at: datetime = Field(..., description="When the state was computed (UTC).")


def stable_segment_id(road_name: Optional[str], start: Tuple[float, float], end: Tuple[float, float]) -> str:
    """Deterministic id for a matched step — see module docstring."""
    key = f"{road_name or ''}|{start[0]:.5f},{start[1]:.5f}|{end[0]:.5f},{end[1]:.5f}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]
