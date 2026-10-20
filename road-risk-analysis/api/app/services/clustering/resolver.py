"""
Pure clustering-radius resolution (no database): which radius applies to a
detection / community report of a given type at a given place.

  zone    = the active zone containing the point with the highest priority,
            then the smallest area, then the lowest id (None when outside
            every zone);
  radius  = zone override for the type → zone default radius →
            profile[zone environment] override for the type →
            profile default.
            Without a zone the profile is settings.unzoned_environment.

`source` says where the value came from: zone_type | zone_default |
profile_type | profile_default.
"""
from dataclasses import asdict, dataclass, field
from typing import Literal, Optional

from app.services.clustering.settings import (
    DETECTION_TYPES, SIGNALEMENT_TYPES, ClusteringSettings,
)
from app.services.clustering.zones import Shape

Category = Literal["detection", "signalement"]

# Default priority per zone kind: a road-section corridor that crosses a city
# never overrides the city's agglomeration polygon.
DEFAULT_PRIORITY = {"agglomeration": 10, "custom": 10, "road_section": 0}


@dataclass
class ZoneRule:
    id: int
    name: str
    kind: str
    environment: str
    shape: Shape
    priority: int = 0
    default_radius_m: Optional[float] = None
    type_radius: dict = field(default_factory=dict)
    signalement_radius: dict = field(default_factory=dict)
    city: Optional[str] = None


@dataclass
class Snapshot:
    settings: ClusteringSettings = field(default_factory=ClusteringSettings)
    zones: list = field(default_factory=list)           # [ZoneRule]


@dataclass(frozen=True)
class Resolution:
    radius_m: float
    environment: str
    source: str
    category: str
    type: Optional[str]
    zone_id: Optional[int] = None
    zone_name: Optional[str] = None
    zone_kind: Optional[str] = None

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def label(self) -> str:
        """e.g. "Douala · urban · 25 m"."""
        where = self.zone_name or "Unzoned"
        return f"{where} · {self.environment} · {self.radius_m:g} m"


def zone_at(snapshot: Snapshot, lat: float, lon: float) -> Optional[ZoneRule]:
    best: Optional[ZoneRule] = None
    for z in snapshot.zones:
        if not z.shape.contains(lat, lon):
            continue
        if best is None or (-z.priority, z.shape.area_m2, z.id) < (-best.priority, best.shape.area_m2, best.id):
            best = z
    return best


def _pick(overrides: Optional[dict], kind: Optional[str]) -> Optional[float]:
    if not kind or not overrides:
        return None
    v = overrides.get(kind)
    return float(v) if v else None


def resolve(snapshot: Snapshot, lat: Optional[float], lon: Optional[float],
            category: Category = "detection", kind: Optional[str] = None,
            zone: Optional[ZoneRule] = None) -> Resolution:
    """The clustering radius at (lat, lon) for a `category` item of type
    `kind`. A missing position resolves against the unzoned profile."""
    s = snapshot.settings
    if zone is None and lat is not None and lon is not None:
        zone = zone_at(snapshot, lat, lon)
    env = zone.environment if zone is not None else s.unzoned_environment
    prof = s.profile(env)
    key = "signalement_radius" if category == "signalement" else "type_radius"

    def done(radius: float, source: str) -> Resolution:
        return Resolution(radius_m=float(radius), environment=env, source=source, category=category, type=kind,
                          zone_id=zone.id if zone else None, zone_name=zone.name if zone else None,
                          zone_kind=zone.kind if zone else None)

    if zone is not None:
        r = _pick(getattr(zone, key), kind)
        if r:
            return done(r, "zone_type")
        if zone.default_radius_m:
            return done(zone.default_radius_m, "zone_default")
    r = _pick(getattr(prof, key), kind)
    if r:
        return done(r, "profile_type")
    return done(prof.default_radius_m, "profile_default")


def resolve_all(snapshot: Snapshot, lat: float, lon: float) -> dict:
    """Every detection and report type at a point ("test a point" in the console)."""
    zone = zone_at(snapshot, lat, lon)
    det_types = list(dict.fromkeys([*DETECTION_TYPES, *snapshot.settings.urban.type_radius,
                                    *snapshot.settings.rural.type_radius, *((zone.type_radius or {}) if zone else {})]))
    return {
        "zone": ({"id": zone.id, "name": zone.name, "kind": zone.kind, "environment": zone.environment,
                  "priority": zone.priority} if zone else None),
        "environment": zone.environment if zone else snapshot.settings.unzoned_environment,
        "default": resolve(snapshot, lat, lon, "detection", None, zone=zone).as_dict(),
        "detections": {t: resolve(snapshot, lat, lon, "detection", t, zone=zone).as_dict() for t in det_types},
        "signalements": {t: resolve(snapshot, lat, lon, "signalement", t, zone=zone).as_dict()
                         for t in SIGNALEMENT_TYPES},
    }
