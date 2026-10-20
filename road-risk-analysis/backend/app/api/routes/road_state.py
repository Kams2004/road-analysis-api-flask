from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.services import road_state_service
from app.services.osrm.models import RoadAwareVehicleState
from app.api.openapi import errors, legacy

router = APIRouter()

_VEHICLE_ID = Path(..., description="Vehicle id (from `POST /vehicles/register`).",
                   examples=["5d0c3b1e-8a5f-4b8e-9d2c-1f3e7a9b6c40"])
_NO_TELEMETRY = errors(legacy(404, "Vehicle 5d0c3b1e-8a5f-4b8e-9d2c-1f3e7a9b6c40 has no live telemetry"))


# ── Debug / future collision-engine read path (Step 2 acceptance criterion H) ──
# Serves the Redis-cached state kept warm by the background task fired from
# POST /vehicles/{id}/ping (see app/api/routes/vehicles.py); computes fresh
# only on a cache miss. See app/services/road_state_service.py for why.

@router.get("/{vehicle_id}", response_model=RoadAwareVehicleState, summary="Get a vehicle's road context",
            responses=_NO_TELEMETRY)
async def get_vehicle_road_state(
    vehicle_id: str = _VEHICLE_ID,
    db: AsyncSession = Depends(get_db),
):
    """
    Map-matched road context of a live vehicle: its latest telemetry snapshot, the OSRM-matched road
    segment (name, direction, bearing, distances along it, upcoming segments) and the match quality.
    Served from the cache kept warm by `POST /vehicles/{vehicle_id}/ping`; computed only on a cache miss.
    """
    try:
        return await road_state_service.get_road_state(vehicle_id, db)
    except road_state_service.NoTelemetryError:
        raise HTTPException(404, f"Vehicle {vehicle_id} has no live telemetry")


# ── Debug: force a fresh OSRM map-match, bypassing all caching/skip guards ──

@router.post("/{vehicle_id}/refresh", response_model=RoadAwareVehicleState,
             summary="Recompute a vehicle's road context", responses=_NO_TELEMETRY)
async def refresh_vehicle_road_state(
    vehicle_id: str = _VEHICLE_ID,
    db: AsyncSession = Depends(get_db),
):
    """
    **Debug** — forces a fresh OSRM map-match of the vehicle's latest telemetry, bypassing every cache
    and skip guard, stores it and returns it.
    """
    try:
        return await road_state_service.compute_and_store(vehicle_id, db, force=True)
    except road_state_service.NoTelemetryError:
        raise HTTPException(404, f"Vehicle {vehicle_id} has no live telemetry")
