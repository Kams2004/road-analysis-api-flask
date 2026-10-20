from datetime import date
from typing import List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db.session import get_db
from app.modules.m08_historical import historical_service
from app.modules.m08_historical.domain import HistoricalEvent, ImportSummary, ZoneRiskProfile
from app.modules.m08_historical.sources.manual import ManualEventSource
from app.modules.m08_historical.sources.ymane import YmaneAuthError, YmaneEventSource, YmaneRequestError
from app.modules.m03_maps.osrm.client import OSRMUnavailableError, get_osrm_client
from app.api.openapi import errors, legacy

router = APIRouter()


# ── Import ──────────────────────────────────────────────────────────────────

class ImportIn(BaseModel):
    """Historical events to import, from a source over a date range."""
    source: str = Field(..., description=(  # "manual" | "ymane"
        "`manual` — the events are given in `events`; `ymane` — events are fetched from the Ymane "
        "telematics API (YMANE_USERNAME / YMANE_PASSWORD) for the date range."), examples=["manual"])
    start: date = Field(..., description="First day of the range (inclusive).", examples=["2026-09-01"])
    end: date = Field(..., description="Last day of the range (inclusive).", examples=["2026-09-30"])
    events: Optional[List[HistoricalEvent]] = Field(None, description=(  # required for source="manual", ignored otherwise
        "The events — required (non-empty) for `source=manual`, ignored otherwise."))


@router.post("/import", response_model=ImportSummary, summary="Import historical driving events",
             responses=errors(
                 legacy(422, "source=manual requires a non-empty 'events' list", summary="manual without events"),
                 legacy(422, "Unknown source 'csv' — expected 'manual' or 'ymane'", summary="unknown source"),
                 legacy(502, "Ymane authentication failed: YMANE_USERNAME/YMANE_PASSWORD not configured",
                        summary="Ymane authentication failed"),
                 legacy(502, "Ymane request failed: Ymane request to /events failed: HTTP 500: …",
                        summary="Ymane request failed")))
async def import_historical_events(body: ImportIn, db: AsyncSession = Depends(get_db)):
    """
    Imports telematics events (speeding, hard braking, accidents …) for `[start, end]`, map-matches
    each one onto the road network and aggregates it into its geographic grid cell
    (`HISTORICAL_GRID_CELL_SIZE_M`). Idempotent: events already imported (same `source` +
    `source_event_id`) are skipped and counted in `skipped_duplicates`; one bad event does not abort
    the batch (see `errors`).
    """
    if body.source == "manual":
        if not body.events:
            raise HTTPException(422, "source=manual requires a non-empty 'events' list")
        source = ManualEventSource(body.events)
    elif body.source == "ymane":
        source = YmaneEventSource()
    else:
        raise HTTPException(422, f"Unknown source '{body.source}' — expected 'manual' or 'ymane'")

    try:
        return await historical_service.import_events(source, body.start, body.end, db)
    except YmaneAuthError as e:
        raise HTTPException(502, f"Ymane authentication failed: {e}")
    except YmaneRequestError as e:
        raise HTTPException(502, f"Ymane request failed: {e}")


# ── Read: zone / route risk (what the mobile app's route-colouring idea
#    would call) ─────────────────────────────────────────────────────────────

@router.get("/risk/zone", response_model=List[ZoneRiskProfile], summary="Get historical risk around a position")
async def zone_risk(
    latitude: float = Query(..., description="WGS84 latitude of the centre (degrees).", examples=[4.0511]),
    longitude: float = Query(..., description="WGS84 longitude of the centre (degrees).", examples=[9.7679]),
    radius_m: float = Query(500.0, description="Search radius, metres."),
    db: AsyncSession = Depends(get_db),
):
    """
    Risk profile of every grid cell with at least one historical event within `radius_m` metres of the
    position, riskiest first. `historical_risk_score` (0–1) is a weighted event count (accident 5,
    hard braking / harsh cornering 1.5, hard acceleration 1.2, speeding 1, other 0.5, idle 0.3)
    saturating at `HISTORICAL_RISK_SATURATION_WEIGHT`.
    """
    return await historical_service.get_zone_risk_profiles(latitude, longitude, radius_m, db)


