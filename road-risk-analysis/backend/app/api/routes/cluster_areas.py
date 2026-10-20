"""
DEPRECATED — circular cluster areas, superseded by cluster zones
(`/cluster-zones`: agglomeration outlines, road-section corridors, custom
polygons, inside the global Cluster Configuration). Kept as a thin wrapper:
an "area" is a `custom` urban cluster zone whose geometry is a 64-vertex
circle (`circle_radius_km` set); its id is the zone id. Legacy areas were
migrated to such zones once (app.services.clustering.service.migrate_legacy).
"""
from datetime import datetime
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, get_principal
from app.api.openapi import errors, legacy
from app.db.session import get_db
from app.models.cluster_zone import ClusterZone
from app.services import audit
from app.services.clustering import service as clustering_service
from app.services.clustering import zones as geo
from app.services.clustering.resolver import DEFAULT_PRIORITY

router = APIRouter()


def _check_type_radius(v: Optional[dict[str, float]]) -> Optional[dict[str, float]]:
    if v and any(r is None or r <= 0 or r > 10000 for r in v.values()):
        raise ValueError("per-type radius must be between 0 and 10000 m")
    return v or None


class ClusterAreaIn(BaseModel):
    """A circular area (city, region) with its own clustering radius. A detection inside several
    active areas uses the smallest one; outside every area the global radius applies."""
    name:             str = Field(description="Display name.", examples=["Douala"])
    country:          Optional[str] = Field(None, description="Country (informative).", examples=["Cameroon"])
    city:             Optional[str] = Field(None, description="City (informative).", examples=["Douala"])
    center_lat:       float = Field(ge=-90, le=90, description="Centre latitude (WGS84).", examples=[4.0511])
    center_lon:       float = Field(ge=-180, le=180, description="Centre longitude (WGS84).", examples=[9.7679])
    area_radius_km:   float = Field(gt=0, le=1000, description="Radius of the area, in kilometres.", examples=[25])
    cluster_radius_m: float = Field(gt=0, le=10000, description="Clustering radius inside the area, in metres.",
                                    examples=[30])
    type_radius:      Optional[dict[str, float]] = Field(
        None, description="Per detection-type clustering radius in metres (0 < r ≤ 10000), overriding "
                          "`cluster_radius_m`; an empty object is stored as `null`.",
        examples=[{"pothole": 20, "speed_bump": 40}])
    is_active:        bool = Field(True, description="Inactive areas are ignored by the clustering.")

    @field_validator("type_radius")
    @classmethod
    def _type_radius(cls, v):
        return _check_type_radius(v)


class ClusterAreaPatch(BaseModel):
    """Partial update: only the fields sent are changed. `null` clears `country`, `city` and
    `type_radius`; it is ignored for the other fields."""
    name:             Optional[str] = Field(None, description="Display name.")
    country:          Optional[str] = Field(None, description="Country (`null` clears it).")
    city:             Optional[str] = Field(None, description="City (`null` clears it).")
    center_lat:       Optional[float] = Field(None, ge=-90, le=90, description="Centre latitude (WGS84).")
    center_lon:       Optional[float] = Field(None, ge=-180, le=180, description="Centre longitude (WGS84).")
    area_radius_km:   Optional[float] = Field(None, gt=0, le=1000, description="Radius of the area, in kilometres.")
    cluster_radius_m: Optional[float] = Field(None, gt=0, le=10000, description="Clustering radius, in metres.")
    type_radius:      Optional[dict[str, float]] = Field(None, description="Per-type radius in metres (`null` clears it).",
                                                         examples=[{"pothole": 20}])
    is_active:        Optional[bool] = Field(None, description="Enable or disable the area.")

    @field_validator("type_radius")
    @classmethod
    def _type_radius(cls, v):
        return _check_type_radius(v)


class ClusterAreaOut(ClusterAreaIn):
    """A cluster area as stored."""
    id:         int = Field(description="Area id.", examples=[3])
    created_at: datetime = Field(description="Creation time (UTC).")
    updated_at: datetime = Field(description="Last change (UTC).")

    class Config:
        from_attributes = True


def _area(z: ClusterZone) -> dict:
    meta = z.source_meta or {}
    return {
        "id": z.id, "name": z.name, "country": z.country, "city": z.city,
        "center_lat": meta.get("center_lat", z.center_lat), "center_lon": meta.get("center_lon", z.center_lon),
        "area_radius_km": z.circle_radius_km, "cluster_radius_m": z.default_radius_m or 0.0,
        "type_radius": z.type_radius, "is_active": z.is_active, "created_at": z.created_at, "updated_at": z.updated_at,
    }


def _snapshot(z: ClusterZone) -> dict:
    a = _area(z)
    return {k: a[k] for k in ClusterAreaIn.model_fields}


