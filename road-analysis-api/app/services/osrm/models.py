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

from pydantic import BaseModel


class MapMatchQuality(str, enum.Enum):
    GOOD = "GOOD"                # high-confidence match, safe to use directly
    DEGRADED = "DEGRADED"        # matched, but low confidence / large GPS-to-road offset
    FAILED = "FAILED"            # OSRM responded but could not produce a usable match
    UNAVAILABLE = "UNAVAILABLE"  # OSRM could not be reached / timed out
    STALE = "STALE"              # telemetry is too old to treat as a current position


class RoadDirection(str, enum.Enum):
    FORWARD = "FORWARD"
    REVERSE = "REVERSE"
    UNKNOWN = "UNKNOWN"


class RoadGeometry(BaseModel):
    """GeoJSON LineString — the format the rest of this project already uses
    for route geometry (see AlongRouteQueryIn / the mobile app's OSRM usage)."""
    type: str = "LineString"
    coordinates: List[Tuple[float, float]]  # [lon, lat] pairs, GeoJSON order


class UpcomingSegment(BaseModel):
    road_segment_id: str
    road_name: Optional[str] = None
    distance_from_vehicle_m: float
    length_m: float
    geometry: RoadGeometry


class RoadContext(BaseModel):
    road_segment_id: str
    road_name: Optional[str] = None
    road_type: Optional[str] = None  # best-effort; not always exposed by the OSRM profile in use

    direction: RoadDirection
    road_bearing_deg: Optional[float] = None

    matched_latitude: float
    matched_longitude: float
    raw_to_matched_distance_m: float

    distance_along_segment_m: float
    distance_to_segment_end_m: float
    segment_length_m: float

    geometry: RoadGeometry
    upcoming_segments: List[UpcomingSegment] = []


class TelemetrySnapshot(BaseModel):
    vehicle_id: str
    latitude: float
    longitude: float
    speed_mps: Optional[float] = None
    heading: Optional[float] = None
    accuracy_m: Optional[float] = None
    acceleration_mps2: Optional[float] = None  # device-reported; Step 3 falls back to a derived estimate if absent
    timestamp: datetime


class RoadAwareVehicleState(BaseModel):
    vehicle_id: str
    telemetry: TelemetrySnapshot
    road_context: Optional[RoadContext] = None
    match_quality: MapMatchQuality
    match_confidence: Optional[float] = None
    match_error: Optional[str] = None
    osrm_latency_ms: Optional[float] = None
    computed_at: datetime


def stable_segment_id(road_name: Optional[str], start: Tuple[float, float], end: Tuple[float, float]) -> str:
    """Deterministic id for a matched step — see module docstring."""
    key = f"{road_name or ''}|{start[0]:.5f},{start[1]:.5f}|{end[0]:.5f},{end[1]:.5f}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]
