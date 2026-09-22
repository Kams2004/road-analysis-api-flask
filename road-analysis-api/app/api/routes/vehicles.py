import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.vehicle import Vehicle
from app.schemas.schemas import (
    VehicleRegisterIn, VehicleOut,
    VehiclePingIn, VehicleNearbyIn, VehicleNearbyOut, VehicleLiveOut,
)
from app.services import alert_service, collision_service, road_state_service, trajectory_service, vehicle_service

router = APIRouter()
logger = logging.getLogger(__name__)


# ── Mobile: register (or re-register) a vehicle identity ──────────────────────

@router.post("/register", response_model=VehicleOut)
async def register_vehicle(
    body: VehicleRegisterIn,
    db: AsyncSession = Depends(get_db),
):
    return await vehicle_service.register_vehicle(db, body.vehicle_id, body.vehicle_type)


# ── Mobile: periodic telemetry ping (position, speed, heading) ────────────────

@router.post("/{vehicle_id}/ping", status_code=204)
async def ping_vehicle(
    vehicle_id: str,
    body: VehiclePingIn,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    vehicle = await db.get(Vehicle, vehicle_id)
    if vehicle is None:
        raise HTTPException(404, "Unknown vehicle_id — call /vehicles/register first")

    await vehicle_service.record_ping(
        db, vehicle_id, vehicle.vehicle_type,
        body.latitude, body.longitude, body.speed_mps, body.heading, body.ts,
        body.accuracy_m, body.acceleration_mps2,
    )

    # Non-blocking: keep the road-aware state (Step 2), predicted trajectory
    # (Step 3), conflict assessment (Step 4), and alert lifecycle (Step 5)
    # warm without slowing down telemetry ingestion (Step 1's contract is
    # unchanged by this).
    background_tasks.add_task(_update_pipeline, vehicle_id, db)


async def _update_pipeline(vehicle_id: str, db: AsyncSession) -> None:
    try:
        road_state = await road_state_service.compute_and_store(vehicle_id, db)
    except road_state_service.NoTelemetryError:
        return
    except Exception:
        logger.exception("Background road-state update failed for vehicle %s", vehicle_id)
        return

    try:
        trajectory = await trajectory_service.compute_and_store(vehicle_id, road_state)
    except Exception:
        logger.exception("Background trajectory update failed for vehicle %s", vehicle_id)
        return

    try:
        await collision_service.compute_and_store(vehicle_id, db, own_trajectory=trajectory)
    except road_state_service.NoTelemetryError:
        return
    except Exception:
        logger.exception("Background collision update failed for vehicle %s", vehicle_id)
        return

    try:
        await alert_service.process_vehicle(vehicle_id, db)
    except road_state_service.NoTelemetryError:
        pass
    except Exception:
        logger.exception("Background alert-engine update failed for vehicle %s", vehicle_id)


# ── Mobile: live vehicles currently within radius_m of a position ─────────────

@router.post("/nearby", response_model=VehicleNearbyOut)
async def nearby_vehicles(body: VehicleNearbyIn):
    items = await vehicle_service.query_nearby(
        body.latitude, body.longitude, body.radius_m, body.exclude_vehicle_id,
    )
    return VehicleNearbyOut(
        total=len(items),
        items=[VehicleLiveOut(**item) for item in items],
    )


# ── Ops / demo: every currently-live vehicle, no position filter ──────────────

@router.get("/live", response_model=VehicleNearbyOut)
async def all_live_vehicles():
    items = await vehicle_service.query_all_live()
    return VehicleNearbyOut(
        total=len(items),
        items=[VehicleLiveOut(**item) for item in items],
    )
