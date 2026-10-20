"""
Pure geometry for cluster zones (no PostGIS, no shapely).

Geometries are GeoJSON dicts in WGS84 ([lon, lat] order):
  Polygon / MultiPolygon        agglomerations and custom zones (holes ok)
  LineString + width_m          road-section corridors: a point is inside
                                when its distance to the polyline is
                                ≤ width_m / 2.

Distances use a local equirectangular projection (error < 0.1 % over the
few kilometres that matter here); areas use the same projection around
each ring's mean latitude.
"""
from dataclasses import dataclass, field
from math import asin, atan2, cos, pi, radians, sin, sqrt
from typing import Any, Iterable, Optional, Sequence

EARTH_R = 6_371_000.0
M_PER_DEG = 111_320.0

BBox = tuple[float, float, float, float]   # min_lat, min_lon, max_lat, max_lon


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * EARTH_R * asin(min(1.0, sqrt(a)))


# ── polygons ──────────────────────────────────────────────────────────────────

def _point_in_ring(lon: float, lat: float, ring: Sequence[Sequence[float]]) -> bool:
    """Even-odd ray casting; `ring` = [[lon, lat], …] (closed or not)."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat):
            x_cross = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


def point_in_polygon(lat: float, lon: float, rings: Sequence) -> bool:
    """Inside the outer ring (rings[0]) and outside every hole (rings[1:])."""
    if not rings or not _point_in_ring(lon, lat, rings[0]):
        return False
    return not any(_point_in_ring(lon, lat, hole) for hole in rings[1:])


def polygons_of(geometry: dict) -> list:
    """Polygon / MultiPolygon → list of polygons (each a list of rings)."""
    t = geometry.get("type")
    if t == "Polygon":
        return [geometry["coordinates"]]
    if t == "MultiPolygon":
        return list(geometry["coordinates"])
    return []


def ring_area_m2(ring: Sequence[Sequence[float]]) -> float:
    if len(ring) < 3:
        return 0.0
    lat0 = sum(p[1] for p in ring) / len(ring)
    kx = M_PER_DEG * cos(radians(lat0))
    s = 0.0
    for i in range(len(ring)):
        x1, y1 = ring[i][0] * kx, ring[i][1] * M_PER_DEG
        x2, y2 = ring[(i + 1) % len(ring)][0] * kx, ring[(i + 1) % len(ring)][1] * M_PER_DEG
        s += x1 * y2 - x2 * y1
    return abs(s) / 2


def polygon_area_m2(geometry: dict) -> float:
    return sum(max(0.0, ring_area_m2(p[0]) - sum(ring_area_m2(h) for h in p[1:]))
               for p in polygons_of(geometry) if p)


# ── polylines / corridors ─────────────────────────────────────────────────────

def point_segment_m(lat: float, lon: float, a: Sequence[float], b: Sequence[float]) -> float:
    """Distance (m) from (lat, lon) to segment a→b ([lon, lat] points)."""
    kx = M_PER_DEG * cos(radians(lat))
    ax, ay = (a[0] - lon) * kx, (a[1] - lat) * M_PER_DEG
    bx, by = (b[0] - lon) * kx, (b[1] - lat) * M_PER_DEG
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    t = 0.0 if seg2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / seg2))
    cx, cy = ax + t * dx, ay + t * dy
    return sqrt(cx * cx + cy * cy)


def point_polyline_m(lat: float, lon: float, line: Sequence[Sequence[float]],
                     max_m: Optional[float] = None) -> float:
    """Distance (m) to the polyline; segments whose bbox is farther than
    `max_m` are skipped (returns inf when none is near)."""
    if len(line) == 1:
        return haversine_m(lat, lon, line[0][1], line[0][0])
    best = float("inf")
    if max_m is not None:
        dlat = max_m / M_PER_DEG
        dlon = max_m / (M_PER_DEG * max(cos(radians(lat)), 1e-6))
    for i in range(len(line) - 1):
        a, b = line[i], line[i + 1]
        if max_m is not None and (
            lat < min(a[1], b[1]) - dlat or lat > max(a[1], b[1]) + dlat
            or lon < min(a[0], b[0]) - dlon or lon > max(a[0], b[0]) + dlon
        ):
            continue
        d = point_segment_m(lat, lon, a, b)
        if d < best:
            best = d
    return best


def line_length_m(line: Sequence[Sequence[float]]) -> float:
    return sum(haversine_m(line[i][1], line[i][0], line[i + 1][1], line[i + 1][0]) for i in range(len(line) - 1))


def lines_of(geometry: dict) -> list:
    t = geometry.get("type")
    if t == "LineString":
        return [geometry["coordinates"]]
    if t == "MultiLineString":
        return list(geometry["coordinates"])
    return []


def corridor_polygon(line: Sequence[Sequence[float]], width_m: float) -> list[list[float]]:
    """Approximate outline of a corridor (for display only): the polyline
    offset by width_m / 2 on both sides, joined with round caps. Containment
    always uses the exact point-to-polyline distance."""
    if len(line) < 2:
        return []
    half = width_m / 2
    left: list[list[float]] = []
    right: list[list[float]] = []
    n = len(line)
    for i in range(n):
        lon, lat = line[i][0], line[i][1]
        kx = M_PER_DEG * cos(radians(lat))
        # averaged direction of the adjacent segments
        p = line[max(0, i - 1)]
        q = line[min(n - 1, i + 1)]
        dx, dy = (q[0] - p[0]) * kx, (q[1] - p[1]) * M_PER_DEG
        norm = sqrt(dx * dx + dy * dy) or 1.0
        nx, ny = -dy / norm, dx / norm
        left.append([lon + nx * half / kx, lat + ny * half / M_PER_DEG])
        right.append([lon - nx * half / kx, lat - ny * half / M_PER_DEG])

    def cap(center, start_angle):
        """Half-circle points (exclusive of the ends), clockwise from start_angle."""
        lon, lat = center[0], center[1]
        kx = M_PER_DEG * cos(radians(lat))
        return [[lon + cos(start_angle - pi * k / 8) * half / kx, lat + sin(start_angle - pi * k / 8) * half / M_PER_DEG]
                for k in range(1, 8)]

    def heading(a, b):
        kx = M_PER_DEG * cos(radians(a[1]))
        return atan2((b[1] - a[1]) * M_PER_DEG, (b[0] - a[0]) * kx)

    end_h = heading(line[-2], line[-1])
    start_h = heading(line[0], line[1])
    ring = left + cap(line[-1], end_h + pi / 2) + right[::-1] + cap(line[0], start_h - pi / 2)
    ring.append(ring[0])
    return ring


# ── bbox / circles / simplification ───────────────────────────────────────────

def _coords(geometry: dict) -> Iterable[Sequence[float]]:
    for poly in polygons_of(geometry):
        for ring in poly:
            yield from ring
    for line in lines_of(geometry):
        yield from line
    if geometry.get("type") == "Point":
        yield geometry["coordinates"]


def bbox_of(geometry: dict, pad_m: float = 0.0) -> Optional[BBox]:
    pts = list(_coords(geometry))
    if not pts:
        return None
    min_lat = min(p[1] for p in pts)
    max_lat = max(p[1] for p in pts)
    min_lon = min(p[0] for p in pts)
    max_lon = max(p[0] for p in pts)
    if pad_m:
        dlat = pad_m / M_PER_DEG
        dlon = pad_m / (M_PER_DEG * max(cos(radians(max(abs(min_lat), abs(max_lat)))), 1e-6))
        min_lat, max_lat, min_lon, max_lon = min_lat - dlat, max_lat + dlat, min_lon - dlon, max_lon + dlon
    return (min_lat, min_lon, max_lat, max_lon)


def circle_polygon(lat: float, lon: float, radius_m: float, vertices: int = 64) -> dict:
    """A circle as a closed GeoJSON Polygon with `vertices` points."""
    kx = M_PER_DEG * cos(radians(lat))
    ring = [[round(lon + cos(2 * pi * k / vertices) * radius_m / kx, 7),
             round(lat + sin(2 * pi * k / vertices) * radius_m / M_PER_DEG, 7)] for k in range(vertices)]
    ring.append(ring[0])
    return {"type": "Polygon", "coordinates": [ring]}


def _dp(points: list, tol: float) -> list:
    """Douglas-Peucker (iterative) in degree space."""
    n = len(points)
    if n < 3:
        return list(points)
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        s, e = stack.pop()
        ax, ay = points[s][0], points[s][1]
        bx, by = points[e][0], points[e][1]
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        best_i, best_d = -1, -1.0
        for i in range(s + 1, e):
            px, py = points[i][0] - ax, points[i][1] - ay
            if seg2 == 0:
                d = px * px + py * py
            else:
                t = max(0.0, min(1.0, (px * dx + py * dy) / seg2))
                qx, qy = px - t * dx, py - t * dy
                d = qx * qx + qy * qy
            if d > best_d:
                best_i, best_d = i, d
        if best_i >= 0 and best_d > tol * tol:
            keep[best_i] = True
            stack.append((s, best_i))
            stack.append((best_i, e))
    return [p for p, k in zip(points, keep) if k]


def _simplify_ring(ring: list, tol: float) -> Optional[list]:
    closed = ring[0] == ring[-1] if ring else False
    pts = ring[:-1] if closed else list(ring)
    if len(pts) < 4:
        return ring if len(pts) >= 3 else None
    # split the closed ring in two chains so the ends are well defined
    far = max(range(len(pts)), key=lambda i: (pts[i][0] - pts[0][0]) ** 2 + (pts[i][1] - pts[0][1]) ** 2)
    a = _dp(pts[: far + 1], tol)
    b = _dp(pts[far:] + [pts[0]], tol)
    out = a[:-1] + b[:-1]
    if len(out) < 3:
        return None
    out = [[round(p[0], 7), round(p[1], 7)] for p in out]
    out.append(out[0])
    return out


def vertex_count(geometry: dict) -> int:
    return sum(1 for _ in _coords(geometry))


def simplify(geometry: dict, tolerance_deg: float = 0.0005, max_vertices: int = 2000) -> dict:
    """Douglas-Peucker on every ring / line; the tolerance doubles until the
    geometry has ≤ max_vertices. Rings that collapse are dropped (a polygon
    whose outer ring collapses is dropped with its holes)."""
    tol = tolerance_deg
    t = geometry.get("type")
    for _ in range(12):
        if t in ("Polygon", "MultiPolygon"):
            polys = []
            for poly in polygons_of(geometry):
                outer = _simplify_ring(list(poly[0]), tol) if poly else None
                if outer is None:
                    continue
                holes = [h for h in (_simplify_ring(list(r), tol) for r in poly[1:]) if h is not None]
                polys.append([outer, *holes])
            if not polys:            # everything collapsed: keep the original
                out = geometry
            else:
                out = ({"type": "Polygon", "coordinates": polys[0]} if len(polys) == 1
                       else {"type": "MultiPolygon", "coordinates": polys})
        elif t == "LineString":
            out = {"type": "LineString",
                   "coordinates": [[round(p[0], 7), round(p[1], 7)] for p in _dp(list(geometry["coordinates"]), tol)]}
        else:
            return geometry
        if vertex_count(out) <= max_vertices:
            return out
        tol *= 2
    return out


# ── compiled zone (what the resolver checks) ──────────────────────────────────

@dataclass
class Shape:
    """A zone's geometry compiled for fast containment tests."""
    polygons: list = field(default_factory=list)       # [[outer, *holes], …]
    lines: list = field(default_factory=list)          # [[[lon, lat], …], …]
    half_width_m: float = 0.0
    bbox: Optional[BBox] = None
    area_m2: float = 0.0

    def contains(self, lat: float, lon: float) -> bool:
        b = self.bbox
        if b is None or not (b[0] <= lat <= b[2] and b[1] <= lon <= b[3]):
            return False
        for poly in self.polygons:
            if point_in_polygon(lat, lon, poly):
                return True
        for line in self.lines:
            if point_polyline_m(lat, lon, line, self.half_width_m) <= self.half_width_m:
                return True
        return False


