"""
Re-sightings from processed videos.

When a job is done, every detection it produced that is not rejected —
including those still pending review — with a known position (confirmed or
approximate) is matched to the nearest ACTIVE hazard of the same type within
the clustering radius (same subtype for distinct-subtype types, exactly like
hazard_service.pick_hazard). Each match is a sighting
(service.apply_sighting): last_seen_at = its recording time when newer,
absences recounted, expiry extended, one `seen` event per detection.

Accepted detections additionally JOIN the hazard through hazard_service
(attach), which records the same sighting.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.detection import Detection, LocationStatus, ReviewStatus
from app.models.hazard import HazardStatus
from app.services.hazards.lifecycle import events
from app.services.hazards.lifecycle.settings import HazardLifecycleSettings

SIGHTING_LOCATIONS = (LocationStatus.confirmed.value, LocationStatus.approximate.value)


def is_sighting_candidate(det) -> bool:
    rs = det.review_status.value if isinstance(det.review_status, ReviewStatus) else det.review_status
    return (rs != ReviewStatus.rejected.value and det.latitude is not None and det.longitude is not None
            and det.location_status in SIGHTING_LOCATIONS)


async def record_job_sightings(db: AsyncSession, job_id: str, cfg: HazardLifecycleSettings, ctx=None) -> int:
    """Sightings of active hazards by the detections of a job. Idempotent.
    Returns the number of new sightings. The caller commits."""
    from app.services import hazard_service
    from app.services.hazards.lifecycle import service
    ctx = ctx or await hazard_service.load_context(db)
    dets = (await db.execute(select(Detection).where(Detection.job_id == job_id))).scalars().all()
    n = 0
    for det in dets:
        if not is_sighting_candidate(det):
            continue
        hazard = await hazard_service.candidate_for(db, det, ctx, HazardStatus.active.value)
        if hazard is None:
            continue
        if await events.exists(db, hazard.id, ("seen",), detection_id=det.id):
            continue
        await service.apply_sighting(db, hazard, det, cfg, source="video")
        n += 1
    return n
