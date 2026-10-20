"""
Global Cluster Configuration (Module 6 — RRAP-06-009/010/011).

  /clustering/config        the urban / out-of-agglomeration profiles
  /clustering/rebuild-hazards  re-cluster existing hazards with the current configuration
  /cluster-zones/…          agglomerations (OSM boundaries), road-section
                            corridors (OSRM) and custom polygons; boundary
                            search; "test a point" resolution

Permissions: module `settings` (view to read, edit to change — settings has
no add/delete right, so creating or removing a zone is an edit). A change
applies to everything clustered afterwards (hazard assignment, processor
de-duplication, community-report merging) within ~10 s in every process; it
never silently re-clusters history — POST /clustering/rebuild-hazards (or
`python -m app.cli rebuild-hazards`) does.
"""
from datetime import datetime
from typing import Annotated, Any, Literal, Optional

from fastapi import APIRouter, Body, Depends, Path, Query, Request
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, api_error, get_principal
from app.api.openapi import err, errors
from app.db.session import get_db
from app.models.cluster_zone import ClusterZone
from app.services import audit
from app.services.clustering import boundaries, zones as geo
from app.services.clustering import service as clustering_service
from app.services.clustering.resolver import DEFAULT_PRIORITY, resolve, resolve_all
from app.services.clustering.settings import (
    DETECTION_TYPES, MAX_RADIUS_M, MIN_RADIUS_M, SIGNALEMENT_TYPES, ClusteringSettings, check_radius_map,
)
from app.services.osrm.client import OSRMUnavailableError, get_osrm_client
from app.services.settings_service import RoutesSettings, get_cached, update_section

config_router = APIRouter()
zones_router = APIRouter()

Kind = Literal["agglomeration", "road_section", "custom"]
Env = Literal["urban", "rural"]

_REBUILD_HINT = ("A change applies to detections and reports clustered from now on; existing hazards are "
                 "re-clustered only by `POST /clustering/rebuild-hazards` (or `python -m app.cli rebuild-hazards`).")


# ── global configuration ──────────────────────────────────────────────────────

class ProfileOut(BaseModel):
    """Clustering parameters of one environment."""
    default_radius_m:   float = Field(description="Radius for every type without an override, metres.",
                                      examples=[30])
    type_radius:        dict[str, float] = Field(description="Per AI-detection-type radius, metres.",
                                                 examples=[{"pothole": 25, "speed_bump": 40}])
    signalement_radius: dict[str, float] = Field(description="Per community-report-type radius, metres.",
                                                 examples=[{"embouteillage": 150, "police": 100}])


class ClusteringConfigOut(BaseModel):
    """The global Cluster Configuration plus the type catalogues the console offers."""
    urban: ProfileOut = Field(description="In agglomeration (inside an urban zone).")
    rural: ProfileOut = Field(description="Out of agglomeration (rural zones, road sections, and — by default — "
                                          "everywhere outside every zone).")
    unzoned_environment: Env = Field(description="Profile used where no zone contains the point.")
    merge_signalements: bool = Field(description="Merge new community reports into an active report of the same "
                                                 "type within the radius.")
    default_corridor_width_m: float = Field(description="Default total width of a road-section corridor, metres.")
    detection_types: list[str] = Field(description="Detection types offered for per-type values.")
    signalement_types: list[str] = Field(description="Community report types offered for per-type values.")
    zone_count: int = Field(description="Active cluster zones.")
    min_radius_m: float = Field(MIN_RADIUS_M, description="Smallest radius accepted, metres.")
    max_radius_m: float = Field(MAX_RADIUS_M, description="Largest radius accepted, metres.")


async def _config_out(db: AsyncSession) -> dict:
    s: ClusteringSettings = await clustering_service.load_settings(db)
    n = len((await db.execute(select(ClusterZone.id).where(ClusterZone.is_active.is_(True)))).all())
    return {**s.model_dump(), "detection_types": list(DETECTION_TYPES), "signalement_types": list(SIGNALEMENT_TYPES),
            "zone_count": n, "min_radius_m": MIN_RADIUS_M, "max_radius_m": MAX_RADIUS_M}


