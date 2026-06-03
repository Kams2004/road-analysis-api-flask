import asyncio
import logging
from celery import Celery
from app.core.config import settings

logger = logging.getLogger(__name__)

celery_app = Celery(
    "road_analysis",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    task_acks_late=True,          # ack only after task completes — safe retries
    worker_prefetch_multiplier=1, # one task at a time per worker (video jobs are heavy)
    task_routes={"app.workers.celery_app.process_video": {"queue": "processing"}},
)


@celery_app.task(
    bind=True,
    name="app.workers.celery_app.process_video",
    max_retries=3,
    default_retry_delay=30,
)
def process_video(self, job_id: str, tmp_path: str, enabled: list):
    """
    Celery task — runs the processing loop for a single job.
    Retries up to 3 times on unexpected failure (30s delay between retries).
    """
    try:
        # Always create a fresh event loop — asyncpg connections are loop-bound
        # and cannot be reused across tasks in the same worker process.
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_run(job_id, tmp_path, enabled))
        finally:
            loop.close()
    except Exception as exc:
        logger.error(f"[Job {job_id}] Task failed: {exc}", exc_info=True)
        raise self.retry(exc=exc)


async def _run(job_id: str, tmp_path: str, enabled: list):
    from app.core.config import settings
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.workers.processor import process_source
    from app.models.job import Job

    # Fresh engine per task — never reuse the module-level engine across event loops
    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with SessionLocal() as db:
            job = await db.get(Job, job_id)
            await process_source(job, tmp_path, db, enabled)
    finally:
        await engine.dispose()
