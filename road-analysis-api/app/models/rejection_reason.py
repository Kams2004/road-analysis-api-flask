import uuid
from datetime import datetime
from sqlalchemy import String, DateTime, Boolean
from sqlalchemy.orm import Mapped, mapped_column
from app.db.session import Base


class RejectionReason(Base):
    """
    Library of rejection reasons, scoped per detection_type.
    Built-in reasons are seeded at startup (is_custom=False).
    User-added reasons have is_custom=True.
    detection_type = "all" means the reason applies to every type.
    """
    __tablename__ = "rejection_reasons"

    id:             Mapped[str]      = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    detection_type: Mapped[str]      = mapped_column(String, index=True)   # pothole | traffic_sign | speed_bump | all
    code:           Mapped[str]      = mapped_column(String)                # machine key e.g. "not_road_side"
    label:          Mapped[str]      = mapped_column(String)                # human label e.g. "Not road-side"
    description:    Mapped[str]      = mapped_column(String)
    is_custom:      Mapped[bool]     = mapped_column(Boolean, default=False)
    created_at:     Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