def _set_circle(z: ClusterZone, lat: float, lon: float, radius_km: float) -> None:
    g = geo.circle_polygon(lat, lon, radius_km * 1000)
    z.geometry = g
    for k, v in clustering_service.geometry_fields(g).items():
        setattr(z, k, v)
    z.center_lat, z.center_lon, z.circle_radius_km = lat, lon, radius_km
    z.source_meta = {**(z.source_meta or {}), "center_lat": lat, "center_lon": lon, "area_radius_km": radius_km}


AreaId = Annotated[int, Path(description="Cluster area id (= its cluster zone id).", examples=[3])]
E_NOT_FOUND = legacy(404, "Cluster area not found")
_DEPRECATED = "**Deprecated** — use `/cluster-zones`. "


@router.get("/", response_model=list[ClusterAreaOut], summary="List cluster areas (deprecated)", deprecated=True)
async def list_areas(db: AsyncSession = Depends(get_db)):
    """**Deprecated** — use `GET /cluster-zones/`. The circular zones (legacy areas), by name."""
    rows = (await db.execute(select(ClusterZone).where(ClusterZone.circle_radius_km.isnot(None))
                             .order_by(ClusterZone.name))).scalars().all()
    return [_area(z) for z in rows]


@router.post("/", response_model=ClusterAreaOut, status_code=201, summary="Create a cluster area (deprecated)",
             deprecated=True)
async def create_area(body: ClusterAreaIn, request: Request, db: AsyncSession = Depends(get_db),
                      principal: Principal = Depends(get_principal)):
    """
    **Deprecated** — use `POST /cluster-zones/`. Creates a `custom` urban cluster zone shaped as a circle
    with `cluster_radius_m` as its default radius. Audited as `cluster_area.create`.
    """
    z = ClusterZone(name=body.name, kind="custom", environment="urban", corridor_width_m=None, source="drawn",
                    default_radius_m=body.cluster_radius_m, type_radius=body.type_radius, signalement_radius=None,
                    priority=DEFAULT_PRIORITY["custom"], is_active=body.is_active, country=body.country,
                    city=body.city, created_by=principal.label, created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow())
    _set_circle(z, body.center_lat, body.center_lon, body.area_radius_km)
    db.add(z)
    await db.flush()
    audit.record(db, request, "cluster_area.create", principal=principal, object_type="cluster_area",
                 object_id=str(z.id), details=_snapshot(z))
    await db.commit()
    await db.refresh(z)
    clustering_service.invalidate()
    return _area(z)


async def _get(db: AsyncSession, area_id: int) -> ClusterZone:
    z = await db.get(ClusterZone, area_id)
    if z is None or z.circle_radius_km is None:
        raise HTTPException(404, "Cluster area not found")
    return z


@router.patch("/{area_id}", response_model=ClusterAreaOut, summary="Update a cluster area (deprecated)",
              responses=errors(E_NOT_FOUND), deprecated=True)
async def update_area(area_id: AreaId, body: ClusterAreaPatch, request: Request, db: AsyncSession = Depends(get_db),
                      principal: Principal = Depends(get_principal)):
    """**Deprecated** — use `PATCH /cluster-zones/{zone_id}`. Audited as `cluster_area.update`."""
    z = await _get(db, area_id)
    before = _snapshot(z)
    data = body.model_dump(exclude_unset=True)
    a = _area(z)
    lat = data.get("center_lat") or a["center_lat"]
    lon = data.get("center_lon") or a["center_lon"]
    rkm = data.get("area_radius_km") or a["area_radius_km"]
    for key, value in data.items():
        if key in ("center_lat", "center_lon", "area_radius_km"):
            continue
        if value is None and key not in ("country", "city", "type_radius"):
            continue
        setattr(z, "default_radius_m" if key == "cluster_radius_m" else key, value)
    _set_circle(z, lat, lon, rkm)
    z.updated_at = datetime.utcnow()
    audit.record(db, request, "cluster_area.update", principal=principal, object_type="cluster_area",
                 object_id=str(z.id), details=audit.diff(before, _snapshot(z)))
    await db.commit()
    await db.refresh(z)
    clustering_service.invalidate()
    return _area(z)


@router.delete("/{area_id}", status_code=204, summary="Delete a cluster area (deprecated)",
               responses=errors(E_NOT_FOUND), deprecated=True)
async def delete_area(area_id: AreaId, request: Request, db: AsyncSession = Depends(get_db),
                      principal: Principal = Depends(get_principal)):
    """**Deprecated** — use `DELETE /cluster-zones/{zone_id}`. Audited as `cluster_area.delete`."""
    z = await _get(db, area_id)
    audit.record(db, request, "cluster_area.delete", principal=principal, object_type="cluster_area",
                 object_id=str(z.id), details=_snapshot(z))
    await db.delete(z)
    await db.commit()
    clustering_service.invalidate()
