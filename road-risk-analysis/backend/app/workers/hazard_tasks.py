"""
Celery tasks of the hazard lifecycle (Module 6, app.modules.m06_hazards.lifecycle).
Registered on the shared `celery_app` (imported at the bottom of
app/workers/celery_app.py), routed to the "processing" queue.

  evaluate_job_hazards   one done job: re-sightings + "not seen" video passes
                         (queued by job_service.schedule_hazard_lifecycle)
  hazard_lifecycle_tick  beat, every minute; runs at most every
                         hazard_lifecycle.expiry_check_minutes: resolves the
                         expired hazards, then evaluates recently done jobs
                         that were never evaluated (broker down at completion)
"""
import logging
from datetime import datetime, timedelta

from app.workers.celery_app import celery_app
from app.workers.job_tasks import _run

logger = logging.getLogger(__name__)

TICK_LOCK_KEY = "hazard_lifecycle:tick"
CATCH_UP_HOURS = 48          # done jobs this recent without a hazard_lifecycle event are evaluated
CATCH_UP_MAX_JOBS = 20       # per tick
CATCH_UP_DELAY_MINUTES = 10  # … finished at least this long ago


@celery_app.task(name="app.workers.celery_app.evaluate_job_hazards")
def evaluate_job_hazards(job_id: str):
    from app.modules.m06_hazards.lifecycle import video_pass
    return _run(lambda db: video_pass.evaluate_job(db, job_id))


def _due(minutes: int) -> bool:
    """One tick per `minutes` across workers (Redis SET NX EX); runs anyway
    when Redis is unreachable."""
    try:
        from app.modules.m07_ingestion import job_slots
        return bool(job_slots.client().set(TICK_LOCK_KEY, datetime.utcnow().isoformat(), nx=True,
                                           ex=max(30, minutes * 60 - 5)))
    except Exception as exc:
        logger.warning(f"[hazard-lifecycle] tick lock unavailable ({exc}) — running")
        return True


@celery_app.task(name="app.workers.celery_app.hazard_lifecycle_tick")
def hazard_lifecycle_tick():
    from app.core.settings_service import get_section
    minutes = _run(lambda db: _minutes(db, get_section))
    if not _due(minutes):
        return {"skipped": "not due"}
    resolved = _run(_expire)
    caught_up = _run(_catch_up)
    return {"expired": resolved, "jobs_evaluated": caught_up}


async def _minutes(db, get_section) -> int:
    cfg = await get_section(db, "hazard_lifecycle")
    return int(cfg.expiry_check_minutes)


async def _expire(db) -> int:
    from app.modules.m06_hazards.lifecycle import expiry
    return await expiry.sweep(db)


async def _catch_up(db) -> int:
    from sqlalchemy import select
    from app.modules.m07_ingestion.models.job import Job, JobStatus
    from app.modules.m07_ingestion.models.job_event import JobEvent
    from app.modules.m06_hazards.lifecycle import video_pass
    now = datetime.utcnow()
    since = now - timedelta(hours=CATCH_UP_HOURS)
    settled = now - timedelta(minutes=CATCH_UP_DELAY_MINUTES)   # leave fresh jobs to their own task
    done_ids = (await db.execute(
        select(Job.id).where(Job.status == JobStatus.done, Job.finished_at >= since, Job.finished_at <= settled)
        .where(~select(JobEvent.id).where(JobEvent.job_id == Job.id, JobEvent.event == "hazard_lifecycle").exists())
        .order_by(Job.finished_at.asc()).limit(CATCH_UP_MAX_JOBS)
    )).scalars().all()
    for job_id in done_ids:
        try:
            await video_pass.evaluate_job(db, job_id)
        except Exception:
            await db.rollback()
            logger.exception(f"[Job {job_id}] hazard lifecycle catch-up failed")
    return len(done_ids)
