from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.services import collision_service, road_state_service
from app.services.collision.models import VehicleConflictReport

router = APIRouter()


# ── Fleet-wide view — read-only, no recompute (item order matters: this
# static path must be registered before /{vehicle_id} or FastAPI would treat
# "active" as a vehicle_id) ─────────────────────────────────────────────────

@router.get("/active", response_model=List[VehicleConflictReport])
async def list_active_conflicts():
    return await collision_service.get_active_reports()


# ── Debug / future consumer read path (Step 4) ─────────────────────────────
# Cache-first, kept warm by the background task fired from POST
# /vehicles/{id}/ping (see app/api/routes/vehicles.py). Computes fresh only
# on a cache miss.

@router.get("/{vehicle_id}", response_model=VehicleConflictReport)
async def get_vehicle_conflicts(
    vehicle_id: str,
    db: AsyncSession = Depends(get_db),
):
    try:
        return await collision_service.get_conflicts(vehicle_id, db)
    except road_state_service.NoTelemetryError:
        raise HTTPException(404, f"Vehicle {vehicle_id} has no live telemetry")


# ── Debug: force a fresh assessment, bypassing the cache ───────────────────

@router.post("/{vehicle_id}/refresh", response_model=VehicleConflictReport)
async def refresh_vehicle_conflicts(
    vehicle_id: str,
    db: AsyncSession = Depends(get_db),
):
    try:
        return await collision_service.compute_and_store(vehicle_id, db)
    except road_state_service.NoTelemetryError:
        raise HTTPException(404, f"Vehicle {vehicle_id} has no live telemetry")
