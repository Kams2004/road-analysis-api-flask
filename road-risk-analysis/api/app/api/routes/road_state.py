from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.services import road_state_service
from app.services.osrm.models import RoadAwareVehicleState

router = APIRouter()


# ── Debug / future collision-engine read path (Step 2 acceptance criterion H) ──
# Serves the Redis-cached state kept warm by the background task fired from
# POST /vehicles/{id}/ping (see app/api/routes/vehicles.py); computes fresh
# only on a cache miss. See app/services/road_state_service.py for why.

@router.get("/{vehicle_id}", response_model=RoadAwareVehicleState)
async def get_vehicle_road_state(
    vehicle_id: str,
    db: AsyncSession = Depends(get_db),
):
    try:
        return await road_state_service.get_road_state(vehicle_id, db)
    except road_state_service.NoTelemetryError:
        raise HTTPException(404, f"Vehicle {vehicle_id} has no live telemetry")


# ── Debug: force a fresh OSRM map-match, bypassing all caching/skip guards ──

@router.post("/{vehicle_id}/refresh", response_model=RoadAwareVehicleState)
async def refresh_vehicle_road_state(
    vehicle_id: str,
    db: AsyncSession = Depends(get_db),
):
    try:
        return await road_state_service.compute_and_store(vehicle_id, db, force=True)
    except road_state_service.NoTelemetryError:
        raise HTTPException(404, f"Vehicle {vehicle_id} has no live telemetry")
