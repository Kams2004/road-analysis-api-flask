"""
Processing-job lifecycle (RRAP-07-005 … 07-014).

    pending ──start──▶ processing ──▶ done
       │                  │  └──────▶ failed  ──retry──▶ pending
       │                  └─cancel──▶ cancelled ─retry─▶ pending
       └──────cancel────────────────▶ cancelled

Pure rules (frame sampling, cancel/retry decisions, computed metrics) are
plain functions so they are unit-tested without a database; the async
helpers below apply them, write `job_events` and enqueue Celery work. User
actions are audited by the routes (app/modules/m07_ingestion/routes/jobs.py).
"""
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Optional

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.m06_hazards.models.detection import Detection, ReviewStatus
from app.modules.m06_hazards.models.detection_review import DetectionReview
from app.modules.m07_ingestion.models.job import CLOUD_SOURCES, Job, JobStatus
from app.modules.m07_ingestion.models.job_event import JobEvent
from app.modules.m07_ingestion.models.job_track_point import JobTrackPoint
from app.modules.m06_hazards.models.validation_label import ValidationLabel
from app.modules.m06_hazards import hazard_service
from app.core.settings_service import ProcessingSettings

logger = logging.getLogger(__name__)

SYSTEM = "system"
CANCEL_CHECK_EVERY = 25          # frames between two cancel_requested checks
TRANSIENT_RETRIES = 2            # worker-side retries of an unexpected error before `failed`
UNREVIEWED = (ReviewStatus.pending.value, ReviewStatus.location_pending.value)
VALID_MODELS = ("pothole", "signs", "speedbump", "vehicles")
# Models run when a job / stream / cloud sync does not name any: the parked
# vehicle model (`vehicles`) is opt-in — it must be requested explicitly.
DEFAULT_MODELS = ("pothole", "signs", "speedbump")
VIDEO_EXTS = (".mp4", ".avi", ".mov", ".mkv")


class PermanentJobError(Exception):
    """A failure retrying cannot fix (unreadable media, missing source)."""


# ── pure rules ────────────────────────────────────────────────────────────────

def _status(job_or_status) -> str:
    st = getattr(job_or_status, "status", job_or_status)
    return st.value if isinstance(st, JobStatus) else st


def resolve_frame_skip(proc: ProcessingSettings, source_type: str, override: Optional[int] = None) -> int:
    """Sampling step of a new job: explicit override > per-source > default."""
    if override is not None:
        return int(override)
    return int(proc.frame_skip_by_source.get(source_type, proc.frame_skip_default))


def model_skips(enabled: Iterable[str], job_skip: int, by_model: dict[str, int]) -> dict[str, int]:
    """Per-model step: a model configured in frame_skip_by_model uses its own
    value, every other model the job's sampling."""
    return {m: max(1, int(by_model.get(m, job_skip) or job_skip)) for m in enabled}


def models_for_frame(frame_index: int, skips: dict[str, int]) -> list[str]:
    """Models to run on the zero-based `frame_index` (frame_index % skip == 0)."""
    return [m for m, step in skips.items() if frame_index % step == 0]


def cancel_plan(status) -> Optional[str]:
    """pending → "revoke" (cancel now), processing → "flag" (the worker stops
    at its next check), anything final → None (409 invalid_transition)."""
    st = _status(status)
    if st == JobStatus.pending.value:
        return "revoke"
    if st == JobStatus.processing.value:
        return "flag"
    return None


@dataclass
class Refusal:
    code: str
    message: str


def retry_refusal(status, attempts: int, max_attempts: int, *, has_reviewed: bool,
                  media_available: bool) -> Optional[Refusal]:
    """None when the job may be retried, else why not (409 code)."""
    st = _status(status)
    if st not in (JobStatus.failed.value, JobStatus.cancelled.value):
        return Refusal("invalid_transition", f"Only failed or cancelled jobs can be retried (status '{st}').")
    if (attempts or 1) >= (max_attempts or 1):
        return Refusal("retry_limit", f"This job already used its {max_attempts} attempt(s).")
    if has_reviewed:
        return Refusal("has_reviewed_detections",
                       "Detections of this job were already reviewed; retrying would discard them.")
    if not media_available:
        return Refusal("source_unavailable", "The source media of this job is no longer available.")
    return None


