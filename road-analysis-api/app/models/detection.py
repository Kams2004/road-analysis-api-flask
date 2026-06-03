import uuid
import enum
from datetime import datetime
from sqlalchemy import String, Float, DateTime, Integer, Boolean, ForeignKey, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column
from app.db.session import Base


class ReviewStatus(str, enum.Enum):
    pending   = "pending"
    validated = "validated"
    rejected  = "rejected"


class Detection(Base):
    __tablename__ = "detections"

    id:           Mapped[str]   = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    job_id:       Mapped[str]   = mapped_column(String, ForeignKey("jobs.id"))

    # Detection info
    type:         Mapped[str]        = mapped_column(String)
    subtype:      Mapped[str | None] = mapped_column(String, nullable=True)
    confidence:   Mapped[float]      = mapped_column(Float)
    frame_number: Mapped[int]        = mapped_column(Integer)

    # Raw OCR text from GPS ROI — always saved as-is, even if incomplete
    raw_gps_text: Mapped[str | None] = mapped_column(String, nullable=True)

    # Transformed decimal coordinates — None when raw_gps_text was unreadable
    # Can be corrected later via PATCH /{id}/location
    latitude:      Mapped[float | None]    = mapped_column(Float, nullable=True)
    longitude:     Mapped[float | None]    = mapped_column(Float, nullable=True)
    gps_interpolated: Mapped[bool]         = mapped_column(Boolean, default=False)  # True = from voter window

    # Other OSD fields
    speed_kmh:     Mapped[float | None]    = mapped_column(Float, nullable=True)
    vehicle_id:    Mapped[str | None]      = mapped_column(String, nullable=True)
    captured_at:   Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    location_name: Mapped[str | None]      = mapped_column(String, nullable=True)
    rpm:           Mapped[int | None]      = mapped_column(Integer, nullable=True)

    # MinIO
    image_url:        Mapped[str | None] = mapped_column(String, nullable=True)
    crop_url:         Mapped[str | None] = mapped_column(String, nullable=True)  # clean unannotated crop — training sample
    context_clip_url: Mapped[str | None] = mapped_column(String, nullable=True)  # ±3s clip, only when GPS was missing

    # Review — cannot be set to validated while latitude is None
    review_status: Mapped[ReviewStatus]    = mapped_column(SAEnum(ReviewStatus), default=ReviewStatus.pending)
    reviewed_by:   Mapped[str | None]      = mapped_column(String, nullable=True)
    reviewed_at:   Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    review_note:   Mapped[str | None]      = mapped_column(String, nullable=True)

    created_at:   Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
