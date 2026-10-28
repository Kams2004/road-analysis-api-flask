"""
/driver-behaviour — Module 5, Driver Behaviour Analytics (RRAP-09).

Every endpoint reads the behaviour events (SPEEDING, HARD_BRAKING,
HARD_ACCELERATION, HARSH_CORNERING) stored in `historical_events` and accepts
the same filters: `period` (7d / 30d / 90d / 12m / all) or `date_from` /
`date_to`, `types`, `country`, `driver`, `vehicle` and `bbox`.

The answers are grouped reads (views.py over app/modules/m08_historical/aggregates.py): daily
summaries when they are complete, the raw events otherwise — never every event in memory —
cached 10 minutes per data version (agg_cache.py).
"""
import csv
import tempfile
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.openapi import errors, legacy
from app.core.db.session import get_db
from app.modules.m08_historical import agg_cache
from app.modules.m08_historical import aggregates as G
from app.modules.m08_historical import rollups
from app.modules.m09_driver_behaviour import analytics as A
from app.modules.m09_driver_behaviour import service, views
from app.modules.m09_driver_behaviour.settings import BEHAVIOUR_TYPES

router = APIRouter()
EXPORT_PAGE = 5000

_FILTER_ERRORS = (
    legacy(422, "Unknown period '5d' — expected 7d, 30d, 90d, 12m or all", summary="unknown period"),
    legacy(422, "'bbox' must be 'south,west,north,east'", summary="malformed bbox"),
)
_BAD_FILTER = errors(*_FILTER_ERRORS)


# ── Filters ──────────────────────────────────────────────────────────────────

class Filters:
    def __init__(
        self,
        period: Optional[str] = Query(None, description=(
            "`7d`, `30d`, `90d`, `12m` or `all` (default: settings `driver_behaviour.default_period`). "
            "Ignored when `date_from` / `date_to` are given."), examples=["90d"]),
        date_from: Optional[date] = Query(None, description="First day (inclusive, UTC)."),
        date_to: Optional[date] = Query(None, description="Last day (inclusive, UTC)."),
        types: Optional[List[str]] = Query(None, description=(
            "Behaviour types (repeat or comma-separate): `SPEEDING`, `HARD_BRAKING`, `HARD_ACCELERATION`, "
            "`HARSH_CORNERING`. Default: all four."), examples=[["SPEEDING"]]),
        country: Optional[List[str]] = Query(None, description=(
            "Only these countries (repeat or comma-separate), as sent by the source, e.g. `CAMEROUN`. "
            "List: `GET /driver-behaviour/countries`."), examples=[["CAMEROUN"]]),
        driver: Optional[str] = Query(None, description="Only this driver (source driver id)."),
        vehicle: Optional[str] = Query(None, description="Only this vehicle (source vehicle id)."),
        bbox: Optional[str] = Query(None, description="Only inside `south,west,north,east` (WGS84 degrees).",
                                    examples=["3.9,9.6,4.2,9.9"]),
    ):
        self.period, self.date_from, self.date_to = period, date_from, date_to
        self.types = service.clean_types(types)
        self.countries = G.clean_countries(country)
        self.driver, self.vehicle = driver or None, vehicle or None
        self.bbox = parse_bbox(bbox)


def parse_bbox(raw: Optional[str]):
    if not raw:
        return None
    try:
        s, w, n, e = (float(x) for x in raw.split(","))
    except ValueError:
        raise HTTPException(422, "'bbox' must be 'south,west,north,east'")
    if s > n or w > e:
        raise HTTPException(422, "'bbox' must be 'south,west,north,east'")
    return s, w, n, e


async def _window(f: Filters, db: AsyncSession):
    cfg = await service.get_settings(db)
    try:
        win = A.resolve_window(f.period, f.date_from, f.date_to, datetime.utcnow(), cfg.default_period)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return cfg, win


async def _scopes(f: Filters, db: AsyncSession):
    """(settings, window, scope, previous scope or None)."""
    cfg, win = await _window(f, db)
    use = await rollups.ready(db, cfg)
    s = G.Scope(win.start, win.end, f.types, f.countries, f.driver, f.vehicle, f.bbox, rollups=use)
    prev = s.shifted(win.prev_start, win.start) if win.has_previous else None
    return cfg, win, s, prev


