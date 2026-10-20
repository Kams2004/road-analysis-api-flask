"""
Hazards = persisted clusters of accepted detections (RRAP-06-001…004,
06-009…011, 06-019).

A detection joins a hazard when it enters `validated`/`corrected` with a
confirmed location:
  1. radius = the global Cluster Configuration resolved at the point for the
     detection type (app.modules.m06_hazards.clustering: zone → urban/rural profile;
     the zone / environment used is stored on a new hazard);
  2. candidate = nearest hazard of the same type within that radius — and
     the same subtype for types listed in validation.distinct_subtype_types
     (two different signs a few metres apart stay separate hazards);
  3. attach and recompute the aggregates, or open a new hazard.
Leaving the accepted states (reopen) detaches it; an empty hazard is deleted.

What happens to a hazard afterwards — re-sightings, "not seen" video passes,
driver votes, expiry, recurring spots — is the hazard lifecycle,
app.modules.m06_hazards.lifecycle (hooked into attach below).

The selection rules are pure functions (area_radius, pick_hazard, aggregate)
so they are unit-tested without a database.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime
from math import cos, radians
from typing import Iterable, Optional, Sequence

from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.m06_hazards.models.detection import ACCEPTED_STATUSES, Detection, LocationStatus, ReviewStatus
from app.modules.m06_hazards.models.hazard import HAZARD_NUMBER_SEQ, Hazard, HazardStatus
from app.modules.m06_hazards.clustering import service as clustering_service
from app.modules.m06_hazards.clustering.resolver import Resolution, Snapshot, resolve as resolve_radius
from app.core.settings_service import ValidationSettings, get_section
from app.core.spatial import _haversine_m

logger = logging.getLogger(__name__)

# Statuses a hazard member can be in: accepted, or accepted then resolved.
MEMBER_STATUSES = (*ACCEPTED_STATUSES, ReviewStatus.resolved)


def hazard_code(number: int) -> str:
    return f"HZ-{number:06d}"


# ── pure rules ────────────────────────────────────────────────────────────────

def area_radius(areas: Iterable, lat: float, lon: float, det_type: str) -> Optional[float]:
    """LEGACY rule of the circular cluster areas (before the global Cluster
    Configuration; kept for reference/tests): radius of the smallest active
    area containing (lat, lon), or None. Not used by the clustering any more."""
    best = None
    for a in areas:
        if not a.is_active:
            continue
        if _haversine_m(lat, lon, a.center_lat, a.center_lon) > a.area_radius_km * 1000:
            continue
        if best is None or a.area_radius_km < best.area_radius_km:
            best = a
    if best is None:
        return None
    override = (best.type_radius or {}).get(det_type)
    return float(override) if override else float(best.cluster_radius_m)


def pick_hazard(hazards: Iterable, lat: float, lon: float, det_type: str, subtype: Optional[str],
                radius_m: float, distinct_subtype_types: Sequence[str]):
    """Nearest hazard of the same type (and subtype, when the type is listed
    in `distinct_subtype_types`) within `radius_m`; None when there is none."""
    best, best_d = None, None
    for h in hazards:
        if h.type != det_type:
            continue
        if det_type in distinct_subtype_types and (h.subtype or None) != (subtype or None):
            continue
        d = _haversine_m(lat, lon, h.latitude, h.longitude)
        if d <= radius_m and (best_d is None or d < best_d):
            best, best_d = h, d
    return best


def aggregate(members: Sequence) -> dict:
    """Centroid, counts, confidence stats and time span of a hazard's members."""
    n = len(members)
    times = [m.captured_at or m.created_at for m in members if (m.captured_at or m.created_at)]
    lat = sum(m.latitude for m in members) / n
    lon = sum(m.longitude for m in members) / n
    named = address_member(members, lat, lon)
    out = {
        "latitude":          lat,
        "longitude":         lon,
        "detection_count":   n,
        "avg_confidence":    round(sum(m.confidence for m in members) / n, 4),
        "max_confidence":    max(m.confidence for m in members),
        "first_detected_at": min(times) if times else None,
        "last_detected_at":  max(times) if times else None,
        "location_name":     named.location_name if named else None,
        "city":              named.city if named else None,
        "country":           named.country if named else None,
    }
    for key in ADDRESS_FIELDS:
        out[key] = getattr(named, key, None) if named else None
    return out