def parse_models(raw: Optional[str]) -> list[str]:
    """Comma list → sorted known models; raises ValueError on unknown names."""
    if raw is None:
        return list(DEFAULT_MODELS)
    requested = {m.strip().lower() for m in raw.split(",") if m.strip()}
    unknown = requested - set(VALID_MODELS)
    if unknown:
        raise ValueError(f"Unknown model(s): {sorted(unknown)}. Valid options: {sorted(VALID_MODELS)}")
    if not requested:
        raise ValueError("models parameter is empty.")
    return sorted(requested)


def connection_usable(conn) -> bool:
    if conn is not None and getattr(conn, "provider", None) == "sftp":
        # No OAuth: usable while enabled; a failed listing / sync does not block imports
        # (the server may simply be back) — the download reports its own error.
        return bool(conn.enabled and conn.status != "disabled" and conn.sftp_host
                    and (conn.sftp_password_key or getattr(conn, "sftp_password_enc", None)))
    return bool(conn is not None and conn.enabled and conn.status == "active" and conn.refresh_token_enc)


def media_available(job: Job, connection_usable: bool = False) -> bool:
    """Local media still retained, or re-downloadable from an active cloud connection."""
    if job.source_path and os.path.exists(job.source_path):
        return True
    return job.source_type in CLOUD_SOURCES and bool(job.source_ref) and connection_usable


def processing_seconds(job, now: Optional[datetime] = None) -> Optional[float]:
    if not getattr(job, "started_at", None):
        return None
    end = job.finished_at or job.cancelled_at
    if end is None or end < job.started_at:
        end = None
    if end is None:
        if _status(job) != JobStatus.processing.value:
            return None
        end = now or datetime.utcnow()
    return round((end - job.started_at).total_seconds(), 1)


def progress(job) -> float:
    if _status(job) == JobStatus.done.value:
        return 1.0
    total = job.total_frames or 0
    if total <= 0:
        return 0.0
    return round(min(1.0, (job.processed or 0) / total), 4)


def readable_error(exc: BaseException) -> str:
    msg = str(exc).strip() or type(exc).__name__
    if not isinstance(exc, PermanentJobError):
        msg = f"{type(exc).__name__}: {msg}"
    return msg[:500]


def reset_for_retry(job: Job) -> None:
    """Counters and outcome fields back to a fresh pending job; attempts+1."""
    job.status = JobStatus.pending
    job.attempts = (job.attempts or 1) + 1
    job.total_frames = job.processed = job.detections = 0
    job.error = None
    job.started_at = job.finished_at = job.cancelled_at = None
    job.cancelled_by = None
    job.cancel_requested = False
    job.avg_confidence = None
    job.detections_by_model = None


# ── database helpers ─────────────────────────────────────────────────────────

def record_event(db: AsyncSession, job_id: str, event: str, actor: Optional[str] = SYSTEM,
                 details: Optional[dict[str, Any]] = None) -> None:
    db.add(JobEvent(job_id=job_id, event=event, actor=actor or SYSTEM, details=details))


async def has_reviewed_detections(db: AsyncSession, job_id: str) -> bool:
    """True when a human already acted on a detection of this job: one is in
    a state other than pending/location_pending without being an automatic
    (system) decision, or a user transition exists in its review history."""
    moved = (await db.execute(
        select(Detection.id).where(
            Detection.job_id == job_id,
            Detection.review_status.notin_(UNREVIEWED),
            or_(Detection.reviewed_by.is_(None), Detection.reviewed_by != SYSTEM),
        ).limit(1)
    )).first()
    if moved:
        return True
    human = (await db.execute(
        select(DetectionReview.id).join(Detection, Detection.id == DetectionReview.detection_id)
        .where(Detection.job_id == job_id, DetectionReview.actor_type == "user").limit(1)
    )).first()
    return human is not None


async def purge_outputs(db: AsyncSession, job_id: str) -> int:
    """Delete every detection (with labels/history) and the track of a job,
    then recompute the hazards that lost members. Returns detections removed."""
    det_ids = (await db.execute(select(Detection.id).where(Detection.job_id == job_id))).scalars().all()
    hazard_ids = (await db.execute(
        select(Detection.hazard_id).where(Detection.job_id == job_id, Detection.hazard_id.isnot(None))
    )).scalars().all()
    if det_ids:
        await db.execute(delete(ValidationLabel).where(ValidationLabel.detection_id.in_(det_ids)))
        await db.execute(delete(DetectionReview).where(DetectionReview.detection_id.in_(det_ids)))
        await db.execute(delete(Detection).where(Detection.job_id == job_id))
    await db.execute(delete(JobTrackPoint).where(JobTrackPoint.job_id == job_id))
    await hazard_service.recompute_ids(db, hazard_ids)
    return len(det_ids)


