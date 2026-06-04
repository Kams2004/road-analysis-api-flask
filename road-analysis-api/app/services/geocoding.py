"""
Reverse geocoding via Nominatim (OpenStreetMap) — no API key required.
Returns a short human-readable string: "Street, City, Country"
Results are cached in-process to avoid redundant network calls for nearby coords.
"""
import logging
import httpx
from typing import Optional

logger = logging.getLogger(__name__)

_NOMINATIM = "https://nominatim.openstreetmap.org/reverse"
_HEADERS   = {"User-Agent": "RoadGuard/1.0 (road-analysis-api)"}
_CACHE: dict[tuple, Optional[str]] = {}
_GRID = 0.001  # ~110m — snap coords to grid before cache lookup


def _snap(v: float) -> float:
    return round(round(v / _GRID) * _GRID, 4)


def reverse_geocode(lat: float, lon: float) -> Optional[str]:
    """
    Return a short location string or None on failure.
    Result is cached per ~110m grid cell.
    """
    key = (_snap(lat), _snap(lon))
    if key in _CACHE:
        return _CACHE[key]

    try:
        r = httpx.get(
            _NOMINATIM,
            params={"lat": lat, "lon": lon, "format": "json", "zoom": 16, "addressdetails": 1},
            headers=_HEADERS,
            timeout=5,
        )
        r.raise_for_status()
        data = r.json()
        addr = data.get("address", {})

        parts = [
            addr.get("road") or addr.get("pedestrian") or addr.get("path"),
            addr.get("suburb") or addr.get("neighbourhood") or addr.get("village") or addr.get("town") or addr.get("city"),
            addr.get("state") or addr.get("region"),
            addr.get("country"),
        ]
        result = ", ".join(p for p in parts if p) or data.get("display_name", "").split(",")[0] or None
        _CACHE[key] = result
        return result
    except Exception as e:
        logger.debug(f"Reverse geocode failed ({lat},{lon}): {e}")
        _CACHE[key] = None
        return None
