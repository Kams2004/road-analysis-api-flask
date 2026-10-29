"""
Orchestrates Step 6.1: pull events from a HistoricalEventSource
(app.modules.m08_historical.sources), map-match each one (matcher.py — reuses
Step 2's OSRM client, never a second integration), and persist durably,
idempotent on (source, source_event_id). Also serves the read side —
aggregated risk for a zone or along a planned route — the raw material
Step 6.2+ builds features from, and already independently useful today for
the mobile app's "colour the route by historical incident density" idea
from the original conversation.

Deliberately NOT machine learning (see app/modules/m08_historical/domain.py's
ZoneRiskProfile docstring) — historical_risk_score is a simple, explainable,
per-type-weighted normalized count. That baseline is what Step 6.2+
(feature engineering) will eventually replace or augment with a learned
model; this step only prepares the data it would train on.
"""
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Dict, List, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.modules.m08_historical.models.historical_event import HistoricalEventRecord
from app.modules.m08_historical import matcher
from app.modules.m08_historical.anonymity import clean_raw
from app.modules.m08_historical.grid import (
    grid_cell_center, grid_cell_id, grid_cell_ids_along_route, grid_cell_ids_in_radius,
)
from app.modules.m08_historical.domain import (
    EventTypeCount, HistoricalEventType, ImportSummary, ZoneRiskProfile,
)
from app.modules.m08_historical.sources.base import HistoricalEventSource
from app.modules.m03_maps.osrm import geo

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
                speed_mps=event.speed_mps,
                speed_limit_mps=event.speed_limit_mps,
                severity=event.severity,
                grid_cell_id=matched.grid_cell_id,
                matched_latitude=matched.matched_latitude,
                matched_longitude=matched.matched_longitude,
                road_name=matched.road_name,
                road_segment_id=matched.road_segment_id,
                match_confidence=matched.match_confidence,
                raw=clean_raw(event.raw),      # anonymous: identifying fields dropped
            ))
            inserted += 1
        except Exception as e:
            logger.exception("Failed to import historical event %s/%s", event.source, event.source_event_id)
            errors.append(f"{event.source_event_id}: {e}")

    await db.commit()
    if inserted:
        from app.modules.m08_historical import rollups
        await rollups.rebuild_safely(db, {e.occurred_at.date() for e in events})

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

    rows = await _recent_rows(db, candidate_cells)
    filtered = [r for r in rows if geo.haversine_m(latitude, longitude, r.latitude, r.longitude) <= radius_m]
    return await _current_profiles(db, filtered)


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

    rows = await _recent_rows(db, candidate_cells)

    # Each event is checked against the legs crossing its cell only (not every leg of the route).
    legs_by_cell: Dict[str, List[int]] = defaultdict(list)
    for i in range(len(waypoints) - 1):
        for cid in grid_cell_ids_along_route(waypoints[i:i + 2], corridor_m, cell_size_m):
            legs_by_cell[cid].append(i)

    def _near_route(r) -> bool:
        point = (r.latitude, r.longitude)
        return any(geo.point_to_segment_distance_m(point, waypoints[i], waypoints[i + 1]) <= corridor_m
                   for i in legs_by_cell.get(r.grid_cell_id, ()))

    filtered = [r for r in rows if _near_route(r)]
    return await _current_profiles(db, filtered)


# ── "current risk" (mobile: around a position / along a route) ───────────────

async def _recent_rows(db: AsyncSession, cells) -> list:
    """The events of these cells within HISTORICAL_RISK_WINDOW_DAYS — only the columns scored."""
    since = datetime.utcnow() - timedelta(days=settings.HISTORICAL_RISK_WINDOW_DAYS)
    H = HistoricalEventRecord
    cols = (H.grid_cell_id, H.event_type, H.occurred_at, H.latitude, H.longitude, H.road_name, H.severity,
            H.speed_mps, H.speed_limit_mps)
    out: list = []
    cells = list(cells)
    for i in range(0, len(cells), 2000):
        out += (await db.execute(select(*cols).where(H.grid_cell_id.in_(cells[i:i + 2000]),
                                                     H.occurred_at >= since))).all()
    return out


def recency(occurred_at: datetime, now: datetime, half_life_days: float) -> float:
    age_days = max(0.0, (now - occurred_at).total_seconds() / 86400.0)
    return 0.5 ** (age_days / half_life_days) if half_life_days > 0 else 1.0


