"""
Exception-pattern answers computed from grouped reads (app/modules/m08_historical/aggregates.py)
instead of loading every event: the same shapes as analytics.py (which keeps the per-event
rules, used by the reports and the tests). Anonymous: every grouping is by type, severity,
source, country, speed band, time or place — never by driver or vehicle.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.m08_historical import aggregates as G
from app.modules.m08_historical import rollups as R
from app.modules.m08_historical.grid import grid_cell_center, grid_cell_id
from app.modules.m09_exception_patterns import analytics as A
from app.modules.m09_exception_patterns.settings import EXCEPTION_TYPES, SEVERITIES, ExceptionPatternSettings

KMH = 3.6


def _avg(total: float, n: int) -> Optional[float]:
    return round(total / n, 1) if n else None


def local_patterns(rows, cfg: ExceptionPatternSettings, types: Sequence[str]):
    """(by_hour[24], by_weekday[7] Monday = 0) in the settings time zone, from UTC (dow, hour) rows."""
    off = datetime.now(ZoneInfo(cfg.timezone)).utcoffset()      # current offset (no DST in the fleet's zones)
    off_min = int(off.total_seconds() // 60) if off else 0
    hours = [{"hour": h, "total": 0, **{t: 0 for t in types}} for h in range(24)]
    weekdays = [{"weekday": d, "total": 0, **{t: 0 for t in types}} for d in range(7)]
    for dow, hour, t, n in rows:
        m = int(hour) * 60 + off_min
        lh, shift = (m // 60) % 24, m // 1440
        wd = (int(dow) - 1 + shift) % 7                    # SQL: 0 = Sunday → Python: 0 = Monday
        for r in (hours[lh], weekdays[wd]):
            r["total"] += int(n)
            if t in r:
                r[t] += int(n)
    return hours, weekdays


# ── overview ─────────────────────────────────────────────────────────────────

async def type_summaries(db: AsyncSession, cfg, s: G.Scope, types: Sequence[str]) -> List[Dict[str, Any]]:
    rows = await G.entity_rows(db, cfg, s, ("event_type", "severity"))
    per: Dict[str, dict] = defaultdict(lambda: {"n": 0, "sev": Counter(), "speed": [0.0, 0],
                                                "limit": [0.0, 0], "excess": [0.0, 0], "max": None})
    total = 0
    for r in rows:
        p = per[r.event_type]
        n = int(r.n or 0)
        total += n
        p["n"] += n
        p["sev"][r.severity] += n
        p["speed"][0] += r.speed_sum or 0.0
        p["speed"][1] += int(r.speed_n or 0)
        p["limit"][0] += r.limit_sum or 0.0
        p["limit"][1] += int(r.limit_n or 0)
        p["excess"][0] += r.excess_sum or 0.0
        p["excess"][1] += int(r.excess_n or 0)
        if r.speed_max is not None:
            p["max"] = r.speed_max if p["max"] is None else max(p["max"], r.speed_max)
    hours_by_type: Dict[str, Counter] = defaultdict(Counter)
    hour_rows = await G.hour_rows(db, s)
    for t in types:
        by_hour, _ = local_patterns([r for r in hour_rows if r[2] == t], cfg, [t])
        for h in by_hour:
            if h["total"]:
                hours_by_type[t][h["hour"]] = h["total"]
    roads: Dict[str, Counter] = defaultdict(Counter)
    for r in await G.cell_rows(db, cfg, s, ("event_type", "road_name")):
        if r.road_name:
            roads[r.event_type][r.road_name] += int(r.n)
    out = []
    for t in types:
        p = per.get(t) or per.default_factory()
        out.append({
            "event_type": t, "count": p["n"], "share": round(p["n"] / total, 4) if total else 0.0,
            "by_severity": {k: p["sev"].get(k, 0) for k in SEVERITIES},
            "high_severity": p["sev"].get("high", 0),
            "avg_speed_kmh": _avg(p["speed"][0] * KMH, p["speed"][1]),
            "max_speed_kmh": round(p["max"] * KMH, 1) if p["max"] is not None else None,
            "avg_limit_kmh": _avg(p["limit"][0] * KMH, p["limit"][1]),
            "avg_excess_pct": _avg(p["excess"][0], p["excess"][1]),
            "peak_hour": hours_by_type[t].most_common(1)[0][0] if hours_by_type[t] else None,
            "top_roads": [{"road_name": rd, "count": c} for rd, c in roads[t].most_common(3)],
        })
    return out


async def overview(db: AsyncSession, cfg, s: G.Scope, prev: Optional[G.Scope], types: Sequence[str]) -> dict:
    by_type = await type_summaries(db, cfg, s, types)
    total = sum(t["count"] for t in by_type)
    high = sum(t["high_severity"] for t in by_type)
    span = (await G.entity_rows(db, cfg, s, ()))[0]
    prev_total = None
    if prev is not None:
        prev_total = int((await G.entity_rows(db, cfg, prev, ()))[0].n or 0)
    who = await G.entity_rows(db, cfg, s, ("source", "country"))
    places = await G.cell_rows(db, cfg, s, ("k_lat", "k_lon"))
    return {
        "total_events": total,
        "previous_total_events": prev_total,
        "change_pct": A._change_pct(total, prev_total) if prev_total is not None else None,
        "high_severity_events": high,
        "high_severity_share": round(high / total, 4) if total else 0.0,
        "avg_speed_kmh": _avg((span.speed_sum or 0.0) * KMH, int(span.speed_n or 0)),
        "max_speed_kmh": round(span.speed_max * KMH, 1) if span.speed_max is not None else None,
        "avg_excess_pct": _avg(span.excess_sum or 0.0, int(span.excess_n or 0)),
        "places": len(places),
        "sources": len({r.source for r in who}),
        "countries": len({r.country for r in who if r.country}),
        "first_event_at": G.as_dt(span.first_at) if total else None,
        "last_event_at": G.as_dt(span.last_at) if total else None,
        "by_type": by_type,
    }


# ── breakdowns ───────────────────────────────────────────────────────────────

async def breakdowns(db: AsyncSession, cfg, s: G.Scope, types: Sequence[str]) -> Dict[str, List[dict]]:
    """Same as analytics.breakdowns, from one grouped read."""
    dims: Dict[str, Dict[str, dict]] = {d: defaultdict(A.new_group) for d in
                                        ("source", "country", "severity", "speed_band", "excess_band")}
    keys = ("source", "country", "event_type", "severity", "speed_band", "excess_band")
    for r in await G.entity_rows(db, cfg, s, keys):
        n = int(r.n or 0)
        values = {"source": r.source, "country": r.country, "severity": r.severity, "speed_band": r.speed_band,
                  "excess_band": r.excess_band if r.event_type == "SPEEDING" else ""}
        for d, k in values.items():
            if k or d == "country":
                A.add_to_group(dims[d][k], r.event_type, r.severity, n)
    return {
        "by_source": A.breakdown_rows(dims["source"], types=types),
        "by_country": A.breakdown_rows(dims["country"], types=types),
        "by_severity": A.breakdown_rows(dims["severity"], SEVERITIES, types),
        "by_speed_band": A.breakdown_rows(dims["speed_band"], R.SPEED_BANDS, types),
        "by_excess_band": A.breakdown_rows(dims["excess_band"], R.EXCESS_BANDS, types),
    }


# ── time / map ───────────────────────────────────────────────────────────────

async def trends(db: AsyncSession, cfg, s: G.Scope, types: Sequence[str], unit: Optional[str]) -> dict:
    rows = await G.entity_rows(db, cfg, s, ("day", "event_type", "severity"))
    start = s.start
    if start is None:
        days = [G.as_date(r.day) for r in rows]
        start = datetime.combine(min(days), datetime.min.time()) if days else s.end - timedelta(days=30)
    unit = unit or A.bucket_unit(start, s.end)
    series = {b: {"bucket": b, "total": 0, "high_severity": 0, **{t: 0 for t in types}}
              for b in A.bucket_starts(start, s.end, unit)}
    for r in rows:
        row = series.get(A.bucket_floor(datetime.combine(G.as_date(r.day), datetime.min.time()), unit))
        if row is None:
            continue
        n = int(r.n)
        row["total"] += n
        row["high_severity"] += n if r.severity == "high" else 0
        if r.event_type in row:
            row[r.event_type] += n
    by_hour, by_weekday = local_patterns(await G.hour_rows(db, s), cfg, types)
    return {"unit": unit, "series": list(series.values()), "by_hour": by_hour, "by_weekday": by_weekday}


def _target(r, size: float) -> str:
    return grid_cell_id(r.lat_sum / r.n, r.lon_sum / r.n, size)


async def heatmap(db: AsyncSession, cfg, s: G.Scope, size: float) -> List[Dict[str, Any]]:
    cells: Dict[str, Dict[str, Any]] = {}
    for r in await G.cell_rows(db, cfg, s, ("k_lat", "k_lon", "event_type", "severity")):
        cid = _target(r, size)
        c = cells.get(cid)
        if c is None:
            lat, lon = grid_cell_center(cid)
            c = cells[cid] = {"cell_id": cid, "latitude": lat, "longitude": lon, "count": 0, "high_severity": 0,
                              "by_type": {t: 0 for t in EXCEPTION_TYPES}}
        n = int(r.n)
        c["count"] += n
        c["high_severity"] += n if r.severity == "high" else 0
        if r.event_type in c["by_type"]:
            c["by_type"][r.event_type] += n
    return sorted(cells.values(), key=lambda c: -c["count"])


async def zones(db: AsyncSession, cfg, s: G.Scope, limit: int) -> List[Dict[str, Any]]:
    size = cfg.zone_cell_size_m
    cells: Dict[str, dict] = {}
    for r in await G.cell_rows(db, cfg, s, ("k_lat", "k_lon", "event_type", "severity", "road_name")):
        cid = _target(r, size)
        c = cells.setdefault(cid, {"n": 0, "by_type": Counter(), "high": 0, "roads": Counter(),
                                   "first": None, "last": None})
        n = int(r.n)
        c["n"] += n
        c["by_type"][r.event_type] += n
        c["high"] += n if r.severity == "high" else 0
        if r.road_name:
            c["roads"][r.road_name] += n
        f, l_ = G.as_dt(r.first_at), G.as_dt(r.last_at)
        c["first"] = f if c["first"] is None or f < c["first"] else c["first"]
        c["last"] = l_ if c["last"] is None or l_ > c["last"] else c["last"]
    out = [A.zone_row(cid, size, c["n"], c["by_type"], c["high"], c["roads"], c["first"], c["last"])
           for cid, c in cells.items() if c["n"] >= cfg.zone_min_events]
    return A.rank_zones(out)[:limit]
