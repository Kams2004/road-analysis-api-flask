"""
Pure behaviour analytics over event rows (no database) — every function takes
rows exposing the `historical_events` columns (ORM rows or SimpleNamespace in
tests) and the `driver_behaviour` settings. See settings.py for the scoring
method and the zone criteria.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from app.modules.m08_historical.grid import grid_cell_center, grid_cell_id
from app.modules.m09_driver_behaviour.settings import BEHAVIOUR_TYPES, DriverBehaviourSettings

MPS_TO_KMH = 3.6

PERIODS: Dict[str, timedelta] = {
    "7d": timedelta(days=7), "30d": timedelta(days=30), "90d": timedelta(days=90), "12m": timedelta(days=365),
}

_SEVERITY_SYNONYMS = {
    "low": "low", "minor": "low", "light": "low", "1": "low", "l": "low", "faible": "low",
    "medium": "medium", "moderate": "medium", "normal": "medium", "2": "medium", "m": "medium", "moyen": "medium",
    "high": "high", "severe": "high", "major": "high", "critical": "high", "3": "high", "h": "high",
    "4": "high", "5": "high", "eleve": "high", "élevé": "high",
}


# ── Period ───────────────────────────────────────────────────────────────────

@dataclass
class Window:
    start: Optional[datetime]        # None = since the first event
    end: datetime                    # exclusive
    prev_start: Optional[datetime]   # comparison window [prev_start, start); None = no comparison
    label: str

    @property
    def has_previous(self) -> bool:
        return self.start is not None and self.prev_start is not None


def resolve_window(period: Optional[str], date_from: Optional[date], date_to: Optional[date],
                   now: datetime, default: str = "90d") -> Window:
    """Custom dates win over `period`; the comparison window has the same length, just before."""
    if date_from or date_to:
        end = datetime.combine(date_to, time.min) + timedelta(days=1) if date_to else now
        start = datetime.combine(date_from, time.min) if date_from else None
        prev = start - (end - start) if start else None
        return Window(start, end, prev, "custom")
    period = period or default
    if period == "all":
        return Window(None, now, None, "all")
    if period not in PERIODS:
        raise ValueError(f"Unknown period '{period}' — expected 7d, 30d, 90d, 12m or all")
    start = now - PERIODS[period]
    return Window(start, now, start - PERIODS[period], period)


def bucket_unit(start: datetime, end: datetime) -> str:
    span = end - start
    if span <= timedelta(days=31):
        return "day"
    if span <= timedelta(days=200):
        return "week"
    return "month"


def bucket_floor(ts: datetime, unit: str) -> datetime:
    d = ts.replace(hour=0, minute=0, second=0, microsecond=0)
    if unit == "week":
        return d - timedelta(days=d.weekday())
    if unit == "month":
        return d.replace(day=1)
    return d


def _next_bucket(cur: datetime, unit: str) -> datetime:
    if unit == "day":
        return cur + timedelta(days=1)
    if unit == "week":
        return cur + timedelta(days=7)
    return cur.replace(year=cur.year + 1, month=1) if cur.month == 12 else cur.replace(month=cur.month + 1)


def bucket_starts(start: datetime, end: datetime, unit: str) -> List[datetime]:
    out, cur = [], bucket_floor(start, unit)
    while cur < end and len(out) < 2000:
        out.append(cur)
        cur = _next_bucket(cur, unit)
    return out


# ── Event-level ──────────────────────────────────────────────────────────────

def excess_pct(ev) -> Optional[float]:
    """Speed above the limit, % of the limit (None when either is unknown)."""
    if ev.speed_mps is None or not ev.speed_limit_mps:
        return None
    return (ev.speed_mps - ev.speed_limit_mps) / ev.speed_limit_mps * 100.0


def normalize_severity(ev, cfg: DriverBehaviourSettings) -> str:
    raw = (ev.severity or "").strip().lower()
    if raw in _SEVERITY_SYNONYMS:
        return _SEVERITY_SYNONYMS[raw]
    if ev.event_type == "SPEEDING":
        x = excess_pct(ev)
        if x is not None:
            if x >= cfg.speeding_high_excess_pct:
                return "high"
            if x >= cfg.speeding_medium_excess_pct:
                return "medium"
            return "low"
    return "unknown"


def event_points(ev, cfg: DriverBehaviourSettings) -> float:
    return cfg.weight(ev.event_type) * cfg.multiplier(normalize_severity(ev, cfg))


def score_from_points(points: float, cfg: DriverBehaviourSettings) -> float:
    return round(100.0 * min(1.0, max(0.0, points) / cfg.saturation_points), 1)


def risk_level(score: float, cfg: DriverBehaviourSettings) -> str:
    if score >= max(cfg.high_risk_score, 85.0):
        return "very_high"
    if score >= cfg.high_risk_score:
        return "high"
    if score >= cfg.high_risk_score / 2:
        return "medium"
    return "low"


def _kmh(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(v * MPS_TO_KMH, 1)


def _avg(xs: Sequence[float]) -> Optional[float]:
    return round(sum(xs) / len(xs), 1) if xs else None


def _local(ts: datetime, tz: ZoneInfo) -> datetime:
    from datetime import timezone as _tz
    return ts.replace(tzinfo=_tz.utc).astimezone(tz)


# ── Summaries ────────────────────────────────────────────────────────────────

def type_summaries(events: Sequence, cfg: DriverBehaviourSettings,
                   types: Sequence[str] = BEHAVIOUR_TYPES) -> List[Dict[str, Any]]:
    """Per behaviour type (speeding / harsh braking / acceleration / cornering):
    counts, severity mix, speed vs limit, peak hour, main roads."""
    tz = ZoneInfo(cfg.timezone)
    by_type: Dict[str, list] = defaultdict(list)
    for e in events:
        by_type[e.event_type].append(e)
    total = len(events) or 1
    out = []
    for t in types:
        rows = by_type.get(t, [])
        sev = Counter(normalize_severity(e, cfg) for e in rows)
        hours = Counter(_local(e.occurred_at, tz).hour for e in rows)
        roads = Counter(e.road_name for e in rows if e.road_name)
        speeds = [e.speed_mps * MPS_TO_KMH for e in rows if e.speed_mps is not None]
        limits = [e.speed_limit_mps * MPS_TO_KMH for e in rows if e.speed_limit_mps]
        excess = [x for x in (excess_pct(e) for e in rows) if x is not None]
        out.append({
            "event_type": t,
            "count": len(rows),
            "share": round(len(rows) / total, 4) if events else 0.0,
            "points": round(sum(event_points(e, cfg) for e in rows), 2),
            "by_severity": {s: sev.get(s, 0) for s in ("low", "medium", "high", "unknown")},
            "high_severity": sev.get("high", 0),
            "avg_speed_kmh": _avg(speeds),
            "max_speed_kmh": round(max(speeds), 1) if speeds else None,
            "avg_limit_kmh": _avg(limits),
            "avg_excess_pct": _avg(excess),
            "peak_hour": hours.most_common(1)[0][0] if hours else None,
            "drivers": len({e.driver_external_id for e in rows if e.driver_external_id}),
            "vehicles": len({e.vehicle_external_id for e in rows if e.vehicle_external_id}),
            "top_roads": [{"road_name": r, "count": c} for r, c in roads.most_common(3)],
        })
    return out


def overview(events: Sequence, prev_events: Optional[Sequence], cfg: DriverBehaviourSettings,
             types: Sequence[str] = BEHAVIOUR_TYPES) -> Dict[str, Any]:
    drivers = {e.driver_external_id for e in events if e.driver_external_id}
    vehicles = {e.vehicle_external_id for e in events if e.vehicle_external_id}
    with_id = sum(1 for e in events if e.driver_external_id or e.vehicle_external_id)
    points = sum(event_points(e, cfg) for e in events)
    ranked = rank_entities(events, None, "driver", cfg) if drivers else []
    times = [e.occurred_at for e in events]
    return {
        "total_events": len(events),
        "previous_total_events": len(prev_events) if prev_events is not None else None,
        "change_pct": _change_pct(len(events), len(prev_events)) if prev_events is not None else None,
        "points": round(points, 2),
        "high_severity_events": sum(1 for e in events if normalize_severity(e, cfg) == "high"),
        "drivers": len(drivers),
        "vehicles": len(vehicles),
        "identified_share": round(with_id / len(events), 4) if events else 0.0,
        "high_risk_drivers": sum(1 for r in ranked if r["score"] >= cfg.high_risk_score),
        "avg_driver_score": _avg([r["score"] for r in ranked]),
        "first_event_at": min(times) if times else None,
        "last_event_at": max(times) if times else None,
        "by_type": type_summaries(events, cfg, types),
    }


def _change_pct(cur: float, prev: float) -> Optional[float]:
    if not prev:
        return None
    return round((cur - prev) / prev * 100.0, 1)


def _entity_key(kind: str):
    return (lambda e: e.driver_external_id) if kind == "driver" else (lambda e: e.vehicle_external_id)


def rank_entities(events: Sequence, prev_events: Optional[Sequence], kind: str,
                  cfg: DriverBehaviourSettings) -> List[Dict[str, Any]]:
    """Drivers (kind=driver) or vehicles (kind=vehicle) ranked by behaviour
    score, riskiest first; only events carrying the identifier count
    (RRAP-09-004 — no profile without a reliable association)."""
    key = _entity_key(kind)
    other = _entity_key("vehicle" if kind == "driver" else "driver")
    groups: Dict[str, list] = defaultdict(list)
    for e in events:
        k = key(e)
        if k:
            groups[k].append(e)
    prev_points: Dict[str, float] = defaultdict(float)
    prev_counts: Dict[str, int] = defaultdict(int)
    for e in prev_events or ():
        k = key(e)
        if k:
            prev_points[k] += event_points(e, cfg)
            prev_counts[k] += 1
    out = []
    for k, rows in groups.items():
        pts = sum(event_points(e, cfg) for e in rows)
        score = score_from_points(pts, cfg)
        by_type = Counter(e.event_type for e in rows)
        roads = Counter(e.road_name for e in rows if e.road_name)
        prev_score = score_from_points(prev_points[k], cfg) if prev_events is not None else None
        out.append({
            "id": k,
            "events": len(rows),
            "by_type": {t: by_type.get(t, 0) for t in BEHAVIOUR_TYPES},
            "high_severity": sum(1 for e in rows if normalize_severity(e, cfg) == "high"),
            "points": round(pts, 2),
            "score": score,
            "level": risk_level(score, cfg),
            "previous_score": prev_score,
            "previous_events": prev_counts[k] if prev_events is not None else None,
            "score_delta": round(score - prev_score, 1) if prev_score is not None else None,
            "trend": _trend(score, prev_score),
            "linked": sorted({other(e) for e in rows if other(e)})[:5],
            "last_event_at": max(e.occurred_at for e in rows),
            "top_road": roads.most_common(1)[0][0] if roads else None,
        })
    out.sort(key=lambda r: (-r["score"], -r["events"], r["id"]))
    for i, r in enumerate(out, 1):
        r["rank"] = i
    return out


def _trend(score: float, prev: Optional[float]) -> Optional[str]:
    if prev is None:
        return None
    if score > prev + 2:
        return "worse"
    if score < prev - 2:
        return "better"
    return "stable"


def trends(events: Sequence, window_start: Optional[datetime], window_end: datetime,
           cfg: DriverBehaviourSettings, types: Sequence[str] = BEHAVIOUR_TYPES,
           unit: Optional[str] = None) -> Dict[str, Any]:
    """Event counts per time bucket and type, plus hour-of-day and weekday
    patterns (local time, settings `timezone`)."""
    tz = ZoneInfo(cfg.timezone)
    start = window_start or (min(e.occurred_at for e in events) if events else window_end - timedelta(days=30))
    unit = unit or bucket_unit(start, window_end)
    starts = bucket_starts(start, window_end, unit)
    series = {b: {"bucket": b, "total": 0, "points": 0.0, **{t: 0 for t in types}} for b in starts}
    hours = [{"hour": h, "total": 0, **{t: 0 for t in types}} for h in range(24)]
    weekdays = [{"weekday": d, "total": 0, **{t: 0 for t in types}} for d in range(7)]
    for e in events:
        b = bucket_floor(e.occurred_at, unit)
        row = series.get(b)
        if row is not None:
            row["total"] += 1
            row["points"] += event_points(e, cfg)
            if e.event_type in row:
                row[e.event_type] += 1
        lt = _local(e.occurred_at, tz)
        for r in (hours[lt.hour], weekdays[lt.weekday()]):
            r["total"] += 1
            if e.event_type in r:
                r[e.event_type] += 1
    out = list(series.values())
    for r in out:
        r["points"] = round(r["points"], 2)
    return {"unit": unit, "series": out, "by_hour": hours, "by_weekday": weekdays}


def heatmap(events: Sequence, cell_size_m: float, cfg: DriverBehaviourSettings) -> List[Dict[str, Any]]:
    """Event concentration per grid cell (count + weighted points). Intensity
    is a count, not an accident probability (RRAP-09-008)."""
    cells: Dict[str, Dict[str, Any]] = {}
    for e in events:
        cid = grid_cell_id(e.latitude, e.longitude, cell_size_m)
        c = cells.get(cid)
        if c is None:
            lat, lon = grid_cell_center(cid)
            c = cells[cid] = {"cell_id": cid, "latitude": lat, "longitude": lon, "count": 0, "points": 0.0,
                              "by_type": {t: 0 for t in BEHAVIOUR_TYPES}}
        c["count"] += 1
        c["points"] += event_points(e, cfg)
        if e.event_type in c["by_type"]:
            c["by_type"][e.event_type] += 1
    out = sorted(cells.values(), key=lambda c: -c["count"])
    for c in out:
        c["points"] = round(c["points"], 2)
    return out


def zones(events: Sequence, cfg: DriverBehaviourSettings) -> List[Dict[str, Any]]:
    """High-risk behaviour zones: cells meeting the configured criteria
    (`zone_min_events`, `zone_min_drivers`), riskiest first."""
    groups: Dict[str, list] = defaultdict(list)
    for e in events:
        groups[grid_cell_id(e.latitude, e.longitude, cfg.zone_cell_size_m)].append(e)
    out = []
    for cid, rows in groups.items():
        if len(rows) < cfg.zone_min_events:
            continue
        entities = {e.driver_external_id or e.vehicle_external_id for e in rows
                    if e.driver_external_id or e.vehicle_external_id}
        if entities and len(entities) < cfg.zone_min_drivers:
            continue
        by_type = Counter(e.event_type for e in rows)
        roads = Counter(e.road_name for e in rows if e.road_name)
        pts = sum(event_points(e, cfg) for e in rows)
        lat, lon = grid_cell_center(cid)
        dominant, dom_n = by_type.most_common(1)[0]
        out.append({
            "cell_id": cid,
            "latitude": lat, "longitude": lon,
            "radius_m": cfg.zone_cell_size_m / 2,
            "events": len(rows),
            "points": round(pts, 2),
            "score": score_from_points(pts, cfg),
            "by_type": {t: by_type.get(t, 0) for t in BEHAVIOUR_TYPES},
            "dominant_type": dominant,
            "dominant_share": round(dom_n / len(rows), 3),
            "multi_behaviour": sum(1 for t in BEHAVIOUR_TYPES if by_type.get(t)) >= 2,
            "drivers": len(entities),
            "high_severity": sum(1 for e in rows if normalize_severity(e, cfg) == "high"),
            "road_name": roads.most_common(1)[0][0] if roads else None,
            "first_event_at": min(e.occurred_at for e in rows),
            "last_event_at": max(e.occurred_at for e in rows),
        })
    out.sort(key=lambda z: (-z["points"], -z["events"]))
    for i, z in enumerate(out, 1):
        z["rank"] = i
    return out


def methodology(cfg: DriverBehaviourSettings) -> Dict[str, Any]:
    from app.modules.m09_driver_behaviour.settings import METHODOLOGY_VERSION
    return {
        "version": METHODOLOGY_VERSION,
        "formula": "score = 100 × min(1, Σ type_weight × severity_multiplier / saturation_points)",
        "settings": cfg.model_dump(),
        "notes": [
            "Event counts and scores are not accident probabilities and do not establish causation.",
            "Without exposure data (distance, driving time) scores are event-based indicators.",
            "Driver / vehicle profiles only use events carrying the identifier.",
        ],
    }


def filter_bbox(events: Iterable, bbox: Optional[Tuple[float, float, float, float]]) -> List:
    if not bbox:
        return list(events)
    s, w, n, e_ = bbox
    return [e for e in events if s <= e.latitude <= n and w <= e.longitude <= e_]