@config_router.get("/config", response_model=ClusteringConfigOut, summary="Get the global Cluster Configuration")
async def get_config(db: AsyncSession = Depends(get_db)):
    """
    The ONE clustering configuration of the platform (RRAP-06-009): an **urban** profile (in agglomeration)
    and a **rural** profile (out of agglomeration), each with a default radius and per-type overrides for AI
    detections (`type_radius`) and community reports (`signalement_radius`). Every detection (hazard
    clustering, processor de-duplication, re-sightings) and every community report (merging) is clustered
    with the radius resolved from it at its own position — see `GET /cluster-zones/resolve`.

    Resolution: the active zone containing the point with the highest `priority`, then the smallest area →
    zone override for the type → zone default → profile[zone environment] override for the type → profile
    default; no zone → the `unzoned_environment` profile (rural by default).
    """
    return await _config_out(db)


@config_router.put("/config", response_model=ClusteringConfigOut, summary="Update the global Cluster Configuration",
                   responses=errors(err(422, "invalid_settings", "Some values are invalid.",
                                        errors=[{"field": "urban.default_radius_m",
                                                 "message": "Input should be greater than or equal to 1"}])))
async def put_config(request: Request,
                     body: dict = Body(..., openapi_examples={
                         "profiles": {"summary": "Urban / rural defaults and per-type values", "value": {
                             "urban": {"default_radius_m": 30, "type_radius": {"pothole": 25, "speed_bump": 40},
                                       "signalement_radius": {"embouteillage": 150}},
                             "rural": {"default_radius_m": 60, "type_radius": {"pothole": 50}}}},
                         "unzoned": {"summary": "Treat unzoned places as urban", "value": {"unzoned_environment": "urban"}},
                     }),
                     db: AsyncSession = Depends(get_db), principal: Principal = Depends(get_principal)):
    """
    Partial update: a profile (`urban` / `rural`) is merged field by field (send the whole `type_radius` /
    `signalement_radius` map to replace it; a `null` value in it removes that type's override). The whole
    configuration is re-validated (`422 invalid_settings`). Applies within ~10 s everywhere. A change applies to detections and reports clustered from now on; existing hazards are re-clustered only by `POST /clustering/rebuild-hazards` (or `python -m app.cli rebuild-hazards`).
    Audited as `clustering.update`.
    """
    try:
        before, after = await update_section(db, "clustering", body, principal.label)
    except (ValidationError, ValueError) as exc:
        errs = (exc.errors() if isinstance(exc, ValidationError) else [{"loc": (), "msg": str(exc)}])
        raise api_error(422, "invalid_settings", "Some values are invalid.",
                        errors=[{"field": ".".join(map(str, e["loc"])), "message": e["msg"]} for e in errs])
    audit.record(db, request, "clustering.update", principal=principal, object_type="settings",
                 object_id="clustering", details=audit.diff(before, after))
    await db.commit()
    return await _config_out(db)


class RebuildOut(BaseModel):
    """Result of a hazard rebuild."""
    hazards: int = Field(description="Hazards after the rebuild.", examples=[42])


@config_router.post("/rebuild-hazards", response_model=RebuildOut, summary="Re-cluster every hazard",
                    responses=errors(err(422, "confirmation_required", "Send confirm=true to rebuild the hazards.")))
async def rebuild_hazards(request: Request,
                          confirm: bool = Query(False, description="Must be `true` (destructive)."),
                          db: AsyncSession = Depends(get_db), principal: Principal = Depends(get_principal)):
    """
    Recreate every hazard from the accepted / resolved detections with the CURRENT Cluster Configuration
    (same as `python -m app.cli rebuild-hazards`). Hazard codes are renumbered and the hazard lifecycle
    timeline (events, driver votes) of the old hazards is discarded. Requires `confirm=true`. Audited as
    `clustering.rebuild_hazards`.
    """
    if not confirm:
        raise api_error(422, "confirmation_required", "Send confirm=true to rebuild the hazards.")
    from app.services.hazard_service import rebuild_all
    clustering_service.invalidate()
    count = await rebuild_all(db)
    audit.record(db, request, "clustering.rebuild_hazards", principal=principal, object_type="hazard",
                 details={"hazards": count})
    await db.commit()
    return RebuildOut(hazards=count)


# ── zones ─────────────────────────────────────────────────────────────────────

def _radius_map(v, signalements: bool, field: str):
    if v is None:
        return None
    return check_radius_map(v, signalements=signalements, field=field) or None


