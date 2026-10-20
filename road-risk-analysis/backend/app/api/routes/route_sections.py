"""
/routes — route-search helpers shared by the console pages that offer a
"Départ → Destination" search (Detection map, Historical risk analysis,
Community reporting):

  GET /routes/sections          predefined road sections (tronçons, settings
                                section `routes`) + the route corridor width
  GET /routes/preview           OSRM driving route(s) between two points
  GET /routes/geocode/search    place autocomplete (Nominatim, Cameroon-biased)
  GET /routes/geocode/reverse   place name of a position ("Ndoghem, Douala")

Readable with the View right of any of those pages, so they work without the
settings permission. Geocoding goes through the server (proper User-Agent,
shared cache, ≤ 1 upstream request/s) instead of every browser calling
Nominatim directly — browsers cannot set a User-Agent and are rejected by
Nominatim's usage policy depending on their headers / blockers.
"""
import asyncio
import logging
import re
import time
from collections import OrderedDict
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, api_error, get_principal
from app.api.openapi import err, errors
from app.api.routes.historical import _ROUTE_PREVIEW_ERRORS, RoutePreviewOut, route_preview
from app.core.config import settings
from app.db.session import get_db
from app.services.settings_service import MobileSettings, RouteSection, RoutesSettings, get_cached

logger = logging.getLogger(__name__)
router = APIRouter()

READ_MODULES = ("map", "historical", "signalements", "navigate", "detections")


async def route_reader(principal: Principal = Depends(get_principal)) -> Principal:
    """View right on any page that offers a route search."""
    if not settings.AUTH_ENFORCED:
        return principal
    if principal.kind != "user":
        raise api_error(401, "not_authenticated", "Please sign in.")
    if not any(principal.can(m, "view") for m in READ_MODULES):
        raise api_error(403, "forbidden", "You do not have permission for this action.",
                        module="map", action="view")
    return principal


route_reader.openapi_access = {"kind": "any_view", "modules": READ_MODULES}  # type: ignore[attr-defined]


# ── Predefined sections ─────────────────────────────────────────────────────

class RouteSectionsOut(BaseModel):
    """Predefined road sections and the route corridor width."""
    sections: list[RouteSection] = Field(..., description=(
        "Predefined road sections (tronçons) from settings section `routes`, each with `from` / `to` "
        "endpoints (`lat`, `lon`, `label`)."))
    corridor_m: int = Field(..., description=(
        "Corridor half-width used for along-route searches (`mobile.route_corridor_m`), metres."),
        examples=[60])


@router.get("/sections", response_model=RouteSectionsOut, summary="List predefined road sections")
async def list_sections(db: AsyncSession = Depends(get_db), _: Principal = Depends(route_reader)):
    """
    Road sections (tronçons, e.g. *Douala – Yaoundé (N3)*) the console offers as quick Départ →
    Destination choices in its route searches, plus the route corridor width. Editable in
    *Platform settings → routes*.
    """
    routes: RoutesSettings = await get_cached(db, "routes")  # type: ignore[assignment]
    mobile: MobileSettings = await get_cached(db, "mobile")  # type: ignore[assignment]
    return {
        "sections": [s.model_dump() for s in routes.sections],
        "corridor_m": mobile.route_corridor_m,
    }


@router.get("/preview", response_model=RoutePreviewOut, summary="Preview driving routes between two points",
            responses=_ROUTE_PREVIEW_ERRORS)
async def preview(
    from_: str = Query(..., alias="from", description="Start point as `lat,lon` (WGS84 degrees).",
                       examples=["4.0511,9.7679"]),
    to: str = Query(..., description="End point as `lat,lon` (WGS84 degrees).", examples=["3.8480,11.5021"]),
    alternatives: bool = Query(True, description="Also return alternative routes (up to 3 in total)."),
    _: Principal = Depends(route_reader),
):
    """
    Driving route(s) between two points through the platform's OSRM server: the recommended route
    first, then up to 2 alternatives, with length (metres) and duration (seconds).
    """
    return await route_preview(from_=from_, to=to, alternatives=alternatives)


# ── Geocoding (Nominatim proxy) ─────────────────────────────────────────────

_NOMINATIM = "https://nominatim.openstreetmap.org"
_HEADERS = {"User-Agent": "VIASafe/1.0 (road-analysis-api; web console geocoding)"}
_CAMEROON_VIEWBOX = "8.4,13.1,16.2,1.6"          # lon1,lat1,lon2,lat2
_CACHE_TTL_S = 6 * 3600
_CACHE_MAX = 2000
_cache: "OrderedDict[tuple, tuple[float, object]]" = OrderedDict()
_lock = asyncio.Lock()
_last_call = 0.0


class PlaceOut(BaseModel):
    """A place found by the geocoder."""
    label: str = Field(..., description="Main line, e.g. a neighbourhood or road.",  # main line, e.g. "Ndogbong"
                       examples=["Ndogbong"])
    secondary: str = Field("", description="Context line (district, city, region).",  # e.g. "Douala, Littoral"
                           examples=["Douala, Littoral"])
    lat: float = Field(..., description="WGS84 latitude (degrees).", examples=[4.0601])
    lon: float = Field(..., description="WGS84 longitude (degrees).", examples=[9.7414])


