"""
Where to DRAW a vehicle on the maps (its own app and the others'): the raw GPS fix cleaned of its
uncertainty, kept in the live hash next to the raw fix (`display_*` fields).

  stopped   no trusted motion (speed > MOVING_SPEED_MPS — slower GPS speeds are noise as often as
            not) and the new fix within the GPS uncertainty of both points (max(HOLD_MIN_M,
            accuracy + the drawn point's accuracy)) of the last drawn point → the point does not move: a parked phone's GPS
            wanders by metres (indoors by 100 m, with fake speeds of several m/s), the vehicle
            does not. The speed (and heading) is only trusted with an accuracy ≤
            SPEED_TRUST_ACCURACY_M; with a poorer fix only a jump beyond the uncertainty counts as
            motion. A held point is replaced by a much more precise fix (≤ IMPROVE × its
            accuracy, i.e. twice as precise), so a parked position gets better, never stuck on a bad first fix.
  on a road the fix is projected onto the road it is map-matched to (previous pipeline run:
            road_state_service cache, ≤ ROAD_MAX_AGE_S old) when it lies within
            max(SNAP_MIN_M, 1.5 × accuracy) (≤ SNAP_MAX_M) of it and — when moving — the heading
            follows the road (≤ SNAP_MAX_HEADING_DIFF, either way on a two-way road).
  otherwise the raw fix (car park, yard, off the mapped roads, poor or no match): never forced
            onto a road it is not on. No snapping either with an accuracy > SNAP_MAX_M: the fix
            does not say which road.

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

MOVING_SPEED_MPS = 3.0
HOLD_MIN_M = 8.0
SPEED_TRUST_ACCURACY_M = 25.0
IMPROVE = 0.5
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
    trusted = acc <= SPEED_TRUST_ACCURACY_M
    moving = trusted and speed is not None and speed > MOVING_SPEED_MPS
    heading = live.get("heading") if moving else None

    out = {"latitude": raw[0], "longitude": raw[1], "snapped": False, "stationary": not moving,
           "road": None, "road_name": None, "accuracy": acc}

    prev_pt, prev_acc = None, None
    if previous and previous.get("display_latitude") is not None:
        prev_pt = (previous["display_latitude"], previous["display_longitude"])
        prev_acc = previous.get("display_accuracy")
    better = prev_acc is not None and acc <= IMPROVE * prev_acc
    # Both points carry a GPS error: two fixes of one parked phone can be acc + prev_acc apart.
    hold_radius = max(HOLD_MIN_M, acc + (prev_acc or 0.0))
    if not moving and prev_pt and not better and geo.haversine_m(*raw, *prev_pt) <= hold_radius:
        out.update(latitude=prev_pt[0], longitude=prev_pt[1], snapped=bool(previous.get("display_snapped")),
                   accuracy=prev_acc or acc)
    elif acc <= SNAP_MAX_M:
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
                diff = 0.0 if heading is None else geo.heading_diff_deg(heading, road_brg)
                aligned = diff <= SNAP_MAX_HEADING_DIFF or diff >= 180 - SNAP_MAX_HEADING_DIFF
                if d <= limit and aligned:
                    out.update(latitude=p[0], longitude=p[1], snapped=True)
                out["road"] = _window(coords, along)
                out["road_name"] = rc.road_name

    r = get_redis()
    await r.hset(_hash_key(vehicle_id), mapping={
        "display_latitude": out["latitude"], "display_longitude": out["longitude"],
        "display_snapped": "1" if out["snapped"] else "", "display_accuracy": out["accuracy"],
    })
    return out


def round_road(road: Optional[List[LatLon]]) -> Optional[List[List[float]]]:
    return [[round(lat, 6), round(lon, 6)] for lat, lon in road] if road else None
