"""
Hazards = persisted clusters of detections (RRAP-06-001…004, 06-009…011,
06-019) — cluster-first (docs/CLUSTERS.md).

Every non-rejected detection with a usable position (confirmed or
approximate GPS) belongs to a cluster from its creation:
  1. radius = the global Cluster Configuration resolved at the point for the
     detection type (app.modules.m06_hazards.clustering: zone → urban/rural profile;
     the zone / environment used is stored on a new cluster);
  2. cluster = nearest OPEN cluster (candidate or confirmed hazard) of the same
     type within that radius — and the same subtype for types listed in
     validation.distinct_subtype_types (two different signs a few metres apart
     stay separate) — whatever the review status of its members;
  3. attach and recompute the aggregates, or open a new cluster: a `candidate`
     (code CL-…, never shown outside the validation screen) when the detection
     is unreviewed, a confirmed hazard (`active`, code HZ-…) when it is
     already accepted (AI auto-accept);
  4. merge: the cluster's centre moved — any other open cluster of the same
     type (and subtype rule) whose centre is within the larger of the two
     radii is merged into it (`merge_nearby`: confirmed before candidate, then
     the oldest is kept with its code; `merged` event).
Distances use the estimated OBJECT position of a detection when there is one
(object_latitude / object_longitude, app.modules.m06_hazards.object_position),
else the vehicle position (latitude / longitude, the OSD GPS fix).
A candidate becomes a hazard (`confirm`: HZ code, `confirmed` event) when
one of its detections is accepted. A detection joining a CONFIRMED hazard is
a re-sighting, auto-validated by the system (policy auto_confirm_resightings).
Rejected detections leave their cluster; a cluster left with no member is
deleted (except a dismissed candidate, kept as history). Detections without a
position join once it is fixed (location_fix).

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
from app.modules.m06_hazards.models.hazard import (
    CANDIDATE_NUMBER_SEQ, HAZARD_NUMBER_SEQ, OPEN_STATUSES, Hazard, HazardStatus,
)
from app.modules.m06_hazards.clustering import service as clustering_service
from app.modules.m06_hazards.clustering.resolver import Resolution, Snapshot, resolve as resolve_radius
from app.core.settings_service import ValidationSettings, get_section
from app.core.spatial import _haversine_m
from app.modules.m06_hazards.object_position import position

logger = logging.getLogger(__name__)

# Statuses of the members that make a hazard confirmed: accepted, or accepted then resolved.
MEMBER_STATUSES = (*ACCEPTED_STATUSES, ReviewStatus.resolved)
UNREVIEWED_STATUSES = (ReviewStatus.pending, ReviewStatus.location_pending)
CLUSTER_LOCATIONS = (LocationStatus.confirmed.value, LocationStatus.approximate.value)


def hazard_code(number: int) -> str:
    return f"HZ-{number:06d}"


def candidate_code(number: int) -> str:
    """Code of a candidate cluster (number = -n): CL-000n."""
    return f"CL-{abs(number):06d}"


def _rs(det) -> str:
    rs = det.review_status
    return rs.value if isinstance(rs, ReviewStatus) else rs


def is_clusterable(det) -> bool:
    """Joins a cluster: not rejected, with a confirmed or approximate position."""
    return (_rs(det) != ReviewStatus.rejected.value and det.latitude is not None and det.longitude is not None
            and det.location_status in CLUSTER_LOCATIONS)


def is_accepted(det) -> bool:
    return _rs(det) in {s.value for s in ACCEPTED_STATUSES}


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
    """Centroid, counts, confidence stats and time span of a hazard's members
    (any review status but rejected). The centroid uses the members with a
    confirmed position when there are any (approximate GPS only otherwise)."""
    n = len(members)
    times = [m.captured_at or m.created_at for m in members if (m.captured_at or m.created_at)]
    located = [m for m in members if getattr(m, "location_status", None) == LocationStatus.confirmed.value] \
        or list(members)
    points = [position(m) for m in located]
    lat = sum(p[0] for p in points) / len(points)
    lon = sum(p[1] for p in points) / len(points)
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
        return min(cands, key=lambda m: (_haversine_m(lat, lon, *position(m)), -m.confidence),
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
                        status=OPEN_STATUSES, *, exclude_id: Optional[str] = None) -> Optional[Hazard]:
    """The cluster `det` would join (bounding-box pre-filter, exact check in
    Python): nearest hazard of the same type (and subtype for distinct-subtype
    types) within the zone-resolved radius, among `status` (one status or a
    tuple; default the open ones — candidate or confirmed active)."""
    pos = position(det)
    if pos is None:
        return None
    lat, lon = pos
    statuses = (status,) if isinstance(status, str) else tuple(status)
    radius = ctx.radius_for(lat, lon, det.type)
    dlat, dlon = _bbox(lat, lon, radius)
    q = select(Hazard).where(
        Hazard.status.in_(statuses), Hazard.type == det.type,
        Hazard.latitude.between(lat - dlat, lat + dlat),
        Hazard.longitude.between(lon - dlon, lon + dlon),
    )
    if exclude_id:
        q = q.where(Hazard.id != exclude_id)
    rows = (await db.execute(q)).scalars().all()
    return pick_hazard(rows, lat, lon, det.type, det.subtype, radius, ctx.distinct_subtype_types)


# Dry runs (recluster-detections --dry-run) roll everything back but sequences
# are not transactional: they draw numbers from here instead, so a dry run
# never burns HZ / CL codes.
_dry_numbers: Optional[list] = None


async def _next_number(db: AsyncSession, seq) -> int:
    if _dry_numbers is not None:
        _dry_numbers[0] += 1
        return 900_000_000 + _dry_numbers[0]
    return int((await db.execute(select(seq.next_value()))).scalar_one())


async def _new_cluster(db: AsyncSession, det: Detection, ctx: ClusterContext, status: str) -> Hazard:
    """A new hazard (status active/resolved: HZ number) or candidate (CL number)."""
    if status in (HazardStatus.candidate.value, HazardStatus.dismissed.value):
        number = -await _next_number(db, CANDIDATE_NUMBER_SEQ)
        code = candidate_code(number)
    else:
        number = await _next_number(db, HAZARD_NUMBER_SEQ)
        code = hazard_code(number)
    lat, lon = position(det)  # type: ignore[misc]
    res = ctx.resolve(lat, lon, det.type)
    hazard = Hazard(
        number=number, code=code, type=det.type,
        subtype=det.subtype if det.type in ctx.distinct_subtype_types else None,
        status=status, latitude=lat, longitude=lon,
        radius_m=res.radius_m, cluster_zone_id=res.zone_id, cluster_zone_name=res.zone_name,
        cluster_environment=res.environment,
    )
    db.add(hazard)
    await db.flush()
    return hazard


async def attach(db: AsyncSession, det: Detection, ctx: Optional[ClusterContext] = None,
                 status: Optional[str] = None, *, lifecycle: bool = True, auto: bool = True) -> Optional[Hazard]:
    """Put a clusterable detection (any review status but rejected, confirmed
    or approximate position) into its cluster, creating it if needed.

    `status` forces the status of a NEW cluster (rebuild_all); by default an
    accepted detection opens a confirmed hazard, an unreviewed one a candidate.

    With `lifecycle` (everything but rebuild/recluster bookkeeping) the hazard
    lifecycle runs (app.modules.m06_hazards.lifecycle.service): with no open
    cluster, a resolved hazard nearby is reopened (accepted detection, policy
    reopen_on_sighting) or keeps the detection as history when it was recorded
    before the resolution; joining a confirmed hazard is a sighting, and with
    `auto` an unreviewed detection is a re-sighting (auto-validated); joining
    a candidate refreshes its last sighting; an accepted detection confirms a
    candidate."""
    if not is_clusterable(det):
        return None
    if det.hazard_id:
        current = await db.get(Hazard, det.hazard_id)
        if current is not None:
            return current
        det.hazard_id = None
    ctx = ctx or await load_context(db)
    accepted = is_accepted(det) or _rs(det) == ReviewStatus.resolved.value
    hazard = await candidate_for(db, det, ctx, status or OPEN_STATUSES)
    history = False
    from app.modules.m06_hazards.lifecycle import service as lifecycle_service
    if hazard is None and lifecycle and status is None and (accepted or auto):
        resolved, mode = await lifecycle_service.place_new(db, det, ctx, accepted=accepted)
        if resolved is not None:
            hazard, history = resolved, mode == "history"
    created = hazard is None
    if hazard is None:
        new_status = status or (HazardStatus.active.value if accepted else HazardStatus.candidate.value)
        hazard = await _new_cluster(db, det, ctx, new_status)
    det.hazard_id = hazard.id
    await recompute(db, hazard)
    if not lifecycle:
        return hazard
    if history:
        await lifecycle_service.on_history_member(db, hazard, det)
        return hazard
    if hazard.status == HazardStatus.candidate.value and accepted:
        await confirm(db, hazard, det)
    if hazard.status == HazardStatus.active.value:
        if not accepted and auto:
            await resight_if_allowed(db, hazard, det)
        await lifecycle_service.on_attached(db, hazard, det, created=created)
    elif hazard.status == HazardStatus.candidate.value:
        await lifecycle_service.on_candidate_member(db, hazard, det)
    if status is None:
        hazard = await merge_nearby(db, hazard, ctx, auto=auto)
    return hazard


# ── merging clusters ──────────────────────────────────────────────────────────

MERGE_SEARCH_M = 150.0     # bounding box searched for merge partners (≥ any cluster radius)


def keep_order(h) -> tuple:
    """Merge target first: confirmed (active) before candidate, then oldest."""
    return (0 if h.status == HazardStatus.active.value else 1, h.created_at, h.number if h.number > 0 else 1e12)


def merge_pair(hazards: Sequence, radius_of, distinct_subtype_types: Sequence[str]):
    """(keep, duplicate) of the first pair of open clusters of the same type
    (and subtype for distinct-subtype types) whose centres are within the
    larger of their two radii (`radius_of(h)`), in keep order; None."""
    ordered = sorted(hazards, key=keep_order)
    radii = {h.id: radius_of(h) for h in ordered}
    for i, keep in enumerate(ordered):
        for dup in ordered[i + 1:]:
            if dup.type != keep.type:
                continue
            if keep.type in distinct_subtype_types and (dup.subtype or None) != (keep.subtype or None):
                continue
            if _haversine_m(keep.latitude, keep.longitude, dup.latitude, dup.longitude) <= \
                    max(radii[keep.id], radii[dup.id]):
                return keep, dup
    return None


async def merge(db: AsyncSession, keep: Hazard, dup: Hazard, log: Optional[list] = None) -> Hazard:
    """Merge cluster `dup` into `keep`: members, timeline and votes move, a
    `merged` event is recorded on `keep` (code unchanged), `dup` is deleted
    and the aggregates recomputed."""
    from app.core import audit
    from app.modules.m06_hazards.lifecycle import events
    from app.modules.m06_hazards.models.hazard_lifecycle import HazardEvent, HazardVote
    members = (await db.execute(select(Detection).where(Detection.hazard_id == dup.id)
                                .execution_options(populate_existing=True))).scalars().all()
    for m in members:
        m.hazard_id = keep.id
    await db.execute(update(HazardEvent).where(HazardEvent.hazard_id == dup.id).values(hazard_id=keep.id))
    await db.execute(update(HazardVote).where(HazardVote.hazard_id == dup.id).values(hazard_id=keep.id))
    info = {"merged_id": dup.id, "merged_code": dup.code, "merged_status": dup.status, "detections": len(members),
            "distance_m": round(_haversine_m(keep.latitude, keep.longitude, dup.latitude, dup.longitude), 1)}
    events.record(db, keep.id, "merged", source="system", details=info)
    audit.record_system(db, "hazard.merge", object_type="hazard", object_id=keep.id,
                        details={"code": keep.code, **info})
    if dup.last_seen_at and (keep.last_seen_at is None or dup.last_seen_at > keep.last_seen_at):
        keep.last_seen_at = dup.last_seen_at
    await db.delete(dup)
    await db.flush()
    await recompute(db, keep)
    line = f"merge {info['merged_code']} ({info['merged_status']}, {len(members)} det.) → {keep.code} " \
           f"({info['distance_m']} m)"
    logger.info(f"[hazard] {line}")
    if log is not None:
        log.append(line)
    return keep


async def merge_nearby(db: AsyncSession, hazard: Optional[Hazard], ctx: ClusterContext, *, auto: bool = True,
                       log: Optional[list] = None) -> Optional[Hazard]:
    """Merge the open clusters within reach of `hazard` (its centre moved)
    into one; returns the cluster that holds its members afterwards. A
    candidate merged into a confirmed hazard: its unreviewed members are
    re-sightings (`auto`)."""
    while hazard is not None and hazard.status in OPEN_STATUSES:
        dlat, dlon = _bbox(hazard.latitude, hazard.longitude, MERGE_SEARCH_M)
        rows = (await db.execute(select(Hazard).where(
            Hazard.status.in_(OPEN_STATUSES), Hazard.type == hazard.type, Hazard.id != hazard.id,
            Hazard.latitude.between(hazard.latitude - dlat, hazard.latitude + dlat),
            Hazard.longitude.between(hazard.longitude - dlon, hazard.longitude + dlon),
        ))).scalars().all()
        radius_of = lambda h: ctx.radius_for(h.latitude, h.longitude, h.type)   # noqa: E731
        pair = None
        for other in sorted(rows, key=lambda h: _haversine_m(hazard.latitude, hazard.longitude,
                                                             h.latitude, h.longitude)):
            pair = merge_pair([hazard, other], radius_of, ctx.distinct_subtype_types)
            if pair is not None:
                break
        if pair is None:
            return hazard
        keep, dup = pair
        was_candidate = dup.status == HazardStatus.candidate.value
        hazard = await merge(db, keep, dup, log)
        if auto and was_candidate and keep.status == HazardStatus.active.value:
            await resight_unreviewed(db, keep)
    return hazard


async def resight_if_allowed(db: AsyncSession, hazard: Hazard, det: Detection) -> bool:
    """An unreviewed member of a CONFIRMED hazard is a re-sighting: decided
    `validated` by the system when the type's policy auto_confirm_resightings."""
    from app.modules.m06_hazards import detection_workflow as wf
    from app.modules.m06_hazards.lifecycle import service as lifecycle_service
    if _rs(det) not in {s.value for s in UNREVIEWED_STATUSES} or hazard.status != HazardStatus.active.value:
        return False
    cfg = await lifecycle_service.load_settings(db)
    from app.modules.m06_hazards.lifecycle.policy import resolve_policy, vehicle_class_of
    if not resolve_policy(cfg, det.type, det.subtype, vehicle_class_of(det)).auto_confirm_resightings:
        return False
    await wf.resight(db, det, hazard)
    return True