class ZoneBase(BaseModel):
    name:        str = Field(min_length=1, max_length=120, description="Display name.", examples=["Douala"])
    environment: Optional[Env] = Field(None, description=(
        "`urban` (in agglomeration) or `rural` (out of agglomeration). Default: urban for agglomeration / "
        "custom, rural for road sections."))
    default_radius_m:   Optional[float] = Field(None, ge=MIN_RADIUS_M, le=MAX_RADIUS_M, description=(
        "Local default radius, metres (null = inherit the environment profile)."), examples=[25])
    type_radius:        Optional[dict[str, float]] = Field(None, description=(
        "Local per-detection-type radius, metres (null / missing type = inherit)."), examples=[{"pothole": 20}])
    signalement_radius: Optional[dict[str, float]] = Field(None, description=(
        "Local per-report-type radius, metres (null / missing type = inherit)."), examples=[{"embouteillage": 120}])
    priority:   Optional[int] = Field(None, ge=-1000, le=1000, description=(
        "Higher wins when zones overlap (then the smallest area). Default: 10 for agglomeration / custom, "
        "0 for road sections."))
    is_active:  bool = Field(True, description="Inactive zones are ignored.")
    country:    Optional[str] = Field(None, description="Country (informative).", examples=["Cameroon"])
    city:       Optional[str] = Field(None, description="City (informative).", examples=["Douala"])

    @field_validator("type_radius")
    @classmethod
    def _tr(cls, v):
        return _radius_map(v, False, "type_radius")

    @field_validator("signalement_radius")
    @classmethod
    def _sr(cls, v):
        return _radius_map(v, True, "signalement_radius")


class ZoneIn(ZoneBase):
    """A new zone with its geometry (GeoJSON): Polygon / MultiPolygon for `agglomeration` and `custom`,
    LineString for `road_section` (+ `corridor_width_m`). Use `POST /cluster-zones/road-section` to build a
    road section from a route."""
    kind:     Kind = Field(description="`agglomeration`, `road_section` or `custom`.", examples=["agglomeration"])
    geometry: dict[str, Any] = Field(description="GeoJSON geometry, WGS84 `[lon, lat]`.")
    corridor_width_m: Optional[float] = Field(None, ge=20, le=5000, description=(
        "Road sections: total corridor width (half on each side of the road), metres."))
    source:     Literal["nominatim", "drawn", "route"] = Field("drawn", description="Where the geometry came from.")
    source_ref: Optional[str] = Field(None, description="e.g. `relation/3832073` (OSM) or a route key.",
                                      examples=["relation/3832073"])
    source_meta: Optional[dict[str, Any]] = Field(None, description="Free metadata (OSM display name …).")
    simplify:   bool = Field(True, description="Simplify the geometry (≤ 2000 vertices).")


class ZonePatch(BaseModel):
    """Partial update: only the fields sent change. `null` clears `default_radius_m`, `type_radius`,
    `signalement_radius`, `country`, `city`, and resets `priority` to the kind's default."""
    name:        Optional[str] = Field(None, min_length=1, max_length=120)
    environment: Optional[Env] = None
    default_radius_m:   Optional[float] = Field(None, ge=MIN_RADIUS_M, le=MAX_RADIUS_M)
    type_radius:        Optional[dict[str, float]] = None
    signalement_radius: Optional[dict[str, float]] = None
    priority:   Optional[int] = Field(None, ge=-1000, le=1000)
    is_active:  Optional[bool] = None
    country:    Optional[str] = None
    city:       Optional[str] = None
    geometry:   Optional[dict[str, Any]] = Field(None, description="New geometry (same family as the kind).")
    corridor_width_m: Optional[float] = Field(None, ge=20, le=5000)

    @field_validator("type_radius")
    @classmethod
    def _tr(cls, v):
        return _radius_map(v, False, "type_radius")

    @field_validator("signalement_radius")
    @classmethod
    def _sr(cls, v):
        return _radius_map(v, True, "signalement_radius")


