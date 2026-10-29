"""
Orchestrates Step 4: for a given vehicle, find nearby live vehicles (Step 1's
Redis GEO index — app.modules.m10_collision.vehicle_service.query_nearby, no new spatial
index built here), compare its predicted trajectory (Step 3, reused via
trajectory_service's cache-first read — never recomputed here) against each
candidate's through the conflict detector, and cache the resulting report.

Mirrors road_state_service.py / trajectory_service.py's shape deliberately:
cache-first reads, no Postgres persistence — a conflict report is even more
perishable than a trajectory (it's derived from two trajectories that are
themselves already ephemeral), so there's no lasting value in storing one.

Bounded work per ping
─────────────────────
  candidates   the GEOSEARCH hits are filtered by select_candidates(): only vehicles
               actually approaching (closing speed > 0 from the live speed / heading) or
               already within the (widest possible) MONITOR distance, the
               `candidate_cap` nearest of them. Non-approaching vehicles beyond that
               distance are reported as cleared (their alert resolves at once).
  pairs        each unordered pair is assessed once per pair of telemetry fixes: the
               result is cached under `collision:pair:{a}:{b}` with the two telemetry
               timestamps it was computed from (TTL `pair_cache_ttl_s`); the other
               vehicle's pipeline reuses it while neither vehicle has sent a newer fix.
               A short NX lock (`collision:pair_lock:{a}:{b}`) lets the second pipeline
               wait briefly for the first instead of computing the same pair in parallel.
  context      a physically-rated conflict (risk above NONE) gets the Context Risk Index
               at its conflict point (context_service.context_for_pair — Redis only) and
               detector.apply_context widens / boosts its level.
"""
import asyncio
import json
import logging
import math
import time
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.modules.m10_collision import (
    context_service, road_state_service, staleness_guard, trajectory_service, vehicle_service,
)
from app.modules.m10_collision.collision import detector, thresholds
from app.modules.m10_collision.collision.models import ConflictRiskLevel, VehicleConflict, VehicleConflictReport
from app.core.storage.redis_client import get_redis
from app.modules.m03_maps.osrm import geo
from app.modules.m10_collision.trajectory.models import Trajectory

logger = logging.getLogger(__name__)

_RISK_SEVERITY = {
    ConflictRiskLevel.CRITICAL: 4,
    ConflictRiskLevel.HIGH: 3,
    ConflictRiskLevel.MODERATE: 2,
    ConflictRiskLevel.LOW: 1,
    ConflictRiskLevel.NONE: 0,
}


_PAIR_LOCK_S = 2
_PAIR_WAIT_S = 0.3
_PAIR_POLL_S = 0.05


def _cache_key(vehicle_id: str) -> str:
    return f"collision:{vehicle_id}"


def _pair_ids(a: str, b: str) -> Tuple[str, str]:
    return (a, b) if a <= b else (b, a)


def _pair_key(a: str, b: str) -> str:
    x, y = _pair_ids(a, b)
    return f"collision:pair:{x}:{y}"


def _pair_lock_key(a: str, b: str) -> str:
    x, y = _pair_ids(a, b)
    return f"collision:pair_lock:{x}:{y}"


# ── candidate selection (pure) ───────────────────────────────────────────────

def _velocity(live: dict) -> Optional[Tuple[float, float]]:
    speed, heading = live.get("speed_mps"), live.get("heading")
    if speed is None:
        return None
    if speed <= 0.0:
        return 0.0, 0.0
    if heading is None:
        return None
    rad = math.radians(heading)
    return speed * math.sin(rad), speed * math.cos(rad)     # east, north


def closing_speed_mps(own: dict, other: dict) -> Optional[float]:
    """Rate at which the straight-line distance shrinks now (> 0 = approaching), from the
    live positions, speeds and headings; None when a velocity is unknown."""
    va, vb = _velocity(own), _velocity(other)
    if va is None or vb is None:
        return None
    ox, oy = geo.local_xy_m(other["latitude"], other["longitude"], own["latitude"])
    sx, sy = geo.local_xy_m(own["latitude"], own["longitude"], own["latitude"])
    rx, ry = ox - sx, oy - sy
    dist = math.hypot(rx, ry)
    if dist < 1e-6:
        return 0.0
    rvx, rvy = vb[0] - va[0], vb[1] - va[1]
    return -(rx * rvx + ry * rvy) / dist


def select_candidates(own: dict, candidates: List[dict], monitor_distance_m: float,
                      cap: int) -> Tuple[List[dict], List[str]]:
    """(to assess, cleared ids). `candidates` are nearest first with `distance_m`. A vehicle is
    assessed when it is approaching (closing speed > 0, or unknown — conservative) or within
    monitor_distance_m; at most `cap` of them, nearest first. Vehicles neither approaching nor
    within that distance are cleared (structurally safe)."""
    keep: List[dict] = []
    cleared: List[str] = []
    for c in sorted(candidates, key=lambda c: c.get("distance_m") or 0.0):
        dist = c.get("distance_m")
        if dist is None:
            dist = geo.haversine_m(own["latitude"], own["longitude"], c["latitude"], c["longitude"])
        closing = closing_speed_mps(own, c)
        if dist <= monitor_distance_m or closing is None or closing > 0.0:
            keep.append(c)
        else:
            cleared.append(c["vehicle_id"])
    return keep[:cap], cleared


