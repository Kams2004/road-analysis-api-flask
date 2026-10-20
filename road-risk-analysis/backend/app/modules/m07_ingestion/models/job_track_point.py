"""
Vehicle trajectory of a processed video (RRAP-06-014): every valid OCR GPS
fix the processor read, so a validator can place a detection whose own fix
was missing on the actual path driven.
"""
from datetime import datetime

from sqlalchemy import String, Float, Integer, ForeignKey, DateTime
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db.session import Base


class JobTrackPoint(Base):
    __tablename__ = "job_track_points"

    id:           Mapped[int]   = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id:       Mapped[str]   = mapped_column(String, ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    frame_number: Mapped[int]   = mapped_column(Integer)
    video_time_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    latitude:     Mapped[float] = mapped_column(Float)
    longitude:    Mapped[float] = mapped_column(Float)
    speed_kmh:    Mapped[float | None] = mapped_column(Float, nullable=True)
    source:       Mapped[str]   = mapped_column(String, default="ocr")
    # Recording time of the fix (timezone-aware): the frame's OSD date-time, else
    # extrapolated from the video's nearest OSD time (as detections.recorded_at).
    recorded_at:  Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
