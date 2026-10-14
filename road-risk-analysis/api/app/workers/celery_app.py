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
    task_routes={
        "app.workers.celery_app.process_video": {"queue": "processing"},
        # Same queue as process_video — the existing `worker` container
        # (celery -A app.workers.celery_app worker -Q processing) already
        # consumes it, no separate worker needed for this task.
        "app.workers.celery_app.sync_ymane_events": {"queue": "processing"},
    },
    beat_schedule={
        "sync-ymane-historical-events": {
            "task": "app.workers.celery_app.sync_ymane_events",
            "schedule": settings.HISTORICAL_YMANE_SYNC_INTERVAL_S,
        },
    },
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


# ── Periodic Ymane historical-event sync ────────────────────────────────────
#
# Fired on a schedule (beat_schedule above), never on-demand from an API
# call — that on-demand pattern is what tripped Ymane's abuse detection
# during manual testing (every test = a fresh login). This task logs in at
# most once per run, reusing that token across every paginated page inside
# YmaneEventSource.fetch(), and stays off entirely unless explicitly enabled
# (see settings.HISTORICAL_YMANE_SYNC_ENABLED's docstring in app/core/config.py).

_YMANE_SYNC_FAILURE_KEY = "historical:ymane:consecutive_failures"


@celery_app.task(name="app.workers.celery_app.sync_ymane_events")
def sync_ymane_events():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_sync_ymane_events())
    finally:
        loop.close()


async def _sync_ymane_events():
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.core.config import settings
    from app.models.historical_event import HistoricalEventRecord
    from app.services import historical_service
    from app.services.historical.sources.ymane import YmaneAuthError, YmaneEventSource, YmaneRequestError
    from app.services.redis_client import get_redis

    if not settings.HISTORICAL_YMANE_SYNC_ENABLED:
        logger.info("[YMANE-SYNC] disabled (HISTORICAL_YMANE_SYNC_ENABLED=False) — skipping")
        return

    redis_client = get_redis()
    failures = int(await redis_client.get(_YMANE_SYNC_FAILURE_KEY) or 0)
    if failures >= settings.HISTORICAL_YMANE_SYNC_MAX_CONSECUTIVE_FAILURES:
        logger.warning(
            "[YMANE-SYNC] circuit open (%d consecutive failures) — skipping until reset "
            "(DEL '%s' in Redis once the underlying issue, e.g. an Ymane account "
            "suspension, is confirmed resolved)",
            failures, _YMANE_SYNC_FAILURE_KEY,
        )
        return

    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with SessionLocal() as db:
            last_synced = (await db.execute(
                select(func.max(HistoricalEventRecord.occurred_at))
                .where(HistoricalEventRecord.source == "ymane")
            )).scalar_one_or_none()

            now = datetime.now(timezone.utc)
            if last_synced is not None:
                start_at = last_synced if last_synced.tzinfo else last_synced.replace(tzinfo=timezone.utc)
            else:
                start_at = now - timedelta(hours=settings.HISTORICAL_YMANE_SYNC_INITIAL_LOOKBACK_HOURS)

            # Ymane's datedebut/datefin are whole dates, not timestamps, so a
            # same-day re-run still re-requests that whole day — harmless
            # (import_events' source+source_event_id uniqueness skips
            # anything already imported) but not maximally traffic-light.
            # Using the checkpoint at all is still what keeps every run's
            # date range small and bounded instead of silently growing to
            # cover the dataset's entire history as it accumulates.
            try:
                source = YmaneEventSource()
                summary = await historical_service.import_events(source, start_at.date(), now.date(), db)
                logger.info(
                    "[YMANE-SYNC] ok — range=%s..%s fetched=%d inserted=%d skipped=%d errors=%d",
                    start_at.date(), now.date(), summary.fetched, summary.inserted,
                    summary.skipped_duplicates, len(summary.errors),
                )
                await redis_client.delete(_YMANE_SYNC_FAILURE_KEY)
            except (YmaneAuthError, YmaneRequestError) as e:
                new_count = await redis_client.incr(_YMANE_SYNC_FAILURE_KEY)
                logger.error(
                    "[YMANE-SYNC] failed (%d/%d consecutive): %s",
                    new_count, settings.HISTORICAL_YMANE_SYNC_MAX_CONSECUTIVE_FAILURES, e,
                )
    finally:
        await engine.dispose()
