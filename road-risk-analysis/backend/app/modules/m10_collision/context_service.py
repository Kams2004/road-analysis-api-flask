"""
Context Risk Index plumbing (layer 2 of the collision engine,
docs/COLLISION-PREVENTION.md): precomputes the per-place context into Redis
every few minutes, and reads it back on the hot path. The maths is pure and
lives in collision/context.py.

Precomputation — refresh(db, r)  (beat task app/workers/collision_tasks.py)
────────────────────────────────
Runs when the last build is older than `collision.context.refresh_interval_s`
or the fleet-event data version (`events:data_version`, bumped by every event
import / summary rebuild — app/modules/m08_historical/agg_cache.py) changed:

  fleet exceptions   last history_window_days, grouped per ~110 m square × day × type ×
                     severity (daily summaries when built, raw events otherwise), read one
                     month at a time → history and habitual-speeding score per 150 m cell
  local speed limit  SPEEDING events grouped per square × limit (km/h) → median per cell
  hazards            validated (`active`) hazards of `hazard_types`
  community reports  visible reports (active, fresh, not merged) mapped to a reason kind

Nothing about a driver or a vehicle's identity: the context is the place, the live conditions
and the parties' current trip (speed vs the local limit, GPS, data age) — the fleet events are
anonymous (app/modules/m08_historical/anonymity.py).

Only cells with data are written, at most `max_cells`.

Redis keys (all JSON strings unless noted)
  collision:ctx:cell:{cell_id}   PlaceCell of one 150 m cell (grid_cell_id), TTL ≥ 2.5 refreshes
  collision:ctx:meta             {built_at, data_version, cells, tz, duration_ms}
  collision:ctx:lock             build in progress (NX)

Hot path — context_for_pair(…)  (collision_service, per conflicting pair)
────────────────────────────
One GET of the conflict point's cell, the meta (time zone) cached in-process for a
minute. Never touches PostgreSQL; any
Redis trouble degrades to "no place context" (C from the live conditions only).
"""
from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from app.core.config import settings
from app.core.storage.redis_client import get_redis
from app.modules.m08_historical.grid import grid_cell_id
from app.modules.m10_collision.collision import context as ctx
from app.modules.m10_collision.collision import thresholds

logger = logging.getLogger(__name__)

CELL_PREFIX = "collision:ctx:cell:"
LEGACY_DRIVERS_KEY = "collision:ctx:drivers"   # former per-plate / driver scores — deleted on every write
META_KEY = "collision:ctx:meta"
LOCK_KEY = "collision:ctx:lock"
DATA_VERSION_KEY = "events:data_version"      # app.modules.m08_historical.agg_cache.VERSION_KEY
DEFAULT_TZ = "Africa/Douala"
_WRITE_CHUNK = 1000
_HISTORY_CHUNK_DAYS = 31


def cell_size_m() -> float:
    return settings.HISTORICAL_GRID_CELL_SIZE_M


def cell_key(cell_id: str) -> str:
    return f"{CELL_PREFIX}{cell_id}"


def cell_ttl_s(cfg) -> int:
    return int(max(900, 2.5 * cfg.refresh_interval_s))


# ── hot path ─────────────────────────────────────────────────────────────────

_meta_cache: Tuple[float, dict] = (0.0, {})


async def _meta(r) -> dict:
    global _meta_cache
    now = time.monotonic()
    if now - _meta_cache[0] < 60 and _meta_cache[1]:
        return _meta_cache[1]
    try:
        raw = await r.get(META_KEY)
        meta = json.loads(raw) if raw else {}
    except Exception:
        meta = {}
    _meta_cache = (now, meta)
    return meta


def reset_meta_cache() -> None:
    global _meta_cache
    _meta_cache = (0.0, {})


async def place_at(r, lat: float, lon: float) -> Optional[ctx.PlaceCell]:
    raw = await r.get(cell_key(grid_cell_id(lat, lon, cell_size_m())))
    if not raw:
        return None
    try:
        return ctx.PlaceCell.model_validate_json(raw)
    except ValueError:
        return None


async def context_for_pair(conflict, own_traj, other_traj, own_live: dict, other_live: dict,
                           now: Optional[datetime] = None) -> ctx.ContextResult:
    """Context Risk Index of a physically-rated conflict (Redis only)."""
    cfg = thresholds.current()
    now = now or datetime.now(timezone.utc)
    point = conflict.conflict_point or (own_live["latitude"], own_live["longitude"])
    cell = None
    tz = DEFAULT_TZ
    try:
        r = get_redis()
        cell = await place_at(r, point[0], point[1])
        if cell is None and conflict.conflict_point is not None:
            cell = await place_at(r, own_live["latitude"], own_live["longitude"])
        tz = (await _meta(r)).get("tz") or DEFAULT_TZ
    except Exception:
        logger.warning("[CONTEXT] Redis lookup failed — place context skipped", exc_info=True)

    def party(traj):
        ts = traj.telemetry_timestamp if traj.telemetry_timestamp.tzinfo else traj.telemetry_timestamp.replace(tzinfo=timezone.utc)
        return ctx.Party(
            vehicle_id=traj.vehicle_id, speed_mps=traj.initial_speed_mps,
            accuracy_m=None if traj.gps_accuracy_assumed else traj.gps_accuracy_m,
            telemetry_age_s=max(0.0, (now - ts).total_seconds()),
        )

    parties = [party(own_traj), party(other_traj)]
    return ctx.compute_context(cell, point, parties, now, tz, cfg.fresh_after_s, cfg.context)


