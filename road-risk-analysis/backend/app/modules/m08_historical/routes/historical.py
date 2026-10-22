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


# ── Module 4 dashboard (web "Historical Risk Analysis"): overview, heatmap,
#    risk zones, trend, import history — overall risk only; per-behaviour
#    analytics live in /driver-behaviour (Module 5). ───────────────────────────

from datetime import datetime as _dt  # noqa: E402
from typing import Any, Dict  # noqa: E402

from app.modules.m09_driver_behaviour.analytics import bucket_unit, resolve_window  # noqa: E402
from app.modules.m09_driver_behaviour.routes.driver_behaviour import WindowOut, parse_bbox  # noqa: E402

_PERIOD_Q = Query("all", description="`7d`, `30d`, `90d`, `12m` or `all` (default). Ignored with dates.")
_FROM_Q = Query(None, description="First day (inclusive, UTC).")
_TO_Q = Query(None, description="Last day (inclusive, UTC).")
_BBOX_Q = Query(None, description="Only inside `south,west,north,east` (WGS84 degrees).")
_PERIOD_ERR = errors(legacy(422, "Unknown period '5d' — expected 7d, 30d, 90d, 12m or all", summary="unknown period"),
                     legacy(422, "'bbox' must be 'south,west,north,east'", summary="malformed bbox"))


def _win(period, date_from, date_to):
    try:
        return resolve_window(period, date_from, date_to, _dt.utcnow(), "all")
    except ValueError as e:
        raise HTTPException(422, str(e))


class CellOut(ZoneRiskProfile):
    """A cell risk profile with its data-coverage indicator."""
    coverage: str = Field(..., description=(
        "`low` (< 3 events: a low score is NOT a safe place), `medium` or `good` (RRAP-08-015)."))


def _cells(rows) -> List[CellOut]:
    return [CellOut(**p.model_dump(), coverage=historical_service.coverage_level(p.total_events))
            for p in historical_service._aggregate_by_cell(rows)]


class HistoricalOverviewOut(BaseModel):
    """Module 4 KPIs over the period."""
    window: WindowOut
    total_events: int = Field(..., description="Historical events in the period (every type).")
    cells: int = Field(..., description="Grid cells with at least one event.")
    high_risk_cells: int = Field(..., description="Cells with score ≥ 0.5.")
    low_coverage_cells: int = Field(..., description="Cells with < 3 events (sparse data).")
    max_score: float = Field(..., description="Highest cell score (0–1).")
    accidents: int = Field(..., description="ACCIDENT events.")
    sources: Dict[str, int] = Field(..., description="Events per source.")
    first_event_at: Optional[_dt] = Field(None, description="Oldest event of the period.")
    last_event_at: Optional[_dt] = Field(None, description="Most recent event of the period.")
    last_import_at: Optional[_dt] = Field(None, description="Most recent import (any period).")
    cell_size_m: float = Field(..., description="Grid cell size, metres (`HISTORICAL_GRID_CELL_SIZE_M`).")
    saturation_weight: float = Field(..., description="Weighted count giving score 1 (`HISTORICAL_RISK_SATURATION_WEIGHT`).")
    weights: Dict[str, float] = Field(..., description="Weight of each event type in the score.")


@router.get("/overview", response_model=HistoricalOverviewOut, summary="Get historical risk KPIs",
            responses=_PERIOD_ERR)
async def historical_overview(period: str = _PERIOD_Q, date_from: Optional[date] = _FROM_Q,
                              date_to: Optional[date] = _TO_Q, bbox: Optional[str] = _BBOX_Q,
                              db: AsyncSession = Depends(get_db)):
    """Events, scored cells, high-risk and low-coverage cells, sources and the scoring parameters."""
    from sqlalchemy import func, select
    from app.modules.m08_historical.models.historical_event import HistoricalEventRecord
    win = _win(period, date_from, date_to)
    rows = await historical_service.load_rows(db, win.start, win.end, parse_bbox(bbox))
    cells = historical_service._aggregate_by_cell(rows)
    srcs: Dict[str, int] = {}
    for r in rows:
        srcs[r.source] = srcs.get(r.source, 0) + 1
    last_import = (await db.execute(select(func.max(HistoricalEventRecord.imported_at)))).scalar()
    return HistoricalOverviewOut(
        window=WindowOut.of(win), total_events=len(rows), cells=len(cells),
        high_risk_cells=sum(1 for c in cells if c.historical_risk_score >= historical_service.HIGH_RISK_SCORE),
        low_coverage_cells=sum(1 for c in cells if c.total_events < historical_service.LOW_COVERAGE_EVENTS),
        max_score=max((c.historical_risk_score for c in cells), default=0.0),
        accidents=sum(1 for r in rows if r.event_type == "ACCIDENT"), sources=srcs,
        first_event_at=rows[0].occurred_at if rows else None, last_event_at=rows[-1].occurred_at if rows else None,
        last_import_at=last_import, cell_size_m=settings.HISTORICAL_GRID_CELL_SIZE_M,
        saturation_weight=settings.HISTORICAL_RISK_SATURATION_WEIGHT, weights=historical_service.weights(),
    )


