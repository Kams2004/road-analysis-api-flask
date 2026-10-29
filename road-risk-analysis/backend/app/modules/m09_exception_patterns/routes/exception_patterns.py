"""
/exception-patterns — Module 9, Exception patterns (functional Module 5, RRAP-09; formerly
/driver-behaviour).

Anonymous statistics of the fleet exceptions stored in `historical_events`: by type, provider
(source), country, severity, speed band / excess over the limit, hour of day, weekday, over
time, and the places where they concentrate. No driver or vehicle anywhere — the events carry
none (app/modules/m08_historical/anonymity.py).

Every endpoint accepts the same filters: `period` (7d / 30d / 90d / 12m / all) or `date_from` /
`date_to`, `types`, `country`, `source`, `severity` and `bbox`. The answers are grouped reads
(views.py over app/modules/m08_historical/aggregates.py): daily summaries when they are
complete, the raw events otherwise — never every event in memory — cached 10 minutes per data
version (agg_cache.py).
"""
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.openapi import errors, legacy
from app.core.db.session import get_db
from app.modules.m03_maps import place_names
from app.modules.m08_historical import agg_cache
from app.modules.m08_historical import aggregates as G
from app.modules.m08_historical import rollups
from app.modules.m09_exception_patterns import analytics as A
from app.modules.m09_exception_patterns import service, views
from app.modules.m09_exception_patterns.settings import EXCEPTION_TYPES, SEVERITIES

router = APIRouter()

_FILTER_ERRORS = (
    legacy(422, "Unknown period '5d' — expected 7d, 30d, 90d, 12m or all", summary="unknown period"),
    legacy(422, "'bbox' must be 'south,west,north,east'", summary="malformed bbox"),
    legacy(422, "'severity' must be low, medium, high or unknown", summary="unknown severity"),
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
            "Exception types (repeat or comma-separate): `SPEEDING`, `HARD_BRAKING`, `HARD_ACCELERATION`, "
            "`HARSH_CORNERING`, `ACCIDENT`, `IDLE_VIOLATION`, `OTHER`. Default: all."), examples=[["SPEEDING"]]),
        country: Optional[List[str]] = Query(None, description=(
            "Only these countries (repeat or comma-separate), as sent by the source, e.g. `CAMEROUN`. "
            "List: `GET /exception-patterns/countries`."), examples=[["CAMEROUN"]]),
        source: Optional[List[str]] = Query(None, description=(
            "Only these sources / providers (repeat or comma-separate), e.g. `ymane`, `besafe`. "
            "List: `GET /exception-patterns/sources`."), examples=[["ymane"]]),
        severity: Optional[List[str]] = Query(None, description=(
            "Only these normalised severities (repeat or comma-separate): `low`, `medium`, `high`, `unknown`."),
            examples=[["high"]]),
        bbox: Optional[str] = Query(None, description="Only inside `south,west,north,east` (WGS84 degrees).",
                                    examples=["3.9,9.6,4.2,9.9"]),
    ):
        self.period, self.date_from, self.date_to = period, date_from, date_to
        self.types = service.clean_types(types)
        self.countries = G.clean_countries(country)
        self.sources = G.clean_values(source)
        self.severities = G.clean_values(severity, lower=True)
        if self.severities and set(self.severities) - set(SEVERITIES):
            raise HTTPException(422, "'severity' must be low, medium, high or unknown")
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
    s = G.Scope(win.start, win.end, f.types, f.countries, f.sources, f.severities, f.bbox, rollups=use, cfg=cfg)
    prev = s.shifted(win.prev_start, win.start) if win.has_previous else None
    return cfg, win, s, prev


def _params(f: Filters, **extra) -> dict:
    return {"period": f.period, "from": f.date_from, "to": f.date_to, "types": f.types, "countries": f.countries,
            "sources": f.sources, "severities": f.severities, "bbox": f.bbox, "today": datetime.utcnow().date(),
            **extra}


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
    """One exception type over the period."""
    event_type: str = Field(..., description="Exception type.", examples=["SPEEDING"])
    count: int = Field(..., description="Exceptions.")
    share: float = Field(..., description="Share of all exceptions of the selection (0–1).")
    by_severity: Dict[str, int] = Field(..., description="Exceptions per normalised severity (low/medium/high/unknown).")
    high_severity: int = Field(..., description="High-severity exceptions.")
    avg_speed_kmh: Optional[float] = Field(None, description="Average recorded speed, km/h (when sent).")
    max_speed_kmh: Optional[float] = Field(None, description="Highest recorded speed, km/h.")
    avg_limit_kmh: Optional[float] = Field(None, description="Average speed limit, km/h (when sent).")
    avg_excess_pct: Optional[float] = Field(None, description="Average excess over the limit, % (speeding).")
    peak_hour: Optional[int] = Field(None, description="Hour of day (local time) with most exceptions.")
    top_roads: List[Dict[str, Any]] = Field(..., description="Up to 3 roads with most exceptions: `{road_name, count}`.")


