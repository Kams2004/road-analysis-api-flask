"""
One-off cluster-first migration of existing data (docs/CLUSTERS.md):

    python -m app.cli recluster-detections [--dry-run]

  1. merge duplicate clusters: open clusters (candidate / active) of the same
     type (and subtype for distinct-subtype types) whose centres are closer
     than the larger of their two resolved radii → merged into the confirmed one, else
     the oldest; the kept code is unchanged, members / timeline / votes move
     to it, a `merged` event is recorded and the duplicate is deleted;
  2. rejected detections still attached leave their cluster (except the
     history of dismissed candidates);
  3. every clusterable detection without a cluster (non-rejected, confirmed
     or approximate position) is attached by the normal rule — an
     unreviewed one joining a confirmed hazard is a re-sighting
     (auto-validated when the policy allows), else it opens / joins a
     candidate;
  4. unreviewed members of confirmed hazards (detections that were pending
     when the hazard was confirmed) are auto-validated as re-sightings.

Idempotent: a second run changes nothing. The real run commits after each
step on one cluster / detection (short transactions — safe while jobs are
processing; no DDL). --dry-run does all of it in one transaction that is
rolled back (codes are not consumed) and reports what would happen.
"""
import logging
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.spatial import _haversine_m
from app.modules.m06_hazards import hazard_service
from app.modules.m06_hazards.models.detection import Detection, LocationStatus, ReviewStatus
from app.modules.m06_hazards.models.hazard import OPEN_STATUSES, Hazard, HazardStatus
from app.modules.m06_hazards.object_position import position

logger = logging.getLogger(__name__)

DUPLICATE_PAIR_M = 30.0
Area = Optional[tuple[float, float, float, float]]     # min_lat, min_lon, max_lat, max_lon (tests)


def _in_area(q, model, area: Area):
    if area is None:
        return q
    return q.where(model.latitude.between(area[0], area[2]), model.longitude.between(area[1], area[3]))


async def snapshot(db: AsyncSession, area: Area = None, distinct_subtype_types=("traffic_sign",), ctx=None) -> dict:
    """Counts reported before / after: clusters by status, review queue size,
    duplicate pairs (non-rejected located detections of the same type —
    same subtype for distinct-subtype types — whose object positions are
    < 30 m apart in different clusters or without one) and, with `ctx`, the
    open cluster pairs within merge distance."""
    by_status = dict((await db.execute(_in_area(
        select(Hazard.status, func.count()).group_by(Hazard.status), Hazard, area))).all())
    queue = (await db.execute(_in_area(select(func.count(Detection.id)).where(
        Detection.review_status.in_(hazard_service.UNREVIEWED_STATUSES)), Detection, area))).scalar() or 0
    rows = (await db.execute(_in_area(select(
        Detection.id, Detection.type, Detection.subtype, Detection.latitude, Detection.longitude, Detection.hazard_id,
        Detection.object_latitude, Detection.object_longitude)
        .where(Detection.review_status != ReviewStatus.rejected, Detection.latitude.isnot(None),
               Detection.longitude.isnot(None),
               Detection.location_status.in_(hazard_service.CLUSTER_LOCATIONS)), Detection, area))).all()
    pairs = 0
    by_type: dict = {}
    for r in rows:
        key = (r.type, r.subtype if r.type in distinct_subtype_types else None)
        lat, lon = position(r)  # type: ignore[misc]
        by_type.setdefault(key, []).append((lat, lon, r.hazard_id))
    for group in by_type.values():
        group.sort(key=lambda p: p[0])
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                if (b[0] - a[0]) * 111_320 > DUPLICATE_PAIR_M:
                    break
                if (a[2] is None or a[2] != b[2]) and _haversine_m(a[0], a[1], b[0], b[1]) < DUPLICATE_PAIR_M:
                    pairs += 1
    out = {"clusters": {s.value: by_status.get(s.value, 0) for s in HazardStatus},
           "review_queue": queue, "duplicate_pairs": pairs}
    if ctx is not None:
        out["cluster_pairs_within_radius"] = await cluster_pairs_within_radius(db, ctx, area)
    return out


async def cluster_pairs_within_radius(db: AsyncSession, ctx, area: Area = None) -> int:
    """Open clusters of the same type (subtype rule) whose centres are within
    the larger of their two radii — what the merge rule removes (0 after a run)."""
    hazards = (await db.execute(_in_area(select(Hazard).where(Hazard.status.in_(OPEN_STATUSES)),
                                         Hazard, area))).scalars().all()
    radius = {h.id: ctx.radius_for(h.latitude, h.longitude, h.type) for h in hazards}
    n = 0
    for i, a in enumerate(hazards):
        for b in hazards[i + 1:]:
            if a.type != b.type or (a.type in ctx.distinct_subtype_types and
                                    (a.subtype or None) != (b.subtype or None)):
                continue
            if _haversine_m(a.latitude, a.longitude, b.latitude, b.longitude) <= max(radius[a.id], radius[b.id]):
                n += 1
    return n