def _first(addr: dict, *keys: str) -> Optional[str]:
    for k in keys:
        if addr.get(k):
            return str(addr[k])
    return None


_ARRONDISSEMENT = re.compile(r"^(.+?)\s+[IVX]{1,4}$")


def _city(addr: dict) -> Optional[str]:
    """City name; "Douala III" / "Yaoundé VI" (arrondissements) → the city."""
    city = _first(addr, "city", "town", "village", "municipality", "county")
    m = _ARRONDISSEMENT.match(city or "")
    return m.group(1) if m else city


def _place(r: dict) -> PlaceOut:
    addr = r.get("address") or {}
    parts = [p.strip() for p in (r.get("display_name") or "").split(",") if p.strip()]
    label = r.get("name") or (parts[0] if parts else "")
    city = _city(addr)
    sec = [p for p in (
        _first(addr, "suburb", "neighbourhood", "quarter") if label != _first(addr, "suburb", "neighbourhood", "quarter") else None,
        city if city != label else None,
        _first(addr, "state", "region"),
    ) if p]
    secondary = ", ".join(dict.fromkeys(sec)) or ", ".join(parts[1:3])
    return PlaceOut(label=label or secondary, secondary=secondary if label else "",
                    lat=float(r["lat"]), lon=float(r["lon"]))


def _short_name(data: dict) -> str:
    """"Ndoghem, Douala" — neighbourhood (or road) + city."""
    addr = data.get("address") or {}
    local = _first(addr, "suburb", "neighbourhood", "quarter", "hamlet", "road", "pedestrian")
    city = _city(addr)
    name = ", ".join(dict.fromkeys(p for p in (local, city) if p))
    if not name:
        name = ", ".join([p.strip() for p in (data.get("display_name") or "").split(",")][:2])
    return name


async def _nominatim(path: str, params: dict) -> object:
    global _last_call
    key = (path, tuple(sorted(params.items())))
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < _CACHE_TTL_S:
        _cache.move_to_end(key)
        return hit[1]
    async with _lock:                                  # usage policy: ≤ 1 request / s
        wait = 1.0 - (time.monotonic() - _last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            async with httpx.AsyncClient(timeout=8, headers=_HEADERS) as client:
                res = await client.get(f"{_NOMINATIM}/{path}", params=params)
        finally:
            _last_call = time.monotonic()
    if res.status_code != 200:
        logger.warning("Nominatim %s → HTTP %s", path, res.status_code)
        raise api_error(502, "geocoding_failed", "The place search service is unavailable.")
    data = res.json()
    _cache[key] = (time.monotonic(), data)
    if len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)
    return data


def _viewbox(raw: Optional[str]) -> str:
    if not raw:
        return _CAMEROON_VIEWBOX
    try:
        a, b, c, d = (float(x) for x in raw.split(","))
    except ValueError:
        raise api_error(422, "invalid_viewbox", "viewbox must be 'lon1,lat1,lon2,lat2'.")
    return f"{a:.4f},{b:.4f},{c:.4f},{d:.4f}"


_GEOCODE_FAILED = err(502, "geocoding_failed", "The place search service is unavailable.")

# ── Photon (prefix / typo-tolerant autocomplete, OpenStreetMap data) ─────────
# Nominatim only matches whole words ("Bafous" → nothing) and ranks the pieces
# of the "Douala-Bafoussam" road above the city; Photon fixes both. Results are
# re-ranked towns first, and Nominatim stays the fallback.

_PHOTON = "https://photon.komoot.io/api/"
_CAMEROON_BBOX = "8.3,1.6,16.2,13.1"              # lon_min,lat_min,lon_max,lat_max
_PLACE_RANK = {"city": 0, "town": 1, "municipality": 1, "administrative": 2, "village": 3,
               "suburb": 4, "quarter": 4, "neighbourhood": 5, "hamlet": 5, "locality": 5}


def _place_rank(osm_key: Optional[str], osm_value: Optional[str]) -> int:
    """Towns before districts before anything else; roads last."""
    if osm_key == "highway":
        return 9
    return _PLACE_RANK.get(osm_value or "", 7)


def _photon_place(f: dict) -> tuple[int, PlaceOut]:
    p = f.get("properties") or {}
    lon, lat = f["geometry"]["coordinates"][:2]
    label = str(p["name"])
    city = p.get("city") or p.get("county")
    m = _ARRONDISSEMENT.match(city or "")
    city = m.group(1) if m else city
    sec = [x for x in (p.get("district") if p.get("district") != label else None,
                       city if city != label else None, p.get("state")) if x]
    return _place_rank(p.get("osm_key"), p.get("osm_value")), PlaceOut(
        label=label, secondary=", ".join(dict.fromkeys(sec)), lat=float(lat), lon=float(lon))


