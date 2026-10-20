"""
Spatial helpers for Signalements — mirrors spatial.py for Detections.
No PostGIS required; uses bbox pre-filter + Python Haversine/point-to-segment.
Only 'actif' signalements are returned (cancelled/rejected are excluded).
"""
from math import radians, cos, sin, asin, sqrt
from typing import List, Tuple

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

EARTH_R = 6_371_000


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * EARTH_R * asin(sqrt(a))


def _point_to_segment_m(p_lat, p_lon, a_lat, a_lon, b_lat, b_lon) -> float:
    mid_lat = radians((a_lat + b_lat) / 2)
    scale   = cos(mid_lat)
    ax, ay  = radians(a_lon) * scale, radians(a_lat)
    bx, by  = radians(b_lon) * scale, radians(b_lat)
    px, py  = radians(p_lon) * scale, radians(p_lat)
    dx, dy  = bx - ax, by - ay
    seg2    = dx * dx + dy * dy
    if seg2 == 0:
        return _haversine_m(p_lat, p_lon, a_lat, a_lon)
    t  = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
    cx = ax + t * dx
    cy = ay + t * dy
    closest_lat = cy * 180 / 3.141592653589793
    closest_lon = (cx / scale) * 180 / 3.141592653589793
    return _haversine_m(p_lat, p_lon, closest_lat, closest_lon)


async def query_signalements_nearby(
    db: AsyncSession,
    lat: float,
    lon: float,
    radius_m: float,
) -> List[str]:
    """Return IDs of active signalements within radius_m metres of (lat, lon)."""
    lat_delta = radius_m / 111_320.0
    lon_delta = radius_m / (111_320.0 * max(cos(radians(lat)), 1e-6))

    sql = text("""
        SELECT s.id
        FROM   signalements s
        WHERE  s.status = 'actif'
        AND    s.latitude  BETWEEN CAST(:lat AS double precision) - CAST(:lat_delta AS double precision)
                               AND CAST(:lat AS double precision) + CAST(:lat_delta AS double precision)
        AND    s.longitude BETWEEN CAST(:lon AS double precision) - CAST(:lon_delta AS double precision)
                               AND CAST(:lon AS double precision) + CAST(:lon_delta AS double precision)
        AND    2 * 6371000 * ASIN(
                   SQRT(
                       POWER(SIN((RADIANS(s.latitude)  - RADIANS(CAST(:lat AS double precision))) / 2), 2)
                       + COS(RADIANS(CAST(:lat AS double precision))) * COS(RADIANS(s.latitude))
                       * POWER(SIN((RADIANS(s.longitude) - RADIANS(CAST(:lon AS double precision))) / 2), 2)
                   )
               ) <= CAST(:radius_m AS double precision)
    """)

    result = await db.execute(sql, {
        "lat": lat, "lon": lon,
        "lat_delta": lat_delta, "lon_delta": lon_delta,
        "radius_m": radius_m,
    })
    return [row[0] for row in result.fetchall()]


async def query_signalements_along_route(
    db: AsyncSession,
    waypoints: List[Tuple[float, float]],
    corridor_m: float,
) -> List[str]:
    """Return IDs of active signalements within corridor_m metres of the polyline."""
    if len(waypoints) < 2:
        return []

    lats = [p[0] for p in waypoints]
    lons = [p[1] for p in waypoints]
    min_lat, max_lat = min(lats), max(lats)
    min_lon, max_lon = min(lons), max(lons)
    lat_pad = corridor_m / 111_320.0
    lon_pad = corridor_m / (111_320.0 * max(cos(radians((min_lat + max_lat) / 2)), 1e-6))

    sql = text("""
        SELECT s.id, s.latitude, s.longitude
        FROM   signalements s
        WHERE  s.status = 'actif'
        AND    s.latitude  BETWEEN CAST(:min_lat AS double precision) - CAST(:lat_pad AS double precision)
                               AND CAST(:max_lat AS double precision) + CAST(:lat_pad AS double precision)
        AND    s.longitude BETWEEN CAST(:min_lon AS double precision) - CAST(:lon_pad AS double precision)
                               AND CAST(:max_lon AS double precision) + CAST(:lon_pad AS double precision)
    """)

    rows = (await db.execute(sql, {
        "min_lat": min_lat, "max_lat": max_lat,
        "min_lon": min_lon, "max_lon": max_lon,
        "lat_pad": lat_pad, "lon_pad": lon_pad,
    })).fetchall()

    matched: List[str] = []
    for sig_id, sig_lat, sig_lon in rows:
        for i in range(len(waypoints) - 1):
            if _point_to_segment_m(
                sig_lat, sig_lon,
                waypoints[i][0], waypoints[i][1],
                waypoints[i + 1][0], waypoints[i + 1][1],
            ) <= corridor_m:
                matched.append(sig_id)
                break
    return matched
