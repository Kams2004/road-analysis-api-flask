"""
Path filter of the reports: "only the potholes on Douala – Bafoussam".

A path is a predefined road section (settings `routes`), a from → to pair of
places (driving route through the platform's OSRM server, like the console's
route searches), or an explicit polyline. Items are kept when they lie within
`corridor_m` of the polyline and are ordered by their distance along it.
"""
import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from app.modules.m03_maps.osrm import geo
from app.modules.m06_hazards.clustering.zones import point_polyline_m

LatLon = Tuple[float, float]


class PathError(Exception):
    """The path could not be resolved (unknown section, no route, OSRM down)."""


@dataclass
class Path:
    name: str
    points: List[LatLon]                      # (lat, lon)
    corridor_m: float
    length_m: float = 0.0
    source: str = "waypoints"                 # section | route | waypoints
    _line: list = field(default_factory=list, repr=False)       # [[lon, lat]] for point_polyline_m
    _bbox: Tuple[float, float, float, float] = (0, 0, 0, 0)

    def __post_init__(self):
        self._line = [[lon, lat] for lat, lon in self.points]
        self.length_m = geo.cumulative_distances_m(self.points)[-1] if len(self.points) > 1 else 0.0
        lats = [p[0] for p in self.points]
        lons = [p[1] for p in self.points]
        pad_lat = self.corridor_m / 111_320.0
        mid = math.radians((min(lats) + max(lats)) / 2)
        pad_lon = pad_lat / max(math.cos(mid), 0.1)
        self._bbox = (min(lats) - pad_lat, min(lons) - pad_lon, max(lats) + pad_lat, max(lons) + pad_lon)

    @property
    def bbox(self) -> Tuple[float, float, float, float]:
        """south, west, north, east around the path + corridor (SQL pre-filter)."""
        return self._bbox

    def offset_m(self, lat: float, lon: float) -> float:
        """Distance from the path, metres (inf when farther than the corridor)."""
        return point_polyline_m(lat, lon, self._line, self.corridor_m)

    def contains(self, lat: Optional[float], lon: Optional[float]) -> bool:
        if lat is None or lon is None:
            return False
        s, w, n, e = self._bbox
        if not (s <= lat <= n and w <= lon <= e):
            return False
        return self.offset_m(lat, lon) <= self.corridor_m

    def km_along(self, lat: float, lon: float) -> float:
        along, _ = geo.project_point_onto_polyline((lat, lon), self.points)
        return round(along / 1000.0, 2)

    def keep(self, items: Sequence, lat_of=lambda x: x.latitude, lon_of=lambda x: x.longitude) -> List[tuple]:
        """[(km_along, offset_m, item)] of the items inside the corridor, in path order."""
        out = []
        for it in items:
            lat, lon = lat_of(it), lon_of(it)
            if self.contains(lat, lon):
                out.append((self.km_along(lat, lon), round(self.offset_m(lat, lon), 1), it))
        out.sort(key=lambda t: t[0])
        return out

    def describe(self) -> dict:
        return {"name": self.name, "source": self.source, "length_km": round(self.length_m / 1000, 1),
                "corridor_m": self.corridor_m, "start": list(self.points[0]), "end": list(self.points[-1])}


async def resolve(spec: dict, corridor_m: float, sections: Sequence, osrm=None) -> Optional[Path]:
    """spec: {section_id} | {from: {lat, lon, label}, to: {…}} | {waypoints: [[lat, lon], …], name}."""
    if not spec:
        return None
    if spec.get("waypoints"):
        pts = [(float(a), float(b)) for a, b in spec["waypoints"]]
        if len(pts) < 2:
            raise PathError("A path needs at least 2 waypoints.")
        return Path(spec.get("name") or "Path", pts, corridor_m, source="waypoints")
    name = spec.get("name")
    if spec.get("section_id"):
        sec = next((s for s in sections if s.id == spec["section_id"]), None)
        if sec is None:
            raise PathError(f"Unknown road section '{spec['section_id']}'.")
        a, b = (sec.from_.lat, sec.from_.lon), (sec.to.lat, sec.to.lon)
        name, source = name or sec.name, "section"
    elif spec.get("from") and spec.get("to"):
        f, t = spec["from"], spec["to"]
        a, b = (float(f["lat"]), float(f["lon"])), (float(t["lat"]), float(t["lon"]))
        name = name or f"{f.get('label') or _fmt(a)} - {t.get('label') or _fmt(b)}"
        source = "route"
    else:
        raise PathError("A path is a section_id, a from/to pair or waypoints.")
    if osrm is None:
        from app.modules.m03_maps.osrm.client import get_osrm_client
        osrm = get_osrm_client()
    try:
        result = await osrm.route_overview([a, b], alternatives=False)
    except Exception as e:  # OSRMUnavailableError and transport errors
        raise PathError(f"Routing service unavailable: {e}")
    data = result.data
    if data.get("code") != "Ok" or not data.get("routes"):
        raise PathError(data.get("message") or "No route found between these points.")
    coords = [(lat, lon) for lon, lat in data["routes"][0]["geometry"]["coordinates"]]
    return Path(name, coords, corridor_m, source=source)


def _fmt(p: LatLon) -> str:
    return f"{p[0]:.4f}, {p[1]:.4f}"