def current_profiles(rows, cfg, now: datetime) -> List[ZoneRiskProfile]:
    """Per cell: score = min(1, Σ type weight × severity multiplier × recency / saturation)."""
    from app.modules.m09_exception_patterns.analytics import normalize_severity
    half = settings.HISTORICAL_RISK_HALF_LIFE_DAYS
    recent_since = now - timedelta(days=90)
    by_cell: Dict[str, dict] = {}
    for r in rows:
        c = by_cell.setdefault(r.grid_cell_id, {"counts": defaultdict(int), "w": defaultdict(float), "roads": defaultdict(int),
                                                "first": None, "last": None, "high": 0, "recent": 0})
        sev = normalize_severity(r, cfg)
        t = r.event_type
        c["counts"][t] += 1
        c["w"][t] += _EVENT_TYPE_WEIGHT.get(HistoricalEventType(t), 0.5) * cfg.multiplier(sev) * recency(r.occurred_at, now, half)
        c["high"] += sev == "high"
        c["recent"] += r.occurred_at >= recent_since
        if r.road_name:
            c["roads"][r.road_name] += 1
        c["first"] = r.occurred_at if c["first"] is None or r.occurred_at < c["first"] else c["first"]
        c["last"] = r.occurred_at if c["last"] is None or r.occurred_at > c["last"] else c["last"]
    out = []
    for cid, c in by_cell.items():
        lat, lon = grid_cell_center(cid)
        weighted = sum(c["w"].values())
        out.append(ZoneRiskProfile(
            grid_cell_id=cid, center_latitude=lat, center_longitude=lon, total_events=sum(c["counts"].values()),
            events_by_type=[EventTypeCount(event_type=HistoricalEventType(t), count=n) for t, n in c["counts"].items()],
            earliest_event_at=c["first"], latest_event_at=c["last"],
            historical_risk_score=round(max(0.0, min(1.0, weighted / settings.HISTORICAL_RISK_SATURATION_WEIGHT)), 4),
            road_name=max(c["roads"], key=c["roads"].get) if c["roads"] else None,
            high_severity=c["high"], recent_events=c["recent"],
            dominant_type=max(c["w"], key=c["w"].get) if c["w"] else None,
        ))
    out.sort(key=lambda p: -p.historical_risk_score)
    return out


async def _current_profiles(db: AsyncSession, rows) -> List[ZoneRiskProfile]:
    from app.core import settings_service
    cfg = await settings_service.get_cached(db, "driver_behaviour")
    return current_profiles(rows, cfg, datetime.utcnow())


# ── Module 4 dashboard: overview, heatmap, risk zones, trend, import history ──

# A cell with fewer events than this has LOW data coverage: a low score there
# does not mean a safe place (RRAP-08-013 / RRAP-08-015).
LOW_COVERAGE_EVENTS = 3
HIGH_RISK_SCORE = 0.5
MAX_ROWS = 200_000


def coverage_level(total_events: int) -> str:
    if total_events < LOW_COVERAGE_EVENTS:
        return "low"
    if total_events < LOW_COVERAGE_EVENTS * 4:
        return "medium"
    return "good"


async def load_rows(db: AsyncSession, start=None, end=None, bbox=None) -> List[HistoricalEventRecord]:
    q = select(HistoricalEventRecord)
    if start is not None:
        q = q.where(HistoricalEventRecord.occurred_at >= start)
    if end is not None:
        q = q.where(HistoricalEventRecord.occurred_at < end)
    if bbox:
        s, w, n, e = bbox
        q = q.where(HistoricalEventRecord.latitude.between(s, n), HistoricalEventRecord.longitude.between(w, e))
    return list((await db.execute(q.order_by(HistoricalEventRecord.occurred_at).limit(MAX_ROWS))).scalars().all())


def risk_points(rows) -> float:
    w = weights()
    return sum(w.get(r.event_type, 0.5) for r in rows)


def trend_series(rows, start, end, unit: str) -> List[dict]:
    """Risk evolution: per bucket, events, weighted risk points, accidents and
    the number of distinct grid cells with events (Module 4 — overall risk,
    not per exception type: that is Module 9, Exception patterns)."""
    from app.modules.m09_exception_patterns.analytics import bucket_floor, bucket_starts
    buckets = {b: [] for b in bucket_starts(start, end, unit)}
    for r in rows:
        b = bucket_floor(r.occurred_at, unit)
        if b in buckets:
            buckets[b].append(r)
    out = []
    for b, rs in buckets.items():
        out.append({
            "bucket": b, "events": len(rs), "risk_points": round(risk_points(rs), 2),
            "accidents": sum(1 for r in rs if r.event_type == HistoricalEventType.ACCIDENT.value),
            "cells": len({r.grid_cell_id for r in rs}),
        })
    return out


def import_history(rows) -> List[dict]:
    """Import batches: events grouped by source and import minute."""
    groups: Dict[Tuple[str, str], list] = defaultdict(list)
    for r in rows:
        at = r.imported_at.replace(second=0, microsecond=0) if r.imported_at else None
        groups[(r.source, at.isoformat() if at else "")].append(r)
    out = []
    for (src, at), rs in groups.items():
        occ = [r.occurred_at for r in rs]
        out.append({
            "source": src, "imported_at": at or None, "events": len(rs),
            "first_event_at": min(occ), "last_event_at": max(occ),
            "by_type": dict(sorted(((t, sum(1 for r in rs if r.event_type == t)) for t in {r.event_type for r in rs}),
                                   key=lambda kv: -kv[1])),
        })
    out.sort(key=lambda g: g["imported_at"] or "", reverse=True)
    return out


