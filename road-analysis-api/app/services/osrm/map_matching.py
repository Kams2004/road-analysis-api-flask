"""
Pure map-matching logic: (telemetry, previous state) → RoadAwareVehicleState.

Deliberately has no knowledge of Redis, Postgres or FastAPI — it only talks
to an OSRMClient and does arithmetic. That keeps it trivially unit-testable
with a mocked client (see tests/test_map_matching.py) and is the "OSRM
service abstraction" Step 3 is expected to sit behind
(app.services.road_state_service wires this into the rest of the app).
"""
import logging
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from app.core.config import settings
from app.services.osrm import geo
from app.services.osrm.client import OSRMClient, OSRMUnavailableError, get_osrm_client
from app.services.osrm.models import (
    MapMatchQuality,
    RoadAwareVehicleState,
    RoadContext,
    RoadDirection,
    RoadGeometry,
    TelemetrySnapshot,
    UpcomingSegment,
    stable_segment_id,
)

logger = logging.getLogger(__name__)

LatLon = Tuple[float, float]

# Raised from an earlier default of 5 once the Step 3 trajectory predictor
# started consuming upcoming_segments as load-bearing path geometry, not just
# debug info — a low cap could truncate the known road ahead well short of
# the fetched OSRM_SEGMENT_WINDOW_AHEAD_M on roads OSM splits into many short
# ways (common in dense urban data), starving the predictor for no reason
# since the geometry was already fetched in the same /route call either way.
MAX_UPCOMING_SEGMENTS = 25


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


def _should_reuse_cache(telemetry: TelemetrySnapshot, previous: Optional[RoadAwareVehicleState]) -> bool:
    """Skip a fresh OSRM round-trip if the vehicle has barely moved since its
    last usable match — avoids hammering OSRM for a stopped/near-stationary
    vehicle pinging every few seconds (see settings.OSRM_MATCH_MIN_*)."""
    if previous is None or previous.road_context is None:
        return False
    if previous.match_quality not in (MapMatchQuality.GOOD, MapMatchQuality.DEGRADED):
        return False
    dt = (telemetry.timestamp - previous.telemetry.timestamp).total_seconds()
    if not (0 <= dt <= settings.OSRM_MATCH_MIN_INTERVAL_S):
        return False
    dist = geo.haversine_m(
        telemetry.latitude, telemetry.longitude,
        previous.telemetry.latitude, previous.telemetry.longitude,
    )
    return dist <= settings.OSRM_MATCH_MIN_DISTANCE_M


def _classify_quality(confidence: Optional[float], raw_to_matched_m: float) -> MapMatchQuality:
    if confidence is None:
        return MapMatchQuality.FAILED
    if (
        confidence >= settings.OSRM_CONFIDENCE_GOOD_THRESHOLD
        and raw_to_matched_m <= settings.OSRM_MATCH_DEFAULT_RADIUS_M * 2
    ):
        return MapMatchQuality.GOOD
    if confidence >= settings.OSRM_CONFIDENCE_DEGRADED_THRESHOLD:
        return MapMatchQuality.DEGRADED
    return MapMatchQuality.FAILED


def _direction_from_headings(vehicle_heading: Optional[float], road_bearing: Optional[float]) -> RoadDirection:
    if vehicle_heading is None or road_bearing is None:
        return RoadDirection.UNKNOWN
    diff = geo.heading_diff_deg(vehicle_heading, road_bearing)
    if diff <= settings.OSRM_HEADING_TOLERANCE_DEG:
        return RoadDirection.FORWARD
    if diff >= 180 - settings.OSRM_HEADING_TOLERANCE_DEG:
        return RoadDirection.REVERSE
    return RoadDirection.UNKNOWN