async def confirm(db: AsyncSession, hazard: Hazard, det: Optional[Detection] = None, actor=None) -> bool:
    """candidate → active: the cluster becomes a hazard (HZ code from the
    hazard sequence, `confirmed` event, recurring flag). False when it was not
    a candidate."""
    from app.core import audit
    from app.modules.m06_hazards.lifecycle import events, recurring, service as lifecycle_service
    from app.modules.m06_hazards.lifecycle.policy import policy_for_hazard
    if hazard.status != HazardStatus.candidate.value:
        return False
    previous = hazard.code
    number = await _next_number(db, HAZARD_NUMBER_SEQ)
    hazard.number, hazard.code = number, hazard_code(number)
    hazard.status = HazardStatus.active.value
    hazard.created_at = datetime.utcnow()          # a hazard from now on (recurring windows, lists)
    hazard.updated_at = hazard.created_at
    by = (getattr(actor, "name", None) or (det.reviewed_by if det is not None else None) or "system")
    events.record(db, hazard.id, "confirmed", source="review", detection_id=det.id if det is not None else None,
                  job_id=det.job_id if det is not None else None,
                  details={"by": by, "previous_code": previous, "detection_id": det.id if det is not None else None})
    audit.record_system(db, "hazard.confirm", object_type="hazard", object_id=hazard.id,
                        details={"code": hazard.code, "previous_code": previous, "by": by,
                                 "detection_id": det.id if det is not None else None})
    cfg = await lifecycle_service.load_settings(db)
    members = await members_of(db, hazard.id)
    pol = policy_for_hazard(cfg, hazard, members)
    if pol.recurring_enabled:
        await recurring.flag_new_hazard(db, hazard, cfg)
    lifecycle_service.refresh_expiry(hazard, pol)
    logger.info(f"[hazard] {previous} confirmed as {hazard.code} by {by}")
    return True