# ── precomputation (beat task) ───────────────────────────────────────────────

async def _history_rows(db, dcfg, ccfg, now: datetime, use_rollups: bool):
    """EventAgg rows of the window, read one month at a time (bounded result sets)."""
    from app.modules.m08_historical import aggregates as G
    end = now.replace(tzinfo=None)
    start = end - timedelta(days=ccfg.history_window_days)
    types = sorted(t for t, w in ccfg.history_type_weights.items() if w > 0)
    cur = start
    while cur < end:
        nxt = min(end, cur + timedelta(days=_HISTORY_CHUNK_DAYS))
        scope = G.Scope(cur, nxt, types, None, rollups=use_rollups)
        rows = await G.cell_rows(db, dcfg, scope, ("day", "k_lat", "k_lon", "event_type", "severity"))
        yield [ctx.EventAgg(r.lat_sum / r.n, r.lon_sum / r.n, r.event_type, r.severity, G.as_date(r.day), int(r.n))
               for r in rows if r.n]
        cur = nxt


async def _history(db, dcfg, ccfg, now: datetime, use_rollups: bool) -> Dict[str, dict]:
    """build_history over the whole window, accumulated month by month."""
    history: Dict[str, dict] = {}
    async for chunk in _history_rows(db, dcfg, ccfg, now, use_rollups):
        ctx.build_history(chunk, ccfg, dcfg.multiplier, now, cell_size_m(), into=history)
    return history


async def _limit_histograms(db, ccfg, now: datetime) -> Dict[str, Dict[float, int]]:
    from sqlalchemy import Integer, cast, func, select
    from app.modules.m08_historical.models.historical_event import HistoricalEventRecord as E
    since = now.replace(tzinfo=None) - timedelta(days=ccfg.history_window_days)
    k_lat, k_lon = cast(E.latitude * 1000, Integer), cast(E.longitude * 1000, Integer)
    kmh = cast(func.round(E.speed_limit_mps * 3.6), Integer)
    q = (select(k_lat.label("k_lat"), k_lon.label("k_lon"), kmh.label("kmh"), func.count().label("n"),
                func.sum(E.latitude).label("lat_sum"), func.sum(E.longitude).label("lon_sum"))
         .where(E.event_type == "SPEEDING", E.speed_limit_mps > 0, E.occurred_at >= since)
         .group_by(k_lat, k_lon, kmh))
    out: Dict[str, Dict[float, int]] = defaultdict(lambda: defaultdict(int))
    for r in (await db.execute(q)).all():
        if not r.n or not r.kmh:
            continue
        cid = grid_cell_id(r.lat_sum / r.n, r.lon_sum / r.n, cell_size_m())
        out[cid][round(r.kmh / 3.6, 2)] += int(r.n)
    return out


async def _hazards(db, ccfg) -> List[tuple]:
    from sqlalchemy import select
    from app.modules.m06_hazards.models.hazard import Hazard, HazardStatus
    rows = (await db.execute(select(Hazard.type, Hazard.latitude, Hazard.longitude).where(
        Hazard.status == HazardStatus.active.value, Hazard.type.in_(list(ccfg.hazard_types))))).all()
    return [(t, float(la), float(lo)) for t, la, lo in rows if la is not None and lo is not None]


async def _reports(db, ccfg, now: datetime) -> List[tuple]:
    from sqlalchemy import or_, select
    from app.core import settings_service
    from app.modules.m11_community.models.signalement import Signalement, SignalementStatus
    from app.modules.m11_community.signalement_votes import is_visible
    community = await settings_service.get_section(db, "community")
    horizon_h = max([community.default_expiry_hours, *community.expiry_hours_by_type.values()])
    since = now.replace(tzinfo=None) - timedelta(hours=horizon_h + 24)
    sigs = (await db.execute(select(Signalement).where(
        Signalement.status == SignalementStatus.actif, Signalement.merged_into_id.is_(None),
        or_(Signalement.reported_at >= since, Signalement.last_reported_at >= since,
            Signalement.last_confirmed_at >= since)))).scalars().all()
    out = []
    naive_now = now.replace(tzinfo=None)
    for s in sigs:
        typ = s.type.value if hasattr(s.type, "value") else str(s.type)
        kind = ccfg.report_kinds.get(typ)
        if kind and is_visible(s, community, naive_now):
            out.append((typ, kind, float(s.latitude), float(s.longitude)))
    return out


