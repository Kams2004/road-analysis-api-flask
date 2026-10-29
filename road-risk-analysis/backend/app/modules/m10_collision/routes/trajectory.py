from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.session import get_db
from app.modules.m10_collision import device_auth, road_state_service, trajectory_service
from app.modules.m10_collision.trajectory.models import Trajectory
from app.api.openapi import errors, legacy

router = APIRouter()

_VEHICLE_ID = Path(..., description="Vehicle id (from `POST /vehicles/register`).",
                   examples=["5d0c3b1e-8a5f-4b8e-9d2c-1f3e7a9b6c40"])
_NO_TELEMETRY = errors(legacy(404, "Vehicle 5d0c3b1e-8a5f-4b8e-9d2c-1f3e7a9b6c40 has no live telemetry"))


# ── Debug / future collision-engine read path (Step 3 acceptance criterion H) ──
# Cache-first, kept warm by the background task fired from POST
# /vehicles/{id}/ping (see app/modules/m10_collision/routes/vehicles.py). Computes fresh only
# on a cache miss.

@router.get("/{vehicle_id}", response_model=Trajectory, summary="Get a vehicle's predicted trajectory",
            responses=_NO_TELEMETRY, dependencies=[Depends(device_auth.vehicle_owner)])
async def get_vehicle_trajectory(
    vehicle_id: str = _VEHICLE_ID,
    db: AsyncSession = Depends(get_db),
):
    """
    Short-horizon prediction of a live vehicle's future positions along the road network (`points`,
    seconds ahead with distance along the road and modelled speed), with its status
    (`VALID`/`DEGRADED`/`STALE`/`UNAVAILABLE`) and confidence. Served from the cache kept warm by
    `POST /vehicles/{vehicle_id}/ping`; computed only on a cache miss.
    """
    try:
        return await trajectory_service.get_trajectory(vehicle_id, db)
    except road_state_service.NoTelemetryError:
        raise HTTPException(404, f"Vehicle {vehicle_id} has no live telemetry")


# ── Debug: force a fresh prediction, bypassing all caching ─────────────────

@router.post("/{vehicle_id}/refresh", response_model=Trajectory,
             summary="Recompute a vehicle's predicted trajectory", responses=_NO_TELEMETRY)
async def refresh_vehicle_trajectory(
    vehicle_id: str = _VEHICLE_ID,
    db: AsyncSession = Depends(get_db),
):
    """**Debug** — forces a fresh prediction from the current road context, bypassing the cache."""
    try:
        return await trajectory_service.get_trajectory(vehicle_id, db, force=True)
    except road_state_service.NoTelemetryError:
        raise HTTPException(404, f"Vehicle {vehicle_id} has no live telemetry")