async def resight_unreviewed(db: AsyncSession, hazard: Hazard, *, skip: Iterable[str] = ()) -> int:
    """After a confirmation: the other unreviewed members are re-sightings.
    Returns how many were auto-validated."""
    from app.modules.m06_hazards.lifecycle import service as lifecycle_service
    if hazard.status != HazardStatus.active.value:
        return 0
    skip = set(skip)
    n = 0
    for m in await members_of(db, hazard.id):
        if m.id in skip:
            continue
        if await resight_if_allowed(db, hazard, m):
            await lifecycle_service.on_attached(db, hazard, m, created=False)
            n += 1
    return n


async def detach(db: AsyncSession, det: Detection) -> None:
    if not det.hazard_id:
        return
    hazard = await db.get(Hazard, det.hazard_id)
    det.hazard_id = None
    if hazard is not None:
        await recompute(db, hazard)


async def reattach(db: AsyncSession, det: Detection, ctx: Optional[ClusterContext] = None, *,
                   auto: bool = True) -> Optional[Hazard]:
    """The position or class of `det` changed: leave the current cluster and
    join the right one (re-evaluated from scratch)."""
    await detach(db, det)
    return await attach(db, det, ctx, auto=auto)


async def members_of(db: AsyncSession, hazard_id: str, *, include_rejected: bool = False) -> list[Detection]:
    q = select(Detection).where(Detection.hazard_id == hazard_id)
    if not include_rejected:
        q = q.where(Detection.review_status != ReviewStatus.rejected)
    return list((await db.execute(
        q.order_by(Detection.captured_at.desc().nullslast(), Detection.created_at.desc())
    )).scalars().all())