ADDRESS_FIELDS = ("road", "locality", "district", "town", "region", "display_location")


def address_member(members: Sequence, lat: float, lon: float):
    """The member whose address represents the hazard: the most central one
    with a structured address (display_location / town), else the most
    central with any place name; higher confidence breaks ties."""
    def central(cands):
        return min(cands, key=lambda m: (_haversine_m(lat, lon, m.latitude, m.longitude), -m.confidence),
                   default=None)
    structured = [m for m in members if getattr(m, "display_location", None) or getattr(m, "town", None)]
    return central(structured) or central([m for m in members if m.location_name or m.city or m.country])


# ── DB layer ──────────────────────────────────────────────────────────────────

@dataclass
class ClusterContext:
    """Everything the assignment rules need, loaded once per request/job:
    the Cluster Configuration snapshot (settings + active zones) and the
    distinct-subtype rule."""
    snapshot: Snapshot = field(default_factory=Snapshot)
    distinct_subtype_types: list[str] = field(default_factory=lambda: ["traffic_sign"])

    def resolve(self, lat: Optional[float], lon: Optional[float], det_type: Optional[str]) -> Resolution:
        return resolve_radius(self.snapshot, lat, lon, "detection", det_type)

    def radius_for(self, lat: float, lon: float, det_type: str) -> float:
        return self.resolve(lat, lon, det_type).radius_m


async def load_context(db: AsyncSession) -> ClusterContext:
    rules: ValidationSettings = await get_section(db, "validation")  # type: ignore[assignment]
    return ClusterContext(
        snapshot=await clustering_service.get_snapshot(db),
        distinct_subtype_types=list(rules.distinct_subtype_types),
    )


def _bbox(lat: float, lon: float, radius_m: float) -> tuple[float, float]:
    return radius_m / 111_320.0, radius_m / (111_320.0 * max(cos(radians(lat)), 1e-6))


async def candidate_for(db: AsyncSession, det: Detection, ctx: ClusterContext,
                        status: str = HazardStatus.active.value) -> Optional[Hazard]:
    """The hazard `det` would join (bounding-box pre-filter, exact check in Python)."""
    if det.latitude is None or det.longitude is None:
        return None
    radius = ctx.radius_for(det.latitude, det.longitude, det.type)
    dlat, dlon = _bbox(det.latitude, det.longitude, radius)
    rows = (await db.execute(select(Hazard).where(
        Hazard.status == status, Hazard.type == det.type,
        Hazard.latitude.between(det.latitude - dlat, det.latitude + dlat),
        Hazard.longitude.between(det.longitude - dlon, det.longitude + dlon),
    ))).scalars().all()
    return pick_hazard(rows, det.latitude, det.longitude, det.type, det.subtype,
                       radius, ctx.distinct_subtype_types)


async def attach(db: AsyncSession, det: Detection, ctx: Optional[ClusterContext] = None,
                 status: str = HazardStatus.active.value, *, lifecycle: bool = True) -> Optional[Hazard]:
    """Attach an accepted, located detection to its hazard (creating it if needed).

    With `lifecycle` (everything but rebuild_all), the hazard lifecycle runs
    (app.modules.m06_hazards.lifecycle.service): with no active candidate, a
    resolved hazard nearby is reopened (policy reopen_on_sighting) or keeps
    the detection as history when it was recorded before the resolution;
    then the detection counts as a sighting (last_seen_at, expiry, recurring
    flag on a new hazard, stale evidence resolved at once)."""
    if det.latitude is None or det.longitude is None or det.location_status != LocationStatus.confirmed.value:
        return None
    ctx = ctx or await load_context(db)
    hazard = await candidate_for(db, det, ctx, status)
    history = False
    if hazard is None and lifecycle and status == HazardStatus.active.value:
        from app.modules.m06_hazards.lifecycle import service as lifecycle_service
        resolved, mode = await lifecycle_service.place_new(db, det, ctx)
        if resolved is not None:
            hazard, history = resolved, mode == "history"
    created = hazard is None
    if hazard is None:
        number = (await db.execute(select(HAZARD_NUMBER_SEQ.next_value()))).scalar_one()
        res = ctx.resolve(det.latitude, det.longitude, det.type)
        hazard = Hazard(
            number=number, code=hazard_code(number), type=det.type,
            subtype=det.subtype if det.type in ctx.distinct_subtype_types else None,
            status=status, latitude=det.latitude, longitude=det.longitude,
            radius_m=res.radius_m, cluster_zone_id=res.zone_id, cluster_zone_name=res.zone_name,
            cluster_environment=res.environment,
        )
        db.add(hazard)
        await db.flush()
    det.hazard_id = hazard.id
    await recompute(db, hazard)
    if lifecycle:
        from app.modules.m06_hazards.lifecycle import service as lifecycle_service
        if history:
            await lifecycle_service.on_history_member(db, hazard, det)
        elif hazard.status == HazardStatus.active.value:
            await lifecycle_service.on_attached(db, hazard, det, created=created)
    return hazard