def monitor_reach_m(cfg) -> float:
    """Largest distance at which any level can fire: MONITOR × the largest scenario multiplier
    × the full context widening."""
    widen = 1.0 + (cfg.context.max_widening if cfg.context.enabled else 0.0)
    return cfg.levels["MONITOR"].max_distance_m * max(cfg.scenario_multipliers.values()) * widen


def orient(conflict: VehicleConflict, own_id: str) -> VehicleConflict:
    """The same pair assessment seen from `own_id` (vehicle_a = own)."""
    if conflict.vehicle_a_id == own_id:
        return conflict
    return conflict.model_copy(update={
        "vehicle_a_id": conflict.vehicle_b_id, "vehicle_b_id": conflict.vehicle_a_id,
        "road_type_a": conflict.road_type_b, "road_type_b": conflict.road_type_a,
    })


def _staleness_key(vehicle_id: str) -> str:
    return f"collision:ts_guard:{vehicle_id}"


async def _load_cached(vehicle_id: str) -> Optional[VehicleConflictReport]:
    r = get_redis()
    raw = await r.get(_cache_key(vehicle_id))
    if not raw:
        return None
    try:
        return VehicleConflictReport.model_validate_json(raw)
    except ValueError:
        logger.warning("Discarding unparseable cached conflict report for %s", vehicle_id)
        return None


async def _store_cached(report: VehicleConflictReport, telemetry_ts: datetime) -> None:
    """`telemetry_ts` is the *own* vehicle's underlying telemetry timestamp
    (own_trajectory.telemetry_timestamp), not `report.generated_at` —
    `generated_at` is wall-clock time at the moment this report finished
    computing, which is exactly the value that can't be used for staleness
    ordering: a slower-finishing pipeline run for an *older* ping would
    still get a *later* `generated_at` than a faster run for a newer one."""
    skip = await staleness_guard.skip_if_stale(
        _staleness_key(report.vehicle_id), telemetry_ts, settings.COLLISION_TTL_S
    )
    if skip:
        logger.info(
            "[COLLISION] vehicle=%s skipped stale write (telemetry older than what's already cached)",
            report.vehicle_id,
        )
        return
    r = get_redis()
    await r.set(_cache_key(report.vehicle_id), report.model_dump_json(), ex=settings.COLLISION_TTL_S)


async def compute_and_store(
    vehicle_id: str,
    db: AsyncSession,
    own_trajectory: Optional[Trajectory] = None,
) -> VehicleConflictReport:
    """Given (optionally) an already-computed Step 3 trajectory for this
    vehicle — the ping-triggered background task has one on hand at no extra
    cost — find nearby vehicles and assess conflicts against each.
    Raises road_state_service.NoTelemetryError if this vehicle isn't live."""
    started = time.monotonic()
    cfg = await thresholds.refresh()

    live = await vehicle_service.get_live(vehicle_id)
    if live is None:
        raise road_state_service.NoTelemetryError(f"vehicle {vehicle_id} has no live telemetry")

    if own_trajectory is None:
        own_trajectory = await trajectory_service.get_trajectory(vehicle_id, db)

    nearby = await vehicle_service.query_nearby(
        live["latitude"], live["longitude"], cfg.candidate_radius_m, vehicle_id,
    )
    candidates, cleared = select_candidates(live, nearby, monitor_reach_m(cfg), cfg.candidate_cap)

    conflicts: List[VehicleConflict] = []
    for candidate in candidates:
        other_id = candidate["vehicle_id"]
        try:
            other_trajectory = await trajectory_service.get_trajectory(other_id, db)
        except road_state_service.NoTelemetryError:
            # Vanished between being found as a candidate and being fetched
            # (dropped out of the live set) — skip it, don't fail the report.
            continue
        conflict = await assess_pair(vehicle_id, own_trajectory, live, other_id, other_trajectory, candidate)
        if conflict is None:
            continue
        if conflict.risk_level != ConflictRiskLevel.NONE:
            conflicts.append(conflict)
        elif conflict.clear_immediately:
            cleared.append(other_id)

    conflicts.sort(
        key=lambda c: (-_RISK_SEVERITY[c.risk_level], c.distance_at_closest_approach_m or float("inf"))
    )
    highest = conflicts[0].risk_level if conflicts else ConflictRiskLevel.NONE

    report = VehicleConflictReport(
        vehicle_id=vehicle_id,
        generated_at=datetime.now(timezone.utc),
        highest_risk=highest,
        conflicts=conflicts,
        cleared_vehicle_ids=cleared,
    )
    await _store_cached(report, own_trajectory.telemetry_timestamp)

    processing_ms = (time.monotonic() - started) * 1000
    logger.info(
        "[COLLISION] vehicle=%s nearby=%d candidates=%d conflicts=%d highestRisk=%s processingTime=%.1fms",
        vehicle_id, len(nearby), len(candidates), len(conflicts), highest.value, processing_ms,
    )
    return report