async def merge_duplicates(db: AsyncSession, ctx, area: Area, commit, log: list) -> int:
    """Merge open clusters of the same type (subtype rule) whose centres are
    within the larger of their two radii (hazard_service.merge_pair), until
    none is left."""
    merges = 0
    radius_of = lambda h: ctx.radius_for(h.latitude, h.longitude, h.type)   # noqa: E731
    while True:
        hazards = (await db.execute(_in_area(select(Hazard).where(Hazard.status.in_(OPEN_STATUSES)),
                                             Hazard, area))).scalars().all()
        pair = hazard_service.merge_pair(hazards, radius_of, ctx.distinct_subtype_types)
        if pair is None:
            return merges
        await hazard_service.merge(db, pair[0], pair[1], log)
        merges += 1
        await commit()


async def run(db: AsyncSession, *, dry_run: bool = False, area: Area = None, verbose: bool = False,
              keep_open: bool = False) -> dict:
    """See module docstring. Returns {before, after, merges, detached,
    attached, candidates_created, auto_validated, log}. `keep_open` (dry
    run): the caller rolls the transaction back itself."""
    ctx = await hazard_service.load_context(db)
    log: list[str] = []
    before = await snapshot(db, area, ctx.distinct_subtype_types, ctx)

    async def commit():
        if dry_run:
            await db.flush()
        else:
            await db.commit()

    if dry_run:
        hazard_service._dry_numbers = [0]
    stats = {"merges": 0, "detached": 0, "attached": 0, "candidates_created": 0, "auto_validated": 0}
    try:
        stats["merges"] = await merge_duplicates(db, ctx, area, commit, log)

        # rejected detections still in a cluster (older code kept none; dismissed candidates keep theirs)
        stale = (await db.execute(_in_area(
            select(Detection).join(Hazard, Hazard.id == Detection.hazard_id).where(
                Detection.review_status == ReviewStatus.rejected, Hazard.status != HazardStatus.dismissed.value),
            Detection, area))).scalars().all()
        for det in stale:
            await hazard_service.detach(db, det)
            stats["detached"] += 1
            await commit()

        # clusterable detections without a cluster, oldest first
        loose = (await db.execute(_in_area(select(Detection).where(
            Detection.hazard_id.is_(None), Detection.review_status != ReviewStatus.rejected,
            Detection.location_status.in_(hazard_service.CLUSTER_LOCATIONS),
            Detection.latitude.isnot(None), Detection.longitude.isnot(None),
        ).order_by(Detection.captured_at.asc().nullsfirst(), Detection.created_at.asc()), Detection, area)
        )).scalars().all()
        for det in loose:
            was = det.review_status
            existing = await hazard_service.candidate_for(db, det, ctx)
            hazard = await hazard_service.attach(db, det, ctx)
            if hazard is None:
                continue
            stats["attached"] += 1
            if existing is None and hazard.status == HazardStatus.candidate.value:
                stats["candidates_created"] += 1
            if was != det.review_status and det.review_status == ReviewStatus.validated:
                stats["auto_validated"] += 1
                log.append(f"re-sighting {det.id[:8]} → validated (matches {hazard.code})")
            await commit()

        # unreviewed members of confirmed hazards
        confirmed = (await db.execute(_in_area(select(Hazard).where(
            Hazard.status == HazardStatus.active.value,
            select(Detection.id).where(Detection.hazard_id == Hazard.id,
                                       Detection.review_status.in_(hazard_service.UNREVIEWED_STATUSES)).exists()),
            Hazard, area))).scalars().all()
        for hazard in confirmed:
            n = await hazard_service.resight_unreviewed(db, hazard)
            if n:
                stats["auto_validated"] += n
                log.append(f"{n} re-sighting(s) of {hazard.code} → validated")
            await commit()

        await db.flush()
        after = await snapshot(db, area, ctx.distinct_subtype_types, ctx)
    finally:
        hazard_service._dry_numbers = None
        if dry_run and not keep_open:
            await db.rollback()
    if verbose:
        for line in log:
            print("  " + line)
    return {"dry_run": dry_run, "before": before, "after": after, **stats, "log": log}
