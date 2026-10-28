"""
Driver behaviour answers computed from grouped reads (app/modules/m08_historical/aggregates.py)
instead of loading every event: the same shapes as analytics.py (which keeps the per-event
rules, used for one driver's / vehicle's own events and by the tests).
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.m08_historical import aggregates as G
from app.modules.m08_historical.grid import grid_cell_center, grid_cell_id
from app.modules.m09_driver_behaviour import analytics as A
from app.modules.m09_driver_behaviour.settings import BEHAVIOUR_TYPES, DriverBehaviourSettings

KMH = 3.6
SEVERITIES = ("low", "medium", "high", "unknown")


def _pts(cfg: DriverBehaviourSettings, t: str, sev: str, n: int) -> float:
    return cfg.weight(t) * cfg.multiplier(sev) * n


def _avg(total: float, n: int) -> Optional[float]:
    return round(total / n, 1) if n else None


def local_patterns(rows, cfg: DriverBehaviourSettings, types: Sequence[str]):
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
    per: Dict[str, dict] = defaultdict(lambda: {"n": 0, "points": 0.0, "sev": Counter(), "speed": [0.0, 0],
                                                "limit": [0.0, 0], "excess": [0.0, 0], "max": None})
    total = 0
    for r in rows:
        p = per[r.event_type]
        n = int(r.n or 0)
        total += n
        p["n"] += n
        p["points"] += _pts(cfg, r.event_type, r.severity, n)
        p["sev"][r.severity] += n
        p["speed"][0] += r.speed_sum or 0.0
        p["speed"][1] += int(r.speed_n or 0)
        p["limit"][0] += r.limit_sum or 0.0
        p["limit"][1] += int(r.limit_n or 0)
        p["excess"][0] += r.excess_sum or 0.0
        p["excess"][1] += int(r.excess_n or 0)
        if r.speed_max is not None:
            p["max"] = r.speed_max if p["max"] is None else max(p["max"], r.speed_max)
    who = {k: defaultdict(set) for k in ("driver", "vehicle")}
    for k in who:
        for r in await G.entity_rows(db, cfg, s, ("event_type", k)):
            if getattr(r, k):
                who[k][r.event_type].add(getattr(r, k))
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
            "points": round(p["points"], 2), "by_severity": {k: p["sev"].get(k, 0) for k in SEVERITIES},
            "high_severity": p["sev"].get("high", 0),
            "avg_speed_kmh": _avg(p["speed"][0] * KMH, p["speed"][1]),
            "max_speed_kmh": round(p["max"] * KMH, 1) if p["max"] is not None else None,
            "avg_limit_kmh": _avg(p["limit"][0] * KMH, p["limit"][1]),
            "avg_excess_pct": _avg(p["excess"][0], p["excess"][1]),
            "peak_hour": hours_by_type[t].most_common(1)[0][0] if hours_by_type[t] else None,
            "drivers": len(who["driver"][t]), "vehicles": len(who["vehicle"][t]),
            "top_roads": [{"road_name": rd, "count": c} for rd, c in roads[t].most_common(3)],
        })
    return out


async def overview(db: AsyncSession, cfg, s: G.Scope, prev: Optional[G.Scope], types: Sequence[str]) -> dict:
    by_type = await type_summaries(db, cfg, s, types)
    total = sum(t["count"] for t in by_type)
    pairs = await G.entity_rows(db, cfg, s, ("driver", "vehicle"))
    drivers = {r.driver for r in pairs if r.driver}
    vehicles = {r.vehicle for r in pairs if r.vehicle}
    identified = sum(int(r.n) for r in pairs if r.driver or r.vehicle)
    span = (await G.entity_rows(db, cfg, s, ()))[0]
    prev_total = None
    if prev is not None:
        prev_total = int((await G.entity_rows(db, cfg, prev, ()))[0].n or 0)
    ranked = (await ranking(db, cfg, s, None, "driver"))[0] if drivers else []
    return {
        "total_events": total,
        "previous_total_events": prev_total,
        "change_pct": A._change_pct(total, prev_total) if prev_total is not None else None,
        "points": round(sum(t["points"] for t in by_type), 2),
        "high_severity_events": sum(t["high_severity"] for t in by_type),
        "drivers": len(drivers), "vehicles": len(vehicles),
        "identified_share": round(identified / total, 4) if total else 0.0,
        "high_risk_drivers": sum(1 for r in ranked if r["score"] >= cfg.high_risk_score),
        "avg_driver_score": A._avg([r["score"] for r in ranked]),
        "first_event_at": G.as_dt(span.first_at) if total else None,
        "last_event_at": G.as_dt(span.last_at) if total else None,
        "by_type": by_type,
    }


# ── rankings ─────────────────────────────────────────────────────────────────

async def ranking(db: AsyncSession, cfg, s: G.Scope, prev: Optional[G.Scope], kind: str,
                  roads_for: int = 0) -> tuple[list[dict], int]:
    """(drivers / vehicles ranked riskiest first, events without the identifier). `roads_for`:
    top road of the first N rows (one extra grouped read)."""
    other = "vehicle" if kind == "driver" else "driver"
    groups: Dict[str, dict] = {}
    unidentified = 0
    for r in await G.entity_rows(db, cfg, s, (kind, "event_type", "severity")):
        k, n = getattr(r, kind), int(r.n)
        if not k:
            unidentified += n
            continue
        g = groups.setdefault(k, {"events": 0, "points": 0.0, "by_type": Counter(), "high": 0, "last": None})
        g["events"] += n
        g["points"] += _pts(cfg, r.event_type, r.severity, n)
        g["by_type"][r.event_type] += n
        g["high"] += n if r.severity == "high" else 0
        last = G.as_dt(r.last_at)
        g["last"] = last if g["last"] is None or last > g["last"] else g["last"]
    prev_pts: Dict[str, float] = defaultdict(float)
    prev_n: Dict[str, int] = defaultdict(int)
    if prev is not None:
        for r in await G.entity_rows(db, cfg, prev, (kind, "event_type", "severity")):
            k = getattr(r, kind)
            if k:
                prev_pts[k] += _pts(cfg, r.event_type, r.severity, int(r.n))
                prev_n[k] += int(r.n)
    linked: Dict[str, set] = defaultdict(set)
    for r in await G.entity_rows(db, cfg, s, (kind, other)):
        if getattr(r, kind) and getattr(r, other):
            linked[getattr(r, kind)].add(getattr(r, other))
    out = []
    for k, g in groups.items():
        score = A.score_from_points(g["points"], cfg)
        prev_score = A.score_from_points(prev_pts[k], cfg) if prev is not None else None
        out.append({
            "id": k, "events": g["events"], "by_type": {t: g["by_type"].get(t, 0) for t in BEHAVIOUR_TYPES},
            "high_severity": g["high"], "points": round(g["points"], 2), "score": score,
            "level": A.risk_level(score, cfg), "previous_score": prev_score,
            "previous_events": prev_n[k] if prev is not None else None,
            "score_delta": round(score - prev_score, 1) if prev_score is not None else None,
            "trend": A._trend(score, prev_score), "linked": sorted(linked[k])[:5], "last_event_at": g["last"],
            "top_road": None,
        })
    out.sort(key=lambda r: (-r["score"], -r["events"], r["id"]))
    for i, r in enumerate(out, 1):
        r["rank"] = i
    return out, unidentified


async def with_top_roads(db: AsyncSession, s: G.Scope, kind: str, rows: list[dict]) -> list[dict]:
    roads = await G.top_roads_of(db, s, kind, [r["id"] for r in rows])
    for r in rows:
        r["top_road"] = roads.get(r["id"])
    return rows


# ── time / map ───────────────────────────────────────────────────────────────

async def trends(db: AsyncSession, cfg, s: G.Scope, types: Sequence[str], unit: Optional[str]) -> dict:
    rows = await G.entity_rows(db, cfg, s, ("day", "event_type", "severity"))
    start = s.start
    if start is None:
        days = [G.as_date(r.day) for r in rows]
        start = datetime.combine(min(days), datetime.min.time()) if days else s.end - timedelta(days=30)
    unit = unit or A.bucket_unit(start, s.end)
    series = {b: {"bucket": b, "total": 0, "points": 0.0, **{t: 0 for t in types}}
              for b in A.bucket_starts(start, s.end, unit)}
    for r in rows:
        b = A.bucket_floor(datetime.combine(G.as_date(r.day), datetime.min.time()), unit)
        row = series.get(b)
        if row is None:
            continue
        n = int(r.n)
        row["total"] += n
        row["points"] += _pts(cfg, r.event_type, r.severity, n)
        if r.event_type in row:
            row[r.event_type] += n
    out = list(series.values())
    for r in out:
        r["points"] = round(r["points"], 2)
    by_hour, by_weekday = local_patterns(await G.hour_rows(db, s), cfg, types)
    return {"unit": unit, "series": out, "by_hour": by_hour, "by_weekday": by_weekday}


def _target(r, size: float) -> str:
    return grid_cell_id(r.lat_sum / r.n, r.lon_sum / r.n, size)


async def heatmap(db: AsyncSession, cfg, s: G.Scope, size: float) -> List[Dict[str, Any]]:
    cells: Dict[str, Dict[str, Any]] = {}
    for r in await G.cell_rows(db, cfg, s, ("k_lat", "k_lon", "event_type", "severity")):
        cid = _target(r, size)
        c = cells.get(cid)
        if c is None:
            lat, lon = grid_cell_center(cid)
            c = cells[cid] = {"cell_id": cid, "latitude": lat, "longitude": lon, "count": 0, "points": 0.0,
                              "by_type": {t: 0 for t in BEHAVIOUR_TYPES}}
        n = int(r.n)
        c["count"] += n
        c["points"] += _pts(cfg, r.event_type, r.severity, n)
        if r.event_type in c["by_type"]:
            c["by_type"][r.event_type] += n
    out = sorted(cells.values(), key=lambda c: -c["count"])
    for c in out:
        c["points"] = round(c["points"], 2)
    return out


async def zones(db: AsyncSession, cfg, s: G.Scope, limit: int) -> List[Dict[str, Any]]:
    size = cfg.zone_cell_size_m
    cells: Dict[str, dict] = {}
    for r in await G.cell_rows(db, cfg, s, ("k_lat", "k_lon", "event_type", "severity", "road_name")):
        cid = _target(r, size)
        c = cells.setdefault(cid, {"n": 0, "points": 0.0, "by_type": Counter(), "high": 0, "roads": Counter(),
                                   "first": None, "last": None, "squares": set()})
        n = int(r.n)
        c["n"] += n
        c["points"] += _pts(cfg, r.event_type, r.severity, n)
        c["by_type"][r.event_type] += n
        c["high"] += n if r.severity == "high" else 0
        if r.road_name:
            c["roads"][r.road_name] += n
        f, l_ = G.as_dt(r.first_at), G.as_dt(r.last_at)
        c["first"] = f if c["first"] is None or f < c["first"] else c["first"]
        c["last"] = l_ if c["last"] is None or l_ > c["last"] else c["last"]
        c["squares"].add((r.k_lat, r.k_lon))
    cands = sorted(((cid, c) for cid, c in cells.items() if c["n"] >= cfg.zone_min_events),
                   key=lambda kv: (-kv[1]["points"], -kv[1]["n"]))
    if cfg.zone_min_drivers <= 1:                          # the criterion never excludes: count the shown ones only
        cands = cands[:limit]
    who = await G.square_entities(db, s, {sq for _, c in cands for sq in c["squares"]})
    out = []
    for cid, c in cands:
        entities = set().union(*(who.get(sq, set()) for sq in c["squares"]))
        if entities and len(entities) < cfg.zone_min_drivers:
            continue
        lat, lon = grid_cell_center(cid)
        dominant, dom_n = c["by_type"].most_common(1)[0]
        out.append({
            "cell_id": cid, "latitude": lat, "longitude": lon, "radius_m": size / 2, "events": c["n"],
            "points": round(c["points"], 2), "score": A.score_from_points(c["points"], cfg),
            "by_type": {t: c["by_type"].get(t, 0) for t in BEHAVIOUR_TYPES}, "dominant_type": dominant,
            "dominant_share": round(dom_n / c["n"], 3),
            "multi_behaviour": sum(1 for t in BEHAVIOUR_TYPES if c["by_type"].get(t)) >= 2,
            "drivers": len(entities), "high_severity": c["high"],
            "road_name": c["roads"].most_common(1)[0][0] if c["roads"] else None,
            "first_event_at": c["first"], "last_event_at": c["last"],
        })
        if len(out) >= limit:
            break
    for i, z in enumerate(out, 1):
        z["rank"] = i
    return out
