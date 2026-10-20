"""
Celery tasks of Module 7 — processing jobs, their housekeeping and the
automatic sources (cloud folders, live streams). Registered on the shared
`celery_app` (imported at the bottom of app/workers/celery_app.py) and all
routed to the "processing" queue the worker consumes.

  process_video            one job: capacity slot → process → done/failed/cancelled
  watchdog_stale_jobs      beat, every 5 min
  cleanup_failed_media     beat, hourly — media of failed/cancelled jobs past retention
  sync_cloud_connections   beat, every minute — dispatches due folder syncs
  sync_cloud_connection    one watched folder: list → import batch of the new files
  capture_streams          beat, every minute — (re)starts capture of active streams
  capture_stream_segment   one segment of a stream → job, then chains the next one
"""
import asyncio
import logging
import os
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from celery.exceptions import Retry

from app.core.config import settings
from app.services import job_slots
from app.services.job_service import PermanentJobError, TRANSIENT_RETRIES, readable_error

logger = logging.getLogger(__name__)

TEMP_DIR = Path(settings.TEMP_DIR)
CAPACITY_RETRY_S = 10
TRANSIENT_RETRY_S = 30
SYNC_MAX_FILES = 20          # downloads per sync run — the rest come on the next run
SYNC_LOCK_S = 3600


def _run(factory):
    """Run `factory(db)` on a fresh event loop with a fresh engine — asyncpg
    connections are loop-bound and cannot be reused across tasks."""
    async def runner():
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
        engine = create_async_engine(settings.DATABASE_URL, echo=False)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with session_factory() as db:
                return await factory(db)
        finally:
            await engine.dispose()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(runner())
    finally:
        loop.close()


# Imported after _run: celery_app imports the other task modules at its end and
# they import _run from here — importing this module first (job_service.enqueue)
# must not hit a partially initialised module without it.
from app.workers.celery_app import celery_app  # noqa: E402


# ── processing ────────────────────────────────────────────────────────────────

@celery_app.task(bind=True, name="app.workers.celery_app.process_video", max_retries=None)
def process_video(self, job_id: str, tmp_path: str = None, enabled: list = None, failures: int = 0):
    """
    Process one job. `tmp_path`/`enabled` are only read from messages queued
    before Module 7 (the job row now carries source_path/enabled_models).
    A job that finds C jobs already running re-queues itself every 10 s
    without consuming an attempt; unexpected errors are retried
    TRANSIENT_RETRIES times (job stays pending), then the job fails.
    """
    r = job_slots.client()
    acquired = False
    try:
        capacity = _run(lambda db: _prepare(db, job_id, tmp_path, enabled))
        if capacity is None:
            return "skipped"
        acquired = _acquire(r, job_id, capacity)
        if not acquired:
            raise self.retry(countdown=CAPACITY_RETRY_S, max_retries=None, kwargs={"failures": failures})
        outcome = _run(lambda db: _process(db, job_id, r))
        if outcome == "done":
            # Module 6 hazard lifecycle: sightings + "not seen" passes of this video.
            from app.services.job_service import schedule_hazard_lifecycle
            schedule_hazard_lifecycle(job_id)
        return outcome
    except Retry:
        raise
    except PermanentJobError as exc:
        error = readable_error(exc)
        _run(lambda db: _finish_failed(db, job_id, error))
        return "failed"
    except Exception as exc:
        logger.error(f"[Job {job_id}] Task failed: {exc}", exc_info=True)
        error = readable_error(exc)
        if failures < TRANSIENT_RETRIES and _run(lambda db: _transient(db, job_id, error, failures + 1)):
            raise self.retry(countdown=TRANSIENT_RETRY_S, max_retries=None, kwargs={"failures": failures + 1})
        _run(lambda db: _finish_failed(db, job_id, error))
        return "failed"
    finally:
        if acquired:
            try:
                job_slots.release(r, job_id)
            except Exception:
                logger.warning(f"[Job {job_id}] could not release capacity slot", exc_info=True)