class RouteRiskIn(BaseModel):
    """A route polyline to score."""
    waypoints: List[Tuple[float, float]] = Field(..., description=(  # [(lat, lon), ...] — the full route polyline
        "The full route polyline as `[latitude, longitude]` pairs (WGS84 degrees), at least 2."),
        examples=[[[4.0511, 9.7679], [4.0620, 9.7350]]])
    corridor_m: float = Field(settings.HISTORICAL_ROUTE_CORRIDOR_M, description=(
        "Half-width of the corridor around the route, metres (default `HISTORICAL_ROUTE_CORRIDOR_M`)."))


@router.post("/risk/route", response_model=List[ZoneRiskProfile], summary="Get historical risk along a route",
             responses=errors(legacy(422, "At least 2 waypoints required.")))
async def route_risk(body: RouteRiskIn, db: AsyncSession = Depends(get_db)):
    """
    **Mobile / console** — risk profile of every grid cell with historical events within `corridor_m`
    metres of the route, riskiest first; used to colour a planned route by historical incident
    density. Scoring as in `GET /historical/risk/zone`. Read-only despite the POST.
    """
    if len(body.waypoints) < 2:
        raise HTTPException(422, "At least 2 waypoints required.")
    return await historical_service.get_zone_risk_profiles_along_route(body.waypoints, body.corridor_m, db)


# ── Route preview (web "Historical Risk Analysis": Départ → Destination) ──

class RoutePreview(BaseModel):
    """One driving route."""
    geometry: List[Tuple[float, float]] = Field(..., description=(  # [(lat, lon), ...]
        "Route polyline as `[latitude, longitude]` pairs (WGS84 degrees)."))
    distance_m: float = Field(..., description="Route length, metres.", examples=[245300.0])
    duration_s: float = Field(..., description="Estimated driving time, seconds.", examples=[14820.0])


class RoutePreviewOut(BaseModel):
    """Driving routes between two points."""
    routes: List[RoutePreview] = Field(..., description=(  # first = recommended, then alternatives
        "Up to 3 routes: the recommended one first, then alternatives."))


def _parse_latlon(raw: str, name: str) -> Tuple[float, float]:
    try:
        lat_s, lon_s = raw.split(",")
        lat, lon = float(lat_s), float(lon_s)
    except ValueError:
        raise HTTPException(422, f"'{name}' must be 'lat,lon'")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise HTTPException(422, f"'{name}' is out of range")
    return lat, lon


_ROUTE_PREVIEW_ERRORS = errors(
    legacy(404, "No route found between these points."),
    legacy(422, "'from' must be 'lat,lon'", summary="malformed point"),
    legacy(422, "'to' is out of range", summary="point out of range"),
    legacy(502, "Routing service unavailable: connection refused"),
)


@router.get("/route-preview", response_model=RoutePreviewOut, summary="Preview driving routes between two points",
            responses=_ROUTE_PREVIEW_ERRORS)
async def route_preview(
    from_: str = Query(..., alias="from", description="Start point as `lat,lon` (WGS84 degrees).",
                       examples=["4.0511,9.7679"]),
    to: str = Query(..., description="End point as `lat,lon` (WGS84 degrees).", examples=["3.8480,11.5021"]),
    alternatives: bool = Query(True, description="Also return alternative routes (up to 3 in total)."),
):
    """
    Driving route(s) between two points, computed by the same OSRM server the road-intelligence layer
    uses (`OSRM_BASE_URL`). Same as `GET /routes/preview`. 404 carries OSRM's message when it has one.
    """
    a, b = _parse_latlon(from_, "from"), _parse_latlon(to, "to")
    try:
        result = await get_osrm_client().route_overview([a, b], alternatives=alternatives)
    except OSRMUnavailableError as e:
        raise HTTPException(502, f"Routing service unavailable: {e}")
    data = result.data
    if data.get("code") != "Ok" or not data.get("routes"):
        raise HTTPException(404, data.get("message") or "No route found between these points.")
    routes = [
        RoutePreview(
            geometry=[(lat, lon) for lon, lat in r["geometry"]["coordinates"]],
            distance_m=float(r.get("distance") or 0.0),
            duration_s=float(r.get("duration") or 0.0),
        )
        for r in data["routes"][:3]
    ]
    return RoutePreviewOut(routes=routes)
