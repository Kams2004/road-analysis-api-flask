"""
Cloud import batches (RRAP-07-002/004): progress of the files pulled from
connected drives — created by POST /cloud-connections/{id}/imports or by a
watched folder — with cancel and retry. Rules: app.services.cloud_import.
Every user action is audited here (the router is self-audited in main.py).
"""
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, actor_name, api_error, get_principal
from app.api.openapi import err, errors, legacy
from app.db.session import get_db
from app.models.cloud_connection import CloudConnection
from app.models.cloud_import import CloudImportBatch, CloudImportItem, ImportItemStatus as S
from app.models.job import Job
from app.schemas.cloud_imports import ImportBatchListOut, ImportBatchOut
from app.services import audit, cloud_import, job_service

router = APIRouter()

BatchId = Annotated[str, Path(description="Import batch id (UUID).")]
ItemId = Annotated[str, Path(description="Import item id (UUID).")]
E_BATCH = legacy(404, "Import not found")
E_ITEM = legacy(404, "Import item not found")


def _actor(principal: Principal) -> str:
    return actor_name(principal, principal.label) or principal.kind


async def _batch(db: AsyncSession, batch_id: str) -> CloudImportBatch:
    b = await db.get(CloudImportBatch, batch_id)
    if b is None:
        raise HTTPException(404, "Import not found")
    return b


async def _item(db: AsyncSession, batch_id: str, item_id: str) -> CloudImportItem:
    it = await db.get(CloudImportItem, item_id)
    if it is None or it.batch_id != batch_id:
        raise HTTPException(404, "Import item not found")
    return it


async def _view(db: AsyncSession, batch: CloudImportBatch) -> dict:
    return (await cloud_import.batch_views(db, [batch]))[0]


def _effective(item: CloudImportItem, job: Optional[Job]) -> str:
    return cloud_import.effective_status(item.status, job.status.value if job is not None else None,
                                        has_job=item.job_id is not None)


def _signal_cancel(item_id: str) -> None:
    try:
        job_slots_client().set(cloud_import.cancel_key(item_id), "1", ex=cloud_import.LIVE_TTL_S)
    except Exception:
        pass


def job_slots_client():
    from app.services import job_slots
    return job_slots.client()


def _revoke(task_id: Optional[str]) -> None:
    if not task_id:
        return
    try:
        from app.workers.celery_app import celery_app
        celery_app.control.revoke(task_id)
    except Exception:   # broker down — the worker re-checks the status before starting
        pass


async def _cancel_one(db: AsyncSession, item: CloudImportItem, actor: str) -> bool:
    """Apply cancel_action to one item. True when something was cancelled."""
    job = await db.get(Job, item.job_id) if item.job_id else None
    action = cloud_import.cancel_action(_effective(item, job))
    if action == "now":
        _revoke(item.celery_task_id)
        cloud_import.mark_item(item, S.cancelled.value)
        cloud_import.discard_partial(item)
        return True
    if action == "signal":
        _signal_cancel(item.id)       # the worker stops at its next chunk and marks it cancelled
        return True
    if action == "job" and job is not None and not job.cancel_requested:
        plan = job_service.cancel_plan(job.status)
        if plan == "revoke":
            _revoke(job.celery_task_id)
            job.cancelled_by = actor
            job_service.mark_cancelled(db, job, actor, {"via": "cloud_import"})
            return True
        if plan == "flag":
            job.cancel_requested = True
            job.cancelled_by = actor
            job_service.record_event(db, job.id, "cancel_requested", actor, {"via": "cloud_import"})
            return True
    return False


async def _retry_one(db: AsyncSession, item: CloudImportItem, actor: str) -> Optional[str]:
    """Apply retry_action to one item. Returns None when retried, else the
    refusal message."""
    job = await db.get(Job, item.job_id) if item.job_id else None
    action = cloud_import.retry_action(_effective(item, job), has_job=job is not None)
    if action is None:
        return f"Only failed or cancelled imports can be retried (status '{_effective(item, job)}')."
    if action == "download":
        conn = await db.get(CloudConnection, item.connection_id) if item.connection_id else None
        if not job_service.connection_usable(conn):
            return "The cloud connection was removed, disabled or needs to be reconnected."
        cloud_import.reset_for_retry(item)
        batch = await db.get(CloudImportBatch, item.batch_id)
        if batch is not None:
            batch.cancel_requested = False
        try:
            job_slots_client().delete(cloud_import.cancel_key(item.id))
        except Exception:
            pass
        await db.commit()
        cloud_import.dispatch(item)
        return None
    conn = await db.get(CloudConnection, job.connection_id) if job.connection_id else None
    refusal = job_service.retry_refusal(
        job.status, job.attempts, job.max_attempts,
        has_reviewed=await job_service.has_reviewed_detections(db, job.id),
        media_available=job_service.media_available(job, job_service.connection_usable(conn)))
    if refusal:
        return refusal.message
    previous = {"status": job.status.value, "attempt": job.attempts, "error": job.error}
    removed = await job_service.purge_outputs(db, job.id)
    job_service.reset_for_retry(job)
    job_service.record_event(db, job.id, "retried", actor,
                             {**previous, "attempt": job.attempts, "detections_removed": removed, "via": "cloud_import"})
    item.status = S.queued_for_processing.value
    await job_service.enqueue_and_commit(db, job, actor)
    return None


