"""
Celery tasks of the daily event files (app/modules/m08_historical/event_feed.py):

  sync_event_feeds   beat, every hour: one check of every enabled "events" source
                     (an SFTP listing; files are fetched only when new or changed)
  sync_event_feed    one source now (beat, or "Check now" in the web console);
                     `force` re-imports the named files even if unchanged

One check per source at a time (Redis lock): a slow catch-up is never run twice.
"""
from typing import Optional

from app.workers.celery_app import celery_app
from app.workers.job_tasks import _run

LOCK_S = 1800


def lock_key(connection_id: str) -> str:
    return f"events:sync:{connection_id}"


@celery_app.task(name="app.workers.celery_app.sync_event_feeds")
def sync_event_feeds():
    return _run(_dispatch)


async def _dispatch(db) -> int:
    from app.core import settings_service
    from app.modules.m08_historical import rollups
    if not await rollups.ready(db, await settings_service.get_cached(db, "driver_behaviour")):
        rebuild_event_rollups.delay()                      # first start / severity rules changed
    from sqlalchemy import select
    from app.modules.m07_ingestion.models.cloud_connection import CloudConnection
    from app.modules.m08_historical.event_feed import PURPOSE
    ids = (await db.execute(select(CloudConnection.id).where(
        CloudConnection.purpose == PURPOSE, CloudConnection.provider == "sftp",
        CloudConnection.enabled.is_(True)))).scalars().all()
    for cid in ids:
        sync_event_feed.delay(cid)
    return len(ids)


@celery_app.task(name="app.workers.celery_app.sync_event_feed")
def sync_event_feed(connection_id: str, force: Optional[list[str]] = None):
    from app.modules.m07_ingestion import job_slots
    from app.modules.m08_historical.event_feed import sync_connection
    r = job_slots.client()
    if not r.set(lock_key(connection_id), "1", nx=True, ex=LOCK_S):
        return {"skipped": "already running"}
    try:
        return _run(lambda db: sync_connection(db, connection_id, set(force) if force else None))
    finally:
        r.delete(lock_key(connection_id))


@celery_app.task(name="app.workers.celery_app.rebuild_event_rollups")
def rebuild_event_rollups():
    """Every daily summary from all events (app/modules/m08_historical/rollups.py) — after the
    severity rules changed, or when they were never built. The screens read the raw events
    until it is done."""
    from app.modules.m07_ingestion import job_slots
    r = job_slots.client()
    if not r.set("events:rollups:full", "1", nx=True, ex=LOCK_S):
        return {"skipped": "already running"}
    try:
        async def run(db):
            from app.core import settings_service
            from app.modules.m08_historical import rollups
            await rollups.rebuild(db, await settings_service.get_cached(db, "driver_behaviour"))
            return {"rebuilt": True}
        return _run(run)
    finally:
        r.delete("events:rollups:full")


PURGE_BATCH = 10_000


@celery_app.task(name="app.workers.celery_app.purge_old_events")
def purge_old_events():
    """Raw events older than RAW_EVENT_RETENTION_DAYS (whole days), in batches; the daily
    summaries stay (a full rebuild never touches days older than the oldest raw event)."""
    async def run(db):
        from datetime import datetime, timedelta
        from sqlalchemy import delete, select
        from app.core.config import settings
        from app.modules.m08_historical.models.historical_event import HistoricalEventRecord as E
        cutoff = (datetime.utcnow() - timedelta(days=settings.RAW_EVENT_RETENTION_DAYS)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        total = 0
        while True:
            ids = (await db.execute(select(E.id).where(E.occurred_at < cutoff).limit(PURGE_BATCH))).scalars().all()
            if not ids:
                break
            await db.execute(delete(E).where(E.id.in_(ids)))
            await db.commit()
            total += len(ids)
        if total:
            from app.modules.m08_historical import agg_cache
            agg_cache.bump()
        return {"deleted": total, "before": cutoff.isoformat()}
    return _run(run)
