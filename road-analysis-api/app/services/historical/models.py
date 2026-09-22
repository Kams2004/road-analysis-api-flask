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
app/services/osrm/models.py already documents why OSRM/OSM ids aren't
treated as durable business keys — they can shift when the road dataset is
refreshed (see osrm/README.md's "replacing/updating the dataset" section).
That caveat matters far more here: historical events accumulate over
months/years and must remain comparable across OSRM dataset refreshes. So
the primary aggregation key is a stable geographic grid cell
(app/services/historical/grid.py), with the matched road name/segment kept
only as descriptive metadata for the *current* dataset, not the join key.
"""
import enum
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel


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
    source: str                          # e.g. "ymane", "manual"
    source_event_id: str                 # source's own id — used for idempotent re-import
    event_type: HistoricalEventType
    occurred_at: datetime

    latitude: float
    longitude: float

    vehicle_external_id: Optional[str] = None  # the SOURCE's vehicle identifier — not our Vehicle.id (Step 1)
    driver_external_id: Optional[str] = None

    speed_mps: Optional[float] = None
    speed_limit_mps: Optional[float] = None
    severity: Optional[str] = None       # raw severity/level from the source, if any — not yet normalized

    raw: Dict[str, Any] = {}             # untouched source payload for this event — forward-compatible, debuggable


class MapMatchedHistoricalEvent(BaseModel):
    """A HistoricalEvent plus where it actually sits on the road network —
    see app/services/historical/matcher.py."""
    event: HistoricalEvent
    grid_cell_id: str
    matched_latitude: Optional[float] = None
    matched_longitude: Optional[float] = None
    road_name: Optional[str] = None
    road_segment_id: Optional[str] = None  # descriptive only — see module docstring
    match_confidence: Optional[float] = None


class ImportSummary(BaseModel):
    source: str
    date_range: Tuple[date, date]
    fetched: int
    matched: int
    inserted: int
    skipped_duplicates: int
    errors: List[str] = []


class EventTypeCount(BaseModel):
    event_type: HistoricalEventType
    count: int


class ZoneRiskProfile(BaseModel):
    """Aggregated historical risk for one grid cell. `historical_risk_score`
    is a deliberately simple, explainable baseline (not ML — see module
    docstring): a normalized event rate, not a learned prediction. Step
    6.2+ is where this becomes a real model input; this is the raw material."""
    grid_cell_id: str
    center_latitude: float
    center_longitude: float
    total_events: int
    events_by_type: List[EventTypeCount]
    earliest_event_at: Optional[datetime] = None
    latest_event_at: Optional[datetime] = None
    historical_risk_score: float  # 0..1, see historical_service._risk_score
    road_name: Optional[str] = None  # most common matched road name in this cell, if any
