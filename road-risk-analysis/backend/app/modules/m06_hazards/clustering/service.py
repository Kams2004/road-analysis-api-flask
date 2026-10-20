"""
DB layer of the Cluster Configuration: snapshot (settings `clustering` +
active cluster_zones) with a short in-process cache, zone helpers and the
one-time migration of the legacy configuration.

Cache: get_snapshot() reloads at most every CACHE_TTL_S (10 s, like the
settings cache) — other processes (Celery workers, other API workers) pick
a change up within that delay; invalidate() is called by every write in
this process so the console sees its own change at once.

Migration (migrate_legacy, at API start-up; idempotent): when the settings
row `clustering` does not exist yet, it is created from the legacy
configuration — cluster_config.radius_m → both profiles' default (unless it
is absent or the old built-in 50 m, which was created on first read and is
indistinguishable from "never set": then urban 30 m / rural 60 m) — and
every legacy circular cluster area becomes a `custom` urban zone (64-vertex
circle) keeping its radius and per-type radii. The existence of that row
is the marker: the migration never runs twice, and zones deleted
afterwards are not resurrected. The legacy tables are left untouched.
"""
import logging
import time
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.m06_hazards.models.cluster_zone import ClusterZone
from app.modules.m06_hazards.clustering import zones
from app.modules.m06_hazards.clustering.resolver import (
    DEFAULT_PRIORITY, Resolution, Snapshot, ZoneRule, resolve as _resolve,
)
from app.modules.m06_hazards.clustering.settings import ClusteringSettings

logger = logging.getLogger(__name__)

CACHE_TTL_S = 10.0
_cache: dict[str, tuple[float, Snapshot]] = {}

LEGACY_DEFAULT_RADIUS_M = 50.0


def invalidate() -> None:
    _cache.clear()
    from app.core.settings_service import clear_cache
    clear_cache("clustering")


def rule_from_row(z) -> ZoneRule:
    return ZoneRule(
        id=z.id, name=z.name, kind=z.kind, environment=z.environment, city=z.city,
        shape=zones.compile_shape(z.geometry, z.corridor_width_m),
        priority=z.priority if z.priority is not None else DEFAULT_PRIORITY.get(z.kind, 0),
        default_radius_m=z.default_radius_m, type_radius=dict(z.type_radius or {}),
        signalement_radius=dict(z.signalement_radius or {}),
    )


async def load_settings(db: AsyncSession) -> ClusteringSettings:
    from app.core.settings_service import get_section
    return await get_section(db, "clustering")  # type: ignore[return-value]


async def load_snapshot(db: AsyncSession) -> Snapshot:
    rows = (await db.execute(select(ClusterZone).where(ClusterZone.is_active.is_(True)))).scalars().all()
    rules = []
    for z in rows:
        try:
            rules.append(rule_from_row(z))
        except Exception:                      # a broken geometry must not stop clustering
            logger.exception("Cluster zone %s has an invalid geometry — ignored", z.id)
    return Snapshot(settings=await load_settings(db), zones=rules)


async def get_snapshot(db: AsyncSession) -> Snapshot:
    hit = _cache.get("snap")
    now = time.monotonic()
    if hit is not None and now - hit[0] < CACHE_TTL_S:
        return hit[1]
    snap = await load_snapshot(db)
    _cache["snap"] = (now, snap)
    return snap


async def resolve(db: AsyncSession, lat: Optional[float], lon: Optional[float],
                  category: str = "detection", kind: Optional[str] = None) -> Resolution:
    return _resolve(await get_snapshot(db), lat, lon, category, kind)  # type: ignore[arg-type]


# ── zone rows ─────────────────────────────────────────────────────────────────

