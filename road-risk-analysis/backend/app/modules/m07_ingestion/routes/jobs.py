"""
Video/data ingestion jobs (Module 7, RRAP-07-001 … 07-014): upload, list
with filters, detail with lifecycle events, cancel, retry, delete, queue
capacity and status summary. Lifecycle rules: app.modules.m07_ingestion.job_service.
Every user action is audited here (the router is self-audited in main.py).
"""
import asyncio
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Optional

from fastapi import APIRouter, UploadFile, File, Depends, HTTPException, Query, Request
from fastapi import Path as PathParam
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, actor_name, api_error, get_principal
from app.api.openapi import err, errors, legacy
from app.core.config import settings
from app.core.db.session import get_db
from app.modules.m07_ingestion.models.cloud_connection import CloudConnection
from app.modules.m07_ingestion.models.job import Job, JobStatus, SourceType
from app.modules.m07_ingestion.models.job_event import JobEvent
from app.modules.m07_ingestion.schemas.jobs import (
    JobCapacityOut, JobDetailOut, JobEventOut, JobListOut, JobOut, JobSummaryOut,
)
from app.core import audit
from app.modules.m07_ingestion import chunked_upload, fingerprint, job_service
from app.core.settings_service import get_section
from app.modules.m05_ai_detection.osd_settings import validate_layout_option
from app.modules.m05_ai_detection import osd_layouts

router = APIRouter()
TEMP_DIR = Path(settings.TEMP_DIR)
TEMP_DIR.mkdir(parents=True, exist_ok=True)

IMAGE_EXTS   = {".jpg", ".jpeg", ".png", ".bmp"}
VIDEO_EXTS   = set(job_service.VIDEO_EXTS)
VALID_MODELS = set(job_service.VALID_MODELS)

SORTABLE = {
    "created_at": Job.created_at, "started_at": Job.started_at, "finished_at": Job.finished_at,
    "status": Job.status, "filename": Job.filename, "detections": Job.detections,
    "avg_confidence": Job.avg_confidence, "attempts": Job.attempts, "source_type": Job.source_type,
}


def _actor(principal: Principal) -> str:
    return actor_name(principal, principal.label) or principal.kind


async def _get(db: AsyncSession, job_id: str) -> Job:
    job = await db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


JobId = Annotated[str, PathParam(description="Job id (UUID).", examples=["7d0c5b9e-2f1a-4c3d-8e6b-1a2b3c4d5e6f"])]
E_NOT_FOUND = legacy(404, "Job not found")


async def _refuse(request: Request, principal: Principal, action: str, job: Job, code: str, message: str):
    await audit.record_now(request, f"job.{action}", principal=principal, outcome="failure", object_type="job",
                           object_id=job.id, details={"code": code, "status": job.status.value})
    raise api_error(409, code, message, job_status=job.status.value)


@router.post("/", response_model=JobOut, status_code=202, summary="Upload a video or image for processing",
             responses=errors(
                 legacy(400, "Unsupported file type: .txt", summary="unsupported file extension"),
                 legacy(400, "Unknown model(s): ['cars']. Valid options: ['pothole', 'signs', 'speedbump', 'vehicles']",
                        summary="unknown model"),
                 legacy(400, "models parameter is empty.", summary="empty model list"),
                 err(409, "duplicate_video", "This video was already processed (job 7d0c5b9e…).",
                     job_id="7d0c5b9e-2f1a-4c3d-8e6b-1a2b3c4d5e6f", job_status="done", filename="drive_0412.mp4",
                     processed_at="2026-10-20T16:44:23", fingerprint="sha256:9f2c…")))
