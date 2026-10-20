"""
Cluster zones (RRAP-06-010) — the places where the global Cluster
Configuration (settings section `clustering`) applies another profile or
local overrides:

  agglomeration  an urban polygon (city boundary, usually from OpenStreetMap)
  road_section   a rural corridor along a route (tronçon): the OSRM route
                 polyline in `geometry` + `corridor_width_m`
  custom         any polygon drawn by hand (or a migrated circular area)

`geometry` is GeoJSON (Polygon / MultiPolygon, or LineString for road
sections); bbox_* are the pre-filter (padded by half the corridor width).
Null overrides inherit the environment profile. See
app.modules.m06_hazards.clustering for the resolution order.
"""
from datetime import datetime
from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db.session import Base


class ClusterZone(Base):
    __tablename__ = "cluster_zones"

    id:          Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name:        Mapped[str] = mapped_column(String)
    kind:        Mapped[str] = mapped_column(String, index=True)        # agglomeration | road_section | custom
    environment: Mapped[str] = mapped_column(String, index=True)        # urban | rural
    geometry:    Mapped[dict] = mapped_column(JSON)
    corridor_width_m: Mapped[float | None] = mapped_column(Float, nullable=True)   # road sections

    bbox_min_lat: Mapped[float] = mapped_column(Float)
    bbox_min_lon: Mapped[float] = mapped_column(Float)
    bbox_max_lat: Mapped[float] = mapped_column(Float)
    bbox_max_lon: Mapped[float] = mapped_column(Float)
    area_km2:     Mapped[float] = mapped_column(Float, default=0.0)
    center_lat:   Mapped[float | None] = mapped_column(Float, nullable=True)   # label / legacy circle centre
    center_lon:   Mapped[float | None] = mapped_column(Float, nullable=True)
    circle_radius_km: Mapped[float | None] = mapped_column(Float, nullable=True)  # migrated circular areas

    # Where the geometry came from: nominatim | drawn | route | legacy_area
    source:      Mapped[str] = mapped_column(String, default="drawn")
    source_ref:  Mapped[str | None] = mapped_column(String, nullable=True)   # e.g. "relation/3832073", route key
    source_meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    default_radius_m:   Mapped[float | None] = mapped_column(Float, nullable=True)
    type_radius:        Mapped[dict | None]  = mapped_column(JSON, nullable=True)
    signalement_radius: Mapped[dict | None]  = mapped_column(JSON, nullable=True)
    priority:   Mapped[int]  = mapped_column(Integer, default=0)
    is_active:  Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    country:    Mapped[str | None] = mapped_column(String, nullable=True)
    city:       Mapped[str | None] = mapped_column(String, nullable=True)

    created_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
