import uuid
from datetime import datetime
from sqlalchemy import String, Float, DateTime, Integer, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from app.db.session import Base


# severity_score unified scale:
#   potholes  — 0: not pothole  | 1: minor | 2: moderate | 3: severe
#   signs     — 0: not a sign   | 1: confirmed
#   speedbump — 0: not a bump   | 1: confirmed
SEVERITY_MIN = 0
SEVERITY_MAX = 3


class ValidationLabel(Base):
    """
    Human-confirmed label for a detection, written at review time.
    One row per reviewed detection.

    - detection_type  : pothole | traffic_sign | speed_bump
    - label           : confirmed class, e.g. "pothole", "not_pothole",
                        "traffic_light", "stop_sign", "speed_bump", "not_speed_bump"
    - severity_score  : 0-3 integer (see scale above)
    - model_confidence: confidence score from the detection model (0-1)
    - crop_url        : MinIO path to the clean (unannotated) cropped image
                        used as training sample
    """
    __tablename__ = "validation_labels"

    id:               Mapped[str]   = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    detection_id:     Mapped[str]   = mapped_column(String, ForeignKey("detections.id"), unique=True)
    detection_type:   Mapped[str]   = mapped_column(String)
    label:            Mapped[str]   = mapped_column(String)
    severity_score:   Mapped[int]   = mapped_column(Integer, default=0)            # 0-3
    model_confidence: Mapped[float] = mapped_column(Float)
    crop_url:         Mapped[str | None] = mapped_column(String, nullable=True)
    labeled_by:       Mapped[str]   = mapped_column(String)
    labeled_at:       Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