class OverviewOut(BaseModel):
    """Exception KPIs of the period, with the comparison period."""
    window: WindowOut
    total_events: int = Field(..., description="Exceptions in the period.")
    previous_total_events: Optional[int] = Field(None, description="Exceptions in the comparison period.")
    change_pct: Optional[float] = Field(None, description="Change vs the comparison period, %.")
    high_severity_events: int = Field(..., description="High-severity exceptions.")
    high_severity_share: float = Field(..., description="Their share (0–1).")
    avg_speed_kmh: Optional[float] = Field(None, description="Average recorded speed, km/h (exceptions with a speed).")
    max_speed_kmh: Optional[float] = Field(None, description="Highest recorded speed, km/h.")
    avg_excess_pct: Optional[float] = Field(None, description="Average excess over the limit, % (when known).")
    places: int = Field(..., description="Distinct ~110 m squares with exceptions (spread).")
    sources: int = Field(..., description="Sources / providers with exceptions in the period.")
    countries: int = Field(..., description="Countries with exceptions in the period.")
    first_event_at: Optional[datetime] = Field(None, description="Oldest exception of the period.")
    last_event_at: Optional[datetime] = Field(None, description="Most recent exception of the period.")
    data_first_event_at: Optional[datetime] = Field(None, description="Oldest exception stored (any period).")
    data_last_event_at: Optional[datetime] = Field(None, description="Most recent exception stored.")
    by_type: List[TypeSummary] = Field(..., description="Per exception type.")


class BreakdownRow(BaseModel):
    """Exceptions of one value of a dimension (provider, country, severity, band)."""
    key: str = Field(..., description=(
        "The value: source name, country (`\"\"` = not sent), severity, or band (`50-70` km/h, `10-20` % …)."),
        examples=["ymane"])
    count: int = Field(..., description="Exceptions.")
    share: float = Field(..., description="Share of the dimension's total (0–1).")
    high_severity: int = Field(..., description="High-severity exceptions.")
    by_type: Dict[str, int] = Field(..., description="Exceptions per type.")


class BreakdownsOut(BaseModel):
    """Exceptions broken down by provider, country, severity, speed and excess over the limit."""
    window: WindowOut
    by_source: List[BreakdownRow] = Field(..., description="Per source / provider, most exceptions first.")
    by_country: List[BreakdownRow] = Field(..., description="Per country, most exceptions first.")
    by_severity: List[BreakdownRow] = Field(..., description="Per normalised severity: low, medium, high, unknown.")
    by_speed_band: List[BreakdownRow] = Field(..., description=(
        "Per band of recorded speed, km/h (`0-30` … `110+`); exceptions without a speed are left out."))
    by_excess_band: List[BreakdownRow] = Field(..., description=(
        "Speeding per band of excess over the limit, % (`0-10` … `50+`); without a known limit left out."))


class ZoneOut(BaseModel):
    """A place where exceptions concentrate (grid cell of `zone_cell_size_m`)."""
    rank: int = Field(..., description="Position, most exceptions first.")
    cell_id: str = Field(..., description="Grid cell id.")
    latitude: float = Field(..., description="Cell centre latitude.")
    longitude: float = Field(..., description="Cell centre longitude.")
    radius_m: float = Field(..., description="Half the cell size, metres.")
    events: int = Field(..., description="Exceptions in the cell.")
    by_type: Dict[str, int] = Field(..., description="Exceptions per type.")
    dominant_type: str = Field(..., description="Most frequent exception type.")
    dominant_share: float = Field(..., description="Its share of the cell's exceptions (0–1).")
    multi_type: bool = Field(..., description="At least two exception types recorded here.")
    high_severity: int = Field(..., description="High-severity exceptions.")
    high_severity_share: float = Field(..., description="Their share (0–1).")
    road_name: Optional[str] = Field(None, description="Most common road.")
    place: Optional[str] = Field(None, description="Place name of the zone centre (reverse geocoding; null until looked up).")
    first_event_at: datetime = Field(..., description="Oldest exception.")
    last_event_at: datetime = Field(..., description="Most recent exception.")