def _viewbox_center(raw: Optional[str]) -> Optional[tuple[float, float]]:
    try:
        a, b, c, d = (float(x) for x in (raw or "").split(","))
    except ValueError:
        return None
    return (b + d) / 2, (a + c) / 2


async def _photon_search(q: str, lang: str, limit: int, viewbox: Optional[str]) -> list[PlaceOut]:
    params = {"q": q, "limit": str(max(limit, 10)), "bbox": _CAMEROON_BBOX,
              "lang": lang[:2] if lang[:2] in ("fr", "en", "de") else "default"}
    center = _viewbox_center(viewbox)
    if center:
        params.update({"lat": f"{center[0]:.4f}", "lon": f"{center[1]:.4f}", "location_bias_scale": "0.2"})
    key = ("photon", tuple(sorted(params.items())))
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < _CACHE_TTL_S:
        data = hit[1]
    else:
        async with httpx.AsyncClient(timeout=8, headers=_HEADERS) as client:
            res = await client.get(_PHOTON, params=params)
        res.raise_for_status()
        data = res.json()
        _cache[key] = (time.monotonic(), data)
        if len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    ranked: list[tuple[int, int, PlaceOut]] = []
    for i, f in enumerate(data.get("features") or [] if isinstance(data, dict) else []):
        if (f.get("properties") or {}).get("countrycode", "CM") != "CM":
            continue
        try:
            rank, place = _photon_place(f)
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        ranked.append((rank, i, place))
    seen: set[tuple[str, str]] = set()
    out: list[PlaceOut] = []
    for _, _, place in sorted(ranked, key=lambda x: (x[0], x[1])):
        k = (place.label.lower(), place.secondary.lower())
        if k not in seen:
            seen.add(k)
            out.append(place)
    return out[:limit]


@router.get("/geocode/search", response_model=list[PlaceOut], summary="Search places (autocomplete)",
            responses=errors(err(422, "invalid_viewbox", "viewbox must be 'lon1,lat1,lon2,lat2'."),
                             _GEOCODE_FAILED))
async def geocode_search(
    q: str = Query(..., min_length=2, max_length=200, description="Text typed by the user (2–200 characters).",
                   examples=["Ndogbong"]),
    lang: str = Query("fr", max_length=10, description="Preferred language of the names (`Accept-Language`)."),
    viewbox: Optional[str] = Query(None, description="lon1,lat1,lon2,lat2 — preferred area (current map view)"),
    limit: int = Query(6, ge=1, le=10, description="Maximum number of places (1–10)."),
    _: Principal = Depends(route_reader),
):
    """
    Place autocomplete (OpenStreetMap data), restricted to Cameroon and biased towards `viewbox`
    (default: the whole country). Photon first: it matches word prefixes and tolerates typos
    (*Bafous*, *bafousaam* → Bafoussam); results are ranked towns first, then districts, roads last.
    Nominatim is the fallback (whole words only). Proxied by the server and cached for 6 hours;
    Nominatim calls are throttled to one request per second.
    """
    try:
        found = await _photon_search(q.strip(), lang, limit, viewbox)
        if found:
            return found
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("Photon search failed (%s) — falling back to Nominatim", exc)
    data = await _nominatim("search", {
        "q": q.strip(), "format": "jsonv2", "addressdetails": "1", "limit": str(limit),
        "countrycodes": "cm", "viewbox": _viewbox(viewbox), "accept-language": lang,
    })
    out: list[PlaceOut] = []
    for r in data if isinstance(data, list) else []:
        try:
            out.append(_place(r))
        except (KeyError, TypeError, ValueError):
            continue
    return out


@router.get("/geocode/reverse", response_model=PlaceOut, summary="Get the place name of a position",
            responses=errors(_GEOCODE_FAILED))
async def geocode_reverse(
    lat: float = Query(..., ge=-90, le=90, description="WGS84 latitude (degrees).", examples=[4.0601]),
    lon: float = Query(..., ge=-180, le=180, description="WGS84 longitude (degrees).", examples=[9.7414]),
    lang: str = Query("fr", max_length=10, description="Preferred language of the names (`Accept-Language`)."),
    _: Principal = Depends(route_reader),
):
    """
    Short place name of a position (e.g. *Ndoghem, Douala*) through Nominatim, with the region and
    country as `secondary`. When nothing is found, `label` is the formatted coordinates. Cached and
    throttled like the search.
    """
    data = await _nominatim("reverse", {
        "lat": f"{lat:.5f}", "lon": f"{lon:.5f}", "format": "jsonv2", "zoom": "16",
        "addressdetails": "1", "accept-language": lang,
    })
    if not isinstance(data, dict) or data.get("error"):
        return PlaceOut(label=f"{lat:.5f}, {lon:.5f}", lat=lat, lon=lon)
    addr = data.get("address") or {}
    return PlaceOut(label=_short_name(data) or f"{lat:.5f}, {lon:.5f}",
                    secondary=", ".join(p for p in (_first(addr, "state", "region"), addr.get("country")) if p),
                    lat=lat, lon=lon)