def _acquire(r, job_id: str, capacity: int) -> bool:
    try:
        return job_slots.acquire(r, job_id, capacity)
    except Exception as exc:  # Redis unreachable — the broker too, so this is short-lived
        logger.warning(f"[Job {job_id}] capacity check unavailable ({exc}) — running unthrottled")
        return True


async def _prepare(db, job_id: str, tmp_path, enabled):
    """Checks before taking a slot. Returns the capacity C, or None to skip."""
    from app.models.cloud_connection import CloudConnection
    from app.models.job import CLOUD_SOURCES, FINAL_STATUSES, Job
    from app.services import job_service
    from app.services.settings_service import get_section

    job = await db.get(Job, job_id)
    if job is None:
        logger.warning(f"[Job {job_id}] no longer exists — skipping")
        return None
    if job.status in FINAL_STATUSES:
        logger.info(f"[Job {job_id}] already {job.status.value} — skipping")
        return None
    if job.cancel_requested:
        job_service.mark_cancelled(db, job, None)
        await db.commit()
        return None
    if tmp_path and not job.source_path:          # message from before Module 7
        job.source_path = tmp_path
    if enabled and not job.enabled_models:
        job.enabled_models = ",".join(enabled)

    if not (job.source_path and os.path.exists(job.source_path)):
        if job.source_type in CLOUD_SOURCES and job.source_ref:
            conn = await db.get(CloudConnection, job.connection_id) if job.connection_id else None
            if not job_service.connection_usable(conn):
                raise PermanentJobError("Source media is gone and its cloud connection is not available")
            await _download_cloud(db, job, conn)
        else:
            raise PermanentJobError("Source media is no longer available")

    proc = await get_section(db, "processing")
    await db.commit()
    return proc.max_concurrent_jobs


async def _download_cloud(db, job, conn) -> None:
    from app.services import cloud_storage
    ext = Path(job.filename or "").suffix.lower() or ".mp4"
    dest = str(TEMP_DIR / f"{uuid.uuid4().hex}{ext}")
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    try:
        token = await cloud_storage.connection_token(db, conn)
        if conn.provider == "sftp":
            from app.services.cloud_providers import get_provider
            provider = get_provider("sftp")
            await provider.download(token, job.source_ref, dest)
            provider.persist_credentials(conn, token)
        else:
            await cloud_storage.download(conn.provider, token, job.source_ref, dest)
    except cloud_storage.CloudError as exc:
        Path(dest).unlink(missing_ok=True)
        raise PermanentJobError(f"Re-download from {conn.provider} failed: {exc}")
    job.source_path = dest


async def _process(db, job_id: str, r):
    from app.models.job import FINAL_STATUSES, Job
    from app.services import job_service
    from app.workers.processor import process_source

    job = await db.get(Job, job_id)
    if job is None or job.status in FINAL_STATUSES:
        return "skipped"
    if job.cancel_requested:
        job_service.mark_cancelled(db, job, None)
        await db.commit()
        return "cancelled"
    # Leftovers of an interrupted attempt would be duplicated by a new pass.
    if await job_service.has_reviewed_detections(db, job_id):
        raise PermanentJobError("Detections of this job were reviewed meanwhile — not re-processed")
    await job_service.purge_outputs(db, job_id)
    await db.commit()

    def beat():
        try:
            job_slots.heartbeat(r, job_id)
        except Exception:
            pass

    enabled = [m for m in (job.enabled_models or "").split(",") if m] or None
    return await process_source(job, job.source_path, db, enabled, heartbeat=beat)


async def _transient(db, job_id: str, error: str, failure_no: int) -> bool:
    """Back to pending before a worker-side retry. False when the job must
    not be retried (gone, final, or a cancel is pending)."""
    from app.models.job import FINAL_STATUSES, Job, JobStatus
    from app.services import job_service

    await db.rollback()
    job = await db.get(Job, job_id)
    if job is None or job.status in FINAL_STATUSES or job.cancel_requested:
        return False
    job.status = JobStatus.pending
    job_service.record_event(db, job_id, "progress_failed", details={
        "error": error, "retry": failure_no, "of": TRANSIENT_RETRIES,
        "retry_in_s": TRANSIENT_RETRY_S})
    await db.commit()
    return True


