"""
Pure exception-pattern analytics over event rows (no database) — every function takes rows
exposing the `historical_events` columns (ORM rows or SimpleNamespace in tests) and the
`driver_behaviour` settings section (settings.py: severity rules, zone criteria, time zone).
Anonymous: nothing is grouped by driver or vehicle (the rows carry none). views.py gives the same
answers from grouped database reads; these per-event versions serve the PDF reports (which
filter event by event on a zone / path) and the tests.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from app.modules.m08_historical import rollups as R
from app.modules.m08_historical.grid import grid_cell_center, grid_cell_id
from app.modules.m09_exception_patterns.settings import (
    EXCEPTION_TYPES, METHODOLOGY_VERSION, SEVERITIES, ExceptionPatternSettings,
)

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
    # Whole UTC days: the daily summaries (app/modules/m08_historical/rollups.py) and the raw
    # events then cover exactly the same span.
    start = (now - PERIODS[period]).replace(hour=0, minute=0, second=0, microsecond=0)
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


def normalize_severity(ev, cfg: ExceptionPatternSettings) -> str:
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


def speed_band(ev) -> str:
    """Band of the recorded speed (rollups.SPEED_BANDS), '' when unknown."""
    return R.band_of(ev.speed_mps * MPS_TO_KMH if ev.speed_mps is not None else None, R.SPEED_BANDS_KMH)


def excess_band(ev) -> str:
    """Band of the excess over the limit (rollups.EXCESS_BANDS), '' when unknown."""
    return R.band_of(excess_pct(ev), R.EXCESS_BANDS_PCT)


def _kmh(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(v * MPS_TO_KMH, 1)


def _avg(xs: Sequence[float]) -> Optional[float]:
    return round(sum(xs) / len(xs), 1) if xs else None


def _local(ts: datetime, tz: ZoneInfo) -> datetime:
    from datetime import timezone as _tz
    return ts.replace(tzinfo=_tz.utc).astimezone(tz)


def _change_pct(cur: float, prev: float) -> Optional[float]:
    if not prev:
        return None
    return round((cur - prev) / prev * 100.0, 1)


def _square(ev) -> Tuple[int, int]:
    """The ~110 m square of the daily summaries (rollups.cell_columns)."""
    return int(ev.latitude * 1000), int(ev.longitude * 1000)


# ── Summaries ────────────────────────────────────────────────────────────────

def type_summaries(events: Sequence, cfg: ExceptionPatternSettings,
                   types: Sequence[str] = EXCEPTION_TYPES) -> List[Dict[str, Any]]:
    """Per exception type: counts, severity mix, speed vs limit, peak hour, main roads."""
    tz = ZoneInfo(cfg.timezone)
    by_type: Dict[str, list] = defaultdict(list)
    for e in events:
        by_type[e.event_type].append(e)
    total = len(events)
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
            "share": round(len(rows) / total, 4) if total else 0.0,
            "by_severity": {s: sev.get(s, 0) for s in SEVERITIES},
            "high_severity": sev.get("high", 0),
            "avg_speed_kmh": _avg(speeds),
            "max_speed_kmh": round(max(speeds), 1) if speeds else None,
            "avg_limit_kmh": _avg(limits),
            "avg_excess_pct": _avg(excess),
            "peak_hour": hours.most_common(1)[0][0] if hours else None,
            "top_roads": [{"road_name": r, "count": c} for r, c in roads.most_common(3)],
        })
    return out


def overview(events: Sequence, prev_events: Optional[Sequence], cfg: ExceptionPatternSettings,
             types: Sequence[str] = EXCEPTION_TYPES) -> Dict[str, Any]:
    times = [e.occurred_at for e in events]
    speeds = [e.speed_mps * MPS_TO_KMH for e in events if e.speed_mps is not None]
    excess = [x for x in (excess_pct(e) for e in events) if x is not None]
    high = sum(1 for e in events if normalize_severity(e, cfg) == "high")
    return {
        "total_events": len(events),
        "previous_total_events": len(prev_events) if prev_events is not None else None,
        "change_pct": _change_pct(len(events), len(prev_events)) if prev_events is not None else None,
        "high_severity_events": high,
        "high_severity_share": round(high / len(events), 4) if events else 0.0,
        "avg_speed_kmh": _avg(speeds),
        "max_speed_kmh": round(max(speeds), 1) if speeds else None,
        "avg_excess_pct": _avg(excess),
        "places": len({_square(e) for e in events}),
        "sources": len({e.source for e in events}),
        "countries": len({e.country for e in events if e.country}),
        "first_event_at": min(times) if times else None,
        "last_event_at": max(times) if times else None,
        "by_type": type_summaries(events, cfg, types),
    }


def breakdown_rows(groups: Dict[str, Dict[str, Any]], order: Optional[Sequence[str]] = None,
                   types: Sequence[str] = EXCEPTION_TYPES) -> List[Dict[str, Any]]:
    """[{key, count, share, high_severity, by_type}] from {key: {"n", "high", "by_type": Counter}};
    `order` fixes the order (and shows empty keys), else most exceptions first."""
    total = sum(g["n"] for g in groups.values())
    keys = list(order) if order is not None else sorted(groups, key=lambda k: (-groups[k]["n"], k))
    out = []
    for k in keys:
        g = groups.get(k) or {"n": 0, "high": 0, "by_type": Counter()}
        out.append({"key": k, "count": g["n"], "share": round(g["n"] / total, 4) if total else 0.0,
                    "high_severity": g["high"], "by_type": {t: g["by_type"].get(t, 0) for t in types}})
    return out


def new_group() -> Dict[str, Any]:
    return {"n": 0, "high": 0, "by_type": Counter()}


def add_to_group(g: Dict[str, Any], event_type: str, severity: str, n: int) -> None:
    g["n"] += n
    g["high"] += n if severity == "high" else 0
    g["by_type"][event_type] += n


def breakdowns(events: Sequence, cfg: ExceptionPatternSettings,
               types: Sequence[str] = EXCEPTION_TYPES) -> Dict[str, List[Dict[str, Any]]]:
    """Exceptions by source (provider), country, normalised severity, speed band and — speeding with
    a known limit — excess over the limit. Unknown speed / excess are left out of the bands."""
    dims: Dict[str, Dict[str, dict]] = {d: defaultdict(new_group) for d in
                                        ("source", "country", "severity", "speed_band", "excess_band")}
    for e in events:
        sev = normalize_severity(e, cfg)
        keys = {"source": e.source, "country": e.country or "", "severity": sev,
                "speed_band": speed_band(e), "excess_band": excess_band(e) if e.event_type == "SPEEDING" else ""}
        for d, k in keys.items():
            if k or d == "country":
                add_to_group(dims[d][k], e.event_type, sev, 1)
    return {
        "by_source": breakdown_rows(dims["source"], types=types),
        "by_country": breakdown_rows(dims["country"], types=types),
        "by_severity": breakdown_rows(dims["severity"], SEVERITIES, types),
        "by_speed_band": breakdown_rows(dims["speed_band"], R.SPEED_BANDS, types),
        "by_excess_band": breakdown_rows(dims["excess_band"], R.EXCESS_BANDS, types),
    }


def trends(events: Sequence, window_start: Optional[datetime], window_end: datetime,
           cfg: ExceptionPatternSettings, types: Sequence[str] = EXCEPTION_TYPES,
           unit: Optional[str] = None) -> Dict[str, Any]:
    """Exception counts per time bucket and type (and high severity), plus hour-of-day and weekday
    patterns (local time, settings `timezone`)."""
    tz = ZoneInfo(cfg.timezone)
    start = window_start or (min(e.occurred_at for e in events) if events else window_end - timedelta(days=30))
    unit = unit or bucket_unit(start, window_end)
    starts = bucket_starts(start, window_end, unit)
    series = {b: {"bucket": b, "total": 0, "high_severity": 0, **{t: 0 for t in types}} for b in starts}
    hours = [{"hour": h, "total": 0, **{t: 0 for t in types}} for h in range(24)]
    weekdays = [{"weekday": d, "total": 0, **{t: 0 for t in types}} for d in range(7)]
    for e in events:
        row = series.get(bucket_floor(e.occurred_at, unit))
        if row is not None:
            row["total"] += 1
            row["high_severity"] += 1 if normalize_severity(e, cfg) == "high" else 0
            if e.event_type in row:
                row[e.event_type] += 1
        lt = _local(e.occurred_at, tz)
        for r in (hours[lt.hour], weekdays[lt.weekday()]):
            r["total"] += 1
            if e.event_type in r:
                r[e.event_type] += 1
    return {"unit": unit, "series": list(series.values()), "by_hour": hours, "by_weekday": weekdays}


def heatmap(events: Sequence, cell_size_m: float, cfg: ExceptionPatternSettings) -> List[Dict[str, Any]]:
    """Exception concentration per grid cell. Intensity is a count, not an accident probability
    (RRAP-09-008)."""
    cells: Dict[str, Dict[str, Any]] = {}
    for e in events:
        cid = grid_cell_id(e.latitude, e.longitude, cell_size_m)
        c = cells.get(cid)
        if c is None:
            lat, lon = grid_cell_center(cid)
            c = cells[cid] = {"cell_id": cid, "latitude": lat, "longitude": lon, "count": 0, "high_severity": 0,
                              "by_type": {t: 0 for t in EXCEPTION_TYPES}}
        c["count"] += 1
        c["high_severity"] += 1 if normalize_severity(e, cfg) == "high" else 0
        if e.event_type in c["by_type"]:
            c["by_type"][e.event_type] += 1
    return sorted(cells.values(), key=lambda c: -c["count"])


def zone_row(cid: str, size: float, n: int, by_type: Counter, high: int, roads: Counter,
             first: datetime, last: datetime) -> Dict[str, Any]:
    lat, lon = grid_cell_center(cid)
    dominant, dom_n = by_type.most_common(1)[0]
    return {
        "cell_id": cid, "latitude": lat, "longitude": lon, "radius_m": size / 2, "events": n,
        "by_type": {t: by_type.get(t, 0) for t in EXCEPTION_TYPES}, "dominant_type": dominant,
        "dominant_share": round(dom_n / n, 3),
        "multi_type": sum(1 for t in EXCEPTION_TYPES if by_type.get(t)) >= 2,
        "high_severity": high, "high_severity_share": round(high / n, 3),
        "road_name": roads.most_common(1)[0][0] if roads else None,
        "first_event_at": first, "last_event_at": last,
    }


def rank_zones(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Most exceptions first (then most high-severity ones); numbered from 1."""
    rows.sort(key=lambda z: (-z["events"], -z["high_severity"], z["cell_id"]))
    for i, z in enumerate(rows, 1):
        z["rank"] = i
    return rows