def _params(f: Filters, **extra) -> dict:
    return {"period": f.period, "from": f.date_from, "to": f.date_to, "types": f.types, "countries": f.countries,
            "driver": f.driver, "vehicle": f.vehicle, "bbox": f.bbox, "today": datetime.utcnow().date(), **extra}


# ── Schemas ──────────────────────────────────────────────────────────────────

class WindowOut(BaseModel):
    """The analysed period."""
    label: str = Field(..., description="`7d` … `all`, or `custom`.")
    start: Optional[datetime] = Field(None, description="Start (UTC, inclusive); null = since the first event.")
    end: datetime = Field(..., description="End (UTC, exclusive).")
    previous_start: Optional[datetime] = Field(None, description="Start of the comparison period (same length).")

    @classmethod
    def of(cls, w):
        return cls(label=w.label, start=w.start, end=w.end, previous_start=w.prev_start)


class TypeSummary(BaseModel):
    """One behaviour type (speeding / harsh braking / acceleration / cornering) over the period."""
    event_type: str = Field(..., description="Behaviour type.", examples=["SPEEDING"])
    count: int = Field(..., description="Events.")
    share: float = Field(..., description="Share of all behaviour events (0–1).")
    points: float = Field(..., description="Weighted points (type weight × severity multiplier).")
    by_severity: Dict[str, int] = Field(..., description="Events per normalised severity (low/medium/high/unknown).")
    high_severity: int = Field(..., description="High-severity events.")
    avg_speed_kmh: Optional[float] = Field(None, description="Average recorded speed, km/h (when sent).")
    max_speed_kmh: Optional[float] = Field(None, description="Highest recorded speed, km/h.")
    avg_limit_kmh: Optional[float] = Field(None, description="Average speed limit, km/h (when sent).")
    avg_excess_pct: Optional[float] = Field(None, description="Average excess over the limit, % (speeding).")
    peak_hour: Optional[int] = Field(None, description="Hour of day (local time) with most events.")
    drivers: int = Field(..., description="Distinct drivers.")
    vehicles: int = Field(..., description="Distinct vehicles.")
    top_roads: List[Dict[str, Any]] = Field(..., description="Up to 3 roads with most events: `{road_name, count}`.")


class OverviewOut(BaseModel):
    """Behaviour KPIs of the period, with the comparison period."""
    window: WindowOut
    total_events: int = Field(..., description="Behaviour events in the period.")
    previous_total_events: Optional[int] = Field(None, description="Events in the comparison period.")
    change_pct: Optional[float] = Field(None, description="Change vs the comparison period, %.")
    points: float = Field(..., description="Total weighted points.")
    high_severity_events: int = Field(..., description="High-severity events.")
    drivers: int = Field(..., description="Distinct identified drivers.")
    vehicles: int = Field(..., description="Distinct identified vehicles.")
    identified_share: float = Field(..., description="Share of events carrying a driver or vehicle id (0–1).")
    high_risk_drivers: int = Field(..., description="Drivers whose score ≥ `high_risk_score`.")
    avg_driver_score: Optional[float] = Field(None, description="Average driver score (0–100).")
    first_event_at: Optional[datetime] = Field(None, description="Oldest event of the period.")
    last_event_at: Optional[datetime] = Field(None, description="Most recent event of the period.")
    data_first_event_at: Optional[datetime] = Field(None, description="Oldest behaviour event stored (any period).")
    data_last_event_at: Optional[datetime] = Field(None, description="Most recent behaviour event stored.")
    by_type: List[TypeSummary] = Field(..., description="Per behaviour type.")


