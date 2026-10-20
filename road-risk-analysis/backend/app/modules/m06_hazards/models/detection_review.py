"""
History of every workflow transition of a detection (RRAP-06-018): who
moved it from which status to which, and why. Written by
app.modules.m06_hazards.detection_workflow alongside the matching audit_logs row.
"""
import uuid
from datetime import datetime
from sqlalchemy import String, DateTime, JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db.session import Base


class DetectionReview(Base):
    __tablename__ = "detection_reviews"

    id:           Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    detection_id: Mapped[str] = mapped_column(
        String, ForeignKey("detections.id", ondelete="CASCADE"), index=True)
    from_status:  Mapped[str | None] = mapped_column(String, nullable=True)   # None on creation
    to_status:    Mapped[str]        = mapped_column(String)
    # accept | correct | reject | reopen | location_fix | auto_reject | auto_accept | resolve | unresolve | create
    action:       Mapped[str]        = mapped_column(String)
    actor_type:   Mapped[str]        = mapped_column(String)                  # user | system
    actor:        Mapped[str | None] = mapped_column(String, nullable=True)   # e-mail or "system"
    note:         Mapped[str | None] = mapped_column(String, nullable=True)
    details:      Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at:   Mapped[datetime]   = mapped_column(DateTime, default=datetime.utcnow)
