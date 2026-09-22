"""
Orchestrates Step 6.1: pull events from a HistoricalEventSource
(app.services.historical.sources), map-match each one (matcher.py — reuses
Step 2's OSRM client, never a second integration), and persist durably,
idempotent on (source, source_event_id). Also serves the read side —
aggregated risk for a zone or along a planned route — the raw material
Step 6.2+ builds features from, and already independently useful today for
the mobile app's "colour the route by historical incident density" idea
from the original conversation.

Deliberately NOT machine learning (see app/services/historical/models.py's
ZoneRiskProfile docstring) — historical_risk_score is a simple, explainable,
per-type-weighted normalized count. That baseline is what Step 6.2+
(feature engineering) will eventually replace or augment with a learned
model; this step only prepares the data it would train on.
"""
import logging
from collections import defaultdict
from datetime import date
from typing import Dict, List, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.historical_event import HistoricalEventRecord
from app.services.historical import matcher
from app.services.historical.grid import (
    grid_cell_center, grid_cell_ids_along_route, grid_cell_ids_in_radius,
)
from app.services.historical.models import (
    EventTypeCount, HistoricalEventType, ImportSummary, ZoneRiskProfile,
)
from app.services.historical.sources.base import HistoricalEventSource
from app.services.osrm import geo

logger = logging.getLogger(__name__)

LatLon = Tuple[float, float]

# Deliberately simple, explainable weighting — not learned. An accident
# matters far more than a single speeding blip; idle violations barely
# register as a safety signal at all.
_EVENT_TYPE_WEIGHT: Dict[HistoricalEventType, float] = {
    HistoricalEventType.ACCIDENT: 5.0,
    HistoricalEventType.HARD_BRAKING: 1.5,
    HistoricalEventType.HARSH_CORNERING: 1.5,
    HistoricalEventType.HARD_ACCELERATION: 1.2,
    HistoricalEventType.SPEEDING: 1.0,
    HistoricalEventType.IDLE_VIOLATION: 0.3,
    HistoricalEventType.OTHER: 0.5,
}


async def import_events(source: HistoricalEventSource, start: date, end: date, db: AsyncSession) -> ImportSummary:
    """Fetch, map-match, and durably store events from `source` for
    [start, end]. Idempotent — re-running over an overlapping range skips
    events already imported (unique on source + source_event_id) rather
    than duplicating them. One bad event doesn't abort the batch."""
    events = await source.fetch(start, end)

    inserted = 0
    skipped = 0
    errors: List[str] = []

    for event in events:
        try:
            existing = (await db.execute(
                select(HistoricalEventRecord.id).where(
                    HistoricalEventRecord.source == event.source,
                    HistoricalEventRecord.source_event_id == event.source_event_id,
                )
            )).scalar_one_or_none()
            if existing is not None:
                skipped += 1
                continue

            matched = await matcher.match_event(event)
            db.add(HistoricalEventRecord(
                source=event.source,
                source_event_id=event.source_event_id,
                event_type=event.event_type.value,
                occurred_at=event.occurred_at.replace(tzinfo=None),
                latitude=event.latitude,
                longitude=event.longitude,
                vehicle_external_id=event.vehicle_external_id,
                driver_external_id=event.driver_external_id,
                speed_mps=event.speed_mps,
                speed_limit_mps=event.speed_limit_mps,
                severity=event.severity,
                grid_cell_id=matched.grid_cell_id,
                matched_latitude=matched.matched_latitude,
                matched_longitude=matched.matched_longitude,
                road_name=matched.road_name,
                road_segment_id=matched.road_segment_id,
                match_confidence=matched.match_confidence,
                raw=event.raw,
            ))
            inserted += 1
        except Exception as e:
            logger.exception("Failed to import historical event %s/%s", event.source, event.source_event_id)
            errors.append(f"{event.source_event_id}: {e}")

    await db.commit()

    logger.info(
        "[HISTORICAL-IMPORT] source=%s range=%s..%s fetched=%d inserted=%d skipped=%d errors=%d",
        source.name, start, end, len(events), inserted, skipped, len(errors),
    )
    return ImportSummary(
        source=source.name, date_range=(start, end), fetched=len(events),
        matched=inserted, inserted=inserted, skipped_duplicates=skipped, errors=errors,
    )


def _risk_score(counts_by_type: Dict[str, int]) -> float:
    weighted = sum(
        _EVENT_TYPE_WEIGHT.get(HistoricalEventType(t), 0.5) * c
        for t, c in counts_by_type.items()
    )
    return max(0.0, min(1.0, weighted / settings.HISTORICAL_RISK_SATURATION_WEIGHT))


