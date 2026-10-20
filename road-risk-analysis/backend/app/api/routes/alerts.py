from typing import List

from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.services import alert_service, road_state_service
from app.services.alerts.models import ConflictAlert
from app.api.openapi import errors, legacy

router = APIRouter()

_VEHICLE_ID = Path(..., description="Vehicle id (from `POST /vehicles/register`).",
                   examples=["5d0c3b1e-8a5f-4b8e-9d2c-1f3e7a9b6c40"])
_NO_TELEMETRY = errors(legacy(404, "Vehicle 5d0c3b1e-8a5f-4b8e-9d2c-1f3e7a9b6c40 has no live telemetry"))


# ── Fleet-wide view — read-only, no recompute (static path registered
# before /{vehicle_id} or FastAPI would treat "active" as a vehicle_id) ────

@router.get("/active", response_model=List[ConflictAlert], summary="List active alerts fleet-wide")
async def list_active_alerts():
    """
    Read-only fleet view: every alert that is not `RESOLVED`, across all live vehicles, each conflict
    once (both vehicles of a pair share the alert), most severe first. Levels: `SAFE` < `MONITOR` <
    `WARNING` < `HIGH_WARNING` < `CRITICAL`.
    """
    return await alert_service.get_active_alerts_fleet_wide()


# ── Read path for one vehicle — cache-first, kept warm by the background
# task fired from POST /vehicles/{id}/ping (see app/api/routes/vehicles.py).
# Never triggers computation or notification. ──────────────────────────────

@router.get("/{vehicle_id}", response_model=List[ConflictAlert], summary="Get a vehicle's alerts")
async def get_vehicle_alerts(vehicle_id: str = _VEHICLE_ID):
    """
    **Mobile** — the vehicle's current alerts as computed by the last pipeline run triggered by its
    pings, most severe first (an unknown or idle vehicle gets `[]`). Read-only: never computes or
    notifies. Lifecycle: `DETECTED` (first tick above `SAFE`) → `ACTIVE` → `RESOLVED`; `transition` says
    what changed on the last tick (`NEW`, `ESCALATED`, `DE_ESCALATED`, `UNCHANGED`, `RESOLVED`).
    """
    return await alert_service.get_alerts(vehicle_id)


# ── Debug: force a fresh alert-engine pass for this vehicle (does trigger
# notifications for any real transition found) ──────────────────────────────

@router.post("/{vehicle_id}/refresh", response_model=List[ConflictAlert],
             summary="Run the alert engine for a vehicle", responses=_NO_TELEMETRY)
async def refresh_vehicle_alerts(
    vehicle_id: str = _VEHICLE_ID,
    db: AsyncSession = Depends(get_db),
):
    """
    **Debug** — runs an alert-engine pass for this vehicle now and returns every alert it touched (new,
    updated or resolved; `[]` = nothing changed). Unlike the GET, this **does** send notifications for
    real transitions.
    """
    try:
        return await alert_service.process_vehicle(vehicle_id, db)
    except road_state_service.NoTelemetryError:
        raise HTTPException(404, f"Vehicle {vehicle_id} has no live telemetry")
