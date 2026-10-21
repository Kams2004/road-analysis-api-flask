"""
Re-sightings from processed videos.

Cluster-first (docs/CLUSTERS.md): every detection that is not rejected —
including those still pending review — with a known position (confirmed or
approximate) joins its cluster when it is created (hazard_service.attach),
which records the sighting (service.apply_sighting: last_seen_at = its
recording time when newer, absences recounted, expiry extended, one `seen`
event per detection). When a job is done, this module checks the job again:
detections without a cluster are attached, attached ones without a `seen`
event get it.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.m06_hazards.models.detection import Detection, LocationStatus, ReviewStatus
from app.modules.m06_hazards.lifecycle import events
from app.modules.m06_hazards.lifecycle.settings import HazardLifecycleSettings

SIGHTING_LOCATIONS = (LocationStatus.confirmed.value, LocationStatus.approximate.value)


def is_sighting_candidate(det) -> bool:
    rs = det.review_status.value if isinstance(det.review_status, ReviewStatus) else det.review_status
    return (rs != ReviewStatus.rejected.value and det.latitude is not None and det.longitude is not None
            and det.location_status in SIGHTING_LOCATIONS)


async def record_job_sightings(db: AsyncSession, job_id: str, cfg: HazardLifecycleSettings, ctx=None) -> int:
    """Sightings of open clusters by the detections of a job. Idempotent.
    Cluster-first: detections are normally attached at creation; any
    clusterable detection still without a cluster (created by an older
    processor, or before its position was known) is attached here. Returns
    the number of new sightings. The caller commits."""
    from app.modules.m06_hazards import hazard_service
    from app.modules.m06_hazards.models.hazard import OPEN_STATUSES, Hazard
    from app.modules.m06_hazards.lifecycle import service
    ctx = ctx or await hazard_service.load_context(db)
    dets = (await db.execute(select(Detection).where(Detection.job_id == job_id)
                             .order_by(Detection.frame_number))).scalars().all()
    n = 0
    for det in dets:
        if not is_sighting_candidate(det):
            continue
        hazard = await db.get(Hazard, det.hazard_id) if det.hazard_id else None
        if hazard is None:
            hazard = await hazard_service.attach(db, det, ctx)
            if hazard is not None:
                n += 1
            continue
        if hazard.status not in OPEN_STATUSES:
            continue
        if await events.exists(db, hazard.id, ("seen",), detection_id=det.id):
            continue
        await service.apply_sighting(db, hazard, det, cfg, source="video")
        n += 1
    return n
