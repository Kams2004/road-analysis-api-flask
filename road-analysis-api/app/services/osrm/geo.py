"""
Pure geometry helpers for the OSRM integration layer — haversine distance,
bearing, projection of a point onto a polyline, and arc-length interpolation.

Kept local to app/services/osrm rather than reusing app/services/spatial.py or
spatial_signalement.py: those already have their own near-identical Haversine
helpers scoped to their own modules (an existing pattern in this codebase),
and this package needs a couple of operations (bearing, point-to-polyline
projection with cumulative distance) that those modules don't provide.
Retrofitting a single shared util would touch working, unrelated code for no
functional gain here.
"""
import bisect
import math
from typing import List, Sequence, Tuple

EARTH_R_M = 6_371_000.0

LatLon = Tuple[float, float]


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return 2 * EARTH_R_M * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing from point 1 to point 2, degrees clockwise from north."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    y = math.sin(dlon) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def destination(lat: float, lon: float, bearing: float, distance_m: float) -> LatLon:
    """Great-circle destination point given a start, bearing and distance."""
    ang_dist = distance_m / EARTH_R_M
    brng = math.radians(bearing)
    phi1, lam1 = math.radians(lat), math.radians(lon)
    phi2 = math.asin(
        math.sin(phi1) * math.cos(ang_dist) + math.cos(phi1) * math.sin(ang_dist) * math.cos(brng)
    )
    lam2 = lam1 + math.atan2(
        math.sin(brng) * math.sin(ang_dist) * math.cos(phi1),
        math.cos(ang_dist) - math.sin(phi1) * math.sin(phi2),
    )
    return math.degrees(phi2), (math.degrees(lam2) + 540) % 360 - 180


def heading_diff_deg(a: float, b: float) -> float:
    """Smallest absolute angular difference between two headings, 0..180."""
    return abs(((a - b + 540) % 360) - 180)


def cumulative_distances_m(coords: Sequence[LatLon]) -> List[float]:
    """Cumulative distance (metres) to each point of a polyline, cum[0] == 0."""
    cum = [0.0]
    for i in range(1, len(coords)):
        cum.append(cum[-1] + haversine_m(*coords[i - 1], *coords[i]))
    return cum


def point_at_distance(coords: Sequence[LatLon], cum: Sequence[float], target_m: float) -> LatLon:
    """
    Inverse of project_point_onto_polyline: the point on the polyline `coords`
    (with precomputed cumulative distances `cum`, from cumulative_distances_m)
    that lies `target_m` metres along it from coords[0].

    Clamps to the polyline's ends rather than raising for an out-of-range
    target — callers that care about "ran out of known road ahead" should
    check `target_m` against `cum[-1]` themselves (see
    app/services/trajectory/predictor.py) rather than rely on this silently
    clamping.
    """
    if not coords:
        raise ValueError("point_at_distance requires at least one coordinate")
    if len(coords) == 1 or target_m <= 0:
        return coords[0]
    if target_m >= cum[-1]:
        return coords[-1]

    idx = bisect.bisect_right(cum, target_m) - 1
    idx = max(0, min(idx, len(coords) - 2))
    seg_start, seg_end = cum[idx], cum[idx + 1]
    t = 0.0 if seg_end <= seg_start else (target_m - seg_start) / (seg_end - seg_start)
    lat1, lon1 = coords[idx]
    lat2, lon2 = coords[idx + 1]
    return lat1 + (lat2 - lat1) * t, lon1 + (lon2 - lon1) * t


def project_point_onto_polyline(point: LatLon, coords: Sequence[LatLon]) -> Tuple[float, int]:
    """
    Project `point` onto the polyline `coords` (a list of (lat, lon)).

    Returns (distance_along_polyline_m, segment_index) — the cumulative
    distance from coords[0] to the closest point on the polyline, and the
    index i such that the closest point lies on segment (coords[i], coords[i+1]).

    Uses an equirectangular approximation (accurate enough over the few-
    hundred-metre windows this is used for) rather than full great-circle
    projection, to keep this dependency-free and fast.
    """
    if len(coords) < 2:
        return 0.0, 0

    cum = cumulative_distances_m(coords)
    mid_lat = math.radians(sum(c[0] for c in coords) / len(coords))
    scale = math.cos(mid_lat)

    def to_xy(lat: float, lon: float) -> Tuple[float, float]:
        return math.radians(lon) * scale, math.radians(lat)

    px, py = to_xy(*point)

    best_dist_along = 0.0
    best_perp_dist = math.inf
    best_idx = 0

    for i in range(len(coords) - 1):
        ax, ay = to_xy(*coords[i])
        bx, by = to_xy(*coords[i + 1])
        dx, dy = bx - ax, by - ay
        seg_len2 = dx * dx + dy * dy
        if seg_len2 == 0:
            t = 0.0
        else:
            t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg_len2))
        cx, cy = ax + t * dx, ay + t * dy
        perp_dist = math.hypot(px - cx, py - cy)
        if perp_dist < best_perp_dist:
            best_perp_dist = perp_dist
            best_idx = i
            seg_m = cum[i + 1] - cum[i]
            best_dist_along = cum[i] + t * seg_m

    return best_dist_along, best_idx


def local_xy_m(lat: float, lon: float, ref_lat: float) -> Tuple[float, float]:
    """
    Equirectangular projection to local metres around `ref_lat`, for short-
    range kinematic math (e.g. closest-point-of-approach between two moving
    vehicles over a few-hundred-metre, few-second window — see
    app/services/collision/detector.py) where a flat-plane approximation is
    accurate enough and much cheaper than doing vector calculus on a sphere.
    Not meant for anything long-range; accuracy degrades with distance from
    ref_lat and with distance from the origin of this projection.
    """
    scale = math.cos(math.radians(ref_lat))
    x = math.radians(lon) * scale * EARTH_R_M
    y = math.radians(lat) * EARTH_R_M
    return x, y


def point_to_segment_distance_m(point: LatLon, seg_a: LatLon, seg_b: LatLon) -> float:
    """Perpendicular distance (metres) from `point` to the line segment
    seg_a→seg_b — used for "is this point within N metres of any leg of a
    planned route" corridor queries (see
    app/services/historical_service.py's route-risk lookup; mirrors the
    same technique app/services/spatial_signalement.py's
    _point_to_segment_m uses, built on local_xy_m instead of duplicating
    the projection math inline)."""
    ref_lat = (seg_a[0] + seg_b[0]) / 2
    px, py = local_xy_m(*point, ref_lat)
    ax, ay = local_xy_m(*seg_a, ref_lat)
    bx, by = local_xy_m(*seg_b, ref_lat)

    dx, dy = bx - ax, by - ay
    seg_len2 = dx * dx + dy * dy
    t = 0.0 if seg_len2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg_len2))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)
