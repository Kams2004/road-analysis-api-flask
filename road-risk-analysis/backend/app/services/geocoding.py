"""
Reverse geocoding via Nominatim (OpenStreetMap) — no API key required.

`reverse_geocode_details(lat, lon)` returns a structured, human-readable
address of a position:

    road      street name                        "Rue Joss"
    locality  quartier / neighbourhood / suburb  "Akwa"
    district  arrondissement / city district     "Douala Ier"
    town      city / town / village              "Douala"
    region    state / region                     "Littoral"
    country                                       "Cameroun"
    display   short label = road, locality, town "Rue Joss, Akwa, Douala"

plus the legacy keys `location_name` ("Rue Joss, Akwa, Littoral, Cameroun"),
`city` (= town) and `country`. Every value may be None.

`parse_address()` turns a Nominatim answer into that dict; it is pure and
unit-tested on real Cameroonian answers.

Nominatim usage policy (https://operations.osmfoundation.org/policies/nominatim/):
identifying User-Agent with a contact (settings.GEOCODING_CONTACT), at most
one request per second for the whole process (lock + min interval), results
cached per ~110 m grid cell (bounded LRU; failures cached 10 minutes so an
outage is not hammered), short timeout. The calls are synchronous — the
processor (Celery worker) calls it directly, async code through
asyncio.to_thread.
"""
import logging
import re
import threading
import time
from collections import OrderedDict
from typing import Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

ADDRESS_KEYS = ("road", "locality", "district", "town", "region", "country")
_EMPTY = {k: None for k in (*ADDRESS_KEYS, "display", "location_name", "city")}

_GRID = 0.001            # ~110 m — snap coords to grid before cache lookup
_CACHE_MAX = 5000
_FAIL_TTL_S = 600.0
_CACHE: "OrderedDict[tuple, tuple[float, Optional[dict]]]" = OrderedDict()
_lock = threading.Lock()  # serialises upstream calls (≤ 1 request / interval)
_cache_lock = threading.Lock()
_last_call = 0.0


def _snap(v: float) -> float:
    return round(round(v / _GRID) * _GRID, 4)


# ── pure parsing ─────────────────────────────────────────────────────────────

def _first(addr: dict, *keys: str) -> Optional[str]:
    for k in keys:
        v = addr.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


# "Douala III", "Yaoundé VI", "Douala 5e", "Douala Ier" — an arrondissement of the city.
_ARRONDISSEMENT = re.compile(r"^(.+?)\s+(?:[IVX]{1,4}(?:er|e|ème)?|\d{1,2}\s?(?:er|e|ème))$", re.IGNORECASE)


