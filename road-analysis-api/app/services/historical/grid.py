"""
Stable geographic grid for aggregating historical events — see
app/services/historical/models.py's module docstring for why this exists
instead of keying aggregation by OSRM road_segment_id.

Pure function of (lat, lon, cell_size_m) — no external reference point, no
database lookup, so the same physical location always maps to the same
cell regardless of when/where it's computed. Cells are approximately square
(longitude step is corrected by cos(latitude) at the point itself), which
is accurate enough for the coarse (tens-to-hundreds of metres) cell sizes
this is meant for — not intended for survey-grade geodesy.
"""
import math
from typing import List, Set, Tuple

DEG_M = 111_320.0  # metres per degree of latitude, ~constant everywhere


def grid_cell_id(lat: float, lon: float, cell_size_m: float) -> str:
    lat_step = cell_size_m / DEG_M
    lon_step = cell_size_m / (DEG_M * max(math.cos(math.radians(lat)), 1e-6))

    lat_idx = math.floor(lat / lat_step)
    lon_idx = math.floor(lon / lon_step)
    return f"{cell_size_m:.0f}m:{lat_idx}:{lon_idx}"


def grid_cell_ids_in_radius(lat: float, lon: float, radius_m: float, cell_size_m: float) -> Set[str]:
    """Every grid cell whose bounding box could contain a point within
    radius_m of (lat, lon). Deliberately a superset (cell corners can be
    slightly further than radius_m from the centre) — callers still apply
    a precise distance check after fetching rows for these cells; this is
    only meant to replace an unindexed lat/lon bounding-box scan with an
    indexed grid_cell_id lookup, not to be the final distance filter."""
    lat_step = cell_size_m / DEG_M
    lon_step = cell_size_m / (DEG_M * max(math.cos(math.radians(lat)), 1e-6))
    lat_delta = radius_m / DEG_M
    lon_delta = radius_m / (DEG_M * max(math.cos(math.radians(lat)), 1e-6))

    min_lat_idx = math.floor((lat - lat_delta) / lat_step)
    max_lat_idx = math.floor((lat + lat_delta) / lat_step)
    min_lon_idx = math.floor((lon - lon_delta) / lon_step)
    max_lon_idx = math.floor((lon + lon_delta) / lon_step)

    return {
        f"{cell_size_m:.0f}m:{la}:{lo}"
        for la in range(min_lat_idx, max_lat_idx + 1)
        for lo in range(min_lon_idx, max_lon_idx + 1)
    }


def grid_cell_ids_along_route(
    waypoints: List[Tuple[float, float]], corridor_m: float, cell_size_m: float,
) -> Set[str]:
    """Union of grid_cell_ids_in_radius around densely-sampled points along
    a route's legs. Waypoints alone aren't dense enough to walk this
    directly — a simplified route can have long, mostly-straight legs
    (see lib/geo.ts's client-side simplification) that would skip over
    grid cells sitting between two distant kept points, so each leg is
    linearly re-sampled at roughly cell_size_m spacing first."""
    cells: Set[str] = set()
    if len(waypoints) < 2:
        return cells

    step_m = max(cell_size_m, 1.0)
    for i in range(len(waypoints) - 1):
        (lat_a, lon_a), (lat_b, lon_b) = waypoints[i], waypoints[i + 1]
        leg_len_m = _haversine_m(lat_a, lon_a, lat_b, lon_b)
        steps = max(1, math.ceil(leg_len_m / step_m))
        for s in range(steps + 1):
            t = s / steps
            lat = lat_a + t * (lat_b - lat_a)
            lon = lon_a + t * (lon_b - lon_a)
            cells |= grid_cell_ids_in_radius(lat, lon, corridor_m, cell_size_m)
    return cells


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def grid_cell_center(cell_id: str) -> Tuple[float, float]:
    """Inverse of grid_cell_id — the approximate centre point of a cell,
    for display/aggregation purposes."""
    size_part, lat_idx_s, lon_idx_s = cell_id.split(":")
    cell_size_m = float(size_part.rstrip("m"))
    lat_idx, lon_idx = int(lat_idx_s), int(lon_idx_s)

    lat_step = cell_size_m / DEG_M
    center_lat = (lat_idx + 0.5) * lat_step
    lon_step = cell_size_m / (DEG_M * max(math.cos(math.radians(center_lat)), 1e-6))
    center_lon = (lon_idx + 0.5) * lon_step
    return center_lat, center_lon