async def member_counts(db: AsyncSession, hazard_ids: Iterable[str]) -> dict[str, dict[str, int]]:
    """Members per review status of each hazard (rejected included), in one query."""
    from sqlalchemy import func
    ids = list({h for h in hazard_ids if h})
    out: dict[str, dict[str, int]] = {hid: {s.value: 0 for s in ReviewStatus} for hid in ids}
    if not ids:
        return out
    rows = (await db.execute(select(Detection.hazard_id, Detection.review_status, func.count())
                             .where(Detection.hazard_id.in_(ids))
                             .group_by(Detection.hazard_id, Detection.review_status))).all()
    for hid, rs, n in rows:
        out[hid][rs.value if isinstance(rs, ReviewStatus) else rs] = n
    return out


async def recompute(db: AsyncSession, hazard: Hazard) -> bool:
    """Refresh the aggregates from the non-rejected members; delete the
    cluster when it has none left (a dismissed candidate is kept as history).
    Returns False when it was deleted."""
    await db.flush()
    members = [m for m in await members_of(db, hazard.id) if m.latitude is not None]
    if not members:
        if hazard.status == HazardStatus.dismissed.value:
            return True
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
    """Recreate every cluster from the detections, oldest first (python -m
    app.cli rebuild-hazards): accepted/resolved detections first (hazards,
    HZ codes in time order), then the unreviewed ones (candidates, or members
    of the hazard they match — no automatic decision here; run
    `recluster-detections` for that). Lifecycle history is lost (events
    cascade). Returns the cluster count."""
    await db.execute(update(Detection).values(hazard_id=None))
    await db.execute(delete(Hazard))
    await db.execute(text("ALTER SEQUENCE hazards_number_seq RESTART WITH 1"))
    ctx = await load_context(db)
    order = (Detection.captured_at.asc().nullsfirst(), Detection.created_at.asc())
    located = (Detection.location_status.in_(CLUSTER_LOCATIONS),
               Detection.latitude.isnot(None), Detection.longitude.isnot(None))
    accepted = (await db.execute(select(Detection).where(
        Detection.review_status.in_(MEMBER_STATUSES), Detection.location_status == LocationStatus.confirmed.value,
        *located[1:]).order_by(*order))).scalars().all()
    for det in accepted:
        resolved = det.review_status == ReviewStatus.resolved
        hazard = await attach(db, det, ctx, HazardStatus.resolved.value if resolved else HazardStatus.active.value,
                              lifecycle=False)
        if hazard is not None and resolved:
            when = det.resolved_at or det.reviewed_at
            if when and (hazard.resolved_at is None or when > hazard.resolved_at):
                hazard.resolved_at = when
                hazard.resolved_by = det.reviewed_by
                hazard.resolution_source = "manual"
        elif hazard is not None and hazard.status == HazardStatus.resolved.value:
            hazard.status = HazardStatus.active.value          # an accepted member keeps it active
            hazard.resolved_at = hazard.resolved_by = hazard.resolution_source = None
    unreviewed = (await db.execute(select(Detection).where(
        Detection.review_status.in_(UNREVIEWED_STATUSES), *located).order_by(*order))).scalars().all()
    for det in unreviewed:
        hazard = await candidate_for(db, det, ctx)
        if hazard is None:
            await attach(db, det, ctx, HazardStatus.candidate.value, lifecycle=False)
        else:
            det.hazard_id = hazard.id
            await recompute(db, hazard)
    await db.commit()
    count = len((await db.execute(select(Hazard.id))).all())
    logger.info(f"Hazards rebuilt: {count} cluster(s) from {len(accepted) + len(unreviewed)} detection(s)")
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