def _build_road_context(
    matched_point: LatLon,
    matched_name: Optional[str],
    raw_to_matched_m: float,
    fallback_bearing: Optional[float],
    route_data: dict,
    vehicle_heading: Optional[float],
) -> Optional[RoadContext]:
    """Turn an OSRM /route response for a window around the matched point
    into a RoadContext — see map_matching module docstring / models.py for
    why "segment" means "the OSRM route step containing the matched point"
    rather than a true junction-to-junction OSM way."""
    if route_data.get("code") != "Ok" or not route_data.get("routes"):
        return None

    route = route_data["routes"][0]
    geometry_coords: List[LatLon] = [(lat, lon) for lon, lat in route["geometry"]["coordinates"]]
    if len(geometry_coords) < 2:
        return None

    dist_along_window, _ = geo.project_point_onto_polyline(matched_point, geometry_coords)

    steps = [step for leg in route.get("legs", []) for step in leg.get("steps", [])]
    if not steps:
        return None

    step_starts: List[float] = []
    cum = 0.0
    for step in steps:
        step_starts.append(cum)
        cum += step.get("distance", 0.0) or 0.0

    idx = 0
    for i, start in enumerate(step_starts):
        if start <= dist_along_window:
            idx = i
        else:
            break

    current_step = steps[idx]
    step_length = current_step.get("distance", 0.0) or 0.0
    distance_along_segment = min(max(0.0, dist_along_window - step_starts[idx]), step_length)
    distance_to_segment_end = max(0.0, step_length - distance_along_segment)

    maneuver = current_step.get("maneuver", {}) or {}
    step_bearing = maneuver.get("bearing_after")
    if step_bearing is None:
        step_bearing = maneuver.get("bearing_before")
    if step_bearing is None:
        step_bearing = fallback_bearing
    else:
        step_bearing = float(step_bearing)

    step_geom = current_step.get("geometry", {}).get("coordinates", []) or []
    road_name = current_step.get("name") or matched_name
    segment_id = stable_segment_id(
        road_name,
        tuple(step_geom[0]) if step_geom else (matched_point[1], matched_point[0]),
        tuple(step_geom[-1]) if step_geom else (matched_point[1], matched_point[0]),
    )

    upcoming: List[UpcomingSegment] = []
    running = step_starts[idx] + step_length
    for step in steps[idx + 1:]:
        s_len = step.get("distance", 0.0) or 0.0
        if s_len <= 0:
            continue
        s_geom = step.get("geometry", {}).get("coordinates", []) or []
        s_name = step.get("name") or None
        upcoming.append(
            UpcomingSegment(
                road_segment_id=stable_segment_id(
                    s_name,
                    tuple(s_geom[0]) if s_geom else (0.0, 0.0),
                    tuple(s_geom[-1]) if s_geom else (0.0, 0.0),
                ),
                road_name=s_name,
                distance_from_vehicle_m=max(0.0, running - dist_along_window),
                length_m=s_len,
                geometry=RoadGeometry(coordinates=[tuple(c) for c in s_geom]),
            )
        )
        running += s_len
        if len(upcoming) >= MAX_UPCOMING_SEGMENTS:
            break

    return RoadContext(
        road_segment_id=segment_id,
        road_name=road_name,
        direction=_direction_from_headings(vehicle_heading, step_bearing),
        road_bearing_deg=step_bearing,
        matched_latitude=matched_point[0],
        matched_longitude=matched_point[1],
        raw_to_matched_distance_m=raw_to_matched_m,
        distance_along_segment_m=distance_along_segment,
        distance_to_segment_end_m=distance_to_segment_end,
        segment_length_m=step_length,
        geometry=RoadGeometry(coordinates=[tuple(c) for c in step_geom]),
        upcoming_segments=upcoming,
    )