class ZoneOut(BaseModel):
    """A cluster zone as stored (geometry only when asked)."""
    id: int = Field(examples=[3])
    name: str
    kind: Kind
    environment: Env
    default_radius_m: Optional[float]
    type_radius: Optional[dict[str, float]]
    signalement_radius: Optional[dict[str, float]]
    priority: int
    is_active: bool
    country: Optional[str]
    city: Optional[str]
    corridor_width_m: Optional[float]
    area_km2: float = Field(description="Area (corridor: length × width), km².")
    bbox: list[float] = Field(description="`[min_lat, min_lon, max_lat, max_lon]` (corridors padded).")
    center: Optional[list[float]] = Field(description="Label position `[lat, lon]`.")
    source: str = Field(description="`nominatim`, `drawn`, `route` or `legacy_area`.")
    source_ref: Optional[str]
    source_meta: Optional[dict[str, Any]]
    vertices: int = Field(description="Vertices of the stored geometry.")
    geometry: Optional[dict[str, Any]] = Field(None, description="GeoJSON geometry (when requested).")
    outline: Optional[dict[str, Any]] = Field(None, description="Road sections: corridor outline polygon for display.")
    created_by: Optional[str]
    created_at: datetime
    updated_at: datetime


def _out(z: ClusterZone, geometry: bool = False) -> dict:
    return {
        "id": z.id, "name": z.name, "kind": z.kind, "environment": z.environment,
        "default_radius_m": z.default_radius_m, "type_radius": z.type_radius, "signalement_radius": z.signalement_radius,
        "priority": z.priority, "is_active": z.is_active, "country": z.country, "city": z.city,
        "corridor_width_m": z.corridor_width_m, "area_km2": z.area_km2 or 0.0,
        "bbox": [z.bbox_min_lat, z.bbox_min_lon, z.bbox_max_lat, z.bbox_max_lon],
        "center": [z.center_lat, z.center_lon] if z.center_lat is not None else None,
        "source": z.source, "source_ref": z.source_ref, "source_meta": z.source_meta,
        "vertices": geo.vertex_count(z.geometry or {}),
        "geometry": z.geometry if geometry else None,
        "outline": clustering_service.corridor_outline(z) if geometry and z.kind == "road_section" else None,
        "created_by": z.created_by, "created_at": z.created_at, "updated_at": z.updated_at,
    }


def _snapshot(z: ClusterZone) -> dict:
    return {k: getattr(z, k) for k in ("name", "kind", "environment", "default_radius_m", "type_radius",
                                       "signalement_radius", "priority", "is_active", "country", "city",
                                       "corridor_width_m", "source", "source_ref")}


def _check_geometry(kind: str, geometry: dict, corridor_width_m: Optional[float], simplify: bool = True) -> dict:
    try:
        g = geo.validate_geometry(geometry, allow_lines=kind == "road_section")
    except ValueError as exc:
        raise api_error(422, "invalid_geometry", str(exc))
    if kind == "road_section" and g["type"] not in ("LineString", "MultiLineString"):
        raise api_error(422, "invalid_geometry", "A road section's geometry is a LineString (the road).")
    if kind == "road_section" and not corridor_width_m:
        raise api_error(422, "invalid_geometry", "A road section needs corridor_width_m.")
    if simplify:
        g = geo.simplify(g, 0.0005 if kind != "road_section" else 0.0001, 2000)
    return g


def _apply_geometry(z: ClusterZone, g: dict) -> None:
    z.geometry = g
    for k, v in clustering_service.geometry_fields(g, z.corridor_width_m).items():
        setattr(z, k, v)


ZoneId = Annotated[int, Path(description="Cluster zone id.", examples=[3])]
E_NOT_FOUND = err(404, "not_found", "Cluster zone not found.")
E_GEOMETRY = err(422, "invalid_geometry", "a polygon ring needs at least 3 points")


async def _get(db: AsyncSession, zone_id: int) -> ClusterZone:
    z = await db.get(ClusterZone, zone_id)
    if z is None:
        raise api_error(404, "not_found", "Cluster zone not found.")
    return z


@zones_router.get("/", response_model=list[ZoneOut], summary="List cluster zones")
async def list_zones(kind: Optional[Kind] = Query(None, description="Only this kind."),
                     environment: Optional[Env] = Query(None, description="Only this environment."),
                     active: Optional[bool] = Query(None, description="Only active (`true`) / inactive (`false`) zones."),
                     q: Optional[str] = Query(None, description="Name contains (case-insensitive)."),
                     geometry: bool = Query(False, description="Include the GeoJSON geometry."),
                     db: AsyncSession = Depends(get_db)):
    """Every cluster zone, by name (geometry omitted unless `geometry=true` — use
    `GET /cluster-zones/geojson` for the map)."""
    stmt = select(ClusterZone).order_by(ClusterZone.name)
    if kind:
        stmt = stmt.where(ClusterZone.kind == kind)
    if environment:
        stmt = stmt.where(ClusterZone.environment == environment)
    if active is not None:
        stmt = stmt.where(ClusterZone.is_active.is_(active))
    if q:
        stmt = stmt.where(ClusterZone.name.ilike(f"%{q.strip()}%"))
    return [_out(z, geometry) for z in (await db.execute(stmt)).scalars().all()]


