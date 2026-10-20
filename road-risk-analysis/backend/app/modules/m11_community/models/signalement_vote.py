"""
Community "still there / not there" votes on a signalement (Waze-style).

Every vote is kept, valid or not: `counted` is false when the voter was too
far from the report (`too_far`), sent no position (`no_position`) or has
since voted again (`duplicate` — a voter's later vote replaces the earlier
one). Administrators review invalid votes to spot false testifiers.
"""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db.session import Base

VOTE_VALUES = ("present", "absent")
INVALID_REASONS = ("too_far", "no_position")     # count as "invalid" (false testimony suspects)
REASONS = INVALID_REASONS + ("duplicate",)


class SignalementVote(Base):
    __tablename__ = "signalement_votes"
    __table_args__ = (
        Index("ix_signalement_votes_signalement_id", "signalement_id"),
        Index("ix_signalement_votes_voter_user_id", "voter_user_id"),
        Index("ix_signalement_votes_voter_device_id", "voter_device_id"),
    )

    id:              Mapped[str]            = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    signalement_id:  Mapped[str]            = mapped_column(String, ForeignKey("signalements.id", ondelete="CASCADE"))
    voter_user_id:   Mapped[str | None]     = mapped_column(String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    voter_device_id: Mapped[str | None]     = mapped_column(String, nullable=True)
    vote:            Mapped[str]            = mapped_column(String)               # present | absent
    latitude:        Mapped[float | None]   = mapped_column(Float, nullable=True)
    longitude:       Mapped[float | None]   = mapped_column(Float, nullable=True)
    distance_m:      Mapped[float | None]   = mapped_column(Float, nullable=True)
    counted:         Mapped[bool]           = mapped_column(Boolean, default=False)
    reason:          Mapped[str | None]     = mapped_column(String, nullable=True)  # too_far | duplicate | no_position
    created_at:      Mapped[datetime]       = mapped_column(DateTime, default=datetime.utcnow)


class SignalementPass(Base):
    """A device passed by a displayed report (the app reports it when the
    driver comes within community.vote_max_distance_m). One row per device
    per report per community.ignore_window_minutes. A device that passed
    and never voted "ignored" the report; enough ignores in the window mark
    it `uncertain` (and optionally hide it)."""
    __tablename__ = "signalement_passes"
    __table_args__ = (
        Index("ix_signalement_passes_signalement_id", "signalement_id"),
        Index("ix_signalement_passes_device_id", "device_id"),
    )

    id:             Mapped[str]          = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    signalement_id: Mapped[str]          = mapped_column(String, ForeignKey("signalements.id", ondelete="CASCADE"))
    device_id:      Mapped[str | None]   = mapped_column(String, nullable=True)
    user_id:        Mapped[str | None]   = mapped_column(String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    latitude:       Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude:      Mapped[float | None] = mapped_column(Float, nullable=True)
    distance_m:     Mapped[float | None] = mapped_column(Float, nullable=True)
    counted:        Mapped[bool]         = mapped_column(Boolean, default=False)  # within vote_max_distance_m
    created_at:     Mapped[datetime]     = mapped_column(DateTime, default=datetime.utcnow)