def weights() -> Dict[str, float]:
    return {t.value: w for t, w in _EVENT_TYPE_WEIGHT.items()}


# ── grouped reads (app/modules/m08_historical/aggregates.py) → Module 4 answers ──

def profiles_from_squares(rows, cell_size_m: float) -> List[ZoneRiskProfile]:
    """Cell risk profiles from ~110 m square rows (k_lat, k_lon, event_type, road_name, n,
    lat_sum, lon_sum, first_at, last_at): each square goes to the grid cell of its mean position."""
    from app.modules.m08_historical.aggregates import as_dt
    cells: Dict[str, dict] = {}
    for r in rows:
        n = int(r.n)
        cid = grid_cell_id(r.lat_sum / n, r.lon_sum / n, cell_size_m)
        c = cells.setdefault(cid, {"counts": defaultdict(int), "roads": defaultdict(int), "first": None, "last": None})
        c["counts"][r.event_type] += n
        if r.road_name:
            c["roads"][r.road_name] += n
        f, l_ = as_dt(r.first_at), as_dt(r.last_at)
        c["first"] = f if c["first"] is None or f < c["first"] else c["first"]
        c["last"] = l_ if c["last"] is None or l_ > c["last"] else c["last"]
    profiles = []
    for cid, c in cells.items():
        lat, lon = grid_cell_center(cid)
        profiles.append(ZoneRiskProfile(
            grid_cell_id=cid, center_latitude=lat, center_longitude=lon,
            total_events=sum(c["counts"].values()),
            events_by_type=[EventTypeCount(event_type=HistoricalEventType(t), count=k) for t, k in c["counts"].items()],
            earliest_event_at=c["first"], latest_event_at=c["last"], historical_risk_score=_risk_score(c["counts"]),
            road_name=max(c["roads"], key=c["roads"].get) if c["roads"] else None,
        ))
    profiles.sort(key=lambda p: -p.historical_risk_score)
    return profiles


def trend_from_days(day_rows, squares: Dict, start, end, unit: str) -> List[dict]:
    """Per bucket: events, weighted risk points, accidents (from (day, event_type, n) rows) and
    the distinct ~110 m squares with events (`squares`: {bucket: n})."""
    from app.modules.m08_historical.aggregates import as_date
    from app.modules.m09_exception_patterns.analytics import bucket_floor, bucket_starts
    w = weights()
    buckets = {b: {"bucket": b, "events": 0, "risk_points": 0.0, "accidents": 0, "cells": 0}
               for b in bucket_starts(start, end, unit)}
    for r in day_rows:
        b = buckets.get(bucket_floor(datetime.combine(as_date(r.day), datetime.min.time()), unit))
        if b is None:
            continue
        n = int(r.n)
        b["events"] += n
        b["risk_points"] += w.get(r.event_type, 0.5) * n
        b["accidents"] += n if r.event_type == HistoricalEventType.ACCIDENT.value else 0
    for d, n in squares.items():
        b = buckets.get(datetime.combine(d, datetime.min.time()))
        if b is not None:
            b["cells"] = n
    for b in buckets.values():
        b["risk_points"] = round(b["risk_points"], 2)
    return list(buckets.values())


async def import_batches(db: AsyncSession) -> List[dict]:
    """Import batches (same source, same import minute), newest first — one grouped read."""
    from app.modules.m08_historical.aggregates import as_dt, import_rows
    groups: Dict[Tuple[str, str], dict] = {}
    for src, minute, t, n, first, last in await import_rows(db):
        at = as_dt(minute)
        key = (src, at.isoformat() if at else "")
        g = groups.setdefault(key, {"source": src, "imported_at": key[1] or None, "events": 0,
                                    "first_event_at": None, "last_event_at": None, "by_type": {}})
        g["events"] += int(n)
        g["by_type"][t] = g["by_type"].get(t, 0) + int(n)
        f, l_ = as_dt(first), as_dt(last)
        g["first_event_at"] = f if g["first_event_at"] is None or f < g["first_event_at"] else g["first_event_at"]
        g["last_event_at"] = l_ if g["last_event_at"] is None or l_ > g["last_event_at"] else g["last_event_at"]
    out = list(groups.values())
    for g in out:
        g["by_type"] = dict(sorted(g["by_type"].items(), key=lambda kv: -kv[1]))
    out.sort(key=lambda g: g["imported_at"] or "", reverse=True)
    return out
