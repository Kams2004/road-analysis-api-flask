"""
City/area-specific clustering radius (RRAP-06-010): dense city centres need
a tighter radius than rural roads. A detection within `area_radius_km` of an
active area's centre uses that area's radius (smallest matching area wins);
otherwise the global ClusterConfig.radius_m applies.
"""
from datetime import datetime
from sqlalchemy import String, Float, DateTime, Integer, Boolean, JSON
from sqlalchemy.orm import Mapped, mapped_column
from app.db.session import Base


class ClusterArea(Base):
    __tablename__ = "cluster_areas"

    id:               Mapped[int]   = mapped_column(Integer, primary_key=True, autoincrement=True)
    name:             Mapped[str]   = mapped_column(String)
    country:          Mapped[str | None] = mapped_column(String, nullable=True)
    city:             Mapped[str | None] = mapped_column(String, nullable=True)
    center_lat:       Mapped[float] = mapped_column(Float)
    center_lon:       Mapped[float] = mapped_column(Float)
    area_radius_km:   Mapped[float] = mapped_column(Float)          # extent of the area
    cluster_radius_m: Mapped[float] = mapped_column(Float)          # radius applied inside it
    type_radius:      Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {"pothole": 25}
    is_active:        Mapped[bool]  = mapped_column(Boolean, default=True)
    created_at:       Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at:       Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