class BehaviourEvent(BaseModel):
    """One driving-behaviour event."""
    id: str = Field(..., description="Event id.")
    source: str = Field(..., description="Source (`manual`, `ymane` …).")
    source_event_id: str = Field(..., description="The source's own id.")
    event_type: str = Field(..., description="Behaviour type.")
    occurred_at: datetime = Field(..., description="When it happened (UTC).")
    latitude: float = Field(..., description="Latitude (degrees).")
    longitude: float = Field(..., description="Longitude (degrees).")
    country: Optional[str] = Field(None, description="Country (customer affiliate) from the source.")
    transporter: Optional[str] = Field(None, description="Transporter from the source.")
    driver_external_id: Optional[str] = Field(None, description="Driver id from the source.")
    vehicle_external_id: Optional[str] = Field(None, description="Vehicle id from the source.")
    speed_kmh: Optional[float] = Field(None, description="Speed, km/h.")
    speed_limit_kmh: Optional[float] = Field(None, description="Speed limit, km/h.")
    excess_pct: Optional[float] = Field(None, description="Excess over the limit, %.")
    severity_raw: Optional[str] = Field(None, description="Severity as sent by the source.")
    severity: str = Field(..., description="Normalised severity: low / medium / high / unknown.")
    points: float = Field(..., description="Weighted points of this event.")
    road_name: Optional[str] = Field(None, description="Matched road name.")
    grid_cell_id: str = Field(..., description="Module 4 grid cell of the event.")


class EventListOut(BaseModel):
    """A page of behaviour events."""
    total: int = Field(..., description="Events matching the filters.")
    items: List[BehaviourEvent] = Field(..., description="This page.")


class EntityRow(BaseModel):
    """A driver or vehicle with its behaviour score over the period."""
    rank: int = Field(..., description="Position, riskiest first.")
    id: str = Field(..., description="Driver / vehicle id (from the source).")
    events: int = Field(..., description="Behaviour events.")
    by_type: Dict[str, int] = Field(..., description="Events per behaviour type.")
    high_severity: int = Field(..., description="High-severity events.")
    points: float = Field(..., description="Weighted points.")
    score: float = Field(..., description="Behaviour risk score 0–100 (see `GET /driver-behaviour/methodology`).")
    level: str = Field(..., description="`low`, `medium`, `high` or `very_high`.")
    previous_score: Optional[float] = Field(None, description="Score over the comparison period.")
    previous_events: Optional[int] = Field(None, description="Events over the comparison period.")
    score_delta: Optional[float] = Field(None, description="score − previous_score.")
    trend: Optional[str] = Field(None, description="`worse`, `better` or `stable` (±2 points).")
    linked: List[str] = Field(..., description="Vehicles driven (driver row) / drivers (vehicle row), up to 5.")
    last_event_at: datetime = Field(..., description="Most recent event.")
    top_road: Optional[str] = Field(None, description="Road with most events.")


class EntityListOut(BaseModel):
    """Ranking of drivers or vehicles."""
    window: WindowOut
    total: int = Field(..., description="Identified drivers / vehicles in the period.")
    unidentified_events: int = Field(..., description="Events without the identifier (not ranked).")
    items: List[EntityRow] = Field(..., description="This page, riskiest first.")


class ZoneOut(BaseModel):
    """A high-risk behaviour zone (grid cell of `zone_cell_size_m`)."""
    rank: int = Field(..., description="Position, most weighted points first.")
    cell_id: str = Field(..., description="Grid cell id.")
    latitude: float = Field(..., description="Cell centre latitude.")
    longitude: float = Field(..., description="Cell centre longitude.")
    radius_m: float = Field(..., description="Half the cell size, metres.")
    events: int = Field(..., description="Behaviour events in the cell.")
    points: float = Field(..., description="Weighted points.")
    score: float = Field(..., description="Points normalised 0–100 like a driver score.")
    by_type: Dict[str, int] = Field(..., description="Events per behaviour type.")
    dominant_type: str = Field(..., description="Most frequent behaviour type.")
    dominant_share: float = Field(..., description="Its share of the cell's events (0–1).")
    multi_behaviour: bool = Field(..., description="At least two behaviour types recorded here.")
    drivers: int = Field(..., description="Distinct drivers / vehicles.")
    high_severity: int = Field(..., description="High-severity events.")
    road_name: Optional[str] = Field(None, description="Most common road.")
    first_event_at: datetime = Field(..., description="Oldest event.")
    last_event_at: datetime = Field(..., description="Most recent event.")


class ZoneListOut(BaseModel):
    """Zones meeting the configured behaviour-risk criteria."""
    window: WindowOut
    criteria: Dict[str, Any] = Field(..., description="Applied criteria (`zone_cell_size_m`, `zone_min_events`, …).")
    items: List[ZoneOut] = Field(..., description="Zones, riskiest first.")