def compile_shape(geometry: dict, corridor_width_m: Optional[float] = None) -> Shape:
    """Polygon/MultiPolygon → polygon shape; LineString/MultiLineString →
    corridor of total width `corridor_width_m`."""
    polys = polygons_of(geometry)
    lines = lines_of(geometry)
    half = (corridor_width_m or 0.0) / 2 if lines else 0.0
    area = polygon_area_m2(geometry) + sum(line_length_m(ln) for ln in lines) * (corridor_width_m or 0.0)
    return Shape(polygons=polys, lines=lines, half_width_m=half,
                 bbox=bbox_of(geometry, pad_m=half), area_m2=area)


def validate_geometry(geometry: Any, *, allow_lines: bool) -> dict:
    """Light structural check of a GeoJSON geometry; raises ValueError."""
    if not isinstance(geometry, dict) or "type" not in geometry or "coordinates" not in geometry:
        raise ValueError("geometry must be a GeoJSON geometry object")
    t = geometry["type"]
    allowed = {"Polygon", "MultiPolygon"} | ({"LineString", "MultiLineString"} if allow_lines else set())
    if t not in allowed:
        raise ValueError(f"geometry type must be one of {sorted(allowed)}")

    def pt(p):
        if (not isinstance(p, (list, tuple)) or len(p) < 2 or not all(isinstance(x, (int, float)) for x in p[:2])
                or not (-180 <= p[0] <= 180 and -90 <= p[1] <= 90)):
            raise ValueError("coordinates must be [lon, lat] pairs in WGS84")
        return [float(p[0]), float(p[1])]

    def ring(r):
        pts = [pt(p) for p in r]
        if len(pts) < 3:
            raise ValueError("a polygon ring needs at least 3 points")
        if pts[0] != pts[-1]:
            pts.append(pts[0])
        return pts

    c = geometry["coordinates"]
    if t == "Polygon":
        coords: Any = [ring(r) for r in c]
    elif t == "MultiPolygon":
        coords = [[ring(r) for r in poly] for poly in c]
    elif t == "LineString":
        coords = [pt(p) for p in c]
        if len(coords) < 2:
            raise ValueError("a line needs at least 2 points")
    else:
        coords = [[pt(p) for p in ln] for ln in c]
    if not coords:
        raise ValueError("geometry is empty")
    return {"type": t, "coordinates": coords}