async def _finish_failed(db, job_id: str, error: str) -> None:
    from app.models.job import FINAL_STATUSES, Job
    from app.services import job_service

    await db.rollback()
    job = await db.get(Job, job_id)
    if job is None or job.status in FINAL_STATUSES:
        return
    if job.cancel_requested:
        job_service.mark_cancelled(db, job, None, {"error": error})
    else:
        job_service.mark_failed(db, job, error)
    await db.commit()
    logger.error(f"[Job {job_id}] FAILED — {error}")


# ── housekeeping ─────────────────────────────────────────────────────────────

@celery_app.task(name="app.workers.celery_app.watchdog_stale_jobs")
def watchdog_stale_jobs():
    return _run(_watchdog)


async def _watchdog(db) -> int:
    from sqlalchemy import select
    from app.models.job import Job, JobStatus
    from app.services.watchdog import mark_stale_jobs

    marked = await mark_stale_jobs(db)
    # Free slots held by jobs that are no longer processing (worker killed).
    try:
        r = job_slots.client()
        held = job_slots.holders(r)
        if held:
            running = set((await db.execute(
                select(Job.id).where(Job.id.in_(held), Job.status.in_([JobStatus.processing, JobStatus.pending]))
            )).scalars().all())
            for job_id in set(held) - running:
                job_slots.release(r, job_id)
                logger.warning(f"[Watchdog] released orphan capacity slot of job {job_id}")
    except Exception:
        logger.warning("[Watchdog] slot reconciliation skipped", exc_info=True)
    if marked:
        logger.warning(f"[Watchdog] {marked} stale job(s) marked")
    return marked


@celery_app.task(name="app.workers.celery_app.cleanup_failed_media")
def cleanup_failed_media():
    return _run(_cleanup_media)


async def _cleanup_media(db) -> int:
    """Delete media of failed/cancelled jobs older than retain_failed_hours
    (they can no longer be retried from local media), plus orphan files."""
    from sqlalchemy import func, select
    from app.models.job import Job, JobStatus
    from app.services import job_service
    from app.services.settings_service import get_section

    proc = await get_section(db, "processing")
    cutoff = datetime.utcnow() - timedelta(hours=proc.retain_failed_hours)
    jobs = (await db.execute(select(Job).where(
        Job.status.in_([JobStatus.failed, JobStatus.cancelled]),
        Job.source_path.isnot(None),
        func.coalesce(Job.finished_at, Job.cancelled_at, Job.created_at) < cutoff,
    ))).scalars().all()
    for job in jobs:
        job_service.delete_media(job)
    await db.commit()

    referenced = set((await db.execute(select(Job.source_path).where(Job.source_path.isnot(None)))).scalars().all())
    orphans = 0
    if TEMP_DIR.exists():
        limit = time.time() - proc.retain_failed_hours * 3600
        for p in TEMP_DIR.iterdir():
            try:
                if p.is_file() and str(p) not in referenced and p.stat().st_mtime < limit:
                    p.unlink()
                    orphans += 1
            except OSError:
                pass
    if jobs or orphans:
        logger.info(f"[Cleanup] media removed: {len(jobs)} job(s), {orphans} orphan file(s)")
    return len(jobs) + orphans


# ── cloud storage sources ────────────────────────────────────────────────────

def _sync_lock(connection_id: str) -> str:
    return f"cloud:sync:{connection_id}"


@celery_app.task(name="app.workers.celery_app.sync_cloud_connections")
def sync_cloud_connections():
    return _run(_dispatch_due_syncs)


