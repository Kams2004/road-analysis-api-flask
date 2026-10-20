"""
Stale job watchdog (RRAP-07-010).
Runs at API startup and every 5 minutes from Celery beat
(app.workers.job_tasks.watchdog_stale_jobs). Marks any job that has been in
'processing' for longer than STALE_JOB_MINUTES (since it started) as failed
— or cancelled, when a cancel was requested — so a crashed worker never
leaves a job processing forever.
"""
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
    cutoff = datetime.utcnow() - timedelta(minutes=settings.STALE_JOB_MINUTES)
    stale = (await db.execute(
        select(Job).where(
            Job.status == JobStatus.processing,
            func.coalesce(Job.started_at, Job.created_at) < cutoff,
        )
    )).scalars().all()

    for job in stale:
        if job.cancel_requested:
            job_service.mark_cancelled(db, job, None, {"reason": "stale"})
        else:
            job_service.mark_failed(
                db, job, f"Marked stale by watchdog: stuck in processing for >{settings.STALE_JOB_MINUTES}m")
        logger.warning(f"[Watchdog] Job {job.id} ({job.filename}) marked as {job.status.value} (stale)")

    if stale:
        await db.commit()

    return len(stale)
