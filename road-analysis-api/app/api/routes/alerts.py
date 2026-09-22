from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.services import alert_service, road_state_service
from app.services.alerts.models import ConflictAlert

router = APIRouter()


# ── Fleet-wide view — read-only, no recompute (static path registered
# before /{vehicle_id} or FastAPI would treat "active" as a vehicle_id) ────

@router.get("/active", response_model=List[ConflictAlert])
async def list_active_alerts():
    return await alert_service.get_active_alerts_fleet_wide()


# ── Read path for one vehicle — cache-first, kept warm by the background
# task fired from POST /vehicles/{id}/ping (see app/api/routes/vehicles.py).
# Never triggers computation or notification. ──────────────────────────────

@router.get("/{vehicle_id}", response_model=List[ConflictAlert])
async def get_vehicle_alerts(vehicle_id: str):
    return await alert_service.get_alerts(vehicle_id)


# ── Debug: force a fresh alert-engine pass for this vehicle (does trigger
# notifications for any real transition found) ──────────────────────────────

@router.post("/{vehicle_id}/refresh", response_model=List[ConflictAlert])
async def refresh_vehicle_alerts(
    vehicle_id: str,
    db: AsyncSession = Depends(get_db),
):
    try:
        return await alert_service.process_vehicle(vehicle_id, db)
    except road_state_service.NoTelemetryError:
        raise HTTPException(404, f"Vehicle {vehicle_id} has no live telemetry")
