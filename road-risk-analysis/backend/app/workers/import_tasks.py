"""
Celery tasks of cloud imports (app.services.cloud_import). Same "processing"
queue as the jobs, so the existing worker consumes them.

  cloud_import_item   one file: import slot → download → verify → job

At most `processing.cloud_import_concurrency` downloads run at once across
every worker (own Redis slot pool, separate from the processing capacity);
a task finding no free slot re-queues itself every few seconds without
consuming an attempt — like process_video. SFTP downloads also take a slot
of their server's own pool (at most sftp_core.PER_HOST_DOWNLOADS per
host:port, to stay polite with servers that drop busy sessions).
"""
import logging

from celery.exceptions import Retry

from app.services import job_slots
from app.workers.celery_app import celery_app
from app.workers.job_tasks import _run

logger = logging.getLogger(__name__)

SLOT_RETRY_S = 5
SLOT_STALE_S = 10 * 60      # no heartbeat for 10 min → the download died with its worker


async def _prepare(db, item_id: str):
    """(capacity of the import pool, per-host pool key + capacity or None),
    or None when the item has nothing to do."""
    from app.services import cloud_import
    from app.models.cloud_connection import CloudConnection
    from app.models.cloud_import import CloudImportItem
    from app.services.settings_service import get_section

    item = await db.get(CloudImportItem, item_id)
    if item is None or item.status not in cloud_import.TRANSFER:
        return None
    proc = await get_section(db, "processing")
    host = None
    conn = await db.get(CloudConnection, item.connection_id) if item.connection_id else None
    if conn is not None and conn.provider == "sftp" and conn.sftp_host:
        from app.services import sftp_core, sftp_source
        host = (sftp_source.host_slots_key(conn.sftp_host, conn.sftp_port or 22),
                max(1, min(sftp_core.PER_HOST_DOWNLOADS, proc.cloud_import_concurrency)))
    return proc.cloud_import_concurrency, host


@celery_app.task(bind=True, name="app.workers.celery_app.cloud_import_item", max_retries=None)
def cloud_import_item(self, item_id: str):
    from app.services import cloud_import

    r = job_slots.client()
    acquired = False
    host_key = None
    try:
        prepared = _run(lambda db: _prepare(db, item_id))
        if prepared is None:
            return "skipped"
        capacity, host = prepared
        try:
            acquired = job_slots.acquire(r, item_id, capacity, stale_s=SLOT_STALE_S, key=cloud_import.SLOTS_KEY)
            if acquired and host is not None:
                if job_slots.acquire(r, item_id, host[1], stale_s=SLOT_STALE_S, key=host[0]):
                    host_key = host[0]
                else:                                   # this server is busy: give the global slot back
                    job_slots.release(r, item_id, key=cloud_import.SLOTS_KEY)
                    acquired = False
        except Exception as exc:
            logger.warning(f"[Import {item_id}] slot pool unavailable ({exc}) — running unthrottled")
            acquired, r = True, None
        if not acquired:
            raise self.retry(countdown=SLOT_RETRY_S, max_retries=None)

        def beat():
            try:
                job_slots.heartbeat(r, item_id, key=cloud_import.SLOTS_KEY)
                if host_key:
                    job_slots.heartbeat(r, item_id, key=host_key)
            except Exception:
                pass

        deps = cloud_import.WorkerDeps(redis=r, heartbeat=beat if r is not None else None)
        return _run(lambda db: cloud_import.run_item(db, item_id, deps))
    except Retry:
        raise
    finally:
        if acquired and r is not None:
            try:
                job_slots.release(r, item_id, key=cloud_import.SLOTS_KEY)
                if host_key:
                    job_slots.release(r, item_id, key=host_key)
            except Exception:
                logger.warning(f"[Import {item_id}] could not release its slot", exc_info=True)