@zones_router.get("/geojson", summary="Cluster zones as a GeoJSON FeatureCollection",
                  response_model=dict[str, Any])
async def zones_geojson(kind: Optional[Kind] = Query(None, description="Only this kind."),
                        environment: Optional[Env] = Query(None, description="Only this environment."),
                        active: Optional[bool] = Query(None, description="Only active / inactive zones."),
                        db: AsyncSession = Depends(get_db)):
    """
    The zones for the map: a FeatureCollection whose features have the zone's display geometry
    (polygons as stored; road sections as their corridor outline, the road itself in
    `properties.centerline`) and properties `id, name, kind, environment, is_active, priority,
    default_radius_m, type_radius, signalement_radius, corridor_width_m, area_km2`.
    """
    stmt = select(ClusterZone).order_by(ClusterZone.name)
    if kind:
        stmt = stmt.where(ClusterZone.kind == kind)
    if environment:
        stmt = stmt.where(ClusterZone.environment == environment)
    if active is not None:
        stmt = stmt.where(ClusterZone.is_active.is_(active))
    features = []
    for z in (await db.execute(stmt)).scalars().all():
        outline = clustering_service.corridor_outline(z) if z.kind == "road_section" else None
        features.append({
            "type": "Feature", "id": z.id,
            "geometry": outline or z.geometry,
            "properties": {
                "id": z.id, "name": z.name, "kind": z.kind, "environment": z.environment, "is_active": z.is_active,
                "priority": z.priority, "default_radius_m": z.default_radius_m, "type_radius": z.type_radius,
                "signalement_radius": z.signalement_radius, "corridor_width_m": z.corridor_width_m,
                "area_km2": z.area_km2, "centerline": z.geometry if outline else None,
            },
        })
    return {"type": "FeatureCollection", "features": features}


class BoundaryCandidate(BaseModel):
    """A place found by the boundary search, with its (simplified) outline."""
    display_name: str = Field(examples=["Communauté urbaine de Douala, Wouri, Littoral, Cameroun"])
    name: str = Field(examples=["Communauté urbaine de Douala"])
    osm_type: Optional[str] = Field(examples=["relation"])
    osm_id: Optional[int] = Field(examples=[3832073])
    class_: Optional[str] = Field(None, alias="class", serialization_alias="class", examples=["boundary"])
    type: Optional[str] = Field(examples=["administrative"])
    addresstype: Optional[str] = Field(None, examples=["municipality"])
    lat: float
    lon: float
    bbox: Optional[list[float]] = Field(description="`[min_lat, min_lon, max_lat, max_lon]`.")
    area_km2: float
    vertices: int
    approximate: bool = Field(description="`true` when OSM has no boundary (a circle around the place node).")
    geometry: dict[str, Any] = Field(description="Simplified GeoJSON Polygon / MultiPolygon.")

    model_config = {"populate_by_name": True}


@zones_router.get("/boundary-search", response_model=list[BoundaryCandidate], response_model_by_alias=True,
                  summary="Search a city / area boundary (OpenStreetMap)",
                  responses=errors(err(502, "boundary_search_failed", "The boundary search service is unreachable.")))
async def boundary_search(q: str = Query(..., min_length=2, max_length=200, description="Place name.",
                                         examples=["Douala"]),
                          limit: int = Query(6, ge=1, le=10, description="Maximum candidates."),
                          countrycodes: Optional[str] = Query(None, description=(
                              "ISO country codes, comma separated (default: CLUSTERING_BOUNDARY_COUNTRYCODES, `cm`; "
                              "empty = worldwide)."), examples=["cm"])):
    """
    Candidate outlines for a place name through Nominatim (OpenStreetMap) — e.g. `Douala` → *Communauté
    urbaine de Douala* (administrative boundary). Polygons are simplified (≤ 2000 vertices); places
    without a boundary in OSM come back as a circle (`approximate`). Cached 24 h; ≤ 1 upstream request/s.
    Pick one and `POST /cluster-zones/` with its `geometry` (`source: nominatim`, `source_ref:
    "<osm_type>/<osm_id>"`).
    """
    try:
        return await boundaries.search(q, limit, countrycodes)
    except boundaries.BoundarySearchError as exc:
        raise api_error(502, "boundary_search_failed", str(exc))


