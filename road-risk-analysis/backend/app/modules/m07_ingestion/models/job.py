import uuid
from datetime import datetime
from sqlalchemy import String, DateTime, Integer, Float, Boolean, JSON, ForeignKey, Enum as SAEnum
from sqlalchemy.orm import Mapped, deferred, mapped_column
from app.core.db.session import Base
from app.modules.m07_ingestion.models import (  # noqa: F401 — register FK targets
    cloud_connection as _cc, stream_source as _ss,
)
import enum


class JobStatus(str, enum.Enum):
    """Lifecycle (RRAP-07-005/012) — transitions live in app.modules.m07_ingestion.job_service."""
    pending    = "pending"
    processing = "processing"
    done       = "done"
    failed     = "failed"
    cancelled  = "cancelled"


# Plain string in the DB (not a native enum) so new sources never need a migration.
class SourceType(str, enum.Enum):
    upload       = "upload"
    google_drive = "google_drive"
    onedrive     = "onedrive"
    sftp         = "sftp"
    stream       = "stream"


CLOUD_SOURCES = (SourceType.google_drive.value, SourceType.onedrive.value, SourceType.sftp.value)
FINAL_STATUSES = (JobStatus.done, JobStatus.failed, JobStatus.cancelled)


class Job(Base):
    __tablename__ = "jobs"

    id:             Mapped[str]          = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    filename:       Mapped[str]          = mapped_column(String)
    status:         Mapped[JobStatus]    = mapped_column(SAEnum(JobStatus), default=JobStatus.pending)
    enabled_models: Mapped[str | None]   = mapped_column(String, nullable=True)  # comma-separated, e.g. "pothole,signs"
    total_frames:   Mapped[int]          = mapped_column(Integer, default=0)
    processed:      Mapped[int]          = mapped_column(Integer, default=0)
    detections:     Mapped[int]          = mapped_column(Integer, default=0)
    error:          Mapped[str | None]   = mapped_column(String, nullable=True)
    created_at:     Mapped[datetime]     = mapped_column(DateTime, default=datetime.utcnow)
    finished_at:    Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Module 7 — where the media came from (RRAP-07-002/003/004)
    source_type:    Mapped[str]          = mapped_column(String, default=SourceType.upload.value)
    source_ref:     Mapped[str | None]   = mapped_column(String, nullable=True)   # cloud file id / stream id
    source_name:    Mapped[str | None]   = mapped_column(String, nullable=True)   # display name
    connection_id:  Mapped[str | None]   = mapped_column(
        String, ForeignKey("cloud_connections.id", ondelete="SET NULL"), nullable=True, index=True)
    stream_id:      Mapped[str | None]   = mapped_column(
        String, ForeignKey("stream_sources.id", ondelete="SET NULL"), nullable=True, index=True)
    source_path:    Mapped[str | None]   = mapped_column(String, nullable=True)   # local media while retained
    submitted_by:   Mapped[str | None]   = mapped_column(String, nullable=True)   # e-mail or "system"

    # Lifecycle (RRAP-07-010/012)
    started_at:       Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancel_requested: Mapped[bool]            = mapped_column(Boolean, default=False)
    cancelled_at:     Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancelled_by:     Mapped[str | None]      = mapped_column(String, nullable=True)
    attempts:         Mapped[int]             = mapped_column(Integer, default=1)
    max_attempts:     Mapped[int]             = mapped_column(Integer, default=3)
    celery_task_id:   Mapped[str | None]      = mapped_column(String, nullable=True)

    # Metrics (RRAP-07-006/013)
    avg_confidence:      Mapped[float | None] = mapped_column(Float, nullable=True)
    frame_skip:          Mapped[int | None]   = mapped_column(Integer, nullable=True)   # sampling actually used
    detections_by_model: Mapped[dict | None]  = mapped_column(JSON, nullable=True)      # {"pothole": 3, …}
    fps:                 Mapped[float | None] = mapped_column(Float, nullable=True)
    duration_s:          Mapped[float | None] = mapped_column(Float, nullable=True)     # video length

    # Content fingerprint of the media ("sha256:<hex>", app.modules.m07_ingestion.fingerprint):
    # the same video processed twice is refused (409 duplicate_video) unless forced.
    # Deferred: never part of a plain SELECT, so code that does not use it keeps
    # working on a database whose startup upgrade has not added the column yet.
    content_fingerprint: Mapped[str | None] = deferred(mapped_column(String, nullable=True))