async def _dispatch_due_syncs(db) -> int:
    from sqlalchemy import or_, select
    from app.models.cloud_connection import CloudConnection, ConnectionStatus
    from app.services.settings_service import get_section

    cfg = await get_section(db, "cloud")
    due_before = datetime.utcnow() - timedelta(minutes=cfg.sync_interval_minutes)
    conns = (await db.execute(select(CloudConnection).where(
        CloudConnection.enabled.is_(True),
        CloudConnection.auto_import.is_(True),
        CloudConnection.status.in_([ConnectionStatus.active.value, ConnectionStatus.error.value]),
        or_(CloudConnection.refresh_token_enc.isnot(None), CloudConnection.provider == "sftp"),
    ))).scalars().all()
    r = job_slots.client()
    sent = 0
    for conn in conns:
        if conn.last_sync_at is not None and conn.last_sync_at > due_before:
            continue
        if r.exists(_sync_lock(conn.id)):
            continue
        sync_cloud_connection.delay(conn.id)
        sent += 1
    return sent


@celery_app.task(name="app.workers.celery_app.sync_cloud_connection")
def sync_cloud_connection(connection_id: str, actor: str = "system"):
    r = job_slots.client()
    token = uuid.uuid4().hex
    if not r.set(_sync_lock(connection_id), token, nx=True, ex=SYNC_LOCK_S):
        logger.info(f"[Cloud {connection_id}] sync already running — skipped")
        return 0
    try:
        return _run(lambda db: sync_connection(db, connection_id, actor))
    finally:
        if r.get(_sync_lock(connection_id)) == token:
            r.delete(_sync_lock(connection_id))


async def sync_connection(db, connection_id: str, actor: str = "system", client=None,
                          dispatch_fn=None) -> int:
    """Watched folder → automatic import: list the folder, and import every
    media file whose current version was not imported yet, as an import
    batch (origin `sync`) — progress shows up with the manual imports.
    Returns the number of files queued. `client`/`dispatch_fn` are test hooks."""
    from app.models.cloud_connection import CloudConnection, ConnectionStatus
    from app.services import audit, cloud_import, cloud_storage, job_service
    from app.services.cloud_providers import get_provider

    conn = await db.get(CloudConnection, connection_id)
    if conn is None or not conn.enabled or not (conn.refresh_token_enc or conn.provider == "sftp"):
        return 0
    created = 0
    listed = 0
    try:
        provider = get_provider(conn.provider)
        token = await cloud_storage.connection_token(db, conn, client=client)
        entries = await provider.walk_media(token, conn.folder_id, recursive=False, client=client)
        provider.persist_credentials(conn, token)
        listed = len(entries)
        entries.sort(key=lambda e: (e.modified or "", e.name))       # oldest first
        priors = await cloud_import.prior_imports(db, conn.id, [e.id for e in entries])
        fresh = [p.entry for p in cloud_import.plan_items(entries, priors) if p.status == "queued"][:SYNC_MAX_FILES]
        if fresh:
            models = [m for m in (conn.auto_import_models or []) if m in job_service.VALID_MODELS] \
                or list(job_service.DEFAULT_MODELS)
            _, items = await cloud_import.import_entries(
                db, conn, fresh, origin="sync", models=models, frame_skip=conn.auto_import_frame_skip,
                options={"folder_id": conn.folder_id, "folder_name": conn.folder_name}, actor=actor or "system",
                dispatch_fn=dispatch_fn)
            created = sum(1 for it in items if it.status == "queued")
        conn = await db.get(CloudConnection, connection_id)
        conn.status = ConnectionStatus.active.value
        conn.last_sync_status, conn.last_sync_error = "ok", None
        audit.record_system(db, "cloud_connection.sync", object_type="cloud_connection", object_id=conn.id,
                            details={"files_listed": listed, "files_queued": created, "by": actor})
    except Exception as exc:
        logger.warning(f"[Cloud {connection_id}] sync failed: {exc}", exc_info=not isinstance(exc, cloud_storage.CloudError))
        await db.rollback()
        conn = await db.get(CloudConnection, connection_id)
        if conn is None:
            return created
        conn.status = ConnectionStatus.error.value
        conn.last_sync_status, conn.last_sync_error = "error", str(exc)[:500]
        audit.record_system(db, "cloud_connection.sync", outcome="failure", object_type="cloud_connection",
                            object_id=conn.id, details={"error": str(exc)[:500], "files_queued": created, "by": actor})
    conn.last_sync_at = datetime.utcnow()
    conn.last_sync_files = created
    await db.commit()
    return created


