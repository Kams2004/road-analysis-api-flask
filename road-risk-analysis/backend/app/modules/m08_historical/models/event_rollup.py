"""
Daily summaries of `historical_events` (app/modules/m08_historical/rollups.py): what the
Historical analysis and Exception patterns screens read instead of the raw events, so their cost
stays the same however many years of events pile up. Rebuilt per day when events of that day
are imported, entirely when the severity rules change. Keys use '' for "none" (primary keys
cannot hold NULL).

Anonymous like the events: no driver or vehicle dimension. These tables replace `event_daily`,
`event_daily_hour` and `event_daily_cell`, which had one (dropped by app/core/db/upgrade.py).
"""
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db.session import Base


class EventDaily(Base):
    """Per day × source × country × transporter × type × normalised severity × speed band ×
    excess band (bands: rollups.SPEED_BANDS_KMH / EXCESS_BANDS_PCT)."""
    __tablename__ = "event_rollup_day"
    __table_args__ = (
        Index("ix_event_rollup_day_country_day", "country", "day"),
        Index("ix_event_rollup_day_source_day", "source", "day"),
    )

    day:         Mapped[date] = mapped_column(Date, primary_key=True)
    source:      Mapped[str]  = mapped_column(String, primary_key=True)
    country:     Mapped[str]  = mapped_column(String, primary_key=True)
    transporter: Mapped[str]  = mapped_column(String, primary_key=True)
    event_type:  Mapped[str]  = mapped_column(String, primary_key=True)
    severity:    Mapped[str]  = mapped_column(String, primary_key=True)    # low | medium | high | unknown
    speed_band:  Mapped[str]  = mapped_column(String, primary_key=True)    # e.g. 50-70 (km/h), '' unknown
    excess_band: Mapped[str]  = mapped_column(String, primary_key=True)    # e.g. 10-20 (% over the limit)
    n:           Mapped[int]  = mapped_column(Integer)
    speed_sum:   Mapped[float] = mapped_column(Float, default=0.0)         # m/s
    speed_n:     Mapped[int]   = mapped_column(Integer, default=0)
    speed_max:   Mapped[float | None] = mapped_column(Float, nullable=True)
    limit_sum:   Mapped[float] = mapped_column(Float, default=0.0)
    limit_n:     Mapped[int]   = mapped_column(Integer, default=0)
    excess_sum:  Mapped[float] = mapped_column(Float, default=0.0)         # % over the limit
    excess_n:    Mapped[int]   = mapped_column(Integer, default=0)
    first_at:    Mapped[datetime] = mapped_column(DateTime)
    last_at:     Mapped[datetime] = mapped_column(DateTime)


class EventDailyHour(Base):
    """Per day × source × country × type × severity × hour (UTC): hour-of-day / weekday patterns."""
    __tablename__ = "event_rollup_hour"

    day:        Mapped[date] = mapped_column(Date, primary_key=True)
    source:     Mapped[str]  = mapped_column(String, primary_key=True)
    country:    Mapped[str]  = mapped_column(String, primary_key=True)
    event_type: Mapped[str]  = mapped_column(String, primary_key=True)
    severity:   Mapped[str]  = mapped_column(String, primary_key=True)
    hour:       Mapped[int]  = mapped_column(Integer, primary_key=True)
    n:          Mapped[int]  = mapped_column(Integer)


class EventDailyCell(Base):
    """Per day × source × country × ~110 m square (lat / lon × 1000, truncated) × type × severity ×
    road: heatmaps, zones, top roads, historical risk cells (any cell size, from the mean position)."""
    __tablename__ = "event_rollup_cell"
    __table_args__ = (Index("ix_event_rollup_cell_square", "k_lat", "k_lon"),)

    day:        Mapped[date] = mapped_column(Date, primary_key=True)
    source:     Mapped[str]  = mapped_column(String, primary_key=True)
    country:    Mapped[str]  = mapped_column(String, primary_key=True)
    k_lat:      Mapped[int]  = mapped_column(Integer, primary_key=True)
    k_lon:      Mapped[int]  = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str]  = mapped_column(String, primary_key=True)
    severity:   Mapped[str]  = mapped_column(String, primary_key=True)
    road_name:  Mapped[str]  = mapped_column(String, primary_key=True)
    n:          Mapped[int]  = mapped_column(Integer)
    lat_sum:    Mapped[float] = mapped_column(Float)
    lon_sum:    Mapped[float] = mapped_column(Float)
    first_at:   Mapped[datetime] = mapped_column(DateTime)
    last_at:    Mapped[datetime] = mapped_column(DateTime)


class EventRollupState(Base):
    """One row: the summaries are complete (built_at set) for the severity rules `rules`."""
    __tablename__ = "event_rollup_state"

    id:       Mapped[int]             = mapped_column(Integer, primary_key=True)
    built_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rules:    Mapped[str | None]      = mapped_column(String, nullable=True)   # hash of the severity settings