class ResolutionOut(BaseModel):
    """Which radius applies to an item at a point, and where it comes from."""
    radius_m: float = Field(examples=[25])
    environment: Env
    source: Literal["zone_type", "zone_default", "profile_type", "profile_default"]
    category: Literal["detection", "signalement"]
    type: Optional[str]
    zone_id: Optional[int]
    zone_name: Optional[str]
    zone_kind: Optional[str]


class PointResolutionOut(BaseModel):
    """Resolution of every type at a point ("test a point")."""
    zone: Optional[dict[str, Any]] = Field(description="Zone containing the point (`id, name, kind, environment, "
                                                       "priority`) or null.")
    environment: Env
    default: ResolutionOut
    detections: dict[str, ResolutionOut]
    signalements: dict[str, ResolutionOut]
    resolution: Optional[ResolutionOut] = Field(None, description="The asked category/type, when `type` is given.")


@zones_router.get("/resolve", response_model=PointResolutionOut, summary="Resolve the clustering radius at a point")
async def resolve_point(lat: float = Query(..., ge=-90, le=90, description="Latitude (WGS84).", examples=[4.05]),
                        lon: float = Query(..., ge=-180, le=180, description="Longitude (WGS84).", examples=[9.70]),
                        category: Literal["detection", "signalement"] = Query("detection"),
                        type: Optional[str] = Query(None, description="Detection or report type.",
                                                    examples=["pothole"]),
                        db: AsyncSession = Depends(get_db)):
    """
    The radius the platform would use at this point for every detection type and report type (and for
    `category` / `type` when given), with the zone and the rule it came from (`zone_type`,
    `zone_default`, `profile_type`, `profile_default`). Uses the live configuration (≤ 10 s cache).
    """
    snap = await clustering_service.get_snapshot(db)
    out = resolve_all(snap, lat, lon)
    if type:
        out["resolution"] = resolve(snap, lat, lon, category, type).as_dict()
    return out


class RoadSectionIn(BaseModel):
    """A rural road-section zone built from a route: a predefined tronçon (`route_key`, settings section
    `routes`) or two points. The OSRM driving route between them becomes the corridor's centre line."""
    name: Optional[str] = Field(None, max_length=120, description="Display name (default: the tronçon's name).",
                                examples=["Douala – Yaoundé (N3)"])
    route_key: Optional[str] = Field(None, description="Id of a predefined road section.", examples=["douala-yaounde-n3"])
    start: Optional[list[float]] = Field(None, min_length=2, max_length=2, description="`[lat, lon]` start.",
                                         examples=[[4.0511, 9.7679]])
    end: Optional[list[float]] = Field(None, min_length=2, max_length=2, description="`[lat, lon]` end.",
                                       examples=[[3.848, 11.5021]])
    corridor_width_m: Optional[float] = Field(None, ge=20, le=5000, description=(
        "Total corridor width, metres (default: `default_corridor_width_m`)."))
    environment: Env = Field("rural", description="Usually `rural` (out of agglomeration).")
    default_radius_m:   Optional[float] = Field(None, ge=MIN_RADIUS_M, le=MAX_RADIUS_M)
    type_radius:        Optional[dict[str, float]] = None
    signalement_radius: Optional[dict[str, float]] = None
    priority:  Optional[int] = Field(None, ge=-1000, le=1000)
    is_active: bool = True
    preview:   bool = Field(False, description="Only compute and return the corridor (nothing is saved).")

    @field_validator("type_radius")
    @classmethod
    def _tr(cls, v):
        return _radius_map(v, False, "type_radius")

    @field_validator("signalement_radius")
    @classmethod
    def _sr(cls, v):
        return _radius_map(v, True, "signalement_radius")


class RoadSectionOut(BaseModel):
    """The saved zone (or the preview when `preview=true`)."""
    zone: Optional[ZoneOut] = Field(None, description="The saved zone (null for a preview).")
    name: str
    geometry: dict[str, Any] = Field(description="Route centre line (GeoJSON LineString).")
    outline: Optional[dict[str, Any]] = Field(description="Corridor outline polygon (display).")
    corridor_width_m: float
    distance_m: float
    area_km2: float


