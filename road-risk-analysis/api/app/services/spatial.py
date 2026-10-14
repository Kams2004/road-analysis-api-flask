"""
Pure-SQL spatial helpers for PostgreSQL (no PostGIS required).

All distance calculations use the Haversine formula expressed directly in
SQL so that filtering happens inside the database before any rows are
transferred to the application layer.

Haversine in SQL
----------------
Given two points (lat1, lon1) and (lat2, lon2) in decimal degrees:

    a = sin²(Δlat/2) + cos(lat1)·cos(lat2)·sin²(Δlon/2)
    d = 2·R·asin(√a)          R = 6 371 000 m

We express this using only the functions available in standard PostgreSQL:
sin, cos, asin, sqrt, radians, power.

Point-to-segment distance
--------------------------
Given a point P and a segment AB we project P onto AB using the dot product,
clamp the projection parameter t ∈ [0, 1], then compute the distance from P
to the nearest point on AB.

Because the distances involved are short (< 100 km) we use a flat-earth
approximation for the projection: scale longitude differences by cos(midLat)
to get isometric (equal-distance) coordinates, do the projection there, then
apply Haversine for the final measurement.
"""

from math import radians, cos, sin, asin, sqrt
from typing import List, Tuple, Dict, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# ─── Python-side Haversine (used to build SQL expressions) ───────────────────

