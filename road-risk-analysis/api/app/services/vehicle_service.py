"""
Live vehicle telemetry — Step 1 of the Predictive Cooperative Collision
Prevention Module (see project docs): vehicles periodically push position,
speed and heading; the platform keeps a live, self-expiring picture of every
connected vehicle for later steps (broker, prediction engine) to consume.

Live state lives in Redis (GEO set + one hash per vehicle, both TTL'd) rather
than Postgres — it changes every few seconds and a vehicle that stops pinging
should simply disappear, with no watchdog job required. Postgres only keeps
the durable vehicle registry (id, type, last_seen).
"""
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.vehicle import Vehicle, VehicleType
from app.services.redis_client import get_redis

GEO_KEY = "vehicles:live:geo"


def _hash_key(vehicle_id: str) -> str:
    return f"vehicle:live:{vehicle_id}"


def _throttle_key(vehicle_id: str) -> str:
    return f"vehicle:lastseen_throttle:{vehicle_id}"


async def register_vehicle(
    db: AsyncSession,
    vehicle_id: Optional[str],
    vehicle_type: VehicleType,
) -> Vehicle:
    vehicle = await db.get(Vehicle, vehicle_id) if vehicle_id else None
    if vehicle is None:
        vehicle = Vehicle(id=vehicle_id, vehicle_type=vehicle_type) if vehicle_id else Vehicle(vehicle_type=vehicle_type)
        db.add(vehicle)
    else:
        vehicle.vehicle_type = vehicle_type
    await db.commit()
    await db.refresh(vehicle)
    return vehicle


async def record_ping(
    db: AsyncSession,
    vehicle_id: str,
    vehicle_type: VehicleType,
    latitude: float,
    longitude: float,
    speed_mps: Optional[float],
    heading: Optional[float],
    ts: Optional[datetime],
    accuracy_m: Optional[float] = None,
    acceleration_mps2: Optional[float] = None,
) -> None:
    ts = ts or datetime.now(timezone.utc)
    r = get_redis()

    await r.geoadd(GEO_KEY, [longitude, latitude, vehicle_id])
    await r.hset(
        _hash_key(vehicle_id),
        mapping={
            "vehicle_type": vehicle_type.value,
            "latitude": latitude,
            "longitude": longitude,
            "speed_mps": speed_mps if speed_mps is not None else "",
            "heading": heading if heading is not None else "",
            "accuracy_m": accuracy_m if accuracy_m is not None else "",
            "acceleration_mps2": acceleration_mps2 if acceleration_mps2 is not None else "",
            "ts": ts.isoformat(),
        },
    )
    await r.expire(_hash_key(vehicle_id), settings.VEHICLE_LIVE_TTL_S)

    # Throttle Postgres writes — last_seen doesn't need per-ping fidelity.
    throttled = await r.set(_throttle_key(vehicle_id), "1", ex=settings.VEHICLE_LAST_SEEN_THROTTLE_S, nx=True)
    if throttled:
        vehicle = await db.get(Vehicle, vehicle_id)
        if vehicle is not None:
            vehicle.last_seen = ts.replace(tzinfo=None)
            await db.commit()


def _parse_hash(vehicle_id: str, h: dict) -> Optional[dict]:
    if not h:
        return None
    return {
        "vehicle_id": vehicle_id,
        "vehicle_type": h["vehicle_type"],
        "latitude": float(h["latitude"]),
        "longitude": float(h["longitude"]),
        "speed_mps": float(h["speed_mps"]) if h.get("speed_mps") else None,
        "heading": float(h["heading"]) if h.get("heading") else None,
        "accuracy_m": float(h["accuracy_m"]) if h.get("accuracy_m") else None,
        "acceleration_mps2": float(h["acceleration_mps2"]) if h.get("acceleration_mps2") else None,
        "ts": h["ts"],
    }


async def get_live(vehicle_id: str) -> Optional[dict]:
    """Latest live telemetry for one vehicle, or None if it isn't currently live."""
    r = get_redis()
    h = await r.hgetall(_hash_key(vehicle_id))
    return _parse_hash(vehicle_id, h)


async def query_all_live() -> List[dict]:
    """All currently-live vehicles, regardless of position. Used by the demo
    simulator to discover a real device's position without a search anchor,
    and generally useful for ops/debugging a live deployment."""
    r = get_redis()
    vehicle_ids = await r.zrange(GEO_KEY, 0, -1)

    results: List[dict] = []
    for vehicle_id in vehicle_ids:
        h = await r.hgetall(_hash_key(vehicle_id))
        if not h:
            await r.zrem(GEO_KEY, vehicle_id)
            continue
        parsed = _parse_hash(vehicle_id, h)
        if parsed:
            results.append(parsed)
    return results


async def query_nearby(
    latitude: float,
    longitude: float,
    radius_m: float,
    exclude_vehicle_id: Optional[str] = None,
) -> List[dict]:
    r = get_redis()
    hits = await r.geosearch(
        name=GEO_KEY,
        longitude=longitude,
        latitude=latitude,
        radius=radius_m,
        unit="m",
        withdist=True,
        sort="ASC",
    )

    results: List[dict] = []
    for vehicle_id, dist_m in hits:
        if exclude_vehicle_id and vehicle_id == exclude_vehicle_id:
            continue
        h = await r.hgetall(_hash_key(vehicle_id))
        if not h:
            # Hash TTL expired but the geo member lingers (GEO has no per-member
            # expiry) — drop it lazily so future scans don't keep re-checking it.
            await r.zrem(GEO_KEY, vehicle_id)
            continue
        parsed = _parse_hash(vehicle_id, h)
        if parsed:
            parsed["distance_m"] = dist_m
            results.append(parsed)
    return results