class HeatCell(BaseModel):
    """Event concentration in one grid cell."""
    cell_id: str = Field(..., description="Grid cell id.")
    latitude: float = Field(..., description="Cell centre latitude.")
    longitude: float = Field(..., description="Cell centre longitude.")
    count: int = Field(..., description="Events.")
    points: float = Field(..., description="Weighted points.")
    by_type: Dict[str, int] = Field(..., description="Events per behaviour type.")


class HeatmapOut(BaseModel):
    """Behaviour heatmap — intensity is an event count, not an accident probability."""
    window: WindowOut
    cell_size_m: float = Field(..., description="Cell size, metres.")
    max_count: int = Field(..., description="Largest cell count (for the colour scale).")
    cells: List[HeatCell] = Field(..., description="Cells with at least one event.")


class TrendsOut(BaseModel):
    """Behaviour events over time, by hour of day and by weekday."""
    window: WindowOut
    unit: str = Field(..., description="Bucket size: `day`, `week` or `month`.")
    types: List[str] = Field(..., description="Types counted.")
    series: List[Dict[str, Any]] = Field(..., description="`{bucket, total, points, <TYPE>: n …}` per bucket.")
    by_hour: List[Dict[str, Any]] = Field(..., description="`{hour, total, <TYPE>: n …}`, local time (settings `timezone`).")
    by_weekday: List[Dict[str, Any]] = Field(..., description="`{weekday (0 = Monday), total, <TYPE>: n …}`.")


class EntityDetailOut(BaseModel):
    """Behaviour profile of one driver or vehicle."""
    window: WindowOut
    kind: str = Field(..., description="`driver` or `vehicle`.")
    id: str = Field(..., description="Driver / vehicle id.")
    ranking: Optional[EntityRow] = Field(None, description="Its row of the ranking (score, trend …).")
    rank_of: int = Field(..., description="Number of ranked drivers / vehicles.")
    by_type: List[TypeSummary] = Field(..., description="Per behaviour type.")
    trends: Dict[str, Any] = Field(..., description="Same shape as `GET /driver-behaviour/trends`.")
    zones: List[HeatCell] = Field(..., description="Where its events happened (grid cells, most events first).")
    recent_events: List[BehaviourEvent] = Field(..., description="Up to 50 most recent events.")


class MethodologyOut(BaseModel):
    """Documented behaviour scoring method (RRAP-09-011)."""
    version: str = Field(..., description="Methodology version.")
    formula: str = Field(..., description="Score formula.")
    settings: Dict[str, Any] = Field(..., description="Current settings section `driver_behaviour`.")
    notes: List[str] = Field(..., description="Interpretation limits.")


def _event_out(r, cfg) -> BehaviourEvent:
    x = A.excess_pct(r)
    return BehaviourEvent(
        id=r.id, source=r.source, source_event_id=getattr(r, "source_event_id", "") or "",
        event_type=r.event_type, occurred_at=r.occurred_at, latitude=r.latitude, longitude=r.longitude,
        country=getattr(r, "country", None), transporter=getattr(r, "transporter", None),
        driver_external_id=r.driver_external_id, vehicle_external_id=r.vehicle_external_id,
        speed_kmh=A._kmh(r.speed_mps), speed_limit_kmh=A._kmh(r.speed_limit_mps),
        excess_pct=round(x, 1) if x is not None else None, severity_raw=r.severity,
        severity=A.normalize_severity(r, cfg), points=round(A.event_points(r, cfg), 2),
        road_name=r.road_name, grid_cell_id=r.grid_cell_id,
    )


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/overview", response_model=OverviewOut, summary="Get driver behaviour KPIs",
            responses=_BAD_FILTER)
async def overview(f: Filters = Depends(), db: AsyncSession = Depends(get_db)):
    """
    Speeding, harsh braking, hard acceleration and harsh cornering over the period: counts,
    severity mix, speed vs limit, peak hour, main roads, distinct drivers / vehicles, and the change
    against the previous period of the same length.
    """
    cfg, win, s, prev = await _scopes(f, db)

    async def compute():
        extent = await service.data_extent(db)
        return OverviewOut(window=WindowOut.of(win), data_first_event_at=extent["first_event_at"],
                           data_last_event_at=extent["last_event_at"],
                           **await views.overview(db, cfg, s, prev, f.types))
    return await agg_cache.cached("db.overview", _params(f), compute, enabled=s.rollups)


