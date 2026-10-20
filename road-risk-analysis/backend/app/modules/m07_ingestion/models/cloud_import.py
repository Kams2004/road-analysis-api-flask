"""
Cloud imports (RRAP-07-002/004): files picked in a connected drive (or found
by the watched-folder sync) and pulled into the processing pipeline.

One batch per "Import" click (or per sync run), one item per file. An item
goes queued → downloading → uploading_to_storage → queued_for_processing,
then follows its job (processing → done | failed | cancelled). Live byte
counters live in Redis (app.modules.m07_ingestion.cloud_import); the row is written at
phase changes and progress milestones.
"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db.session import Base
from app.modules.m07_ingestion.models import (  # noqa: F401 — register FK targets
    cloud_connection as _cc, job as _job,
)


class ImportItemStatus(str, enum.Enum):
    queued                = "queued"
    downloading           = "downloading"
    uploading_to_storage  = "uploading_to_storage"
    queued_for_processing = "queued_for_processing"
    processing            = "processing"
    done                  = "done"
    failed                = "failed"
    skipped_duplicate     = "skipped_duplicate"
    cancelled             = "cancelled"


class CloudImportBatch(Base):
    __tablename__ = "cloud_import_batches"

    id:              Mapped[str]             = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    connection_id:   Mapped[str | None]      = mapped_column(
        String, ForeignKey("cloud_connections.id", ondelete="SET NULL"), nullable=True, index=True)
    provider:        Mapped[str]             = mapped_column(String)
    connection_name: Mapped[str | None]      = mapped_column(String, nullable=True)
    origin:          Mapped[str]             = mapped_column(String, default="manual")   # manual | sync
    models:          Mapped[str | None]      = mapped_column(String, nullable=True)      # comma list
    frame_skip:      Mapped[int | None]      = mapped_column(Integer, nullable=True)
    options:         Mapped[dict | None]     = mapped_column(JSON, nullable=True)
    item_count:      Mapped[int]             = mapped_column(Integer, default=0)
    cancel_requested: Mapped[bool]           = mapped_column(Boolean, default=False)
    created_by:      Mapped[str | None]      = mapped_column(String, nullable=True)
    created_at:      Mapped[datetime]        = mapped_column(DateTime, default=datetime.utcnow, index=True)
    finished_at:     Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CloudImportItem(Base):
    __tablename__ = "cloud_import_items"

    id:            Mapped[str]             = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    batch_id:      Mapped[str]             = mapped_column(
        String, ForeignKey("cloud_import_batches.id", ondelete="CASCADE"), index=True)
    connection_id: Mapped[str | None]      = mapped_column(
        String, ForeignKey("cloud_connections.id", ondelete="SET NULL"), nullable=True, index=True)
    file_id:       Mapped[str]             = mapped_column(String, index=True)   # provider file id
    version:       Mapped[str | None]      = mapped_column(String, nullable=True)  # checksum / etag / modified
    name:          Mapped[str]             = mapped_column(String)
    path:          Mapped[str | None]      = mapped_column(String, nullable=True)  # folder path (display)
    mime_type:     Mapped[str | None]      = mapped_column(String, nullable=True)
    checksum:      Mapped[str | None]      = mapped_column(String, nullable=True)  # md5 when the provider gives one
    modified_at:   Mapped[str | None]      = mapped_column(String, nullable=True)
    status:        Mapped[str]             = mapped_column(String, default=ImportItemStatus.queued.value, index=True)
    bytes_total:   Mapped[int | None]      = mapped_column(BigInteger, nullable=True)
    bytes_done:    Mapped[int]             = mapped_column(BigInteger, default=0)
    speed_bps:     Mapped[float | None]    = mapped_column(Float, nullable=True)
    attempts:      Mapped[int]             = mapped_column(Integer, default=0)
    error:         Mapped[str | None]      = mapped_column(String, nullable=True)
    dest_path:     Mapped[str | None]      = mapped_column(String, nullable=True)
    job_id:        Mapped[str | None]      = mapped_column(
        String, ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True, index=True)
    duplicate_of_job_id: Mapped[str | None] = mapped_column(String, nullable=True)
    celery_task_id: Mapped[str | None]     = mapped_column(String, nullable=True)
    created_at:    Mapped[datetime]        = mapped_column(DateTime, default=datetime.utcnow)
    started_at:    Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at:   Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at:    Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
