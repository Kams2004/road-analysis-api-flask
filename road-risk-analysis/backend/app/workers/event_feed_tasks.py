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