def zones(events: Sequence, cfg: ExceptionPatternSettings) -> List[Dict[str, Any]]:
    """Exception zones: cells of `zone_cell_size_m` with at least `zone_min_events` exceptions."""
    groups: Dict[str, list] = defaultdict(list)
    for e in events:
        groups[grid_cell_id(e.latitude, e.longitude, cfg.zone_cell_size_m)].append(e)
    out = []
    for cid, rows in groups.items():
        if len(rows) < cfg.zone_min_events:
            continue
        out.append(zone_row(cid, cfg.zone_cell_size_m, len(rows), Counter(e.event_type for e in rows),
                            sum(1 for e in rows if normalize_severity(e, cfg) == "high"),
                            Counter(e.road_name for e in rows if e.road_name),
                            min(e.occurred_at for e in rows), max(e.occurred_at for e in rows)))
    return rank_zones(out)


def methodology(cfg: ExceptionPatternSettings) -> Dict[str, Any]:
    return {
        "version": METHODOLOGY_VERSION,
        "severity_rule": (
            f"The source's level (1 / low, 2 / medium, 3 / high and synonyms); else, for speeding with a known "
            f"limit, high from {cfg.speeding_high_excess_pct:g} % over the limit, medium from "
            f"{cfg.speeding_medium_excess_pct:g} %, low below; else unknown."),
        "speed_bands_kmh": list(R.SPEED_BANDS),
        "excess_bands_pct": list(R.EXCESS_BANDS),
        "settings": cfg.model_dump(),
        "notes": [
            "Anonymous statistics: no driver or vehicle is stored, ranked or profiled.",
            "Exception counts are not accident probabilities and do not establish causation.",
            "Without exposure data (distance, driving time) a place with more traffic shows more exceptions.",
            "Hours and weekdays are local time (settings `timezone`).",
        ],
    }


def filter_bbox(events: Iterable, bbox: Optional[Tuple[float, float, float, float]]) -> List:
    if not bbox:
        return list(events)
    s, w, n, e_ = bbox
    return [e for e in events if s <= e.latitude <= n and w <= e.longitude <= e_]
