"""
Orchestrates the Step 2 pipeline: live vehicle telemetry (Step 1, in Redis)
→ OSRM map-matching → RoadAwareVehicleState, cached in Redis and mirrored as
a throttled summary in Postgres.

This is the "OSRM service abstraction" other parts of the app (and Step 3
later) should call — never app.services.osrm.client directly — so the OSRM
integration stays swappable in one place (see app/services/osrm/map_matching.py).

Vehicle state vs. road network data
────────────────────────────────────
Per the Step 2 design: the *computed* RoadAwareVehicleState (position along
segment, matched point, etc.) is vehicle state — continuously changing, kept
in Redis with a short TTL, mirrored to Postgres only as a throttled summary
(see app.models.vehicle_road_state). It is never treated as road network
data. OSRM itself remains the sole source of road network truth; nothing
here duplicates OSM/OSRM data into Postgres.
"""
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.vehicle_road_state import VehicleRoadState
from app.services import vehicle_service
from app.services.osrm import map_matching
from app.services.osrm.models import RoadAwareVehicleState, RoadDirection, TelemetrySnapshot
from app.services.redis_client import get_redis

logger = logging.getLogger(__name__)


class NoTelemetryError(Exception):
    """Raised when a vehicle has no live telemetry to map-match against."""


def _cache_key(vehicle_id: str) -> str:
    return f"road_state:{vehicle_id}"


def _db_throttle_key(vehicle_id: str) -> str:
    return f"road_state:db_throttle:{vehicle_id}"


async def _load_cached(vehicle_id: str) -> Optional[RoadAwareVehicleState]:
    r = get_redis()
    raw = await r.get(_cache_key(vehicle_id))
    if not raw:
        return None
    try:
        return RoadAwareVehicleState.model_validate_json(raw)
    except ValueError:
        logger.warning("Discarding unparseable cached road state for %s", vehicle_id)
        return None


async def _store_cached(state: RoadAwareVehicleState) -> None:
    r = get_redis()
    await r.set(_cache_key(state.vehicle_id), state.model_dump_json(), ex=settings.ROAD_STATE_TTL_S)


async def _persist_summary(db: AsyncSession, state: RoadAwareVehicleState) -> None:
    """Throttled — road position doesn't need per-computation fidelity in
    Postgres, only Redis (see module docstring)."""
    r = get_redis()
    throttled = await r.set(_db_throttle_key(state.vehicle_id), "1", ex=settings.ROAD_STATE_DB_THROTTLE_S, nx=True)
    if not throttled:
        return

    row = await db.get(VehicleRoadState, state.vehicle_id)
    if row is None:
        row = VehicleRoadState(vehicle_id=state.vehicle_id)
        db.add(row)

    rc = state.road_context
    row.road_segment_id = rc.road_segment_id if rc else None
    row.road_name = rc.road_name if rc else None
    row.direction = rc.direction.value if rc else RoadDirection.UNKNOWN.value
    row.matched_latitude = rc.matched_latitude if rc else None
    row.matched_longitude = rc.matched_longitude if rc else None
    row.match_quality = state.match_quality.value
    row.match_confidence = state.match_confidence
    row.matched_at = state.computed_at.replace(tzinfo=None)
    await db.commit()


def _telemetry_from_live(vehicle_id: str, live: dict) -> TelemetrySnapshot:
    ts = live["ts"]
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    return TelemetrySnapshot(
        vehicle_id=vehicle_id,
        latitude=live["latitude"],
        longitude=live["longitude"],
        speed_mps=live.get("speed_mps"),
        heading=live.get("heading"),
        accuracy_m=live.get("accuracy_m"),
        acceleration_mps2=live.get("acceleration_mps2"),
        timestamp=ts,
    )


async def compute_and_store(vehicle_id: str, db: AsyncSession, force: bool = False) -> RoadAwareVehicleState:
    """Fetch the vehicle's latest live telemetry, map-match it, cache the
    result and (throttled) persist a summary. Raises NoTelemetryError if the
    vehicle isn't currently live (see app.services.vehicle_service)."""
    live = await vehicle_service.get_live(vehicle_id)
    if live is None:
        raise NoTelemetryError(f"vehicle {vehicle_id} has no live telemetry")

    telemetry = _telemetry_from_live(vehicle_id, live)
    previous = None if force else await _load_cached(vehicle_id)

    state = await map_matching.match_vehicle_to_road(telemetry, previous)

    await _store_cached(state)
    await _persist_summary(db, state)
    return state


async def get_road_state(vehicle_id: str, db: AsyncSession, force: bool = False) -> RoadAwareVehicleState:
    """Read path for API consumers (the future collision engine, the Step 2
    debug endpoint): serve the cached state if there is one — it's kept warm
    by a background task on every ping — computing fresh only on a cache
    miss or when `force` bypasses the cache entirely."""
    if not force:
        cached = await _load_cached(vehicle_id)
        if cached is not None:
            return cached
    return await compute_and_store(vehicle_id, db, force=force)