async def submit_job(
    request: Request,
    file: UploadFile = File(..., description=(
        "The media to analyse (multipart/form-data field `file`). Videos: "
        + ", ".join(f"`{e}`" for e in sorted(VIDEO_EXTS)) + "; images: "
        + ", ".join(f"`{e}`" for e in sorted(IMAGE_EXTS)) + ".")),
    models: Optional[str] = Query(
        default=None,
        description=(
            "Comma-separated list of models to run. "
            "Allowed values: pothole, signs, speedbump, vehicles (parked / roadside vehicles). "
            "Example: ?models=pothole  or  ?models=pothole,signs,vehicles  "
            "Defaults to pothole, signs, speedbump when omitted (`vehicles` is opt-in)."
        ),
    ),
    frame_skip: Optional[int] = Query(None, ge=1, le=60,
                                      description="Process one frame out of N (overrides the default sampling)."),
    osd_layout: Optional[str] = Query(None, description="Dashcam overlay layout: `auto` (default: detected on the "
                                                       "first frames, or settings `osd`), a profile name (`total`, "
                                                       "`besafe` or a configured one) or `none` (no OCR)."),
    force: bool = Query(False, description="Process the file even when the same content (SHA-256) was already "
                                           "processed or is queued (`409 duplicate_video` otherwise)."),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """
    Create a processing job from an uploaded file (`multipart/form-data`, field `file`) and queue it
    (RRAP-07-001). Answers `202 Accepted` with the job in status `pending`; follow it with
    `GET /jobs/{job_id}`. Detections appear as frames are processed (`processing` → `done`).

    `models` and `frame_skip` are **query** parameters. If the processing queue is unreachable the job
    is created directly in status `failed` (see `error`) and can be retried. Audited as `job.create`.

    The content is fingerprinted (SHA-256) while it is received: when a job with the same content is
    done, queued or processing, the upload is discarded and the answer is `409 duplicate_video` with
    that job (`job_id`, `job_status`, `filename`, `processed_at`) — send it again with `force=true`
    to process it anyway (detections then join the existing clusters: no duplicate hazards).
    """
    ext = Path(file.filename or "").suffix.lower()
    if ext not in IMAGE_EXTS | VIDEO_EXTS:
        raise HTTPException(400, f"Unsupported file type: {ext}")
    try:
        enabled = job_service.parse_models(models)
        osd_cfg = await get_section(db, "osd")
        layout = validate_layout_option(osd_cfg, osd_layout)  # type: ignore[arg-type]
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    tmp_path = TEMP_DIR / f"{uuid.uuid4().hex}{ext}"
    hasher = fingerprint.Hasher()
    with open(tmp_path, "wb") as fh:
        while chunk := await file.read(1 << 20):
            fh.write(chunk)
            hasher.update(chunk)
    fp = hasher.fingerprint()

    previous = await _refuse_duplicate(db, request, principal, fp, file.filename, force,
                                       discard=lambda: tmp_path.unlink(missing_ok=True))
    return await _create_upload_job(db, request, principal, tmp_path=tmp_path, filename=file.filename, fp=fp,
                                    enabled=enabled, frame_skip=frame_skip, layout=layout, previous=previous)


async def _refuse_duplicate(db: AsyncSession, request: Request, principal: Principal, fp: str,
                            filename: Optional[str], force: bool, discard=None):
    """409 duplicate_video when the same content is done / queued / processing (unless `force`);
    returns the previous job (or None)."""
    previous = await fingerprint.find_duplicate(db, fp)
    if previous is not None and not force:
        if discard is not None:
            discard()
        details = fingerprint.duplicate_details(previous)
        await audit.record_now(request, "job.create", principal=principal, outcome="failure", object_type="job",
                               details={"code": "duplicate_video", "filename": filename, **details})
        raise api_error(409, "duplicate_video",
                        f"This video was already processed (job {previous.id}).", fingerprint=fp, **details)
    return previous


async def _create_upload_job(db: AsyncSession, request: Request, principal: Principal, *, tmp_path: Path,
                             filename: Optional[str], fp: str, enabled, frame_skip: Optional[int],
                             layout: Optional[str], previous) -> Job:
    actor = _actor(principal)
    job = await job_service.new_job(
        db, filename=filename, source_type=SourceType.upload.value, enabled=enabled,
        source_path=str(tmp_path), submitted_by=actor, frame_skip_override=frame_skip,
        source_name=filename,
    )
    job.content_fingerprint = fp
    job.osd_layout_requested = layout
    audit.record(db, request, "job.create", principal=principal, object_type="job", object_id=job.id,
                 details={"filename": filename, "models": enabled, "frame_skip": job.frame_skip,
                          **({"osd_layout": layout} if layout else {}),
                          **({"forced_duplicate_of": previous.id} if previous is not None else {})})
    await job_service.enqueue_and_commit(db, job, actor)
    await db.refresh(job)
    return job


# ── chunked (resumable) upload — app.modules.m07_ingestion.chunked_upload ──────

class UploadInitIn(BaseModel):
    filename: str = Field(..., min_length=1, max_length=255, description="Original file name (its extension "
                                                                         "decides video / image).")
    size: int = Field(..., gt=0, description="File size in bytes.")


class UploadStateOut(BaseModel):
    upload_id: str = Field(description="Upload id (32 hex characters).")
    filename: str
    size: int
    chunk_size: int = Field(description="Bytes per chunk (the last one is shorter).")
    total_chunks: int
    received: list[int] = Field(description="Chunk indexes already stored.")


def _upload_error(exc: chunked_upload.UploadError):
    return api_error(exc.status, exc.code, exc.message)


def _state(up: chunked_upload.Upload) -> dict:
    return {"upload_id": up.id, "filename": up.filename, "size": up.size, "chunk_size": up.chunk_size,
            "total_chunks": up.total_chunks, "received": up.received()}


def _owned(upload_id: str, principal: Principal) -> chunked_upload.Upload:
    try:
        return chunked_upload.load(TEMP_DIR, upload_id, _actor(principal))
    except chunked_upload.UploadError as exc:
        raise _upload_error(exc)


@router.post("/uploads/", response_model=UploadStateOut, status_code=201, summary="Start a chunked upload",
             responses=errors(legacy(400, "Unsupported file type: .txt"),
                              err(413, "file_too_large", "Files are limited to 20 GB.")))
async def start_upload(body: UploadInitIn, principal: Principal = Depends(get_principal)):
    """
    Large videos are sent in chunks (each a short request, retried on its own) instead of one
    `POST /jobs/` that a slow link or a proxy timeout would cut: start here, send every chunk to
    `POST /jobs/uploads/{upload_id}/chunks/{index}`, then `POST /jobs/uploads/{upload_id}/complete`.
    Unfinished uploads are removed after 24 hours.
    """
    ext = Path(body.filename).suffix.lower()
    if ext not in IMAGE_EXTS | VIDEO_EXTS:
        raise HTTPException(400, f"Unsupported file type: {ext}")
    chunked_upload.sweep(TEMP_DIR)
    try:
        up = chunked_upload.create(TEMP_DIR, uuid.uuid4().hex, Path(body.filename).name, body.size, _actor(principal))
    except chunked_upload.UploadError as exc:
        raise _upload_error(exc)
    return _state(up)


@router.get("/uploads/{upload_id}/", response_model=UploadStateOut, summary="Get a chunked upload",
            responses=errors(err(404, "upload_not_found", "Unknown or expired upload.")))
async def get_upload(upload_id: str, principal: Principal = Depends(get_principal)):
    """The chunks already received — to resume an interrupted upload."""
    return _state(_owned(upload_id, principal))


@router.post("/uploads/{upload_id}/chunks/{index}/", response_model=UploadStateOut, summary="Send one chunk",
             responses=errors(err(404, "upload_not_found", "Unknown or expired upload."),
                              err(422, "invalid_chunk", "Chunk 3 must be 8388608 bytes (got 1200).")))
async def put_chunk(upload_id: str, index: int, request: Request, principal: Principal = Depends(get_principal)):
    """Raw bytes of chunk `index` (`application/octet-stream`, exactly `chunk_size` bytes except the
    last one). Sending a chunk again overwrites it (safe retries)."""
    up = _owned(upload_id, principal)
    if index < 0 or index >= up.total_chunks:
        raise api_error(422, "invalid_chunk", f"Chunk index must be 0–{up.total_chunks - 1}.")
    expected = up.chunk_len(index)
    body = bytearray()
    async for part in request.stream():
        body.extend(part)
        if len(body) > expected:
            raise api_error(422, "invalid_chunk", f"Chunk {index} must be {expected} bytes.")
    try:
        await asyncio.to_thread(chunked_upload.write_chunk, up, index, bytes(body))
    except chunked_upload.UploadError as exc:
        raise _upload_error(exc)
    return _state(up)


@router.post("/uploads/{upload_id}/complete/", response_model=JobOut, status_code=202,
             summary="Finish a chunked upload and create the job",
             responses=errors(err(404, "upload_not_found", "Unknown or expired upload."),
                              err(409, "upload_incomplete", "2 chunk(s) missing, first: 7."),
                              err(409, "duplicate_video", "This video was already processed (job 7d0c5b9e…).")))
async def complete_upload(
    upload_id: str, request: Request,
    models: Optional[str] = Query(None, description="As `POST /jobs/`."),
    frame_skip: Optional[int] = Query(None, ge=1, le=60, description="As `POST /jobs/`."),
    osd_layout: Optional[str] = Query(None, description="As `POST /jobs/`."),
    force: bool = Query(False, description="As `POST /jobs/`."),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """
    Creates the job from the received file — same options, duplicate check and answer as `POST /jobs/`.
    A `409 duplicate_video` keeps the upload: call again with `force=true` to process it anyway
    without sending the file again.
    """
    up = _owned(upload_id, principal)
    missing = up.missing()
    if missing:
        raise api_error(409, "upload_incomplete", f"{len(missing)} chunk(s) missing, first: {missing[0]}.",
                        missing=missing[:50])
    try:
        enabled = job_service.parse_models(models)
        osd_cfg = await get_section(db, "osd")
        layout = validate_layout_option(osd_cfg, osd_layout)  # type: ignore[arg-type]
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    def digest() -> str:
        h = fingerprint.Hasher()
        with open(up.data, "rb") as fh:
            while chunk := fh.read(1 << 20):
                h.update(chunk)
        return h.fingerprint()
    fp = await asyncio.to_thread(digest)
    previous = await _refuse_duplicate(db, request, principal, fp, up.filename, force)
    ext = Path(up.filename).suffix.lower()
    try:
        tmp_path = await asyncio.to_thread(chunked_upload.take_file, up, TEMP_DIR / f"{uuid.uuid4().hex}{ext}")
    except chunked_upload.UploadError as exc:
        raise _upload_error(exc)
    return await _create_upload_job(db, request, principal, tmp_path=tmp_path, filename=up.filename, fp=fp,
                                    enabled=enabled, frame_skip=frame_skip, layout=layout, previous=previous)


@router.post("/uploads/{upload_id}/abort/", status_code=204, summary="Abandon a chunked upload",
             responses=errors(err(404, "upload_not_found", "Unknown or expired upload.")))
async def abort_upload(upload_id: str, principal: Principal = Depends(get_principal)):
    """Deletes the chunks received so far."""
    chunked_upload.remove(_owned(upload_id, principal))


@router.get("/osd-layouts", summary="Dashcam overlay layouts")
async def list_osd_layouts(db: AsyncSession = Depends(get_db), principal: Principal = Depends(get_principal)):
    """Overlay (OSD) layouts a job can use (`osd_layout` of `POST /jobs/`): the built-in equipment
    profiles (`total`, `besafe`) and the ones of settings `osd`, each with its fields (anchors on the
    device's 0–1000 grid) and the derived OCR regions (frame ratios x0, y0, x1, y1). RRAP-05-014."""
    osd_cfg = await get_section(db, "osd")
    out = []
    for name, p in osd_cfg.available().items():  # type: ignore[attr-defined]
        spec = (osd_cfg.profiles.get(name).model_dump() if name in osd_cfg.profiles  # type: ignore[attr-defined]
                else osd_layouts.BUILTIN_PROFILES.get(name, {}))
        out.append({"name": name, "label": p.label, "builtin": p.builtin, "aliases": list(p.aliases),
                    "detect": p.detect, "timezone": p.timezone, "date_formats": list(p.date_formats),
                    "line_height": spec.get("line_height", 50), "fields": spec.get("fields", []),
                    "rois": [{"field": r.field, "box": list(r.box)} for r in p.rois]})
    return {"default_layout": osd_cfg.default_layout, "layouts": out}  # type: ignore[attr-defined]


@router.get("/", response_model=JobListOut, summary="List jobs",
            responses=errors(err(422, "invalid_filter", "Unknown status: ['running']"),
                             err(422, "invalid_sort", "Cannot sort by 'size'.")))
async def list_jobs(
    status: Optional[str] = Query(None, description="Comma list: pending,processing,done,failed,cancelled"),
    source_type: Optional[str] = Query(None, description="upload | google_drive | onedrive | stream (comma list)"),
    search: Optional[str] = Query(None, description="Filename, source name or job id"),
    date_from: Optional[datetime] = Query(None, description="Created at or after this instant (UTC)."),
    date_to: Optional[datetime] = Query(None, description="Created at or before this instant (UTC)."),
    stream_id: Optional[str] = Query(None, description="Only the segment jobs of this stream."),
    connection_id: Optional[str] = Query(None, description="Only the jobs imported from this cloud connection."),
    sort: str = Query("-created_at", description=f"Field, '-' prefix for descending: {', '.join(SORTABLE)}"),
    skip: int = Query(0, ge=0, description="Number of items to skip (offset)."),
    limit: int = Query(20, ge=1, le=200, description="Page size (max 200)."),
    db: AsyncSession = Depends(get_db),
):
    """Processing jobs of every source (upload, cloud import, stream segment) with filters, sorting and
    pagination. Each job carries its progress (0–1) and processing time."""
    conds = []
    if status:
        wanted = [s.strip() for s in status.split(",") if s.strip()]
        bad = set(wanted) - {s.value for s in JobStatus}
        if bad:
            raise api_error(422, "invalid_filter", f"Unknown status: {sorted(bad)}")
        conds.append(Job.status.in_([JobStatus(s) for s in wanted]))
    if source_type:
        conds.append(Job.source_type.in_([s.strip() for s in source_type.split(",") if s.strip()]))
    if search:
        like = f"%{search.strip()}%"
        conds.append(or_(Job.filename.ilike(like), Job.source_name.ilike(like), Job.id.ilike(like)))
    if date_from:
        conds.append(Job.created_at >= date_from)
    if date_to:
        conds.append(Job.created_at <= date_to)
    if stream_id:
        conds.append(Job.stream_id == stream_id)
    if connection_id:
        conds.append(Job.connection_id == connection_id)

    field = sort.lstrip("-")
    if field not in SORTABLE:
        raise api_error(422, "invalid_sort", f"Cannot sort by '{field}'.")
    col = SORTABLE[field]
    order = col.desc().nullslast() if sort.startswith("-") else col.asc().nullsfirst()

    total = (await db.execute(select(func.count()).select_from(Job).where(*conds))).scalar() or 0
    rows = (await db.execute(
        select(Job).where(*conds).order_by(order, Job.id).offset(skip).limit(limit)
    )).scalars().all()
    return {"total": total, "items": rows}


@router.get("/capacity", response_model=JobCapacityOut, summary="Get the processing capacity")
async def capacity(db: AsyncSession = Depends(get_db)):
    """Configured concurrency limits and the current number of running and queued jobs."""
    proc = await get_section(db, "processing")
    counts = await job_service.status_counts(db)
    return JobCapacityOut(
        max_concurrent_jobs=proc.max_concurrent_jobs,
        running=counts[JobStatus.processing.value],
        pending=counts[JobStatus.pending.value],
        worker_concurrency=settings.WORKER_CONCURRENCY,
    )


@router.get("/summary", response_model=JobSummaryOut, summary="Count jobs by status")
async def summary(db: AsyncSession = Depends(get_db)):
    """Job counts per status, overall and for jobs created in the last 24 hours."""
    return JobSummaryOut(
        by_status=await job_service.status_counts(db),
        last_24h=await job_service.status_counts(db, datetime.utcnow() - timedelta(hours=24)),
    )


@router.get("/{job_id}", response_model=JobDetailOut, summary="Get a job", responses=errors(E_NOT_FOUND))
async def get_job(job_id: JobId, db: AsyncSession = Depends(get_db)):
    """A job with its lifecycle events (newest first): `created`, `queued`, `started`, `cancel_requested`,
    `cancelled`, `failed`, `retried`, `done` …"""
    job = await _get(db, job_id)
    events = (await db.execute(
        select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.at.desc(), JobEvent.id.desc())
    )).scalars().all()
    return JobDetailOut(**JobOut.model_validate(job).model_dump(),
                        events=[JobEventOut.model_validate(e) for e in events])


@router.post("/{job_id}/cancel", response_model=JobOut, summary="Cancel a job",
             responses=errors(E_NOT_FOUND, err(
                 409, "invalid_transition", "Cannot cancel a job in status 'done'.", job_status="done")))
async def cancel_job(job_id: JobId, request: Request, db: AsyncSession = Depends(get_db),
                     principal: Principal = Depends(get_principal)):
    """
    * `pending` → `cancelled` immediately (the queued task is revoked);
    * `processing` → `cancel_requested` is set and the worker stops at its next check, keeping the
      detections found so far (the job then becomes `cancelled`).

    Refused with `409 invalid_transition` for `done`/`failed`/`cancelled` jobs or when a cancel was
    already requested. Audited as `job.cancel` (refusals too, as failures).
    """
    job = await _get(db, job_id)
    plan = job_service.cancel_plan(job.status)
    if plan is None or job.cancel_requested:
        await _refuse(request, principal, "cancel", job, "invalid_transition",
                      f"Cannot cancel a job in status '{job.status.value}'"
                      + (" (cancel already requested)." if job.cancel_requested else "."))
    actor = _actor(principal)
    if plan == "revoke":
        if job.celery_task_id:
            try:
                from app.workers.celery_app import celery_app
                celery_app.control.revoke(job.celery_task_id)
            except Exception:  # broker down — the worker also checks the status before starting
                pass
        job.cancelled_by = actor
        job_service.mark_cancelled(db, job, actor)
    else:
        job.cancel_requested = True
        job.cancelled_by = actor
        job_service.record_event(db, job.id, "cancel_requested", actor)
    audit.record(db, request, "job.cancel", principal=principal, object_type="job", object_id=job.id,
                 details={"from": "pending" if plan == "revoke" else "processing", "mode": plan})
    await db.commit()
    await db.refresh(job)
    return job


@router.post("/{job_id}/retry", response_model=JobOut, summary="Retry a failed or cancelled job",
             responses=errors(E_NOT_FOUND,
                              err(409, "invalid_transition",
                                  "Only failed or cancelled jobs can be retried (status 'done').", job_status="done"),
                              err(409, "retry_limit", "This job already used its 3 attempt(s).", job_status="failed"),
                              err(409, "has_reviewed_detections",
                                  "Detections of this job were already reviewed; retrying would discard them.",
                                  job_status="failed"),
                              err(409, "source_unavailable", "The source media of this job is no longer available.",
                                  job_status="failed")))
async def retry_job(job_id: JobId, request: Request, db: AsyncSession = Depends(get_db),
                    principal: Principal = Depends(get_principal)):
    """
    Run a `failed` or `cancelled` job again: its previous detections, track and outputs are purged,
    `attempts` is incremented and the job is queued again (`pending`). Conditions, checked in this
    order: status `failed`/`cancelled`, `attempts < max_attempts`, no detection of the job already
    reviewed by a human, and the source media still available (retained file, or re-downloadable from
    an active cloud connection). Audited as `job.retry` (refusals too).
    """
    job = await _get(db, job_id)
    conn = await db.get(CloudConnection, job.connection_id) if job.connection_id else None
    refusal = job_service.retry_refusal(
        job.status, job.attempts, job.max_attempts,
        has_reviewed=await job_service.has_reviewed_detections(db, job.id),
        media_available=job_service.media_available(job, job_service.connection_usable(conn)),
    )
    if refusal:
        await _refuse(request, principal, "retry", job, refusal.code, refusal.message)

    actor = _actor(principal)
    previous = {"status": job.status.value, "attempt": job.attempts, "error": job.error}
    removed = await job_service.purge_outputs(db, job.id)
    job_service.reset_for_retry(job)
    job_service.record_event(db, job.id, "retried", actor,
                             {**previous, "attempt": job.attempts, "detections_removed": removed})
    audit.record(db, request, "job.retry", principal=principal, object_type="job", object_id=job.id,
                 details={**previous, "new_attempt": job.attempts, "detections_removed": removed})
    await job_service.enqueue_and_commit(db, job, actor)
    await db.refresh(job)
    return job


@router.delete("/{job_id}", status_code=204, summary="Delete a job and its data",
               responses=errors(E_NOT_FOUND, err(
                   409, "job_processing", "Cancel the job and wait for it to stop before deleting it.",
                   job_status="processing")))
async def delete_job(job_id: JobId, request: Request, db: AsyncSession = Depends(get_db),
                     principal: Principal = Depends(get_principal)):
    """
    Delete a job and all its related data — detections with their labels and review history, the
    vehicle track, events and the retained media; hazards that lost members are recomputed (or
    deleted when empty). A `pending` job's queued task is revoked. Refused while `processing`
    (`409 job_processing`): cancel it first. Audited as `job.delete`.
    """
    job = await _get(db, job_id)
    if job.status == JobStatus.processing:
        await _refuse(request, principal, "delete", job, "job_processing",
                      "Cancel the job and wait for it to stop before deleting it.")
    if job.status == JobStatus.pending and job.celery_task_id:
        try:
            from app.workers.celery_app import celery_app
            celery_app.control.revoke(job.celery_task_id)
        except Exception:
            pass
    removed = await job_service.purge_outputs(db, job_id)
    job_service.delete_media(job)
    audit.record(db, request, "job.delete", principal=principal, object_type="job", object_id=job.id,
                 details={"filename": job.filename, "status": job.status.value,
                          "source_type": job.source_type, "detections_removed": removed})
    await db.execute(JobEvent.__table__.delete().where(JobEvent.job_id == job_id))
    await db.delete(job)
    await db.commit()