def _ts(t: Trajectory) -> str:
    ts = t.telemetry_timestamp
    return (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).isoformat()


async def _cached_pair(r, own_id: str, own: Trajectory, other_id: str, other: Trajectory) -> Optional[VehicleConflict]:
    raw = await r.get(_pair_key(own_id, other_id))
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if data.get("ts", {}).get(own_id) != _ts(own) or data.get("ts", {}).get(other_id) != _ts(other):
            return None                       # either vehicle sent a newer fix → re-evaluate
        return orient(VehicleConflict.model_validate(data["conflict"]), own_id)
    except (ValueError, KeyError, TypeError):
        return None


async def _compute_pair(own_id: str, own: Trajectory, own_live: dict, other_id: str, other: Trajectory,
                        other_live: dict) -> Optional[VehicleConflict]:
    conflict = detector.assess_conflict(own_id, own, other_id, other)
    if conflict is None:
        return None
    cfg = thresholds.current()
    if cfg.context.enabled and conflict.risk_level != ConflictRiskLevel.NONE:
        try:
            result = await context_service.context_for_pair(conflict, own, other, own_live, other_live)
            conflict = detector.apply_context(conflict, result)
        except Exception:
            logger.exception("[COLLISION] context failed for %s/%s — physical level kept", own_id, other_id)
    return conflict


async def assess_pair(own_id: str, own: Trajectory, own_live: dict, other_id: str, other: Trajectory,
                      other_live: dict) -> Optional[VehicleConflict]:
    """One unordered pair, assessed once per pair of telemetry fixes (see module docstring)."""
    cfg = thresholds.current()
    if cfg.pair_cache_ttl_s <= 0:
        return await _compute_pair(own_id, own, own_live, other_id, other, other_live)
    r = get_redis()
    try:
        hit = await _cached_pair(r, own_id, own, other_id, other)
        if hit is not None:
            return hit
        locked = bool(await r.set(_pair_lock_key(own_id, other_id), "1", nx=True, ex=_PAIR_LOCK_S))
        if not locked:
            # The other vehicle's pipeline is computing this very pair: wait for it briefly.
            waited = 0.0
            while waited < _PAIR_WAIT_S:
                await asyncio.sleep(_PAIR_POLL_S)
                waited += _PAIR_POLL_S
                hit = await _cached_pair(r, own_id, own, other_id, other)
                if hit is not None:
                    return hit
    except Exception:
        logger.warning("[COLLISION] pair cache unavailable — computing %s/%s", own_id, other_id, exc_info=True)
        return await _compute_pair(own_id, own, own_live, other_id, other, other_live)

    conflict = await _compute_pair(own_id, own, own_live, other_id, other, other_live)
    try:
        if conflict is not None:
            a, _b = _pair_ids(own_id, other_id)
            canonical = orient(conflict, a)
            await r.set(_pair_key(own_id, other_id), json.dumps({
                "ts": {own_id: _ts(own), other_id: _ts(other)},
                "conflict": json.loads(canonical.model_dump_json()),
            }), ex=max(1, int(math.ceil(cfg.pair_cache_ttl_s))))
        if locked:
            await r.delete(_pair_lock_key(own_id, other_id))
    except Exception:
        logger.warning("[COLLISION] pair cache write failed for %s/%s", own_id, other_id, exc_info=True)
    return conflict


async def get_conflicts(vehicle_id: str, db: AsyncSession, force: bool = False) -> VehicleConflictReport:
    """Read path for API consumers — cache-first, mirroring
    trajectory_service.get_trajectory / road_state_service.get_road_state."""
    if not force:
        cached = await _load_cached(vehicle_id)
        if cached is not None:
            return cached
    return await compute_and_store(vehicle_id, db)


async def get_active_reports() -> List[VehicleConflictReport]:
    """Fleet-wide view: every currently-cached conflict report with any
    reported risk, across all live vehicles. Read-only — does not trigger
    computation; relies on the ping-triggered background task
    (app/modules/m10_collision/routes/vehicles.py) to keep these warm."""
    live = await vehicle_service.query_all_live()
    reports: List[VehicleConflictReport] = []
    for v in live:
        cached = await _load_cached(v["vehicle_id"])
        if cached is not None and cached.highest_risk != ConflictRiskLevel.NONE:
            reports.append(cached)
    reports.sort(key=lambda r: -_RISK_SEVERITY[r.highest_risk])
    return reports
