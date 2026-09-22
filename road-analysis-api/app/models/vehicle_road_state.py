from datetime import datetime
from sqlalchemy import String, Float, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from app.db.session import Base


class VehicleRoadState(Base):
    """
    Latest road-aware position for one vehicle — one row per vehicle, kept
    up to date on a throttled cadence (see settings.ROAD_STATE_DB_THROTTLE_S).

    This is a durable *summary* only (per Step 2 design: vehicle position is
    continuously-changing state, not road-network data — see
    app/services/road_state_service.py). It intentionally does NOT store the
    full OSRM road geometry or upcoming-segments list; those are cheap to
    recompute from OSRM and are served from the Redis-cached
    RoadAwareVehicleState instead. If this row is missing/stale, the caller
    should treat the vehicle as having no known road-aware state, not error.
    """
    __tablename__ = "vehicle_road_states"

    vehicle_id: Mapped[str] = mapped_column(String, ForeignKey("vehicles.id"), primary_key=True)

    road_segment_id: Mapped[str | None] = mapped_column(String, nullable=True)
    road_name:       Mapped[str | None] = mapped_column(String, nullable=True)
    direction:       Mapped[str]        = mapped_column(String, default="UNKNOWN")  # RoadDirection value

    matched_latitude:  Mapped[float | None] = mapped_column(Float, nullable=True)
    matched_longitude: Mapped[float | None] = mapped_column(Float, nullable=True)

    match_quality:    Mapped[str]         = mapped_column(String)  # MapMatchQuality value
    match_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    matched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
