"""
Stale job watchdog.
Runs once at startup. Marks any job that has been in 'processing' state
for longer than STALE_JOB_MINUTES as failed.
This handles the case where the worker process crashed mid-job.
"""
import logging
from datetime import datetime, timedelta
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.job import Job, JobStatus

logger = logging.getLogger(__name__)


async def mark_stale_jobs(db: AsyncSession) -> int:
    """
    Mark stale processing jobs as failed.
    Returns the number of jobs marked.
    """
    cutoff = datetime.utcnow() - timedelta(minutes=settings.STALE_JOB_MINUTES)
    stale = (await db.execute(
        select(Job).where(
            Job.status == JobStatus.processing,
            Job.created_at < cutoff,
        )
    )).scalars().all()

    for job in stale:
        job.status     = JobStatus.failed
        job.error      = f"Marked stale by watchdog: stuck in processing for >{settings.STALE_JOB_MINUTES}m"
        job.finished_at = datetime.utcnow()
        logger.warning(f"[Watchdog] Job {job.id} ({job.filename}) marked as failed (stale)")

    if stale:
        await db.commit()

    return len(stale)