@router.get("/events", response_model=EventListOut, summary="List driver behaviour events",
            responses=_BAD_FILTER)
async def list_events(
    f: Filters = Depends(),
    search: Optional[str] = Query(None, description="Text in road name, driver, vehicle or source event id."),
    severity: Optional[str] = Query(None, description="Only this normalised severity (`low`, `medium`, `high`, `unknown`)."),
    sort: str = Query("-occurred_at", description="`-occurred_at` (default), `occurred_at`, `-speed`, `event_type`."),
    offset: int = Query(0, ge=0, description="Rows to skip."),
    limit: int = Query(50, ge=1, le=500, description="Page size."),
    db: AsyncSession = Depends(get_db),
):
    """Behaviour events with their location, speed / limit, normalised severity and weighted points, newest first."""
    cfg, win = await _window(f, db)
    if severity and severity not in ("low", "medium", "high", "unknown"):
        raise HTTPException(422, "'severity' must be low, medium, high or unknown")
    total, rows = await service.page_events(db, win.start, win.end, f.types, f.driver, f.vehicle, f.bbox,
                                            search, sort, offset, limit, f.countries, severity, cfg)
    return EventListOut(total=total, items=[_event_out(r, cfg) for r in rows])


@router.get("/events/export", summary="Export driver behaviour events as CSV",
            response_class=StreamingResponse,
            responses={200: {"content": {"text/csv": {}}, "description": "CSV file."}, **_BAD_FILTER})