EARTH_R = 6_371_000  # metres


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Python Haversine — used only in unit tests / fallbacks."""
    r = EARTH_R
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * r * asin(sqrt(a))


# ─── SQL expression factories ─────────────────────────────────────────────────

def _sql_haversine(lat1_expr: str, lon1_expr: str, lat2_expr: str, lon2_expr: str) -> str:
    """
    Return a SQL fragment (string) that evaluates to the Haversine distance
    in metres between two lat/lon expressions.
    All four arguments must be valid SQL column references or literals.
    """
    return f"""
        2 * 6371000 * ASIN(
            SQRT(
                POWER(SIN((RADIANS({lat2_expr}) - RADIANS({lat1_expr})) / 2), 2)
                + COS(RADIANS({lat1_expr})) * COS(RADIANS({lat2_expr}))
                * POWER(SIN((RADIANS({lon2_expr}) - RADIANS({lon1_expr})) / 2), 2)
            )
        )
    """


# ─── Nearby query ─────────────────────────────────────────────────────────────

async def query_nearby(
    db: AsyncSession,
    lat: float,
    lon: float,
    radius_m: float,
) -> List[str]:
    """
    Return the IDs of validated detections whose GPS position is within
    `radius_m` metres of (lat, lon).

    Uses a bounding-box pre-filter (cheap index scan) then applies the
    exact Haversine formula to the surviving rows.
    """
    lat_delta = radius_m / 111_320.0
    lon_delta = radius_m / (111_320.0 * max(cos(radians(lat)), 1e-6))

    # asyncpg requires explicit casts when arithmetic is done on bound
    # parameters so PostgreSQL can resolve the operator unambiguously.
    sql = text("""
        SELECT d.id
        FROM   detections d
        WHERE  d.review_status = 'validated'
        AND    d.latitude  IS NOT NULL
        AND    d.longitude IS NOT NULL
        AND    d.latitude  BETWEEN CAST(:lat AS double precision) - CAST(:lat_delta AS double precision)
                               AND CAST(:lat AS double precision) + CAST(:lat_delta AS double precision)
        AND    d.longitude BETWEEN CAST(:lon AS double precision) - CAST(:lon_delta AS double precision)
                               AND CAST(:lon AS double precision) + CAST(:lon_delta AS double precision)
        AND    2 * 6371000 * ASIN(
                   SQRT(
                       POWER(SIN((RADIANS(d.latitude)  - RADIANS(CAST(:lat AS double precision))) / 2), 2)
                       + COS(RADIANS(CAST(:lat AS double precision))) * COS(RADIANS(d.latitude))
                       * POWER(SIN((RADIANS(d.longitude) - RADIANS(CAST(:lon AS double precision))) / 2), 2)
                   )
               ) <= CAST(:radius_m AS double precision)
    """)

    result = await db.execute(sql, {
        "lat":       lat,
        "lon":       lon,
        "lat_delta": lat_delta,
        "lon_delta": lon_delta,
        "radius_m":  radius_m,
    })
    return [row[0] for row in result.fetchall()]


# ─── Along-route query ────────────────────────────────────────────────────────

def _point_to_segment_m(
    p_lat: float, p_lon: float,
    a_lat: float, a_lon: float,
    b_lat: float, b_lon: float,
) -> float:
    """
    Python implementation of point-to-segment distance (metres).
    Used as fallback / unit tests; the SQL path is used for DB queries.
    """
    # flat-earth projection: scale lon by cos(mid_lat)
    mid_lat = radians((a_lat + b_lat) / 2)
    scale   = cos(mid_lat)

    ax = radians(a_lon) * scale;  ay = radians(a_lat)
    bx = radians(b_lon) * scale;  by = radians(b_lat)
    px = radians(p_lon) * scale;  py = radians(p_lat)

    dx = bx - ax;  dy = by - ay
    seg2 = dx * dx + dy * dy

    if seg2 == 0:
        return _haversine_m(p_lat, p_lon, a_lat, a_lon)

    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
    cx = ax + t * dx
    cy = ay + t * dy

    closest_lat = cy * 180 / 3.141592653589793
    closest_lon = (cx / scale) * 180 / 3.141592653589793
    return _haversine_m(p_lat, p_lon, closest_lat, closest_lon)


async def query_along_route(
    db: AsyncSession,
    waypoints: List[Tuple[float, float]],  # list of (lat, lon)
    corridor_m: float,
) -> List[str]:
    """
    Return the IDs of validated detections that lie within `corridor_m`
    metres of any segment of the polyline defined by `waypoints`.

    Strategy
    --------
    1. Compute a bounding box around the entire route (+ corridor padding)
       for an initial cheap pre-filter.
    2. For each detection that survives the bounding box, compute the
       point-to-segment distance against every route segment in Python
       (this is fast because the bounding box already limits the candidate
       set to a small number of rows — typically < a few hundred for a
       city-scale route).

    We do NOT try to express multi-segment point-to-segment distance in a
    single SQL query because it would require one CASE/UNNEST per segment
    and become both complex and slow without PostGIS.  The two-phase
    approach (SQL bbox → Python geometry) is the correct trade-off for a
    standard PostgreSQL setup.
    """
    if len(waypoints) < 2:
        return []

    lats = [p[0] for p in waypoints]
    lons = [p[1] for p in waypoints]
    min_lat, max_lat = min(lats), max(lats)
    min_lon, max_lon = min(lons), max(lons)

    # Expand bounding box by corridor in each direction
    lat_pad = corridor_m / 111_320.0
    mid_lat = (min_lat + max_lat) / 2
    lon_pad = corridor_m / (111_320.0 * max(cos(radians(mid_lat)), 1e-6))

    sql = text("""
        SELECT d.id, d.latitude, d.longitude
        FROM   detections d
        WHERE  d.review_status = 'validated'
        AND    d.latitude  IS NOT NULL
        AND    d.longitude IS NOT NULL
        AND    d.latitude  BETWEEN CAST(:min_lat AS double precision) - CAST(:lat_pad AS double precision)
                               AND CAST(:max_lat AS double precision) + CAST(:lat_pad AS double precision)
        AND    d.longitude BETWEEN CAST(:min_lon AS double precision) - CAST(:lon_pad AS double precision)
                               AND CAST(:max_lon AS double precision) + CAST(:lon_pad AS double precision)
    """)

    rows = (await db.execute(sql, {
        "min_lat": min_lat, "max_lat": max_lat,
        "min_lon": min_lon, "max_lon": max_lon,
        "lat_pad": lat_pad,
        "lon_pad": lon_pad,
    })).fetchall()

    # Phase 2 — exact point-to-segment check in Python
    matched_ids: List[str] = []
    for det_id, det_lat, det_lon in rows:
        for i in range(len(waypoints) - 1):
            dist = _point_to_segment_m(
                det_lat, det_lon,
                waypoints[i][0],   waypoints[i][1],
                waypoints[i + 1][0], waypoints[i + 1][1],
            )
            if dist <= corridor_m:
                matched_ids.append(det_id)
                break   # no need to check more segments for this detection

    return matched_ids


# ─── Clustering ───────────────────────────────────────────────────────────────

def cluster_detections(
    detections: List[Dict[str, Any]],
    radius_m: float,
) -> List[Dict[str, Any]]:
    """
    Greedy single-linkage clustering.

    Every detection belongs to exactly one cluster.  A detection that has no
    neighbour within `radius_m` forms a singleton cluster of size 1.

    Each detection dict must have at least: id, latitude, longitude.
    Returns a list of cluster dicts:
        {
            cluster_id:  int,
            centroid_lat: float,
            centroid_lon: float,
            count:        int,
            detection_ids: List[str],
        }
    """
    used = [False] * len(detections)
    clusters: List[Dict[str, Any]] = []

    for i, det in enumerate(detections):
        if used[i]:
            continue
        group = [det]
        used[i] = True

        for j in range(i + 1, len(detections)):
            if used[j]:
                continue
            dist = _haversine_m(
                det["latitude"],  det["longitude"],
                detections[j]["latitude"], detections[j]["longitude"],
            )
            if dist <= radius_m:
                group.append(detections[j])
                used[j] = True

        centroid_lat = sum(d["latitude"]  for d in group) / len(group)
        centroid_lon = sum(d["longitude"] for d in group) / len(group)
        clusters.append({
            "cluster_id":    len(clusters),
            "centroid_lat":  centroid_lat,
            "centroid_lon":  centroid_lon,
            "count":         len(group),
            "detection_ids": [d["id"] for d in group],
        })

    return clusters