def _aggregate_by_cell(rows: List[HistoricalEventRecord]) -> List[ZoneRiskProfile]:
    by_cell: Dict[str, List[HistoricalEventRecord]] = defaultdict(list)
    for r in rows:
        by_cell[r.grid_cell_id].append(r)

    profiles: List[ZoneRiskProfile] = []
    for cell_id, cell_rows in by_cell.items():
        counts: Dict[str, int] = defaultdict(int)
        road_names: Dict[str, int] = defaultdict(int)
        for r in cell_rows:
            counts[r.event_type] += 1
            if r.road_name:
                road_names[r.road_name] += 1

        center_lat, center_lon = grid_cell_center(cell_id)
        occurred_ats = [r.occurred_at for r in cell_rows]

        profiles.append(ZoneRiskProfile(
            grid_cell_id=cell_id,
            center_latitude=center_lat,
            center_longitude=center_lon,
            total_events=len(cell_rows),
            events_by_type=[
                EventTypeCount(event_type=HistoricalEventType(t), count=c) for t, c in counts.items()
            ],
            earliest_event_at=min(occurred_ats) if occurred_ats else None,
            latest_event_at=max(occurred_ats) if occurred_ats else None,
            historical_risk_score=_risk_score(counts),
            road_name=max(road_names, key=road_names.get) if road_names else None,
        ))

    profiles.sort(key=lambda p: -p.historical_risk_score)
    return profiles


async def get_zone_risk_profiles(
    latitude: float, longitude: float, radius_m: float, db: AsyncSession,
) -> List[ZoneRiskProfile]:
    """Every grid cell with at least one historical event within radius_m
    of (latitude, longitude).

    Filters by grid_cell_id (indexed, see migrations/add_historical_events.sql)
    rather than a raw latitude/longitude range — a lat/lon BETWEEN query has
    no index to use and degrades to a full-table scan as the table grows,
    where cost tracks total historical data ever collected. Filtering by
    grid cell membership instead means cost tracks the *query area* (how
    many cells a radius touches), which stays roughly constant regardless
    of how much data exists elsewhere in the country. grid_cell_ids_in_radius
    returns a slightly-generous superset (square cells vs. a circular
    radius), so the precise haversine filter below still applies — this
    only changes which rows the database bothers returning in the first
    place, not the final accuracy.
    """
    cell_size_m = settings.HISTORICAL_GRID_CELL_SIZE_M
    candidate_cells = grid_cell_ids_in_radius(latitude, longitude, radius_m, cell_size_m)
    if not candidate_cells:
        return []

    rows = (await db.execute(
        select(HistoricalEventRecord).where(HistoricalEventRecord.grid_cell_id.in_(candidate_cells))
    )).scalars().all()

    filtered = [r for r in rows if geo.haversine_m(latitude, longitude, r.latitude, r.longitude) <= radius_m]
    return _aggregate_by_cell(filtered)


async def get_zone_risk_profiles_along_route(
    waypoints: List[LatLon], corridor_m: float, db: AsyncSession,
) -> List[ZoneRiskProfile]:
    """Every grid cell with at least one historical event within corridor_m
    of any leg of the polyline `waypoints` — this is what the mobile app
    calls to colour a planned route by historical incident density.
    Same grid_cell_id-indexed filtering as get_zone_risk_profiles, see its
    docstring; grid_cell_ids_along_route handles routes whose waypoints are
    sparse relative to the corridor (e.g. a client-simplified route) by
    re-sampling each leg before computing cell coverage.
    """
    if len(waypoints) < 2:
        return []

    cell_size_m = settings.HISTORICAL_GRID_CELL_SIZE_M
    candidate_cells = grid_cell_ids_along_route(waypoints, corridor_m, cell_size_m)
    if not candidate_cells:
        return []
    if len(candidate_cells) > 20_000:
        logger.warning(
            "[HISTORICAL] route risk query touches %d grid cells (very long route or tiny cell size) — "
            "this query will be slower than usual", len(candidate_cells),
        )

    rows = (await db.execute(
        select(HistoricalEventRecord).where(HistoricalEventRecord.grid_cell_id.in_(candidate_cells))
    )).scalars().all()

    def _near_route(r: HistoricalEventRecord) -> bool:
        point = (r.latitude, r.longitude)
        for i in range(len(waypoints) - 1):
            if geo.point_to_segment_distance_m(point, waypoints[i], waypoints[i + 1]) <= corridor_m:
                return True
        return False

    filtered = [r for r in rows if _near_route(r)]
    return _aggregate_by_cell(filtered)
