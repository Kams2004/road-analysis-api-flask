"""
Stale job watchdog (RRAP-07-010).
Runs at API startup and every 5 minutes from Celery beat
(app.workers.job_tasks.watchdog_stale_jobs). A 'processing' job whose worker
heartbeat (job_slots, Redis — refreshed at least every 30 s while it works) is
older than STALE_JOB_MINUTES is marked failed — or cancelled, when a cancel
was requested — so a killed worker never leaves a job processing forever,
while a long video that keeps beating is never interrupted. Without a slot
entry the start time stands in for the heartbeat.
"""
import time
import logging
from datetime import datetime, timedelta
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.modules.m07_ingestion.models.job import Job, JobStatus
from app.modules.m07_ingestion import job_service

logger = logging.getLogger(__name__)


async def mark_stale_jobs(db: AsyncSession) -> int:
    """
    Mark stale processing jobs as failed (or cancelled).
    Returns the number of jobs marked.
    """
    limit_s = settings.STALE_JOB_MINUTES * 60
    cutoff = datetime.utcnow() - timedelta(seconds=limit_s)
    candidates = (await db.execute(
        select(Job).where(
            Job.status == JobStatus.processing,
            func.coalesce(Job.started_at, Job.created_at) < cutoff,
        )
    )).scalars().all()
    stale = [j for j in candidates if heartbeat_age_s(j.id) is None or heartbeat_age_s(j.id) > limit_s]

    for job in stale:
        if job.cancel_requested:
            job_service.mark_cancelled(db, job, None, {"reason": "stale"})
        else:
            job_service.mark_failed(
                db, job, f"Marked stale by watchdog: no worker heartbeat for >{settings.STALE_JOB_MINUTES} min "
                         "(worker stopped or restarted)")
        logger.warning(f"[Watchdog] Job {job.id} ({job.filename}) marked as {job.status.value} (stale)")

    if stale:
        await db.commit()

    return len(stale)


def heartbeat_age_s(job_id: str) -> float | None:
    """Seconds since the job's last worker heartbeat; None when it holds no slot or Redis is down."""
    try:
        from app.modules.m07_ingestion import job_slots
        beat = job_slots.last_beat(job_slots.client(), job_id)
    except Exception:
        return None
    return None if beat is None else max(0.0, time.time() - beat)