@router.get("/heatmap", response_model=List[CellOut], summary="Get the historical risk heatmap",
            responses=_PERIOD_ERR)
async def historical_heatmap(period: str = _PERIOD_Q, date_from: Optional[date] = _FROM_Q,
                             date_to: Optional[date] = _TO_Q, bbox: Optional[str] = _BBOX_Q,
                             db: AsyncSession = Depends(get_db)):
    """Every grid cell with events in the period and its overall historical risk score (0–1), riskiest first."""
    win = _win(period, date_from, date_to)
    return _cells(await historical_service.load_rows(db, win.start, win.end, parse_bbox(bbox)))


@router.get("/zones", response_model=List[CellOut], summary="List the top historical risk zones",
            responses=_PERIOD_ERR)
async def historical_zones(period: str = _PERIOD_Q, date_from: Optional[date] = _FROM_Q,
                           date_to: Optional[date] = _TO_Q, bbox: Optional[str] = _BBOX_Q,
                           min_score: float = Query(0.5, ge=0, le=1, description="Minimum score (0–1)."),
                           limit: int = Query(50, ge=1, le=500, description="Maximum zones."),
                           db: AsyncSession = Depends(get_db)):
    """Risk zones: cells whose historical score is at least `min_score`, riskiest first, with their coverage."""
    win = _win(period, date_from, date_to)
    cells = _cells(await historical_service.load_rows(db, win.start, win.end, parse_bbox(bbox)))
    return [c for c in cells if c.historical_risk_score >= min_score][:limit]


class TrendPoint(BaseModel):
    """Historical risk in one time bucket."""
    bucket: _dt = Field(..., description="Bucket start (UTC).")
    events: int = Field(..., description="Events.")
    risk_points: float = Field(..., description="Type-weighted event count (the score's numerator).")
    accidents: int = Field(..., description="ACCIDENT events.")
    cells: int = Field(..., description="Distinct grid cells with events.")


class HistoricalTrendOut(BaseModel):
    """Risk evolution over time."""
    window: WindowOut
    unit: str = Field(..., description="`day`, `week` or `month`.")
    series: List[TrendPoint] = Field(..., description="Oldest bucket first.")


@router.get("/trends", response_model=HistoricalTrendOut, summary="Get the historical risk trend",
            responses=_PERIOD_ERR)
async def historical_trends(period: str = _PERIOD_Q, date_from: Optional[date] = _FROM_Q,
                            date_to: Optional[date] = _TO_Q, bbox: Optional[str] = _BBOX_Q,
                            unit: Optional[str] = Query(None, description="`day`, `week` or `month` (default: from the period)."),
                            db: AsyncSession = Depends(get_db)):
    """Whether road safety improves or deteriorates: events and weighted risk points per day / week / month."""
    if unit not in (None, "day", "week", "month"):
        raise HTTPException(422, "'unit' must be day, week or month")
    win = _win(period, date_from, date_to)
    rows = await historical_service.load_rows(db, win.start, win.end, parse_bbox(bbox))
    start = win.start or (rows[0].occurred_at if rows else win.end)
    u = unit or bucket_unit(start, win.end)
    return HistoricalTrendOut(window=WindowOut.of(win), unit=u,
                              series=historical_service.trend_series(rows, start, win.end, u) if rows or win.start else [])


class ImportBatchOut(BaseModel):
    """Events imported together (same source, same minute)."""
    source: str = Field(..., description="Source.")
    imported_at: Optional[str] = Field(None, description="Import time (UTC, minute).")
    events: int = Field(..., description="Events imported.")
    first_event_at: _dt = Field(..., description="Oldest event of the batch.")
    last_event_at: _dt = Field(..., description="Most recent event of the batch.")
    by_type: Dict[str, int] = Field(..., description="Events per type.")


@router.get("/imports", response_model=List[ImportBatchOut], summary="List historical import batches")
async def historical_imports(limit: int = Query(50, ge=1, le=500, description="Maximum batches."),
                             db: AsyncSession = Depends(get_db)):
    """Import history (driving event collection), newest first."""
    rows = await historical_service.load_rows(db)
    return historical_service.import_history(rows)[:limit]