def geometry_fields(geometry: dict, corridor_width_m: Optional[float] = None) -> dict:
    """Derived columns of a zone geometry: bbox (padded for corridors), area, label centre."""
    shape = zones.compile_shape(geometry, corridor_width_m)
    b = shape.bbox
    if b is None:
        raise ValueError("geometry is empty")
    lines = zones.lines_of(geometry)
    if lines:
        mid = lines[0][len(lines[0]) // 2]
        center = (mid[1], mid[0])
    else:
        polys = zones.polygons_of(geometry)
        outer = max((p[0] for p in polys), key=zones.ring_area_m2)
        center = (sum(p[1] for p in outer) / len(outer), sum(p[0] for p in outer) / len(outer))
    return {
        "bbox_min_lat": b[0], "bbox_min_lon": b[1], "bbox_max_lat": b[2], "bbox_max_lon": b[3],
        "area_km2": round(shape.area_m2 / 1e6, 4), "center_lat": center[0], "center_lon": center[1],
    }


def corridor_outline(z) -> Optional[dict]:
    """Display polygon of a road-section corridor (None for polygons)."""
    lines = zones.lines_of(z.geometry or {})
    if not lines or not z.corridor_width_m:
        return None
    rings = [zones.corridor_polygon(zones.simplify({"type": "LineString", "coordinates": ln}, 0.0002, 1500)
                                    ["coordinates"], z.corridor_width_m) for ln in lines]
    rings = [r for r in rings if r]
    if not rings:
        return None
    return ({"type": "Polygon", "coordinates": [rings[0]]} if len(rings) == 1
            else {"type": "MultiPolygon", "coordinates": [[r] for r in rings]})


# ── legacy (cluster_config / cluster_areas) ───────────────────────────────────

def settings_from_legacy(radius_m: Optional[float]) -> ClusteringSettings:
    s = ClusteringSettings()
    if radius_m and abs(radius_m - LEGACY_DEFAULT_RADIUS_M) > 1e-9:
        s.urban.default_radius_m = float(radius_m)
        s.rural.default_radius_m = float(radius_m)
    return s


def zone_from_legacy_area(a) -> ClusterZone:
    """A legacy circular ClusterArea → a custom urban zone (64-vertex circle)."""
    geometry = zones.circle_polygon(a.center_lat, a.center_lon, a.area_radius_km * 1000)
    z = ClusterZone(
        name=a.name, kind="custom", environment="urban", geometry=geometry, corridor_width_m=None,
        source="legacy_area", source_ref=str(a.id),
        source_meta={"center_lat": a.center_lat, "center_lon": a.center_lon, "area_radius_km": a.area_radius_km},
        default_radius_m=a.cluster_radius_m, type_radius=dict(a.type_radius or {}) or None,
        signalement_radius=None, priority=DEFAULT_PRIORITY["custom"], is_active=bool(a.is_active),
        country=a.country, city=a.city, created_by="migration",
        circle_radius_km=a.area_radius_km,
        created_at=a.created_at or datetime.utcnow(), updated_at=datetime.utcnow(),
    )
    for k, v in geometry_fields(geometry).items():
        setattr(z, k, v)
    z.center_lat, z.center_lon = a.center_lat, a.center_lon
    return z


async def migrate_legacy(db: AsyncSession) -> bool:
    """Create the `clustering` settings row (+ zones from legacy areas) once.
    Returns True when it migrated something."""
    from app.modules.m06_hazards.models.cluster_area import ClusterArea
    from app.modules.m06_hazards.models.cluster_config import ClusterConfig
    from app.modules.m13_admin.models.system_setting import SystemSetting
    if await db.get(SystemSetting, "clustering") is not None:
        return False
    legacy = await db.get(ClusterConfig, 1)
    s = settings_from_legacy(legacy.radius_m if legacy else None)
    areas = (await db.execute(select(ClusterArea))).scalars().all()
    for a in areas:
        db.add(zone_from_legacy_area(a))
    db.add(SystemSetting(section="clustering", value=s.model_dump(), updated_at=datetime.utcnow(),
                         updated_by="migration"))
    await db.commit()
    invalidate()
    logger.info("Cluster configuration migrated: urban %.0f m / rural %.0f m, %d legacy area(s) → zones",
                s.urban.default_radius_m, s.rural.default_radius_m, len(areas))
    return True
