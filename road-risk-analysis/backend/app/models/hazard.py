"""
A hazard is a persisted cluster of accepted detections of the same kind at
the same place (RRAP-06-001…004): one real-world pothole seen on several
drives is one hazard with N detections. Membership is `detections.hazard_id`;
the aggregate columns below are recomputed by app.services.hazard_service
whenever membership changes.

Lifecycle columns (app.services.hazards.lifecycle): last_seen_at (recording
time of the latest sighting), expires_at (None = never), absence_count
(consecutive valid "not seen" video passes), resolution_source, and the
recurring-spot flag. The timeline is in hazard_events, driver votes in
hazard_votes (app.models.hazard_lifecycle).
"""
import enum
import uuid
from datetime import datetime
from sqlalchemy import Boolean, String, Float, DateTime, Integer, Sequence
from sqlalchemy.orm import Mapped, mapped_column
from app.db.session import Base


class HazardStatus(str, enum.Enum):
    active   = "active"
    resolved = "resolved"


class ResolutionSource(str, enum.Enum):
    manual        = "manual"          # a console user
    video_absence = "video_absence"   # N valid video passes without it
    votes         = "votes"           # drivers' "not there" votes
    expiry        = "expiry"          # not seen for the policy's expiry hours


HAZARD_NUMBER_SEQ = Sequence("hazards_number_seq")


class Hazard(Base):
    __tablename__ = "hazards"

    id:       Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    number:   Mapped[int] = mapped_column(Integer, HAZARD_NUMBER_SEQ, unique=True)
    code:     Mapped[str] = mapped_column(String, unique=True, index=True)   # HZ-000001
    type:     Mapped[str] = mapped_column(String, index=True)
    subtype:  Mapped[str | None] = mapped_column(String, nullable=True)
    status:   Mapped[str] = mapped_column(String, default=HazardStatus.active.value, index=True)

    latitude:  Mapped[float] = mapped_column(Float)    # centroid of the members
    longitude: Mapped[float] = mapped_column(Float)
    radius_m:  Mapped[float] = mapped_column(Float)    # clustering radius applied when it was created
    # Cluster Configuration used when it was created (app.services.clustering):
    # zone (None = unzoned) and environment profile (urban | rural).
    cluster_zone_id:     Mapped[int | None] = mapped_column(Integer, nullable=True)
    cluster_zone_name:   Mapped[str | None] = mapped_column(String, nullable=True)
    cluster_environment: Mapped[str | None] = mapped_column(String, nullable=True)
    country:       Mapped[str | None] = mapped_column(String, nullable=True)
    city:          Mapped[str | None] = mapped_column(String, nullable=True)
    location_name: Mapped[str | None] = mapped_column(String, nullable=True)
    # Structured address of the most central geocoded member (hazard_service.aggregate).
    road:             Mapped[str | None] = mapped_column(String, nullable=True)
    locality:         Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    district:         Mapped[str | None] = mapped_column(String, nullable=True)
    town:             Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    region:           Mapped[str | None] = mapped_column(String, nullable=True)
    display_location: Mapped[str | None] = mapped_column(String, nullable=True)

    detection_count: Mapped[int]   = mapped_column(Integer, default=0)
    avg_confidence:  Mapped[float] = mapped_column(Float, default=0.0)
    max_confidence:  Mapped[float] = mapped_column(Float, default=0.0)
    first_detected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_detected_at:  Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    resolved_at:     Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_by:     Mapped[str | None]      = mapped_column(String, nullable=True)
    resolution_note: Mapped[str | None]      = mapped_column(String, nullable=True)
    resolution_source: Mapped[str | None]    = mapped_column(String, nullable=True)   # ResolutionSource

    # Lifecycle (app.services.hazards.lifecycle)
    last_seen_at:     Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    expires_at:       Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    absence_count:    Mapped[int]  = mapped_column(Integer, default=0, server_default="0")
    recurring_spot:   Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    recurrence_count: Mapped[int]  = mapped_column(Integer, default=0, server_default="0")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
