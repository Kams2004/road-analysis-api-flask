"""
Data model for Step 6.1 — Historical Data & AI Dataset Preparation.

This is deliberately NOT the AI model itself (that's 6.2+ — feature
engineering, target definition, training). 6.1's job is narrower: define a
canonical, source-independent shape for a historical driving event
(speeding, hard braking, accident, ...), map-match it onto the road network
the same way live telemetry is (Step 2), and aggregate it into a durable,
queryable risk profile per geographic zone — the raw material 6.2+ will
build features from, and something already independently useful today (the
mobile app coloring a planned route by historical incident density).

Why a source-independent event shape
─────────────────────────────────────
The obvious first source is Ymane (see sources/ymane.py) — but the exact
response shape from Ymane's API is unconfirmed as of this writing (their
data endpoints are currently returning 500s independent of request shape;
see conversation notes). Rather than build the whole pipeline around
Ymane's specific field names, every source adapter (sources/base.py)
translates into this one canonical HistoricalEvent shape. When Ymane access
is restored, only sources/ymane.py's field-mapping needs adjusting — engine
code (matcher.py, historical_service.py) doesn't change. The same shape
also accepts manually-entered or CSV-imported events (sources/manual.py),
so a fleet without Ymane access isn't blocked from using this at all.

Why a geographic grid, not road_segment_id, for aggregation
─────────────────────────────────────────────────────────────
app/modules/m03_maps/osrm/models.py already documents why OSRM/OSM ids aren't
treated as durable business keys — they can shift when the road dataset is
refreshed (see osrm/README.md's "replacing/updating the dataset" section).
That caveat matters far more here: historical events accumulate over
months/years and must remain comparable across OSRM dataset refreshes. So
the primary aggregation key is a stable geographic grid cell
(app/modules/m08_historical/grid.py), with the matched road name/segment kept
only as descriptive metadata for the *current* dataset, not the join key.
"""
import enum
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field


class HistoricalEventType(str, enum.Enum):
    """Canonical event taxonomy — every source adapter maps its own vendor-
    specific type codes into these, so nothing downstream needs to know
    Ymane's (or anyone else's) internal type IDs."""
    SPEEDING = "SPEEDING"
    HARD_BRAKING = "HARD_BRAKING"
    HARD_ACCELERATION = "HARD_ACCELERATION"
    HARSH_CORNERING = "HARSH_CORNERING"
    ACCIDENT = "ACCIDENT"
    IDLE_VIOLATION = "IDLE_VIOLATION"
    OTHER = "OTHER"


class HistoricalEvent(BaseModel):
    """One driving event, in canonical form, before map-matching."""
    source: str = Field(..., description="Source name, e.g. `manual` or `ymane`.",  # e.g. "ymane", "manual"
                        examples=["manual"])
    source_event_id: str = Field(..., description=(  # source's own id — used for idempotent re-import
        "The source's own event id; `source` + `source_event_id` make re-imports idempotent."),
        examples=["evt-000123"])
    event_type: HistoricalEventType = Field(..., description=(
        "`SPEEDING`, `HARD_BRAKING`, `HARD_ACCELERATION`, `HARSH_CORNERING`, `ACCIDENT`, `IDLE_VIOLATION` "
        "or `OTHER`."), examples=["HARD_BRAKING"])
    occurred_at: datetime = Field(..., description="When the event happened (UTC if no offset).")

    latitude: float = Field(..., description="WGS84 latitude (degrees).", examples=[4.0511])
    longitude: float = Field(..., description="WGS84 longitude (degrees).", examples=[9.7679])

    vehicle_external_id: Optional[str] = Field(None, description=(  # the SOURCE's vehicle identifier — not our Vehicle.id (Step 1)
        "The source's vehicle identifier (not a platform vehicle id)."))
    driver_external_id: Optional[str] = Field(None, description="The source's driver identifier.")

    speed_mps: Optional[float] = Field(None, description="Speed at the event, m/s.")
    speed_limit_mps: Optional[float] = Field(None, description="Speed limit at the event, m/s.")
    severity: Optional[str] = Field(None, description=(  # raw severity/level from the source, if any — not yet normalized
        "Raw severity from the source, not normalised."))

    raw: Dict[str, Any] = Field({}, description=(  # untouched source payload for this event — forward-compatible, debuggable
        "Untouched source payload, kept for reference."))


class MapMatchedHistoricalEvent(BaseModel):
    """A HistoricalEvent plus where it actually sits on the road network —
    see app/modules/m08_historical/matcher.py."""
    event: HistoricalEvent
    grid_cell_id: str
    matched_latitude: Optional[float] = None
    matched_longitude: Optional[float] = None
    road_name: Optional[str] = None
    road_segment_id: Optional[str] = None  # descriptive only — see module docstring
    match_confidence: Optional[float] = None


class ImportSummary(BaseModel):
    """Result of an import run."""
    source: str = Field(..., description="Source imported from.")
    date_range: Tuple[date, date] = Field(..., description="`[start, end]` imported (inclusive).")
    fetched: int = Field(..., description="Events received from the source.")
    matched: int = Field(..., description="Events map-matched onto the road network.")
    inserted: int = Field(..., description="New events stored.")
    skipped_duplicates: int = Field(..., description="Events already imported earlier (skipped).")
    errors: List[str] = Field([], description="Per-event problems (the batch continues).")


class EventTypeCount(BaseModel):
    """Number of events of one type."""
    event_type: HistoricalEventType = Field(..., description="Event type.")
    count: int = Field(..., description="Number of events.")


class ZoneRiskProfile(BaseModel):
    """Aggregated historical risk for one grid cell. `historical_risk_score`
    is a deliberately simple, explainable baseline (not ML — see module
    docstring): a normalized event rate, not a learned prediction. Step
    6.2+ is where this becomes a real model input; this is the raw material."""
    grid_cell_id: str = Field(..., description="Geographic grid cell id (`HISTORICAL_GRID_CELL_SIZE_M` cells).")
    center_latitude: float = Field(..., description="Cell centre, latitude (degrees).")
    center_longitude: float = Field(..., description="Cell centre, longitude (degrees).")
    total_events: int = Field(..., description="Events in the cell.")
    events_by_type: List[EventTypeCount] = Field(..., description="Events per type.")
    earliest_event_at: Optional[datetime] = Field(None, description="Oldest event.")
    latest_event_at: Optional[datetime] = Field(None, description="Most recent event.")
    historical_risk_score: float = Field(..., description=(  # 0..1, see historical_service._risk_score
        "Risk score 0–1: type-weighted event count divided by `HISTORICAL_RISK_SATURATION_WEIGHT`, capped at 1."),
        examples=[0.42])
    road_name: Optional[str] = Field(None, description=(  # most common matched road name in this cell, if any
        "Most common matched road name in the cell."))
