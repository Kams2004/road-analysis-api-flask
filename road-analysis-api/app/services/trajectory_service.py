"""
Orchestrates Step 3: a Step 2 RoadAwareVehicleState → predict_trajectory()
→ a cached Trajectory, ready for a future collision engine (or the debug
endpoint) to read without touching OSRM, Redis, or the kinematic model
directly — see app/services/trajectory/predictor.py for the actual math.

Mirrors app/services/road_state_service.py's shape deliberately: cache-first
reads, a throttle-free write path (trajectories are cheap to recompute and
change every ping, so — unlike Step 2's Postgres summary — nothing here is
persisted to Postgres at all; there is no lasting value in storing a
15-second-old prediction once a fresher one exists).
"""
import logging
import time
from datetime import datetime
from typing import Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services import road_state_service, vehicle_service
from app.services.osrm.models import RoadAwareVehicleState
from app.services.redis_client import get_redis
from app.services.trajectory.models import Trajectory
from app.services.trajectory.predictor import predict_trajectory

logger = logging.getLogger(__name__)


def _cache_key(vehicle_id: str) -> str:
    return f"trajectory:{vehicle_id}"


def _prev_speed_key(vehicle_id: str) -> str:
    return f"trajectory:prev_speed:{vehicle_id}"


async def _load_cached(vehicle_id: str) -> Optional[Trajectory]:
    r = get_redis()
    raw = await r.get(_cache_key(vehicle_id))
    if not raw:
        return None
    try:
        return Trajectory.model_validate_json(raw)
    except ValueError:
        logger.warning("Discarding unparseable cached trajectory for %s", vehicle_id)
        return None


async def _store_cached(trajectory: Trajectory) -> None:
    r = get_redis()
    await r.set(_cache_key(trajectory.vehicle_id), trajectory.model_dump_json(), ex=settings.TRAJECTORY_TTL_S)


async def _load_prev_speed_sample(vehicle_id: str) -> Optional[Tuple[float, datetime]]:
    """The last (speed, timestamp) pair this vehicle reported — used to
    derive acceleration when the device doesn't report it directly. Kept
    here rather than in road_state_service since it's purely a Step 3
    concern (Step 2 has no notion of acceleration)."""
    r = get_redis()
    raw = await r.hgetall(_prev_speed_key(vehicle_id))
    if not raw or "speed_mps" not in raw or "ts" not in raw:
        return None
    try:
        return float(raw["speed_mps"]), datetime.fromisoformat(raw["ts"])
    except ValueError:
        return None


async def _store_speed_sample(vehicle_id: str, speed_mps: Optional[float], ts: datetime) -> None:
    if speed_mps is None:
        return
    r = get_redis()
    key = _prev_speed_key(vehicle_id)
    await r.hset(key, mapping={"speed_mps": speed_mps, "ts": ts.isoformat()})
    await r.expire(key, int(settings.TRAJECTORY_ACCEL_SAMPLE_MAX_AGE_S) + 5)


async def compute_and_store(vehicle_id: str, road_state: RoadAwareVehicleState) -> Trajectory:
    """Given an already-computed Step 2 state (the ping-triggered background
    task has one on hand already, at no extra OSRM cost), predict and cache
    this vehicle's trajectory."""
    started = time.monotonic()

    live = await vehicle_service.get_live(vehicle_id)
    vehicle_type = live.get("vehicle_type") if live else None

    prev_sample = await _load_prev_speed_sample(vehicle_id)
    trajectory = predict_trajectory(road_state, vehicle_type=vehicle_type, prev_speed_sample=prev_sample)

    await _store_cached(trajectory)
    await _store_speed_sample(vehicle_id, road_state.telemetry.speed_mps, trajectory.telemetry_timestamp)

    processing_ms = (time.monotonic() - started) * 1000
    logger.info(
        "[TRAJECTORY] vehicle=%s speed=%.1fkm/h heading=%s roadSegment=%s horizon=%ss points=%d "
        "status=%s confidence=%.2f processingTime=%.1fms",
        vehicle_id,
        (road_state.telemetry.speed_mps or 0.0) * 3.6,
        road_state.telemetry.heading,
        trajectory.road_segment_id,
        trajectory.prediction_horizon_s,
        len(trajectory.points),
        trajectory.status.value,
        trajectory.confidence,
        processing_ms,
    )
    return trajectory


async def get_trajectory(vehicle_id: str, db: AsyncSession, force: bool = False) -> Trajectory:
    """Read path for API consumers — cache-first, mirroring
    road_state_service.get_road_state. May raise
    road_state_service.NoTelemetryError if the vehicle isn't live at all."""
    if not force:
        cached = await _load_cached(vehicle_id)
        if cached is not None:
            return cached

    road_state = await road_state_service.get_road_state(vehicle_id, db, force=force)
    return await compute_and_store(vehicle_id, road_state)