@zones_router.post("/road-section", response_model=RoadSectionOut, status_code=201,
                   summary="Create (or preview) a road-section zone from a route",
                   responses=errors(err(404, "route_not_found", "No route found between these points."),
                                    err(422, "invalid_road_section", "Give route_key, or start and end."),
                                    err(502, "routing_unavailable", "The routing service is unavailable.")))
async def create_road_section(body: RoadSectionIn, request: Request, db: AsyncSession = Depends(get_db),
                              principal: Principal = Depends(get_principal)):
    """
    Computes the OSRM driving route of a predefined tronçon (`route_key`) or between `start` and `end`,
    and saves it as a `road_section` zone (environment `rural` by default): every detection / report
    within `corridor_width_m / 2` of the road uses that zone's configuration — e.g. *Douala – Yaoundé*,
    out of the cities. Where the corridor crosses an agglomeration, the agglomeration wins (higher default
    priority). `preview=true` returns the corridor without saving. Audited as `cluster_zone.create`.
    """
    cfg: ClusteringSettings = await clustering_service.load_settings(db)
    width = body.corridor_width_m or cfg.default_corridor_width_m
    name = body.name
    if body.route_key:
        routes: RoutesSettings = await get_cached(db, "routes")  # type: ignore[assignment]
        sec = next((s for s in routes.sections if s.id == body.route_key), None)
        if sec is None:
            raise api_error(422, "invalid_road_section", f"Unknown road section '{body.route_key}'.")
        a, b = (sec.from_.lat, sec.from_.lon), (sec.to.lat, sec.to.lon)
        name = name or sec.name
    elif body.start and body.end:
        a, b = (body.start[0], body.start[1]), (body.end[0], body.end[1])
    else:
        raise api_error(422, "invalid_road_section", "Give route_key, or start and end.")
    if not name:
        raise api_error(422, "invalid_road_section", "Give a name.")
    try:
        result = await get_osrm_client().route_overview([a, b], alternatives=False)
    except OSRMUnavailableError as exc:
        raise api_error(502, "routing_unavailable", f"The routing service is unavailable: {exc}")
    data = result.data
    if data.get("code") != "Ok" or not data.get("routes"):
        raise api_error(404, "route_not_found", data.get("message") or "No route found between these points.")
    route = data["routes"][0]
    line = geo.simplify({"type": "LineString", "coordinates": route["geometry"]["coordinates"]}, 0.0001, 2000)
    z = ClusterZone(name=name, kind="road_section", environment=body.environment, corridor_width_m=width,
                    source="route", source_ref=body.route_key or f"{a[0]:.5f},{a[1]:.5f};{b[0]:.5f},{b[1]:.5f}",
                    source_meta={"distance_m": route.get("distance"), "duration_s": route.get("duration"),
                                 "from": list(a), "to": list(b)},
                    default_radius_m=body.default_radius_m, type_radius=body.type_radius,
                    signalement_radius=body.signalement_radius,
                    priority=body.priority if body.priority is not None else DEFAULT_PRIORITY["road_section"],
                    is_active=body.is_active, created_by=principal.label)
    _apply_geometry(z, line)
    out = {"name": name, "geometry": line, "outline": clustering_service.corridor_outline(z),
           "corridor_width_m": width, "distance_m": float(route.get("distance") or 0.0), "area_km2": z.area_km2,
           "zone": None}
    if body.preview:
        return out
    z.created_at = z.updated_at = datetime.utcnow()
    db.add(z)
    await db.flush()
    audit.record(db, request, "cluster_zone.create", principal=principal, object_type="cluster_zone",
                 object_id=str(z.id), details=_snapshot(z))
    await db.commit()
    await db.refresh(z)
    clustering_service.invalidate()
    out["zone"] = _out(z, True)
    return out


@zones_router.post("/", response_model=ZoneOut, status_code=201, summary="Create a cluster zone",
                   responses=errors(E_GEOMETRY))
