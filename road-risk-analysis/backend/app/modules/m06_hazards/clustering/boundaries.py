"""
Boundary search for agglomerations: "Douala" → candidate OSM areas with
their outline, through Nominatim search
(`format=jsonv2&polygon_geojson=1&countrycodes=…`).

Nominatim usage policy: identifying User-Agent (+ settings.GEOCODING_CONTACT
when set), ≤ 1 request/s for the process (async lock + min interval),
results cached 24 h (bounded), short timeout. Polygons are simplified
(Douglas-Peucker 0.0005° ≈ 55 m, tolerance doubled until ≤ 2000 vertices).
A result that is only a Point (a city node without boundary) becomes a
circle fallback sized from its bounding box (clamped 1–30 km), flagged
`approximate`.
"""
import asyncio
import logging
import time
from collections import OrderedDict
from math import cos, radians
from typing import Optional

import httpx

from app.core.config import settings
from app.modules.m06_hazards.clustering import zones

logger = logging.getLogger(__name__)

SIMPLIFY_TOLERANCE_DEG = 0.0005
MAX_VERTICES = 2000
_CACHE_TTL_S = 24 * 3600
_CACHE_MAX = 200
_cache: "OrderedDict[tuple, tuple[float, list]]" = OrderedDict()
_lock = asyncio.Lock()
_last_call = 0.0


class BoundarySearchError(Exception):
    pass


def _user_agent() -> str:
    contact = settings.GEOCODING_CONTACT
    return "VIASafe/1.0 (road-analysis-api; cluster configuration boundary search" + (
        f"; {contact})" if contact else ")")


def _bbox_from(raw) -> Optional[list[float]]:
    """Nominatim boundingbox [min_lat, max_lat, min_lon, max_lon] → [min_lat, min_lon, max_lat, max_lon]."""
    try:
        a, b, c, d = (float(x) for x in raw)
        return [a, c, b, d]
    except Exception:
        return None


def candidate_from(r: dict) -> Optional[dict]:
    """One Nominatim jsonv2 result → a boundary candidate (pure)."""
    geom = r.get("geojson") or {}
    try:
        lat, lon = float(r["lat"]), float(r["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    bbox = _bbox_from(r.get("boundingbox"))
    approximate = False
    if geom.get("type") in ("Polygon", "MultiPolygon"):
        geometry = zones.simplify(geom, SIMPLIFY_TOLERANCE_DEG, MAX_VERTICES)
    elif geom.get("type") in ("LineString", "MultiLineString"):
        return None                                       # roads etc. are not areas
    else:
        # Point: circle of half the bbox's smaller side (Nominatim gives
        # place nodes a size-dependent bbox), clamped 1–30 km.
        radius_m = 5000.0
        if bbox:
            h = (bbox[2] - bbox[0]) * zones.M_PER_DEG
            w = (bbox[3] - bbox[1]) * zones.M_PER_DEG * cos(radians(lat))
            radius_m = max(1000.0, min(30_000.0, min(h, w) / 2))
        geometry = zones.circle_polygon(lat, lon, radius_m)
        approximate = True
    b = zones.bbox_of(geometry)
    return {
        "display_name": r.get("display_name") or r.get("name") or "",
        "name": r.get("name") or (r.get("display_name") or "").split(",")[0],
        "osm_type": r.get("osm_type"),
        "osm_id": r.get("osm_id"),
        "class": r.get("category") or r.get("class"),
        "type": r.get("type"),
        "addresstype": r.get("addresstype"),
        "lat": lat, "lon": lon,
        "bbox": list(b) if b else bbox,
        "area_km2": round(zones.polygon_area_m2(geometry) / 1e6, 3),
        "vertices": zones.vertex_count(geometry),
        "approximate": approximate,
        "geometry": geometry,
    }


def rank(candidates: list[dict]) -> list[dict]:
    """Real boundaries first (polygons before circle fallbacks), keep Nominatim's order otherwise."""
    return sorted(candidates, key=lambda c: c["approximate"])


async def search(q: str, limit: int = 6, countrycodes: Optional[str] = None) -> list[dict]:
    """Candidate boundaries for a place name. Raises BoundarySearchError when Nominatim fails."""
    global _last_call
    q = q.strip()
    if not q:
        return []
    cc = settings.CLUSTERING_BOUNDARY_COUNTRYCODES if countrycodes is None else countrycodes
    params = {"q": q, "format": "jsonv2", "polygon_geojson": "1", "limit": str(max(1, min(limit, 10))),
              "addressdetails": "0"}
    if cc:
        params["countrycodes"] = cc
    if settings.GEOCODING_CONTACT:
        params["email"] = settings.GEOCODING_CONTACT
    key = tuple(sorted(params.items()))
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < _CACHE_TTL_S:
        _cache.move_to_end(key)
        return hit[1]
    async with _lock:
        wait = 1.0 - (time.monotonic() - _last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            async with httpx.AsyncClient(timeout=settings.CLUSTERING_BOUNDARY_TIMEOUT_S,
                                         headers={"User-Agent": _user_agent()}) as client:
                res = await client.get(settings.CLUSTERING_BOUNDARY_URL, params=params)
        except httpx.HTTPError as exc:
            logger.warning("Boundary search failed: %s", exc)
            raise BoundarySearchError("The boundary search service is unreachable.") from exc
        finally:
            _last_call = time.monotonic()
    if res.status_code != 200:
        logger.warning("Boundary search → HTTP %s", res.status_code)
        raise BoundarySearchError(f"The boundary search service answered HTTP {res.status_code}.")
    out = rank([c for c in (candidate_from(r) for r in res.json() or []) if c is not None])
    _cache[key] = (time.monotonic(), out)
    if len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)
    return out