def _split_arrondissement(name: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """("Douala III") → ("Douala", "Douala III"); ("Douala") → ("Douala", None)."""
    if not name:
        return None, None
    m = _ARRONDISSEMENT.match(name)
    return (m.group(1), name) if m else (name, None)


def _uniq(parts) -> list[str]:
    seen, out = set(), []
    for p in parts:
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out


def parse_address(addr: Optional[dict], display_name: Optional[str] = None) -> dict:
    """Structured address from a Nominatim `address` dict (see module doc)."""
    addr = addr or {}
    road = _first(addr, "road", "pedestrian", "footway", "street", "path", "cycleway", "track", "highway")
    locality = _first(addr, "quarter", "neighbourhood", "suburb", "hamlet", "city_block", "residential",
                      "isolated_dwelling", "allotments", "croft")
    town_raw = _first(addr, "city", "town", "village", "municipality")
    town, arr = _split_arrondissement(town_raw)
    district = _first(addr, "city_district", "district", "borough", "subdistrict") or arr or _first(addr, "county")
    if not town:
        # Rural point: no settlement tag — fall back on the department (county).
        town = _first(addr, "county") if district != _first(addr, "county") else None
    region = _first(addr, "state", "region", "state_district", "province")
    country = _first(addr, "country")
    if locality and town and locality.lower() == town.lower():
        locality = None

    display_parts = _uniq([road, locality, town])
    if len(display_parts) < 2:
        display_parts = _uniq([*display_parts, district, region])[:3]
    display = ", ".join(display_parts) or None
    if not display and display_name:
        display = ", ".join(p.strip() for p in display_name.split(",")[:3] if p.strip()) or None

    # Legacy "Street, Quarter, Region, Country" label (kept for compatibility).
    legacy = [
        road,
        _first(addr, "suburb", "neighbourhood", "quarter", "village", "town", "city"),
        _first(addr, "state", "region"),
        country,
    ]
    location_name = ", ".join(p for p in legacy if p) or \
        ((display_name or "").split(",")[0].strip() or None)

    return {
        "road": road, "locality": locality, "district": district, "town": town,
        "region": region, "country": country, "display": display,
        "location_name": location_name, "city": town,
    }


def empty_address() -> dict:
    return dict(_EMPTY)


def has_address(geo: Optional[dict]) -> bool:
    return bool(geo) and any(geo.get(k) for k in (*ADDRESS_KEYS, "display"))


def apply_to(obj, geo: dict, *, keep_location_name: bool = False) -> None:
    """Copy a geocoding result onto a Detection/Hazard-like object."""
    for k in ("road", "locality", "district", "town", "region"):
        setattr(obj, k, geo.get(k))
    obj.display_location = geo.get("display")
    if geo.get("country"):
        obj.country = geo["country"]
    if geo.get("city"):
        obj.city = geo["city"]
    if geo.get("location_name") and not (keep_location_name and getattr(obj, "location_name", None)):
        obj.location_name = geo["location_name"]


# ── network + cache ──────────────────────────────────────────────────────────

def _user_agent() -> str:
    contact = settings.GEOCODING_CONTACT.strip()
    return f"VIASafe/1.0 (road-risk-analysis; {contact})" if contact else \
        "VIASafe/1.0 (road-risk-analysis; reverse geocoding of road hazards)"


def reverse_geocode(lat: float, lon: float) -> Optional[str]:
    """Short legacy location string or None. Cached per ~110 m grid cell."""
    return reverse_geocode_details(lat, lon)["location_name"]


def reverse_geocode_details(lat: float, lon: float) -> dict:
    """Structured address of (lat, lon); every value None on failure / disabled."""
    if not settings.GEOCODING_ENABLED or lat is None or lon is None:
        return empty_address()
    key = (_snap(lat), _snap(lon))
    now = time.monotonic()
    with _cache_lock:
        hit = _CACHE.get(key)
        if hit is not None and (hit[1] is not None or now - hit[0] < _FAIL_TTL_S):
            _CACHE.move_to_end(key)
            return dict(hit[1] or _EMPTY)
    result = _lookup(lat, lon)
    with _cache_lock:
        _CACHE[key] = (time.monotonic(), result)
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
    return dict(result or _EMPTY)


def cache_clear() -> None:
    with _cache_lock:
        _CACHE.clear()


def _lookup(lat: float, lon: float) -> Optional[dict]:
    global _last_call
    params = {"lat": f"{lat:.6f}", "lon": f"{lon:.6f}", "format": "jsonv2", "zoom": 17, "addressdetails": 1}
    if settings.GEOCODING_CONTACT.strip() and "@" in settings.GEOCODING_CONTACT:
        params["email"] = settings.GEOCODING_CONTACT.strip()
    headers = {"User-Agent": _user_agent()}
    if settings.GEOCODING_LANGUAGE:
        headers["Accept-Language"] = settings.GEOCODING_LANGUAGE
    with _lock:  # usage policy: ≤ 1 request per GEOCODING_MIN_INTERVAL_S, process-wide
        wait = settings.GEOCODING_MIN_INTERVAL_S - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        try:
            r = httpx.get(settings.GEOCODING_URL, params=params, headers=headers,
                          timeout=settings.GEOCODING_TIMEOUT_S)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            logger.debug(f"Reverse geocode failed ({lat},{lon}): {e}")
            return None
        finally:
            _last_call = time.monotonic()
    if not isinstance(data, dict) or data.get("error"):
        return None
    return parse_address(data.get("address"), data.get("display_name"))