def delete_media(job: Job) -> None:
    if job.source_path:
        try:
            os.unlink(job.source_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning(f"[Job {job.id}] could not delete media {job.source_path}: {exc}")
        job.source_path = None


def mark_failed(db: AsyncSession, job: Job, error: str, actor: str = SYSTEM) -> None:
    job.status = JobStatus.failed
    job.error = error[:500]
    job.finished_at = datetime.utcnow()
    record_event(db, job.id, "failed", actor, {"error": job.error, "attempt": job.attempts})


def mark_cancelled(db: AsyncSession, job: Job, actor: Optional[str], details: Optional[dict] = None) -> None:
    now = datetime.utcnow()
    job.status = JobStatus.cancelled
    job.cancelled_at = now
    job.finished_at = now
    job.cancelled_by = job.cancelled_by or actor or SYSTEM
    record_event(db, job.id, "cancelled", actor or job.cancelled_by, details)


async def new_job(db: AsyncSession, *, filename: str, source_type: str, enabled: list[str], source_path: str,
                  submitted_by: Optional[str], frame_skip_override: Optional[int] = None,
                  proc: Optional[ProcessingSettings] = None, **source) -> Job:
    """Create a pending job and its `created` event (caller commits, then enqueue())."""
    from app.core.settings_service import get_section
    proc = proc or await get_section(db, "processing")  # type: ignore[assignment]
    job = Job(
        id=str(uuid.uuid4()), filename=filename, enabled_models=",".join(enabled),
        source_type=source_type, source_path=source_path, submitted_by=submitted_by,
        frame_skip=resolve_frame_skip(proc, source_type, frame_skip_override),
        attempts=1, max_attempts=proc.max_attempts, status=JobStatus.pending,
        total_frames=0, processed=0, detections=0, cancel_requested=False, **source,
    )
    db.add(job)
    record_event(db, job.id, "created", submitted_by,
                 {"source_type": source_type, "frame_skip": job.frame_skip, "models": enabled})
    return job


def enqueue(job: Job) -> str:
    """Send the job to the processing queue under a fresh task id (stored on
    the job — the caller commits). Raises when the broker is unreachable."""
    from app.workers.job_tasks import process_video
    task_id = str(uuid.uuid4())
    job.celery_task_id = task_id
    process_video.apply_async(args=[job.id], queue="processing", task_id=task_id)
    return task_id


def schedule_hazard_lifecycle(job_id: str) -> bool:
    """Job completion hook (status `done`): queue the hazard lifecycle of the
    job — re-sightings and "not seen" video passes
    (app.modules.m06_hazards.lifecycle.video_pass). Never raises: when the
    broker is down, the periodic hazard-lifecycle sweep picks the job up
    (it has no `hazard_lifecycle` event yet)."""
    try:
        from app.workers.hazard_tasks import evaluate_job_hazards
        evaluate_job_hazards.apply_async(args=[job_id], queue="processing")
        return True
    except Exception as exc:
        logger.warning(f"[Job {job_id}] could not queue the hazard lifecycle ({exc}) — left to the sweep")
        return False


async def enqueue_and_commit(db: AsyncSession, job: Job, actor: Optional[str] = SYSTEM) -> None:
    """Commit the job, enqueue it, then record `queued` (or `failed` when the
    broker is down — the job must never sit in pending with no task)."""
    await db.commit()
    try:
        task_id = enqueue(job)
        record_event(db, job.id, "queued", actor, {"task_id": task_id, "attempt": job.attempts})
    except Exception as exc:  # broker unreachable
        logger.error(f"[Job {job.id}] enqueue failed: {exc}")
        mark_failed(db, job, f"Could not queue the job: {exc}")
    await db.commit()


async def status_counts(db: AsyncSession, since: Optional[datetime] = None) -> dict[str, int]:
    q = select(Job.status, func.count()).group_by(Job.status)
    if since is not None:
        q = q.where(Job.created_at >= since)
    rows = (await db.execute(q)).all()
    counts = {s.value: 0 for s in JobStatus}
    for st, n in rows:
        counts[_status(st)] = n
    return counts