class ZoneListOut(BaseModel):
    """Places meeting the configured zone criteria."""
    window: WindowOut
    criteria: Dict[str, Any] = Field(..., description="Applied criteria (`zone_cell_size_m`, `zone_min_events`, types).")
    total: int = Field(..., description="Zones meeting the criteria (before `limit`).")
    items: List[ZoneOut] = Field(..., description="Zones, most exceptions first.")


class HeatCell(BaseModel):
    """Exception concentration in one grid cell."""
    cell_id: str = Field(..., description="Grid cell id.")
    latitude: float = Field(..., description="Cell centre latitude.")
    longitude: float = Field(..., description="Cell centre longitude.")
    count: int = Field(..., description="Exceptions.")
    high_severity: int = Field(..., description="High-severity exceptions.")
    by_type: Dict[str, int] = Field(..., description="Exceptions per type.")


class HeatmapOut(BaseModel):
    """Exception heatmap — intensity is an exception count, not an accident probability."""
    window: WindowOut
    cell_size_m: float = Field(..., description="Cell size, metres.")
    max_count: int = Field(..., description="Largest cell count (for the colour scale).")
    cells: List[HeatCell] = Field(..., description="Cells with at least one exception.")


class TrendsOut(BaseModel):
    """Exceptions over time, by hour of day and by weekday."""
    window: WindowOut
    unit: str = Field(..., description="Bucket size: `day`, `week` or `month`.")
    types: List[str] = Field(..., description="Types counted.")
    series: List[Dict[str, Any]] = Field(..., description="`{bucket, total, high_severity, <TYPE>: n …}` per bucket.")
    by_hour: List[Dict[str, Any]] = Field(..., description="`{hour, total, <TYPE>: n …}`, local time (settings `timezone`).")
    by_weekday: List[Dict[str, Any]] = Field(..., description="`{weekday (0 = Monday), total, <TYPE>: n …}`, local time.")


class MethodologyOut(BaseModel):
    """How the exception statistics are computed."""
    version: str = Field(..., description="Methodology version.")
    severity_rule: str = Field(..., description="How the severity is normalised.")
    speed_bands_kmh: List[str] = Field(..., description="Bands of recorded speed, km/h.")
    excess_bands_pct: List[str] = Field(..., description="Bands of excess over the limit, %.")
    settings: Dict[str, Any] = Field(..., description="Current settings section `driver_behaviour`.")
    notes: List[str] = Field(..., description="Interpretation limits.")


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/overview", response_model=OverviewOut, summary="Get exception KPIs", responses=_BAD_FILTER)
async def overview(f: Filters = Depends(), db: AsyncSession = Depends(get_db)):
    """
    Exceptions of the period: counts per type, severity mix, speed vs limit, peak hour, main roads,
    spread (places, sources, countries) and the change against the previous period of the same length.
    """
    cfg, win, s, prev = await _scopes(f, db)

    async def compute():
        extent = await service.data_extent(db)
        return OverviewOut(window=WindowOut.of(win), data_first_event_at=extent["first_event_at"],
                           data_last_event_at=extent["last_event_at"],
                           **await views.overview(db, cfg, s, prev, f.types))
    return await agg_cache.cached("xp.overview", _params(f), compute, enabled=s.rollups)


@router.get("/breakdowns", response_model=BreakdownsOut, summary="Get exceptions by provider, country, severity and speed",
            responses=_BAD_FILTER)
async def breakdowns(f: Filters = Depends(), db: AsyncSession = Depends(get_db)):
    """
    Exceptions per source / provider, country, normalised severity, band of recorded speed and —
    for speeding with a known limit — band of excess over the limit, each with its type mix.
    """
    cfg, win, s, _ = await _scopes(f, db)

    async def compute():
        return BreakdownsOut(window=WindowOut.of(win), **await views.breakdowns(db, cfg, s, f.types))
    return await agg_cache.cached("xp.breakdowns", _params(f), compute, enabled=s.rollups)


@router.get("/trends", response_model=TrendsOut, summary="Get exception trends and time patterns",
            responses=_BAD_FILTER)