async def build(db, now: Optional[datetime] = None) -> dict:
    """Everything the Redis map needs: {"cells": {cell_id: PlaceCell}, "meta": {...}}."""
    from app.core import settings_service
    from app.modules.m08_historical import rollups as R
    now = now or datetime.now(timezone.utc)
    cfg = await thresholds.refresh(db, force=True)
    ccfg = cfg.context
    dcfg = await settings_service.get_section(db, "driver_behaviour")
    use_rollups = await R.ready(db, dcfg)
    size = cell_size_m()

    history = await _history(db, dcfg, ccfg, now, use_rollups)
    limits = ctx.infer_local_limits(await _limit_histograms(db, ccfg, now), ccfg.speed_limit_min_events)
    hazards = ctx.spread_points(await _hazards(db, ccfg), ccfg.hazard_radius_m, size)
    reports = ctx.spread_points(await _reports(db, ccfg, now), ccfg.report_radius_m, size)
    cells = ctx.build_place_cells(history, limits, hazards, reports, ccfg)
    meta = {"built_at": now.isoformat(), "cells": len(cells), "tz": dcfg.timezone, "rollups": use_rollups}
    return {"cells": cells, "meta": meta}


def write(r, data: dict, ttl_s: int) -> None:
    """Write a build with the sync Redis client (pipelined, chunked); stale cells simply expire.
    The per-plate / driver scores of earlier versions are deleted."""
    items = list(data["cells"].items())
    for i in range(0, len(items), _WRITE_CHUNK):
        pipe = r.pipeline(transaction=False)
        for cid, cell in items[i:i + _WRITE_CHUNK]:
            pipe.set(cell_key(cid), cell.model_dump_json(), ex=ttl_s)
        pipe.execute()
    r.delete(LEGACY_DRIVERS_KEY, f"{LEGACY_DRIVERS_KEY}:tmp")
    r.set(META_KEY, json.dumps(data["meta"]), ex=ttl_s)


def due(meta_raw: Optional[str], data_version: str, interval_s: int, now: datetime) -> bool:
    """A refresh is due when nothing was built, the interval elapsed or new events arrived."""
    if not meta_raw:
        return True
    try:
        meta = json.loads(meta_raw)
        built = datetime.fromisoformat(meta["built_at"])
    except (ValueError, KeyError, TypeError):
        return True
    if str(meta.get("data_version", "")) != str(data_version):
        return True
    return (now - built).total_seconds() >= interval_s - 5


async def refresh(db, r, force: bool = False) -> dict:
    """Beat entry point: rebuild the Redis context map when due. `r` is a sync Redis client."""
    now = datetime.now(timezone.utc)
    cfg = await thresholds.refresh(db, force=True)
    if not cfg.context.enabled:
        return {"skipped": "context disabled"}
    version = str(r.get(DATA_VERSION_KEY) or "0")
    if not force and not due(r.get(META_KEY), version, cfg.context.refresh_interval_s, now):
        return {"skipped": "not due"}
    started = time.monotonic()
    data = await build(db, now)
    data["meta"]["data_version"] = version
    data["meta"]["duration_ms"] = round((time.monotonic() - started) * 1000)
    write(r, data, cell_ttl_s(cfg.context))
    logger.info("[CONTEXT] refreshed cells=%d in %dms", data["meta"]["cells"], data["meta"]["duration_ms"])
    return data["meta"]


# ── calibration (python -m app.cli collision-calibration) ────────────────────

def place_index(history: Dict[str, dict], ccfg) -> Dict[str, float]:
    """Place part of the index (history + habitual speeding) per cell — pure."""
    cells = ctx.build_place_cells(history, {}, {}, {}, ccfg)
    return {cid: c.score for cid, c in cells.items()}


async def calibrate(db, holdout_days: int = 90, min_events: int = 5, now: Optional[datetime] = None) -> dict:
    """Out-of-sample check of the place index: built from the events BEFORE the last
    `holdout_days`, compared with where harsh braking and accidents happened DURING them
    (holdout_days = 0: same window, in-sample — optimistic by construction)."""
    from app.core import settings_service
    from app.modules.m08_historical import aggregates as G
    from app.modules.m08_historical import rollups as R
    now = now or datetime.now(timezone.utc)
    cfg = await thresholds.refresh(db, force=True)
    ccfg = cfg.context
    dcfg = await settings_service.get_section(db, "driver_behaviour")
    use_rollups = await R.ready(db, dcfg)
    cutoff = now - timedelta(days=holdout_days)
    size = cell_size_m()

    history = await _history(db, dcfg, ccfg, cutoff, use_rollups)
    index = place_index(history, ccfg)

    start = cutoff if holdout_days > 0 else cutoff - timedelta(days=ccfg.history_window_days)
    scope = G.Scope(start.replace(tzinfo=None), now.replace(tzinfo=None), ["HARD_BRAKING", "ACCIDENT"], None,
                    rollups=use_rollups)
    outcomes: Dict[str, int] = defaultdict(int)
    for r in await G.cell_rows(db, dcfg, scope, ("k_lat", "k_lon")):
        if r.n:
            outcomes[grid_cell_id(r.lat_sum / r.n, r.lon_sum / r.n, size)] += int(r.n)
    report = ctx.calibration_report(index, dict(outcomes), min_events)
    report.update({"holdout_days": holdout_days, "index_cells": len(index),
                   "outcome_events": int(sum(outcomes.values())), "cutoff": cutoff.isoformat()})
    return report
