from typing import List

from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.session import get_db
from app.modules.m10_collision import device_auth, collision_service, road_state_service
from app.modules.m10_collision.collision.models import VehicleConflictReport
from app.api.openapi import errors, legacy

router = APIRouter()

_VEHICLE_ID = Path(..., description="Vehicle id (from `POST /vehicles/register`).",
                   examples=["5d0c3b1e-8a5f-4b8e-9d2c-1f3e7a9b6c40"])
_NO_TELEMETRY = errors(legacy(404, "Vehicle 5d0c3b1e-8a5f-4b8e-9d2c-1f3e7a9b6c40 has no live telemetry"))


# ── Fleet-wide view — read-only, no recompute (item order matters: this
# static path must be registered before /{vehicle_id} or FastAPI would treat
# "active" as a vehicle_id) ─────────────────────────────────────────────────

@router.get("/active", response_model=List[VehicleConflictReport], summary="List current conflicts fleet-wide")
async def list_active_conflicts():
    """
    Read-only fleet view: the latest cached conflict report of every live vehicle that currently has
    conflicts (`highest_risk` above `NONE`), highest risk first. Never recomputes.
    """
    return await collision_service.get_active_reports()


# ── Debug / future consumer read path (Step 4) ─────────────────────────────
# Cache-first, kept warm by the background task fired from POST
# /vehicles/{id}/ping (see app/modules/m10_collision/routes/vehicles.py). Computes fresh only
# on a cache miss.

@router.get("/{vehicle_id}", response_model=VehicleConflictReport, summary="Get a vehicle's conflicts",
            responses=_NO_TELEMETRY, dependencies=[Depends(device_auth.vehicle_owner)])
async def get_vehicle_conflicts(
    vehicle_id: str = _VEHICLE_ID,
    db: AsyncSession = Depends(get_db),
):
    """
    Pairwise conflict assessment of a live vehicle against nearby vehicles: for each, the risk level
    (`NONE` < `LOW` < `MODERATE` < `HIGH` < `CRITICAL`), time and distance at closest approach,
    encounter type and road relation; worst first. Served from the cache kept warm by
    `POST /vehicles/{vehicle_id}/ping`; computed only on a cache miss.
    """
    try:
        return await collision_service.get_conflicts(vehicle_id, db)
    except road_state_service.NoTelemetryError:
        raise HTTPException(404, f"Vehicle {vehicle_id} has no live telemetry")


# ── Debug: force a fresh assessment, bypassing the cache ───────────────────

@router.post("/{vehicle_id}/refresh", response_model=VehicleConflictReport,
             summary="Recompute a vehicle's conflicts", responses=_NO_TELEMETRY)
async def refresh_vehicle_conflicts(
    vehicle_id: str = _VEHICLE_ID,
    db: AsyncSession = Depends(get_db),
):
    """**Debug** — forces a fresh conflict assessment for this vehicle, bypassing the cache."""
    try:
        return await collision_service.compute_and_store(vehicle_id, db)
    except road_state_service.NoTelemetryError:
        raise HTTPException(404, f"Vehicle {vehicle_id} has no live telemetry")
