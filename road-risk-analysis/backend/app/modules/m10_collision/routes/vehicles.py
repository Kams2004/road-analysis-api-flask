import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, api_error, get_principal
from app.core.db.session import get_db
from app.modules.m10_collision.models.vehicle import Vehicle
from app.modules.m10_collision.schemas import (
    PingAck, VehicleRegisterIn, VehicleOut, VehiclePingIn, VehicleNearbyIn, VehicleNearbyOut, VehicleLiveOut,
)
from app.modules.m10_collision import (
    alert_service, collision_service, device_auth, display, road_state_service, trajectory_service,
    vehicle_service,
)
from app.modules.m10_collision.alerts.models import AlertLifecycleStatus
from app.modules.m12_integration.mobile_config import mobile_radius
from app.api.openapi import errors, legacy

router = APIRouter()
logger = logging.getLogger(__name__)


# ── Mobile: register (or re-register) a vehicle identity ──────────────────────

@router.post("/register", response_model=VehicleOut, summary="Register a vehicle for live telemetry",
             responses=errors(403))
async def register_vehicle(
    body: VehicleRegisterIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """
    **Mobile / simulator** — creates the vehicle identity used by the ping endpoint and answers its
    `id`. Idempotent: resending a known `vehicle_id` keeps it (updating `vehicle_type`); an unknown
    `vehicle_id` is created with that id, so a device survives a backend reset. Anonymous: no plate
    or driver is stored (`plate` / `driver_external_id` sent by older app versions are ignored).

    A new vehicle (or one registered before device secrets) gets a `device_secret`, answered only
    this once: send it as `X-Device-Secret` from then on. With an API key, re-registering a vehicle
    that has a secret needs it too (403 `device_secret_invalid`) — a device cannot take over another
    device's vehicle.
    """
    existing = await db.get(Vehicle, body.vehicle_id) if body.vehicle_id else None
    if existing is not None:
        if principal.kind == "client" and not device_auth.secret_matches(existing, request.headers.get(device_auth.HEADER)):
            raise api_error(403, "device_secret_invalid",
                            "This vehicle belongs to another device (missing or wrong X-Device-Secret).")
    vehicle = await vehicle_service.register_vehicle(db, body.vehicle_id, body.vehicle_type)
    secret = None
    if not vehicle.device_secret_hash:
        secret, vehicle.device_secret_hash = device_auth.new_secret()
        await db.commit()
        await db.refresh(vehicle)
    out = VehicleOut.model_validate(vehicle)
    out.device_secret = secret
    return out


# ── Mobile: periodic telemetry ping (position, speed, heading) ────────────────

@router.post("/{vehicle_id}/ping", response_model=PingAck, summary="Send a telemetry ping",
             dependencies=[Depends(device_auth.vehicle_owner)],
             responses=errors(403, legacy(404, "Unknown vehicle_id — call /vehicles/register first")))
async def ping_vehicle(
    body: VehiclePingIn,
    background_tasks: BackgroundTasks,
    vehicle_id: str = Path(..., description="Id returned by `POST /vehicles/register`."),
    db: AsyncSession = Depends(get_db),
):
    """
    **Mobile / simulator** — periodic position fix (every few seconds while driving). Units: WGS84
    degrees, speed in **m/s**, heading in degrees clockwise from north, accuracy in metres,
    acceleration in m/s²; out-of-range values (latitude/longitude, negative speed, acceleration
    ±20 m/s²) → 422. A speed above 100 m/s (360 km/h, e.g. the app's fast demo drive) is clamped to
    100 m/s instead of being rejected. The fix becomes the vehicle's live state (expires after `VEHICLE_LIVE_TTL_S`
    seconds without pings) and `last_seen` is updated (throttled).

    The answer says where to draw the vehicle (GPS fix held still while stopped, snapped onto its road
    while driving on it — display.py), the road around it, and its current alert level.

    After the answer is sent, the collision pipeline runs in the background for this vehicle:
    road-state map-matching → trajectory prediction → conflict assessment with nearby vehicles →
    alert engine (may notify on a NEW / ESCALATED alert, pushed at once on `WS /alerts/ws`). Failures
    there never affect the ping.
    """
    vehicle = await db.get(Vehicle, vehicle_id)
    if vehicle is None:
        raise HTTPException(404, "Unknown vehicle_id — call /vehicles/register first")

    previous = await vehicle_service.get_live(vehicle_id)
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

    shown = await display.update(vehicle_id, await vehicle_service.get_live(vehicle_id), previous)
    alerts = [a for a in await alert_service.get_alerts(vehicle_id) if a.status != AlertLifecycleStatus.RESOLVED]
    top = max(alerts, key=lambda a: _LEVEL_RANK.get(a.level.value, 0), default=None)
    return PingAck(
        display_latitude=shown["latitude"], display_longitude=shown["longitude"],
        snapped=shown["snapped"], stationary=shown["stationary"],
        road=display.round_road(shown["road"]), road_name=shown["road_name"],
        alert_level=top.level.value if top and top.level.value != "SAFE" else None, alerts=len(alerts),
    )


_LEVEL_RANK = {"SAFE": 0, "MONITOR": 1, "WARNING": 2, "HIGH_WARNING": 3, "CRITICAL": 4}


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

@router.post("/nearby", response_model=VehicleNearbyOut, summary="List live vehicles around a position")
async def nearby_vehicles(body: VehicleNearbyIn, db: AsyncSession = Depends(get_db)):
    """
    **Mobile** — live vehicles within `radius_m` metres (default and maximum:
    `mobile.visibility_radius_m`), nearest first, each with `distance_m`. Read-only despite the POST.
    """
    radius = await mobile_radius(db, body.radius_m)   # default & cap: mobile.visibility_radius_m
    items = await vehicle_service.query_nearby(
        body.latitude, body.longitude, radius, body.exclude_vehicle_id,
    )
    return VehicleNearbyOut(
        total=len(items),
        items=[VehicleLiveOut(**item) for item in items],
    )


# ── Ops / demo: every currently-live vehicle, no position filter ──────────────

@router.get("/live", response_model=VehicleNearbyOut, summary="List every live vehicle")
async def all_live_vehicles():
    """
    **Ops / demo** — every vehicle currently live (pinged within `VEHICLE_LIVE_TTL_S` seconds),
    without position filter; `distance_m` is null.
    """
    items = await vehicle_service.query_all_live()
    return VehicleNearbyOut(
        total=len(items),
        items=[VehicleLiveOut(**item) for item in items],
    )