async def match_vehicle_to_road(
    telemetry: TelemetrySnapshot,
    previous: Optional[RoadAwareVehicleState] = None,
    client: Optional[OSRMClient] = None,
) -> RoadAwareVehicleState:
    """The Step 2 entrypoint: vehicle telemetry → road-aware vehicle state.

    `previous` is the vehicle's last computed RoadAwareVehicleState (if any)
    — used both to stabilize matching against GPS noise (fed to OSRM /match
    as a 2-point trace, letting OSRM's own HMM smooth single-point jitter)
    and to skip redundant OSRM calls for a near-stationary vehicle.
    """
    client = client or get_osrm_client()
    now = datetime.now(timezone.utc)
    ts = _aware(telemetry.timestamp)

    age_s = (now - ts).total_seconds()
    if age_s > settings.OSRM_STALE_TELEMETRY_S:
        return RoadAwareVehicleState(
            vehicle_id=telemetry.vehicle_id,
            telemetry=telemetry,
            road_context=None,
            match_quality=MapMatchQuality.STALE,
            match_error=f"telemetry is {age_s:.0f}s old (> {settings.OSRM_STALE_TELEMETRY_S:.0f}s)",
            computed_at=now,
        )

    if _should_reuse_cache(telemetry, previous):
        return RoadAwareVehicleState(
            vehicle_id=telemetry.vehicle_id,
            telemetry=telemetry,
            road_context=previous.road_context,
            match_quality=previous.match_quality,
            match_confidence=previous.match_confidence,
            osrm_latency_ms=None,
            computed_at=now,
        )

    # ── 1. Map-match the current point (plus the previous one, if recent
    #        enough, so OSRM's HMM has real history to smooth against). ──
    points: List[LatLon] = [(telemetry.latitude, telemetry.longitude)]
    timestamps = [int(ts.timestamp())]
    radiuses = [max(telemetry.accuracy_m or settings.OSRM_MATCH_DEFAULT_RADIUS_M, 5.0)]

    if previous is not None:
        prev_ts = _aware(previous.telemetry.timestamp)
        prev_dist_m = geo.haversine_m(
            telemetry.latitude, telemetry.longitude,
            previous.telemetry.latitude, previous.telemetry.longitude,
        )
        # A near-stationary vehicle (or one whose position isn't changing
        # between pings) produces a near-zero-distance trace here, which hits
        # the exact "duplicating a single point" problem described below —
        # OSRM scores it at confidence 0 same as a true duplicate. Only feed
        # /match a previous point that represents real movement; otherwise
        # fall through to the single-point path (which correctly skips
        # /match) below, same as a vehicle's very first-ever ping.
        if (
            0 < (ts - prev_ts).total_seconds() <= settings.OSRM_STALE_TELEMETRY_S
            and prev_dist_m > settings.OSRM_MATCH_MIN_TRACE_DISTANCE_M
        ):
            points = [(previous.telemetry.latitude, previous.telemetry.longitude)] + points
            timestamps = [int(prev_ts.timestamp())] + timestamps
            radiuses = [max(previous.telemetry.accuracy_m or settings.OSRM_MATCH_DEFAULT_RADIUS_M, 5.0)] + radiuses

    total_latency_ms = 0.0
    matched_point: Optional[LatLon] = None
    matched_name: Optional[str] = None
    confidence: Optional[float] = None

    if len(points) >= 2:
        # OSRM's /match needs at least two points to do anything HMM-based —
        # and duplicating a single point to satisfy that produces a
        # zero-distance, zero-duration trace whose confidence score is
        # always 0 (verified against the live OSRM server), which is worse
        # than useless. So /match is only attempted once there's a real
        # previous point; a vehicle's very first ping goes straight to the
        # /nearest fallback below instead.
        try:
            match_result = await client.match(points, timestamps=timestamps, radiuses=radiuses)
        except OSRMUnavailableError as e:
            logger.warning("OSRM unavailable for vehicle %s: %s", telemetry.vehicle_id, e)
            return RoadAwareVehicleState(
                vehicle_id=telemetry.vehicle_id, telemetry=telemetry, road_context=None,
                match_quality=MapMatchQuality.UNAVAILABLE, match_error=str(e), computed_at=now,
            )
        total_latency_ms += match_result.latency_ms
        data = match_result.data

        if data.get("code") == "Ok" and data.get("matchings") and data.get("tracepoints"):
            tracepoint = data["tracepoints"][-1]
            if tracepoint is not None:
                lon, lat = tracepoint["location"]
                matched_point = (lat, lon)
                matched_name = tracepoint.get("name") or None
                confidence = data["matchings"][0].get("confidence")
    else:
        data = {"code": "NoHistory"}

    if matched_point is None:
        # /match found nothing usable (isolated point, no road within radius,
        # or too little history) — fall back to a plain nearest-road snap so
        # a single bad ping doesn't leave the vehicle with no road context at
        # all. Lower, fixed confidence since /nearest has no HMM behind it.
        try:
            nearest_result = await client.nearest((telemetry.latitude, telemetry.longitude))
        except OSRMUnavailableError as e:
            return RoadAwareVehicleState(
                vehicle_id=telemetry.vehicle_id, telemetry=telemetry, road_context=None,
                match_quality=MapMatchQuality.UNAVAILABLE, match_error=str(e), computed_at=now,
            )
        total_latency_ms += nearest_result.latency_ms
        ndata = nearest_result.data
        if ndata.get("code") != "Ok" or not ndata.get("waypoints"):
            return RoadAwareVehicleState(
                vehicle_id=telemetry.vehicle_id, telemetry=telemetry, road_context=None,
                match_quality=MapMatchQuality.FAILED,
                match_error=f"no usable match (match={data.get('code')}, nearest={ndata.get('code')})",
                osrm_latency_ms=total_latency_ms, computed_at=now,
            )
        wp = ndata["waypoints"][0]
        lon, lat = wp["location"]
        matched_point = (lat, lon)
        matched_name = wp.get("name") or None
        confidence = 0.2

    raw_to_matched_m = geo.haversine_m(telemetry.latitude, telemetry.longitude, *matched_point)
    quality = _classify_quality(confidence, raw_to_matched_m)
    if quality == MapMatchQuality.FAILED:
        return RoadAwareVehicleState(
            vehicle_id=telemetry.vehicle_id, telemetry=telemetry, road_context=None,
            match_quality=quality, match_confidence=confidence,
            match_error="map-match confidence below usable threshold",
            osrm_latency_ms=total_latency_ms, computed_at=now,
        )

    # ── 2. First-pass road bearing from the match's own steps (for the
    #        route-window request below, and as a fallback if that fails). ──
    fallback_bearing: Optional[float] = None
    matchings0 = (data.get("matchings") or [{}])[0]
    for leg in matchings0.get("legs", []):
        for step in leg.get("steps", []):
            b = (step.get("maneuver") or {}).get("bearing_after") or (step.get("maneuver") or {}).get("bearing_before")
            if b is not None:
                fallback_bearing = float(b)

    # ── 3. Bounded road-geometry window around the matched point — this is
    #        where "current segment" / "upcoming segments" come from. ──
    ahead_point = geo.destination(*matched_point, fallback_bearing or 0.0, settings.OSRM_SEGMENT_WINDOW_AHEAD_M)
    behind_point = geo.destination(*matched_point, ((fallback_bearing or 0.0) + 180) % 360, settings.OSRM_SEGMENT_WINDOW_BEHIND_M)

    try:
        route_result = await client.route([behind_point, ahead_point])
        total_latency_ms += route_result.latency_ms
        road_context = _build_road_context(
            matched_point, matched_name, raw_to_matched_m, fallback_bearing,
            route_result.data, telemetry.heading,
        )
    except OSRMUnavailableError as e:
        logger.warning("OSRM route-window unavailable for vehicle %s: %s", telemetry.vehicle_id, e)
        road_context = None

    if road_context is None:
        # We still have a matched point/name from /match even though the
        # route-window request failed or returned nothing usable — degrade
        # rather than discard everything the vehicle already told us.
        road_context = RoadContext(
            road_segment_id=stable_segment_id(matched_name, matched_point, matched_point),
            road_name=matched_name,
            direction=_direction_from_headings(telemetry.heading, fallback_bearing),
            road_bearing_deg=fallback_bearing,
            matched_latitude=matched_point[0],
            matched_longitude=matched_point[1],
            raw_to_matched_distance_m=raw_to_matched_m,
            distance_along_segment_m=0.0,
            distance_to_segment_end_m=0.0,
            segment_length_m=0.0,
            geometry=RoadGeometry(coordinates=[(matched_point[1], matched_point[0])]),
        )
        quality = MapMatchQuality.DEGRADED if quality == MapMatchQuality.GOOD else quality

    return RoadAwareVehicleState(
        vehicle_id=telemetry.vehicle_id,
        telemetry=telemetry,
        road_context=road_context,
        match_quality=quality,
        match_confidence=confidence,
        osrm_latency_ms=total_latency_ms,
        computed_at=now,
    )
