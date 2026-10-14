from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.services import road_state_service, trajectory_service
from app.services.trajectory.models import Trajectory

router = APIRouter()


# ── Debug / future collision-engine read path (Step 3 acceptance criterion H) ──
# Cache-first, kept warm by the background task fired from POST
# /vehicles/{id}/ping (see app/api/routes/vehicles.py). Computes fresh only
# on a cache miss.

@router.get("/{vehicle_id}", response_model=Trajectory)
async def get_vehicle_trajectory(
    vehicle_id: str,
    db: AsyncSession = Depends(get_db),
):
    try:
        return await trajectory_service.get_trajectory(vehicle_id, db)
    except road_state_service.NoTelemetryError:
        raise HTTPException(404, f"Vehicle {vehicle_id} has no live telemetry")


# ── Debug: force a fresh prediction, bypassing all caching ─────────────────

@router.post("/{vehicle_id}/refresh", response_model=Trajectory)
async def refresh_vehicle_trajectory(
    vehicle_id: str,
    db: AsyncSession = Depends(get_db),
):
    try:
        return await trajectory_service.get_trajectory(vehicle_id, db, force=True)
    except road_state_service.NoTelemetryError:
        raise HTTPException(404, f"Vehicle {vehicle_id} has no live telemetry")