async def detach(db: AsyncSession, det: Detection) -> None:
    if not det.hazard_id:
        return
    hazard = await db.get(Hazard, det.hazard_id)
    det.hazard_id = None
    if hazard is not None:
        await recompute(db, hazard)


async def members_of(db: AsyncSession, hazard_id: str) -> list[Detection]:
    return list((await db.execute(
        select(Detection).where(Detection.hazard_id == hazard_id)
        .order_by(Detection.captured_at.desc().nullslast(), Detection.created_at.desc())
    )).scalars().all())


async def recompute(db: AsyncSession, hazard: Hazard) -> bool:
    """Refresh the aggregates from the members; delete the hazard when it has
    none left. Returns False when it was deleted."""
    await db.flush()
    members = [m for m in await members_of(db, hazard.id) if m.latitude is not None]
    if not members:
        await db.delete(hazard)
        await db.flush()
        return False
    for key, value in aggregate(members).items():
        setattr(hazard, key, value)
    if hazard.last_seen_at is None:
        # Lifecycle default (settings defaults); refined by the lifecycle hooks.
        from app.modules.m06_hazards.lifecycle.timeutil import recording_time
        seen = [t for t in (recording_time(m) for m in members) if t is not None]
        hazard.last_seen_at = max(seen) if seen else None
    hazard.updated_at = datetime.utcnow()
    return True


async def recompute_ids(db: AsyncSession, hazard_ids: Iterable[str]) -> None:
    for hid in {h for h in hazard_ids if h}:
        hazard = await db.get(Hazard, hid)
        if hazard is not None:
            await recompute(db, hazard)


async def rebuild_all(db: AsyncSession) -> int:
    """Recreate every hazard from the accepted/resolved detections, oldest
    first (python -m app.cli rebuild-hazards). Returns the hazard count."""
    await db.execute(update(Detection).values(hazard_id=None))
    await db.execute(delete(Hazard))
    await db.execute(text("ALTER SEQUENCE hazards_number_seq RESTART WITH 1"))
    ctx = await load_context(db)
    dets = (await db.execute(
        select(Detection).where(
            Detection.review_status.in_(MEMBER_STATUSES),
            Detection.location_status == LocationStatus.confirmed.value,
            Detection.latitude.isnot(None), Detection.longitude.isnot(None),
        ).order_by(Detection.captured_at.asc().nullsfirst(), Detection.created_at.asc())
    )).scalars().all()
    for det in dets:
        resolved = det.review_status == ReviewStatus.resolved
        hazard = await attach(db, det, ctx, HazardStatus.resolved.value if resolved else HazardStatus.active.value,
                              lifecycle=False)
        if hazard is not None and resolved:
            when = det.resolved_at or det.reviewed_at
            if when and (hazard.resolved_at is None or when > hazard.resolved_at):
                hazard.resolved_at = when
                hazard.resolved_by = det.reviewed_by
                hazard.resolution_source = "manual"
    await db.commit()
    count = len((await db.execute(select(Hazard.id))).all())
    logger.info(f"Hazards rebuilt: {count} hazard(s) from {len(dets)} detection(s)")
    return count


async def ensure_built(db: AsyncSession) -> None:
    """Startup back-fill: build hazards once when none exist yet but accepted
    detections do (first boot after the Module 6 upgrade)."""
    if (await db.execute(select(Hazard.id).limit(1))).first() is not None:
        return
    has_members = (await db.execute(select(Detection.id).where(
        Detection.review_status.in_(MEMBER_STATUSES),
        Detection.location_status == LocationStatus.confirmed.value,
    ).limit(1))).first()
    if has_members:
        await rebuild_all(db)