# ── live streams ─────────────────────────────────────────────────────────────

def capture_lock(stream_id: str) -> str:
    return f"stream:capture:{stream_id}"


@celery_app.task(name="app.workers.celery_app.capture_streams")
def capture_streams():
    return _run(_dispatch_captures)


async def _dispatch_captures(db) -> int:
    from sqlalchemy import select
    from app.models.stream_source import StreamSource, StreamStatus

    streams = (await db.execute(
        select(StreamSource).where(StreamSource.status == StreamStatus.active.value)
    )).scalars().all()
    r = job_slots.client()
    sent = 0
    for s in streams:
        # The lock is held from dispatch to the end of the chain of segments.
        if r.set(capture_lock(s.id), "queued", nx=True, ex=(s.segment_seconds or 60) + 300):
            capture_stream_segment.delay(s.id)
            sent += 1
    return sent


@celery_app.task(name="app.workers.celery_app.capture_stream_segment")
def capture_stream_segment(stream_id: str):
    r = job_slots.client()
    next_seconds = 0
    try:
        next_seconds = _run(lambda db: _capture(db, stream_id))
    finally:
        if next_seconds:
            # Keep the lock and record the next segment right away (no gap).
            r.set(capture_lock(stream_id), "queued", ex=next_seconds + 300)
            capture_stream_segment.delay(stream_id)
        else:
            r.delete(capture_lock(stream_id))


async def _capture(db, stream_id: str) -> int:
    """Record one segment and create its job. Returns the segment length when
    the stream is still active and the next segment should follow, else 0."""
    from app.models.stream_source import StreamSource, StreamStatus
    from app.services import job_service
    from app.services.stream_capture import capture_segment

    s = await db.get(StreamSource, stream_id)
    if s is None or s.status != StreamStatus.active.value:
        return 0
    started = datetime.utcnow()
    t0 = time.monotonic()
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    dest = str(TEMP_DIR / f"stream_{stream_id[:8]}_{started:%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}.mp4")
    try:
        seg = await asyncio.to_thread(capture_segment, s.url, s.segment_seconds or 60, dest)
    except Exception as exc:
        await db.refresh(s)
        s.status = StreamStatus.error.value
        s.last_error = str(exc)[:500]
        await db.commit()
        logger.warning(f"[Stream {s.name}] capture failed: {exc}")
        return 0

    await db.refresh(s)
    label = f"{s.name} {started:%H:%M}"
    job = await job_service.new_job(
        db, filename=f"{label}.mp4", source_type="stream",
        enabled=list(s.enabled_models or job_service.DEFAULT_MODELS), source_path=seg.path,
        submitted_by="system", frame_skip_override=s.frame_skip,
        source_ref=s.id, source_name=label, stream_id=s.id,
    )
    s.last_segment_at = datetime.utcnow()
    s.segments_processed = (s.segments_processed or 0) + 1
    s.last_error = None
    await job_service.enqueue_and_commit(db, job)
    logger.info(f"[Stream {s.name}] segment {seg.frames} frames @ {seg.fps:.1f}fps → job {job.id}")
    seconds = s.segment_seconds or 60
    if s.status != StreamStatus.active.value:
        return 0
    # A source delivering much faster than real time is a file/VOD, not a
    # live feed: don't loop it back-to-back, the next beat tick resumes it.
    return seconds if time.monotonic() - t0 >= seconds * 0.5 else 0
