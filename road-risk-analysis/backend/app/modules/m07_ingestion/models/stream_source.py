"""
A live camera stream (RTSP/RTMP/HTTP/HLS) captured in fixed-length segments,
each processed as a regular job (RRAP-07-003/011).
"""
import uuid
import enum
from datetime import datetime
from sqlalchemy import String, DateTime, Integer, JSON
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db.session import Base


class StreamStatus(str, enum.Enum):
    active = "active"
    paused = "paused"
    error  = "error"


class StreamSource(Base):
    __tablename__ = "stream_sources"

    id:                 Mapped[str]             = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name:               Mapped[str]             = mapped_column(String)
    url:                Mapped[str]             = mapped_column(String)
    camera_type:        Mapped[str | None]      = mapped_column(String, nullable=True)
    status:             Mapped[str]             = mapped_column(String, default=StreamStatus.paused.value)
    segment_seconds:    Mapped[int]             = mapped_column(Integer, default=60)
    frame_skip:         Mapped[int | None]      = mapped_column(Integer, nullable=True)  # None = processing default for streams
    enabled_models:     Mapped[list]            = mapped_column(JSON, default=list)
    last_segment_at:    Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error:         Mapped[str | None]      = mapped_column(String, nullable=True)
    segments_processed: Mapped[int]             = mapped_column(Integer, default=0)
    created_by:         Mapped[str | None]      = mapped_column(String, nullable=True)
    created_at:         Mapped[datetime]        = mapped_column(DateTime, default=datetime.utcnow)
