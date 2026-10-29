"""
Where to DRAW a vehicle on the maps (its own app and the others'): the raw GPS fix cleaned of its
uncertainty, kept in the live hash next to the raw fix (`display_*` fields).

  stopped   speed ≤ HOLD_SPEED_MPS and the new fix within the GPS uncertainty
            (max(HOLD_MIN_M, accuracy)) of the last drawn point → the point does not move: a
            parked phone's GPS wanders by metres, the vehicle does not.
  on a road the fix is projected onto the road it is map-matched to (previous pipeline run:
            road_state_service cache, ≤ ROAD_MAX_AGE_S old) when it lies within
            max(SNAP_MIN_M, 1.5 × accuracy) (≤ SNAP_MAX_M) of it and — when moving — the heading
            follows the road (≤ SNAP_MAX_HEADING_DIFF, either way on a two-way road).
  otherwise the raw fix (car park, yard, off the mapped roads, poor or no match): never forced
            onto a road it is not on.

The ping answers the result with the road around the vehicle (`road`, ≤ ROAD_WINDOW_M each way)
so the app snaps its own position at every GPS fix without waiting for the next ping.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional, Tuple

from app.core.storage.redis_client import get_redis
from app.modules.m03_maps.osrm import geo
from app.modules.m10_collision import road_state_service
from app.modules.m10_collision.vehicle_service import _hash_key
from app.modules.m10_collision.collision import thresholds

LatLon = Tuple[float, float]

HOLD_SPEED_MPS = 0.8
HOLD_MIN_M = 8.0
SNAP_MIN_M = 12.0
SNAP_MAX_M = 35.0
SNAP_MAX_HEADING_DIFF = 50.0
ROAD_MAX_AGE_S = 15.0
ROAD_WINDOW_M = 250.0
_TRUSTED = ("GOOD", "DEGRADED")


def _project(point: LatLon, coords: List[LatLon]) -> Optional[Tuple[LatLon, float, float, float]]:
    """(projected point, distance to it m, distance along m, road bearing there) or None."""
    if len(coords) < 2:
        return None
    along, i = geo.project_point_onto_polyline(point, coords)
    cum = geo.cumulative_distances_m(coords)
    p = geo.point_at_distance(coords, cum, along)
    return p, geo.haversine_m(*point, *p), along, geo.bearing_deg(*coords[i], *coords[i + 1])


def _road_coords(state) -> List[LatLon]:
    """Current segment + the segments ahead, as one (lat, lon) polyline."""
    rc = state.road_context
    coords = [(lat, lon) for lon, lat in rc.geometry.coordinates]
    for up in rc.upcoming_segments:
        nxt = [(lat, lon) for lon, lat in up.geometry.coordinates]
        if coords and nxt and geo.haversine_m(*coords[-1], *nxt[0]) < 5:
            nxt = nxt[1:]
        coords += nxt
    return coords


def _window(coords: List[LatLon], along: float) -> List[LatLon]:
    """The polyline between along − ROAD_WINDOW_M and along + ROAD_WINDOW_M."""
    cum = geo.cumulative_distances_m(coords)
    lo, hi = max(0.0, along - ROAD_WINDOW_M), min(cum[-1], along + ROAD_WINDOW_M)
    out = [geo.point_at_distance(coords, cum, lo)]
    out += [c for c, d in zip(coords, cum) if lo < d < hi]
    out.append(geo.point_at_distance(coords, cum, hi))
    return out


async def update(vehicle_id: str, live: dict, previous: Optional[dict]) -> dict:
    """Display position of the fix just recorded (`live`, vehicle_service._parse_hash shape) given the
    previous one (`previous`, same shape, None at the first ping); stored in the live hash."""
    raw = (live["latitude"], live["longitude"])
    acc = live.get("accuracy_m") or thresholds.current().assumed_accuracy_m
    speed = live.get("speed_mps")
    heading = live.get("heading")
    stopped = speed is not None and speed <= HOLD_SPEED_MPS

    out = {"latitude": raw[0], "longitude": raw[1], "snapped": False, "stationary": stopped,
           "road": None, "road_name": None}

    prev_pt = None
    if previous and previous.get("display_latitude") is not None:
        prev_pt = (previous["display_latitude"], previous["display_longitude"])
    if stopped and prev_pt and geo.haversine_m(*raw, *prev_pt) <= max(HOLD_MIN_M, acc):
        out.update(latitude=prev_pt[0], longitude=prev_pt[1], snapped=bool(previous.get("display_snapped")))
    else:
        state = await road_state_service.load_cached(vehicle_id)
        rc = state.road_context if state else None
        fresh = False
        if state is not None:
            age = (datetime.now(timezone.utc) - state.computed_at).total_seconds()
            fresh = age <= ROAD_MAX_AGE_S
        if rc is not None and fresh and state.match_quality.value in _TRUSTED:
            coords = _road_coords(state)
            hit = _project(raw, coords)
            if hit:
                p, d, along, road_brg = hit
                limit = min(SNAP_MAX_M, max(SNAP_MIN_M, 1.5 * acc))
                diff = 0.0 if heading is None or stopped else geo.heading_diff_deg(heading, road_brg)
                aligned = diff <= SNAP_MAX_HEADING_DIFF or diff >= 180 - SNAP_MAX_HEADING_DIFF
                if d <= limit and aligned:
                    out.update(latitude=p[0], longitude=p[1], snapped=True)
                out["road"] = _window(coords, along)
                out["road_name"] = rc.road_name

    r = get_redis()
    await r.hset(_hash_key(vehicle_id), mapping={
        "display_latitude": out["latitude"], "display_longitude": out["longitude"],
        "display_snapped": "1" if out["snapped"] else "",
    })
    return out


def round_road(road: Optional[List[LatLon]]) -> Optional[List[List[float]]]:
    return [[round(lat, 6), round(lon, 6)] for lat, lon in road] if road else None
