import uuid
from datetime import datetime
from sqlalchemy import String, DateTime, Integer, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column
from app.db.session import Base
import enum


class JobStatus(str, enum.Enum):
    pending    = "pending"
    processing = "processing"
    done       = "done"
    failed     = "failed"


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
