import uuid
from datetime import datetime
from sqlalchemy import String, Float, DateTime, UniqueConstraint, Index, JSON
from sqlalchemy.orm import Mapped, mapped_column
from app.db.session import Base


class HistoricalEventRecord(Base):
    """
    Durable storage for one imported historical driving event (speeding,
    hard braking, accident, ...) — see app/services/historical/models.py's
    HistoricalEvent (the in-flight/API shape) for why this is named
    differently: the two are used together constantly (import/read paths
    both touch this ORM row and that pydantic shape), so distinct names
    avoid an import-aliasing headache rather than a `from ... import
    HistoricalEvent as HistoricalEventRow` at every call site.

    Deliberately durable Postgres, unlike Steps 2-5's Redis-only live
    vehicle state: historical events accumulate over months/years for
    training (Step 6.2+) and must survive independently of any one vehicle's
    live session — the opposite data-lifetime characteristic, the opposite
    storage choice, for the same reasons Step 2 documented the other way.
    """
    __tablename__ = "historical_events"
    __table_args__ = (
        UniqueConstraint("source", "source_event_id", name="uq_historical_event_source"),
        Index("ix_historical_events_grid_cell", "grid_cell_id"),
        Index("ix_historical_events_occurred_at", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))

    source:          Mapped[str] = mapped_column(String)          # "ymane" | "manual" | ...
    source_event_id: Mapped[str] = mapped_column(String)          # source's own id — idempotent re-import key
    event_type:       Mapped[str] = mapped_column(String)         # HistoricalEventType value

    occurred_at: Mapped[datetime] = mapped_column(DateTime)

    latitude:  Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)

    vehicle_external_id: Mapped[str | None] = mapped_column(String, nullable=True)
    driver_external_id:  Mapped[str | None] = mapped_column(String, nullable=True)

    speed_mps:       Mapped[float | None] = mapped_column(Float, nullable=True)
    speed_limit_mps: Mapped[float | None] = mapped_column(Float, nullable=True)
    severity:        Mapped[str | None]   = mapped_column(String, nullable=True)

    # Map-matched — see app/services/historical/matcher.py.
    grid_cell_id:       Mapped[str]         = mapped_column(String)  # primary aggregation key — see grid.py
    matched_latitude:   Mapped[float | None] = mapped_column(Float, nullable=True)
    matched_longitude:  Mapped[float | None] = mapped_column(Float, nullable=True)
    road_name:          Mapped[str | None]  = mapped_column(String, nullable=True)
    road_segment_id:    Mapped[str | None]  = mapped_column(String, nullable=True)  # descriptive only, not stable long-term
    match_confidence:   Mapped[float | None] = mapped_column(Float, nullable=True)

    raw: Mapped[dict] = mapped_column(JSON, default=dict)  # untouched source payload — forward-compatible, debuggable

    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
