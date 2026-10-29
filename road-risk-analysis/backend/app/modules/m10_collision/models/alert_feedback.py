import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db.session import Base


class AlertFeedback(Base):
    """
    A driver's verdict on one collision alert (POST /alerts/{conflict_id}/feedback):
    `false_alert` or `useful`. One row per conflict × vehicle — answering again replaces the
    verdict (idempotent). The alert itself is ephemeral (Redis), so the level and context it had
    when answered are copied here; GET /collision/metrics derives the false-alert rate from it.
    """
    __tablename__ = "collision_alert_feedback"
    __table_args__ = (
        UniqueConstraint("conflict_id", "vehicle_id", name="uq_collision_feedback_conflict_vehicle"),
        Index("ix_collision_feedback_updated_at", "updated_at"),
    )

    id:          Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    conflict_id: Mapped[str] = mapped_column(String)
    vehicle_id:  Mapped[str] = mapped_column(String)
    verdict:     Mapped[str] = mapped_column(String)                 # false_alert | useful
    comment:     Mapped[str | None] = mapped_column(Text, nullable=True)
    level:            Mapped[str | None]   = mapped_column(String, nullable=True)   # alert level when answered
    physical_level:   Mapped[str | None]   = mapped_column(String, nullable=True)
    context_index:    Mapped[float | None] = mapped_column(Float, nullable=True)
    context_level_boost: Mapped[int | None] = mapped_column(Integer, nullable=True)
    other_vehicle_id: Mapped[str | None]   = mapped_column(String, nullable=True)
    submitted_by:     Mapped[str | None]   = mapped_column(String, nullable=True)   # API client / user
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