@router.get("/", response_model=ImportBatchListOut, summary="List cloud imports")
async def list_imports(
    connection_id: Optional[str] = Query(None, description="Only the imports of this connection."),
    active: Optional[bool] = Query(None, description="`true` = only imports still in progress, `false` = finished."),
    origin: Optional[str] = Query(None, description="`manual` or `sync` (watched folder)."),
    include_items: bool = Query(False, description="Embed the items (else only the roll-up)."),
    skip: int = Query(0, ge=0, description="Number of batches to skip (offset)."),
    limit: int = Query(20, ge=1, le=100, description="Page size (max 100)."),
    db: AsyncSession = Depends(get_db),
):
    """Import batches, newest first, each with its roll-up: status, items per status, bytes, combined
    speed, overall progress and `active` (poll while true)."""
    q = select(CloudImportBatch)
    if connection_id:
        q = q.where(CloudImportBatch.connection_id == connection_id)
    if origin:
        q = q.where(CloudImportBatch.origin == origin)
    if active is not None:
        q = q.where(CloudImportBatch.finished_at.is_(None) if active else CloudImportBatch.finished_at.isnot(None))
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    n_active = (await db.execute(select(func.count()).select_from(
        select(CloudImportBatch.id).where(CloudImportBatch.finished_at.is_(None)).subquery()))).scalar_one()
    batches = (await db.execute(q.order_by(CloudImportBatch.created_at.desc()).offset(skip).limit(limit))).scalars().all()
    return {"total": total, "active": n_active,
            "items": await cloud_import.batch_views(db, list(batches), with_items=include_items)}


@router.get("/{batch_id}", response_model=ImportBatchOut, summary="Get an import with live progress",
            responses=errors(E_BATCH))
async def get_import(batch_id: BatchId, db: AsyncSession = Depends(get_db)):
    """
    The batch and every item with live progress: while `downloading`, `bytes_done` / `percent` /
    `speed_bps` / `eta_s` come from the worker's live counters (refreshed every ~0.5 s); after
    hand-over the status and `job_progress` follow the processing job. Poll every 1–2 s while
    `active`.
    """
    return await _view(db, await _batch(db, batch_id))


@router.post("/{batch_id}/cancel", response_model=ImportBatchOut, summary="Cancel an import",
             responses=errors(E_BATCH, err(409, "invalid_transition", "Nothing left to cancel in this import.")))
async def cancel_import(batch_id: BatchId, request: Request, db: AsyncSession = Depends(get_db),
                        principal: Principal = Depends(get_principal)):
    """
    Cancel everything still in progress: queued files are cancelled at once, downloads stop at their
    next chunk (partial file deleted), files already handed over have their job cancelled (`pending`
    → cancelled, `processing` → stops at its next check). Finished files are kept. Audited as
    `cloud_import.cancel`.
    """
    batch = await _batch(db, batch_id)
    actor = _actor(principal)
    items = (await db.execute(select(CloudImportItem).where(CloudImportItem.batch_id == batch.id))).scalars().all()
    batch.cancel_requested = True
    n = 0
    for it in items:
        n += await _cancel_one(db, it, actor)
    if not n:
        await db.rollback()
        await audit.record_now(request, "cloud_import.cancel", principal=principal, outcome="failure",
                               object_type="cloud_import", object_id=batch.id, details={"code": "invalid_transition"})
        raise api_error(409, "invalid_transition", "Nothing left to cancel in this import.")
    audit.record(db, request, "cloud_import.cancel", principal=principal, object_type="cloud_import",
                 object_id=batch.id, details={"items_cancelled": n})
    await db.commit()
    return await _view(db, batch)


@router.post("/{batch_id}/retry", response_model=ImportBatchOut, summary="Retry the failed files of an import",
             responses=errors(E_BATCH, err(409, "invalid_transition", "No failed or cancelled file to retry.")))
