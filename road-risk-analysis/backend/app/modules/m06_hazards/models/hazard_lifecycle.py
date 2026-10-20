"""
Hazard lifecycle history (app.modules.m06_hazards.lifecycle).

hazard_events — the timeline of a hazard: every sighting (`seen`), valid
video pass without it (`not_seen`), counted driver vote, expiry, resolution
and reopening. `recorded_at` is when the evidence happened (video recording
time, vote time), `created_at` when the row was written.

hazard_votes — every driver vote, counted or not (`reason` too_far /
cooldown when it was refused), so administrators can spot false testifiers.
"""
import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db.session import Base
from app.modules.m06_hazards.models import hazard as _hazard  # noqa: F401 — registers the hazards FK target

EVENT_KINDS = ("seen", "not_seen", "vote_still_there", "vote_not_there", "expired", "resolved", "reopened")
VOTE_VALUES = ("still_there", "not_there")


class HazardEvent(Base):
    __tablename__ = "hazard_events"
    __table_args__ = (
        Index("ix_hazard_events_hazard_id_created_at", "hazard_id", "created_at"),
        Index("ix_hazard_events_job_id", "job_id"),
        Index("ix_hazard_events_detection_id", "detection_id"),
    )

    id:           Mapped[str]             = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    hazard_id:    Mapped[str]             = mapped_column(String, ForeignKey("hazards.id", ondelete="CASCADE"))
    kind:         Mapped[str]             = mapped_column(String)             # EVENT_KINDS
    source:       Mapped[str | None]      = mapped_column(String, nullable=True)  # video | review | vote | expiry | manual | system
    job_id:       Mapped[str | None]      = mapped_column(String, nullable=True)
    detection_id: Mapped[str | None]      = mapped_column(String, nullable=True)
    user_id:      Mapped[str | None]      = mapped_column(String, nullable=True)
    details:      Mapped[dict | None]     = mapped_column(JSON, nullable=True)
    recorded_at:  Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at:   Mapped[datetime]        = mapped_column(DateTime, default=datetime.utcnow)


class HazardVote(Base):
    __tablename__ = "hazard_votes"
    __table_args__ = (
        Index("ix_hazard_votes_hazard_id", "hazard_id"),
        Index("ix_hazard_votes_user_id", "user_id"),
    )

    id:         Mapped[str]          = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    hazard_id:  Mapped[str]          = mapped_column(String, ForeignKey("hazards.id", ondelete="CASCADE"))
    user_id:    Mapped[str | None]   = mapped_column(String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    device_id:  Mapped[str | None]   = mapped_column(String, nullable=True)
    vote:       Mapped[str]          = mapped_column(String)               # still_there | not_there
    latitude:   Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude:  Mapped[float | None] = mapped_column(Float, nullable=True)
    distance_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    counted:    Mapped[bool]         = mapped_column(Boolean, default=False)
    reason:     Mapped[str | None]   = mapped_column(String, nullable=True)  # too_far | cooldown
    created_at: Mapped[datetime]     = mapped_column(DateTime, default=datetime.utcnow)
