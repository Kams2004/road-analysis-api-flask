"""
Orchestrates Step 4: for a given vehicle, find nearby live vehicles (Step 1's
Redis GEO index — app.services.vehicle_service.query_nearby, no new spatial
index built here), compare its predicted trajectory (Step 3, reused via
trajectory_service's cache-first read — never recomputed here) against each
candidate's through the conflict detector, and cache the resulting report.

Mirrors road_state_service.py / trajectory_service.py's shape deliberately:
cache-first reads, no Postgres persistence — a conflict report is even more
perishable than a trajectory (it's derived from two trajectories that are
themselves already ephemeral), so there's no lasting value in storing one.
"""
import logging
import time
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services import road_state_service, trajectory_service, vehicle_service
from app.services.collision import detector
from app.services.collision.models import ConflictRiskLevel, VehicleConflict, VehicleConflictReport
from app.services.redis_client import get_redis
from app.services.trajectory.models import Trajectory

logger = logging.getLogger(__name__)

_RISK_SEVERITY = {
    ConflictRiskLevel.CRITICAL: 4,
    ConflictRiskLevel.HIGH: 3,
    ConflictRiskLevel.MODERATE: 2,
    ConflictRiskLevel.LOW: 1,
    ConflictRiskLevel.NONE: 0,
}


def _cache_key(vehicle_id: str) -> str:
    return f"collision:{vehicle_id}"


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


async def _store_cached(report: VehicleConflictReport) -> None:
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

    live = await vehicle_service.get_live(vehicle_id)
    if live is None:
        raise road_state_service.NoTelemetryError(f"vehicle {vehicle_id} has no live telemetry")

    if own_trajectory is None:
        own_trajectory = await trajectory_service.get_trajectory(vehicle_id, db)

    candidates = await vehicle_service.query_nearby(
        live["latitude"], live["longitude"], settings.COLLISION_CANDIDATE_RADIUS_M, vehicle_id,
    )

    conflicts: List[VehicleConflict] = []
    for candidate in candidates:
        other_id = candidate["vehicle_id"]
        try:
            other_trajectory = await trajectory_service.get_trajectory(other_id, db)
        except road_state_service.NoTelemetryError:
            # Vanished between being found as a candidate and being fetched
            # (dropped out of the live set) — skip it, don't fail the report.
            continue
        conflict = detector.assess_conflict(vehicle_id, own_trajectory, other_id, other_trajectory)
        if conflict is not None and conflict.risk_level != ConflictRiskLevel.NONE:
            conflicts.append(conflict)

    conflicts.sort(
        key=lambda c: (-_RISK_SEVERITY[c.risk_level], c.distance_at_closest_approach_m or float("inf"))
    )
    highest = conflicts[0].risk_level if conflicts else ConflictRiskLevel.NONE

    report = VehicleConflictReport(
        vehicle_id=vehicle_id,
        generated_at=datetime.now(timezone.utc),
        highest_risk=highest,
        conflicts=conflicts,
    )
    await _store_cached(report)

    processing_ms = (time.monotonic() - started) * 1000
    logger.info(
        "[COLLISION] vehicle=%s candidates=%d conflicts=%d highestRisk=%s processingTime=%.1fms",
        vehicle_id, len(candidates), len(conflicts), highest.value, processing_ms,
    )
    return report


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
    (app/api/routes/vehicles.py) to keep these warm."""
    live = await vehicle_service.query_all_live()
    reports: List[VehicleConflictReport] = []
    for v in live:
        cached = await _load_cached(v["vehicle_id"])
        if cached is not None and cached.highest_risk != ConflictRiskLevel.NONE:
            reports.append(cached)
    reports.sort(key=lambda r: -_RISK_SEVERITY[r.highest_risk])
    return reports