async def trends(f: Filters = Depends(),
                 unit: Optional[str] = Query(None, description="`day`, `week` or `month` (default: from the period length)."),
                 db: AsyncSession = Depends(get_db)):
    """Exceptions per day / week / month and type, and the hour-of-day and weekday patterns (local time)."""
    if unit not in (None, "day", "week", "month"):
        raise HTTPException(422, "'unit' must be day, week or month")
    cfg, win, s, _ = await _scopes(f, db)

    async def compute():
        return TrendsOut(window=WindowOut.of(win), types=f.types, **await views.trends(db, cfg, s, f.types, unit))
    return await agg_cache.cached("xp.trends", _params(f, unit=unit), compute, enabled=s.rollups)


@router.get("/heatmap", response_model=HeatmapOut, summary="Get the exception heatmap", responses=_BAD_FILTER)
async def heatmap(f: Filters = Depends(),
                  cell_size_m: Optional[float] = Query(None, ge=25, le=5000, description=(
                      "Cell size, metres (default settings `zone_cell_size_m`).")),
                  db: AsyncSession = Depends(get_db)):
    """
    Concentration of the selected exceptions per grid cell. The intensity is an exception count,
    not an accident probability (RRAP-09-008).
    """
    cfg, win, s, _ = await _scopes(f, db)
    size = cell_size_m or cfg.zone_cell_size_m

    async def compute():
        cells = await views.heatmap(db, cfg, s, size)
        return HeatmapOut(window=WindowOut.of(win), cell_size_m=size,
                          max_count=max((c["count"] for c in cells), default=0), cells=cells)
    return await agg_cache.cached("xp.heatmap", _params(f, size=size), compute, enabled=s.rollups)


@router.get("/zones", response_model=ZoneListOut, summary="List the places where exceptions concentrate",
            responses=_BAD_FILTER)
async def zones(f: Filters = Depends(), limit: int = Query(50, ge=1, le=1000, description="Maximum zones."),
                db: AsyncSession = Depends(get_db)):
    """
    Grid cells of `zone_cell_size_m` with at least `zone_min_events` exceptions of the selected
    types, most exceptions first, with their place name, main road and type mix. A zone is a
    pattern worth investigating, not a confirmed accident hotspot; Module 4 (`/historical`) gives
    the overall historical risk score of the same place.
    """
    cfg, win, s, _ = await _scopes(f, db)
    criteria = {"zone_cell_size_m": cfg.zone_cell_size_m, "zone_min_events": cfg.zone_min_events, "types": f.types}

    async def compute():
        every = await views.zones(db, cfg, s, 100_000)
        items = await place_names.attach(db, every[:limit])
        return ZoneListOut(window=WindowOut.of(win), criteria=criteria, total=len(every), items=items)
    return await agg_cache.cached("xp.zones", _params(f, limit=limit), compute, enabled=s.rollups)


class CountryOut(BaseModel):
    """A country present in the events (customer affiliate sent by the source)."""
    country: str = Field(..., examples=["CAMEROUN"])
    events: int = Field(..., description="Events stored for it (every type, all time).")


@router.get("/countries", response_model=List[CountryOut], summary="List the countries of the exceptions")
async def countries(db: AsyncSession = Depends(get_db)):
    """Countries found in the exceptions, most first — the values of the `country` filter."""
    cfg = await service.get_settings(db)
    use = await rollups.ready(db, cfg)

    async def compute():
        return [CountryOut(country=c, events=n) for c, n in await G.countries(db, use)]
    return await agg_cache.cached("xp.countries", {}, compute, enabled=use)


class SourceOut(BaseModel):
    """A source / provider of the exceptions (daily file provider, API, manual import)."""
    source: str = Field(..., examples=["ymane"])
    events: int = Field(..., description="Exceptions stored from it (every type, all time).")


@router.get("/sources", response_model=List[SourceOut], summary="List the sources / providers of the exceptions")
async def sources(db: AsyncSession = Depends(get_db)):
    """Sources found in the exceptions (e.g. `ymane`, `besafe`), most first — the values of the `source` filter."""
    cfg = await service.get_settings(db)
    use = await rollups.ready(db, cfg)

    async def compute():
        return [SourceOut(source=s, events=n) for s, n in await G.sources(db, use)]
    return await agg_cache.cached("xp.sources", {}, compute, enabled=use)


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


@router.get("/methodology", response_model=MethodologyOut, summary="Get the exception statistics method")
async def methodology(db: AsyncSession = Depends(get_db)):
    """Severity rule, speed / excess bands, zone criteria and time zone in force (settings section
    `driver_behaviour`), and the interpretation limits."""
    return A.methodology(await service.get_settings(db))


__all__ = ["router", "EXCEPTION_TYPES"]