async def retry_import(batch_id: BatchId, request: Request, db: AsyncSession = Depends(get_db),
                       principal: Principal = Depends(get_principal)):
    """Retry every `failed` / `cancelled` file: a failed download is pulled again (resuming its
    partial file), a failed or cancelled job is re-run (job retry rules apply; refused files are
    left as they are). Audited as `cloud_import.retry`."""
    batch = await _batch(db, batch_id)
    actor = _actor(principal)
    rows = (await db.execute(
        select(CloudImportItem, Job).outerjoin(Job, Job.id == CloudImportItem.job_id)
        .where(CloudImportItem.batch_id == batch.id))).all()
    candidates = [it for it, job in rows if _effective(it, job) in (S.failed.value, S.cancelled.value)]
    if not candidates:
        raise api_error(409, "invalid_transition", "No failed or cancelled file to retry.")
    retried, refused = 0, {}
    for it in candidates:
        msg = await _retry_one(db, it, actor)
        if msg is None:
            retried += 1
        else:
            refused[it.name] = msg
    batch.cancel_requested = False
    audit.record(db, request, "cloud_import.retry", principal=principal, object_type="cloud_import",
                 object_id=batch.id, details={"retried": retried, "refused": refused})
    await db.commit()
    return await _view(db, batch)


@router.post("/{batch_id}/items/{item_id}/cancel", response_model=ImportBatchOut, summary="Cancel one file",
             responses=errors(E_BATCH, E_ITEM, err(409, "invalid_transition", "This file is already done.")))
async def cancel_item(batch_id: BatchId, item_id: ItemId, request: Request, db: AsyncSession = Depends(get_db),
                      principal: Principal = Depends(get_principal)):
    """Cancel one file of an import (same rules as cancelling the whole import). Audited as
    `cloud_import.item_cancel`."""
    batch = await _batch(db, batch_id)
    item = await _item(db, batch_id, item_id)
    if not await _cancel_one(db, item, _actor(principal)):
        raise api_error(409, "invalid_transition", "This file is already finished.")
    audit.record(db, request, "cloud_import.item_cancel", principal=principal, object_type="cloud_import",
                 object_id=batch.id, details={"item_id": item.id, "file": item.name})
    await db.commit()
    return await _view(db, batch)


@router.post("/{batch_id}/items/{item_id}/retry", response_model=ImportBatchOut, summary="Retry one file",
             responses=errors(E_BATCH, E_ITEM,
                              err(409, "retry_refused", "Only failed or cancelled imports can be retried "
                                                        "(status 'done').")))
async def retry_item(batch_id: BatchId, item_id: ItemId, request: Request, db: AsyncSession = Depends(get_db),
                     principal: Principal = Depends(get_principal)):
    """Retry one `failed` / `cancelled` file: download again (resuming) when it failed before the
    hand-over, else re-run its job (job retry rules: attempts left, no reviewed detection, media
    still available). Audited as `cloud_import.item_retry` (refusals too)."""
    batch = await _batch(db, batch_id)
    item = await _item(db, batch_id, item_id)
    msg = await _retry_one(db, item, _actor(principal))
    if msg is not None:
        await db.rollback()
        await audit.record_now(request, "cloud_import.item_retry", principal=principal, outcome="failure",
                               object_type="cloud_import", object_id=batch.id,
                               details={"item_id": item_id, "reason": msg})
        raise api_error(409, "retry_refused", msg)
    audit.record(db, request, "cloud_import.item_retry", principal=principal, object_type="cloud_import",
                 object_id=batch.id, details={"item_id": item.id, "file": item.name})
    await db.commit()
    return await _view(db, batch)


@router.delete("/{batch_id}", status_code=204, summary="Remove a finished import from the history",
               responses=errors(E_BATCH, err(409, "import_active", "Cancel the import or wait for it to finish.")))
async def delete_import(batch_id: BatchId, request: Request, db: AsyncSession = Depends(get_db),
                        principal: Principal = Depends(get_principal)):
    """Forget a finished import (its items). The jobs it created and their detections are kept.
    Audited as `cloud_import.delete`."""
    batch = await _batch(db, batch_id)
    view = await _view(db, batch)
    if view["active"]:
        raise api_error(409, "import_active", "Cancel the import or wait for it to finish.")
    items = (await db.execute(select(CloudImportItem).where(CloudImportItem.batch_id == batch.id))).scalars().all()
    for it in items:
        cloud_import.discard_partial(it)
        await db.delete(it)
    audit.record(db, request, "cloud_import.delete", principal=principal, object_type="cloud_import",
                 object_id=batch.id, details={"files": len(items), "status": view["status"]})
    await db.delete(batch)
    await db.commit()