async def create_zone(body: ZoneIn, request: Request, db: AsyncSession = Depends(get_db),
                      principal: Principal = Depends(get_principal)):
    """
    Add an agglomeration (e.g. the outline picked from `GET /cluster-zones/boundary-search`), a
    hand-drawn `custom` polygon, or a `road_section` LineString. Null overrides inherit the environment
    profile. A change applies to detections and reports clustered from now on; existing hazards are re-clustered only by `POST /clustering/rebuild-hazards` (or `python -m app.cli rebuild-hazards`). Audited as `cluster_zone.create`.
    """
    g = _check_geometry(body.kind, body.geometry, body.corridor_width_m, body.simplify)
    env = body.environment or ("rural" if body.kind == "road_section" else "urban")
    z = ClusterZone(
        name=body.name.strip(), kind=body.kind, environment=env,
        corridor_width_m=body.corridor_width_m if body.kind == "road_section" else None,
        source=body.source, source_ref=body.source_ref, source_meta=body.source_meta,
        default_radius_m=body.default_radius_m, type_radius=body.type_radius,
        signalement_radius=body.signalement_radius,
        priority=body.priority if body.priority is not None else DEFAULT_PRIORITY[body.kind],
        is_active=body.is_active, country=body.country, city=body.city, created_by=principal.label,
        created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
    )
    _apply_geometry(z, g)
    db.add(z)
    await db.flush()
    audit.record(db, request, "cluster_zone.create", principal=principal, object_type="cluster_zone",
                 object_id=str(z.id), details=_snapshot(z))
    await db.commit()
    await db.refresh(z)
    clustering_service.invalidate()
    return _out(z, True)


@zones_router.get("/{zone_id}", response_model=ZoneOut, summary="Get a cluster zone", responses=errors(E_NOT_FOUND))
async def get_zone(zone_id: ZoneId, db: AsyncSession = Depends(get_db)):
    """One zone with its geometry (and the corridor outline of a road section)."""
    return _out(await _get(db, zone_id), True)


@zones_router.patch("/{zone_id}", response_model=ZoneOut, summary="Update a cluster zone",
                    responses=errors(E_NOT_FOUND, E_GEOMETRY))
async def update_zone(zone_id: ZoneId, body: ZonePatch, request: Request, db: AsyncSession = Depends(get_db),
                      principal: Principal = Depends(get_principal)):
    """Change the fields sent (see `ZonePatch`). A change applies to detections and reports clustered from now on; existing hazards are re-clustered only by `POST /clustering/rebuild-hazards` (or `python -m app.cli rebuild-hazards`). Audited as `cluster_zone.update`."""
    z = await _get(db, zone_id)
    before = _snapshot(z)
    data = body.model_dump(exclude_unset=True)
    geometry = data.pop("geometry", None)
    nullable = {"default_radius_m", "type_radius", "signalement_radius", "country", "city"}
    for key, value in data.items():
        if key == "priority" and value is None:
            z.priority = DEFAULT_PRIORITY.get(z.kind, 0)
            continue
        if value is None and key not in nullable:
            continue
        if key == "corridor_width_m" and z.kind != "road_section":
            continue
        setattr(z, key, value.strip() if key == "name" else value)
    if geometry is not None:
        _apply_geometry(z, _check_geometry(z.kind, geometry, z.corridor_width_m))
        if z.source == "nominatim":
            z.source = "drawn"
    elif "corridor_width_m" in data and z.kind == "road_section":
        _apply_geometry(z, z.geometry)
    z.updated_at = datetime.utcnow()
    audit.record(db, request, "cluster_zone.update", principal=principal, object_type="cluster_zone",
                 object_id=str(z.id), details={**audit.diff(before, _snapshot(z)),
                                               **({"geometry": "changed"} if geometry is not None else {})})
    await db.commit()
    await db.refresh(z)
    clustering_service.invalidate()
    return _out(z, True)


@zones_router.delete("/{zone_id}", status_code=204, summary="Delete a cluster zone", responses=errors(E_NOT_FOUND))
async def delete_zone(zone_id: ZoneId, request: Request, db: AsyncSession = Depends(get_db),
                      principal: Principal = Depends(get_principal)):
    """Remove the zone; its places fall back to another zone or the unzoned profile for what is clustered
    afterwards. Hazards keep the zone name they were created with. Audited as `cluster_zone.delete`."""
    z = await _get(db, zone_id)
    audit.record(db, request, "cluster_zone.delete", principal=principal, object_type="cluster_zone",
                 object_id=str(z.id), details=_snapshot(z))
    await db.delete(z)
    await db.commit()
    clustering_service.invalidate()
