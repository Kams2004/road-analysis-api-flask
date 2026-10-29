"""
Live vehicle telemetry — Step 1 of the Predictive Cooperative Collision
Prevention Module (see project docs): vehicles periodically push position,
speed and heading; the platform keeps a live, self-expiring picture of every
connected vehicle for later steps (broker, prediction engine) to consume.

Live state lives in Redis (GEO set + one hash per vehicle, both TTL'd) rather
than Postgres — it changes every few seconds and a vehicle that stops pinging
should simply disappear, with no watchdog job required. Postgres only keeps
the durable vehicle registry (id, type, last_seen, optional plate / driver id).

Each ping also lands in a short motion history (`vehicle:motion:{id}`, the
last few samples): the live `speed_mps` / `heading` are smoothed over the
last 3 of them (trajectory/smoothing.py) so one jittery GPS fix does not
drive the prediction; the raw values stay in `raw_speed_mps` / `raw_heading`.
The plate / driver id are copied into the live hash on every ping (the ping
route already loads the registry row), so the collision pipeline never has to
read Postgres to find the behaviour score of a vehicle.
"""
import json
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.modules.m10_collision.collision import thresholds
from app.modules.m10_collision.models.vehicle import Vehicle, VehicleType
from app.modules.m10_collision.trajectory.smoothing import smooth_motion
from app.core.storage.redis_client import get_redis

GEO_KEY = "vehicles:live:geo"
MOTION_SAMPLES = 5        # kept per vehicle (out-of-order pings are re-sorted); 3 are used
SMOOTHING_SAMPLES = 3


def _hash_key(vehicle_id: str) -> str:
    return f"vehicle:live:{vehicle_id}"


def _throttle_key(vehicle_id: str) -> str:
    return f"vehicle:lastseen_throttle:{vehicle_id}"


def _motion_key(vehicle_id: str) -> str:
    return f"vehicle:motion:{vehicle_id}"


def _clean_ref(value: Optional[str]) -> Optional[str]:
    v = " ".join((value or "").split())
    return v or None


async def register_vehicle(
    db: AsyncSession,
    vehicle_id: Optional[str],
    vehicle_type: VehicleType,
    plate: Optional[str] = None,
    driver_external_id: Optional[str] = None,
) -> Vehicle:
    """`plate` / `driver_external_id`: None keeps the stored value, "" clears it."""
    vehicle = await db.get(Vehicle, vehicle_id) if vehicle_id else None
    if vehicle is None:
        vehicle = Vehicle(id=vehicle_id, vehicle_type=vehicle_type) if vehicle_id else Vehicle(vehicle_type=vehicle_type)
        db.add(vehicle)
    else:
        vehicle.vehicle_type = vehicle_type
    if plate is not None:
        vehicle.plate = _clean_ref(plate)
    if driver_external_id is not None:
        vehicle.driver_external_id = _clean_ref(driver_external_id)
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
    plate: Optional[str] = None,
    driver_external_id: Optional[str] = None,
) -> None:
    ts = ts or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    r = get_redis()

    smoothed_speed, smoothed_heading = await _smoothed(r, vehicle_id, ts, speed_mps, heading)

    def opt(v):
        return v if v is not None else ""

    await r.geoadd(GEO_KEY, [longitude, latitude, vehicle_id])
    await r.hset(
        _hash_key(vehicle_id),
        mapping={
            "vehicle_type": vehicle_type.value,
            "latitude": latitude,
            "longitude": longitude,
            "speed_mps": opt(smoothed_speed),
            "heading": opt(smoothed_heading),
            "raw_speed_mps": opt(speed_mps),
            "raw_heading": opt(heading),
            "accuracy_m": opt(accuracy_m),
            "acceleration_mps2": opt(acceleration_mps2),
            "plate": plate or "",
            "driver_external_id": driver_external_id or "",
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


async def _smoothed(r, vehicle_id: str, ts: datetime, speed_mps: Optional[float],
                    heading: Optional[float]) -> tuple:
    """Append this sample to the motion history and smooth over the last ones. Any Redis
    trouble falls back to the raw values (smoothing is an improvement, never a dependency)."""
    key = _motion_key(vehicle_id)
    try:
        await r.lpush(key, json.dumps({"ts": ts.isoformat(), "s": speed_mps, "h": heading}))
        await r.ltrim(key, 0, MOTION_SAMPLES - 1)
        await r.expire(key, max(60, settings.VEHICLE_LIVE_TTL_S))
        samples = []
        for raw in await r.lrange(key, 0, MOTION_SAMPLES - 1):
            try:
                d = json.loads(raw)
                t = datetime.fromisoformat(d["ts"])
                samples.append((t if t.tzinfo else t.replace(tzinfo=timezone.utc), d.get("s"), d.get("h")))
            except (ValueError, KeyError, TypeError):
                continue
        if not samples:
            return speed_mps, heading
        # The newest sample by timestamp is the current fix; an out-of-order (late) ping is
        # smoothed into the history but does not become "now".
        return smooth_motion(samples, SMOOTHING_SAMPLES, thresholds.current().stationary_speed_mps)
    except Exception:
        return speed_mps, heading


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
        "raw_speed_mps": float(h["raw_speed_mps"]) if h.get("raw_speed_mps") else None,
        "raw_heading": float(h["raw_heading"]) if h.get("raw_heading") else None,
        "plate": h.get("plate") or None,
        "driver_external_id": h.get("driver_external_id") or None,
        "ts": h["ts"],
        # display.py: where to draw it (held still when stopped, snapped onto its road)
        "display_latitude": float(h["display_latitude"]) if h.get("display_latitude") else None,
        "display_longitude": float(h["display_longitude"]) if h.get("display_longitude") else None,
        "display_snapped": bool(h.get("display_snapped")),
        "display_accuracy": float(h["display_accuracy"]) if h.get("display_accuracy") else None,
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