async def export_events(f: Filters = Depends(), db: AsyncSession = Depends(get_db)):
    """The filtered events as a CSV file (RRAP-09-012), oldest first."""
    cfg, win = await _window(f, db)
    out = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+", newline="", encoding="utf-8")
    w = csv.writer(out)
    w.writerow(["occurred_at_utc", "event_type", "severity", "points", "country", "transporter", "driver", "vehicle",
                "speed_kmh", "speed_limit_kmh", "excess_pct", "latitude", "longitude", "road_name", "source",
                "event_id"])
    offset = 0
    while True:                                   # pages of EXPORT_PAGE: memory stays bounded
        _, rows = await service.page_events(db, win.start, win.end, f.types, f.driver, f.vehicle, f.bbox, None,
                                            "occurred_at", offset, EXPORT_PAGE, f.countries)
        for r in rows:
            x = A.excess_pct(r)
            w.writerow([r.occurred_at.isoformat(), r.event_type, A.normalize_severity(r, cfg),
                        round(A.event_points(r, cfg), 2), r.country or "", r.transporter or "",
                        r.driver_external_id or "", r.vehicle_external_id or "",
                        A._kmh(r.speed_mps) or "", A._kmh(r.speed_limit_mps) or "",
                        round(x, 1) if x is not None else "", r.latitude, r.longitude, r.road_name or "",
                        r.source, r.id])
        if len(rows) < EXPORT_PAGE:
            break
        offset += EXPORT_PAGE
    out.seek(0)

    def chunks():
        try:
            while True:
                part = out.read(256 * 1024)
                if not part:
                    break
                yield part
        finally:
            out.close()
    name = f"driver-behaviour-{datetime.utcnow():%Y%m%d-%H%M}.csv"
    return StreamingResponse(chunks(), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


async def _ranking(kind: str, f: Filters, db: AsyncSession, offset: int, limit: int) -> EntityListOut:
    cfg, win, s, prev = await _scopes(f, db)

    async def compute():
        rows, unidentified = await views.ranking(db, cfg, s, prev, kind)
        page = await views.with_top_roads(db, s, kind, rows[offset:offset + limit])
        return EntityListOut(window=WindowOut.of(win), total=len(rows), unidentified_events=unidentified,
                             items=[EntityRow(**r) for r in page])
    return await agg_cache.cached(f"db.rank.{kind}", _params(f, offset=offset, limit=limit), compute,
                                  enabled=s.rollups)


@router.get("/drivers", response_model=EntityListOut, summary="Rank drivers by behaviour risk",
            responses=_BAD_FILTER)
async def drivers(f: Filters = Depends(), offset: int = Query(0, ge=0, description="Rows to skip."),
                  limit: int = Query(50, ge=1, le=500, description="Page size."),
                  db: AsyncSession = Depends(get_db)):
    """
    Drivers ranked by behaviour risk score (0–100, weighted and normalised — see
    `GET /driver-behaviour/methodology`) with the trend against the previous period. Only events
    carrying a driver id are attributed (RRAP-09-004).
    """
    return await _ranking("driver", f, db, offset, limit)


@router.get("/vehicles", response_model=EntityListOut, summary="Rank vehicles by behaviour risk",
            responses=_BAD_FILTER)
async def vehicles(f: Filters = Depends(), offset: int = Query(0, ge=0, description="Rows to skip."),
                   limit: int = Query(50, ge=1, le=500, description="Page size."),
                   db: AsyncSession = Depends(get_db)):
    """Vehicles ranked by behaviour risk score, as `GET /driver-behaviour/drivers`."""
    return await _ranking("vehicle", f, db, offset, limit)


async def _detail(kind: str, entity_id: str, f: Filters, db: AsyncSession) -> EntityDetailOut:
    cfg, win, s, prev = await _scopes(f, db)
    ranking, _ = await views.ranking(db, cfg, s, prev, kind)
    row = next((r for r in ranking if r["id"] == entity_id), None)
    if row is not None:
        await views.with_top_roads(db, s, kind, [row])
    # One driver's / vehicle's own events: a bounded set, analysed event by event.
    mine = await service.load_events(db, win.start, win.end, f.types, entity_id if kind == "driver" else None,
                                     entity_id if kind == "vehicle" else None, f.bbox, f.countries)
    if row is None:
        if not await service.load_events(db, None, None, f.types, entity_id if kind == "driver" else None,
                                          entity_id if kind == "vehicle" else None):
            raise HTTPException(404, f"No behaviour events for {kind} '{entity_id}'.")
    return EntityDetailOut(
        window=WindowOut.of(win), kind=kind, id=entity_id,
        ranking=EntityRow(**row) if row else None, rank_of=len(ranking),
        by_type=A.type_summaries(mine, cfg, f.types),
        trends=A.trends(mine, win.start, win.end, cfg, f.types),
        zones=A.heatmap(mine, cfg.zone_cell_size_m, cfg)[:20],
        recent_events=[_event_out(e, cfg) for e in sorted(mine, key=lambda e: e.occurred_at, reverse=True)[:50]],
    )


@router.get("/drivers/{driver_id}", response_model=EntityDetailOut, summary="Get a driver's behaviour profile",
            responses=errors(legacy(404, "No behaviour events for driver 'D-17'."), *_FILTER_ERRORS))
async def driver_detail(driver_id: str, f: Filters = Depends(), db: AsyncSession = Depends(get_db)):
    """Score and trend, counts per behaviour type, time patterns, locations and recent events of one driver."""
    return await _detail("driver", driver_id, f, db)


@router.get("/vehicles/{vehicle_id}", response_model=EntityDetailOut, summary="Get a vehicle's behaviour profile",
            responses=errors(legacy(404, "No behaviour events for vehicle 'LT-458'."), *_FILTER_ERRORS))
async def vehicle_detail(vehicle_id: str, f: Filters = Depends(), db: AsyncSession = Depends(get_db)):
    """Score and trend, counts per behaviour type, time patterns, locations and recent events of one vehicle."""
    return await _detail("vehicle", vehicle_id, f, db)


@router.get("/heatmap", response_model=HeatmapOut, summary="Get the driver behaviour heatmap",
            responses=_BAD_FILTER)
async def heatmap(f: Filters = Depends(),
                  cell_size_m: Optional[float] = Query(None, ge=25, le=5000, description=(
                      "Cell size, metres (default settings `zone_cell_size_m`).")),
                  db: AsyncSession = Depends(get_db)):
    """
    Concentration of the selected behaviour types per grid cell. The intensity is an event count
    (and weighted points), not an accident probability (RRAP-09-008).
    """
    cfg, win, s, _ = await _scopes(f, db)
    size = cell_size_m or cfg.zone_cell_size_m

    async def compute():
        cells = await views.heatmap(db, cfg, s, size)
        return HeatmapOut(window=WindowOut.of(win), cell_size_m=size,
                          max_count=max((c["count"] for c in cells), default=0), cells=cells)
    return await agg_cache.cached("db.heatmap", _params(f, size=size), compute, enabled=s.rollups)


@router.get("/trends", response_model=TrendsOut, summary="Get driver behaviour trends",
            responses=_BAD_FILTER)
async def trends(f: Filters = Depends(),
                 unit: Optional[str] = Query(None, description="`day`, `week` or `month` (default: from the period length)."),
                 db: AsyncSession = Depends(get_db)):
    """Events per day / week / month and type, and the hour-of-day and weekday patterns (local time)."""
    if unit not in (None, "day", "week", "month"):
        raise HTTPException(422, "'unit' must be day, week or month")
    cfg, win, s, _ = await _scopes(f, db)

    async def compute():
        return TrendsOut(window=WindowOut.of(win), types=f.types, **await views.trends(db, cfg, s, f.types, unit))
    return await agg_cache.cached("db.trends", _params(f, unit=unit), compute, enabled=s.rollups)


@router.get("/zones", response_model=ZoneListOut, summary="List high-risk behaviour zones",
            responses=_BAD_FILTER)
async def zones(f: Filters = Depends(), limit: int = Query(100, ge=1, le=1000, description="Maximum zones."),
                db: AsyncSession = Depends(get_db)):
    """
    Areas where unsafe behaviours repeat: grid cells with at least `zone_min_events` events of the
    selected types (and `zone_min_drivers` distinct drivers when identities exist). A zone is a
    pattern worth investigating, not a confirmed accident hotspot. Module 4 (`/historical`) gives the
    overall historical risk score of the same place.
    """
    cfg, win, s, _ = await _scopes(f, db)
    criteria = {"zone_cell_size_m": cfg.zone_cell_size_m, "zone_min_events": cfg.zone_min_events,
                "zone_min_drivers": cfg.zone_min_drivers, "types": f.types}

    async def compute():
        return ZoneListOut(window=WindowOut.of(win), criteria=criteria, items=await views.zones(db, cfg, s, limit))
    return await agg_cache.cached("db.zones", _params(f, limit=limit), compute, enabled=s.rollups)


class CountryOut(BaseModel):
    """A country present in the events (customer affiliate sent by the source)."""
    country: str = Field(..., examples=["CAMEROUN"])
    events: int = Field(..., description="Events stored for it (every type, all time).")


@router.get("/countries", response_model=List[CountryOut], summary="List the countries of the events")
async def countries(db: AsyncSession = Depends(get_db)):
    """Countries found in the events, most events first — the values of the `country` filter."""
    cfg = await service.get_settings(db)
    use = await rollups.ready(db, cfg)

    async def compute():
        return [CountryOut(country=c, events=n) for c, n in await G.countries(db, use)]
    return await agg_cache.cached("db.countries", {}, compute, enabled=use)


class DataStatusOut(BaseModel):
    """Completeness of one daily event-file source."""
    name: str = Field(..., description="Source name.")
    first_day: Optional[date] = Field(None, description="First day received.")
    complete_until: Optional[date] = Field(None, description="Every day up to this one is imported.")
    due_until: date = Field(..., description="Last day whose file is due by now.")
    missing: List[date] = Field(..., description="Due days without a file (last 60 days).")
    failed: List[date] = Field(..., description="Days whose file could not be imported.")


@router.get("/data-status", response_model=List[DataStatusOut], summary="Get the completeness of the event data")
async def data_status(db: AsyncSession = Depends(get_db)):
    """For each daily event-file source: up to which day the data is complete and which days are
    missing — so a missing file is never mistaken for a day without incidents."""
    from app.modules.m08_historical import event_feed
    return [DataStatusOut(**h) for h in await event_feed.source_health(db)]


@router.get("/methodology", response_model=MethodologyOut, summary="Get the behaviour scoring method")
async def methodology(db: AsyncSession = Depends(get_db)):
    """The scoring formula, weights, severity rules and zone criteria in force (settings section `driver_behaviour`)."""
    return A.methodology(await service.get_settings(db))


__all__ = ["router", "BEHAVIOUR_TYPES"]
