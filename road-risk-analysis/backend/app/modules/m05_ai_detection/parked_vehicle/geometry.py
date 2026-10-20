"""
Pure geometry of the parked-vehicle pipeline — monocular flat-road pinhole
camera, the same model app.modules.m05_ai_detection.static_check uses for signs and potholes
but with a per-camera calibration (settings `camera` / `camera_profiles`).

Image coordinates are NORMALISED: x and y in 0..1 of the frame width /
height. The pinhole focal length is expressed in frame heights:
f = 0.5 / tan(vertical_fov / 2). A horizontal image offset is converted to
frame heights by multiplying by the aspect ratio (width / height).

  range_from_bottom   a grounded object's contact line (box bottom) below
                      the horizon: r = camera_height · f / (y_bottom − horizon)
  range_from_height   apparent height of an object of known size:
                      r = object_height · f / h
  lateral_offset      metres to the right of the optical axis at range r:
                      x = (cx − 0.5 − center_offset) · aspect · r / f
  ground contact      both ranges agree within a tolerance for a vehicle
                      standing on the road; a car on a billboard / a carrier
                      / a reflection sits higher in the image than its size
                      says, so its bottom range is much larger (or its
                      bottom is above the horizon).

World positions are local east/north metres (static_check.enu) or lat/lon
(app.modules.m03_maps.osrm.geo.destination). No I/O.
"""
from __future__ import annotations

import math
from typing import Iterable, Optional, Sequence

from app.modules.m03_maps.osrm.geo import destination
from app.modules.m05_ai_detection.static_check import enu  # noqa: F401  (re-exported: local east/north metres)

from .settings import CameraCalibration

MIN_BELOW_HORIZON = 0.01     # box bottom closer to the horizon than this → range unknown
MIN_BOX_HEIGHT = 0.005       # apparent height below this → range unknown
TRUNCATED_EDGE = 0.985       # box bottom at/after this fraction of the height is cut by the frame


def focal(cam: CameraCalibration) -> float:
    """Normalised focal length (frame heights)."""
    return 0.5 / math.tan(math.radians(cam.vertical_fov_deg) / 2)


def normalise_box(box: Sequence[float], width: int, height: int) -> tuple[float, float, float, float]:
    """Pixel box → normalised (x1, y1, x2, y2)."""
    return box[0] / width, box[1] / height, box[2] / width, box[3] / height


def centre(nbox: Sequence[float]) -> tuple[float, float]:
    return (nbox[0] + nbox[2]) / 2, (nbox[1] + nbox[3]) / 2


def area(nbox: Sequence[float]) -> float:
    return max(0.0, nbox[2] - nbox[0]) * max(0.0, nbox[3] - nbox[1])


def range_from_bottom(y_bottom: float, cam: CameraCalibration) -> Optional[float]:
    """Distance (m) of a grounded object from its contact line; None at / above the horizon."""
    below = y_bottom - cam.horizon_y
    if below < MIN_BELOW_HORIZON:
        return None
    return cam.camera_height_m * focal(cam) / below


def range_from_height(h: float, object_height_m: float, cam: CameraCalibration) -> Optional[float]:
    """Distance (m) of an object of known height from its apparent height (frame heights)."""
    if h < MIN_BOX_HEIGHT:
        return None
    return object_height_m * focal(cam) / h


def lateral_offset(cx: float, range_m: float, aspect: float, cam: CameraCalibration) -> float:
    """Metres to the right (+) / left (−) of the camera axis at `range_m`."""
    return (cx - 0.5 - cam.center_offset) * aspect * range_m / focal(cam)


def ground_contact_error(r_bottom: float, r_height: float) -> float:
    """|r_bottom − r_height| / mean — 0 for a perfectly grounded object."""
    mean = (r_bottom + r_height) / 2
    return abs(r_bottom - r_height) / mean if mean > 0 else math.inf


def is_truncated_bottom(nbox: Sequence[float]) -> bool:
    """The box is cut by the bottom edge of the frame (contact line not visible)."""
    return nbox[3] >= TRUNCATED_EDGE


def target_range(nbox: Sequence[float], object_height_m: float, cam: CameraCalibration) -> Optional[float]:
    """Best range estimate of a vehicle box: the contact line when visible,
    else the apparent height (box cut by the bottom of the frame)."""
    if not is_truncated_bottom(nbox):
        r = range_from_bottom(nbox[3], cam)
        if r is not None:
            return r
    return range_from_height(nbox[3] - nbox[1], object_height_m, cam)


def offset_enu(east: float, north: float, heading_deg: float, forward_m: float, right_m: float
               ) -> tuple[float, float]:
    """Local east/north of a point `forward_m` ahead and `right_m` to the right
    of (east, north) for a vehicle heading `heading_deg` (clockwise from north)."""
    h = math.radians(heading_deg)
    fe, fn = math.sin(h), math.cos(h)          # forward unit vector
    re, rn = fn, -fe                           # right = forward rotated 90° clockwise
    return east + fe * forward_m + re * right_m, north + fn * forward_m + rn * right_m


def world_position(lat: float, lon: float, heading_deg: float, range_m: float, lateral_m: float
                   ) -> tuple[float, float]:
    """(lat, lon) of a target `range_m` ahead and `lateral_m` right of a GPS fix."""
    plat, plon = destination(lat, lon, heading_deg, range_m)
    if lateral_m == 0:
        return plat, plon
    return destination(plat, plon, (heading_deg + (90 if lateral_m > 0 else -90)) % 360, abs(lateral_m))


def corridor_halfwidth(cy: float, cam: CameraCalibration) -> float:
    t = min(max((cy - cam.horizon_y) / max(1.0 - cam.horizon_y, 1e-6), 0.0), 1.0)
    return cam.corridor_halfwidth_at_horizon + t * (cam.corridor_halfwidth_at_bottom - cam.corridor_halfwidth_at_horizon)


def in_corridor(cx: float, cy: float, cam: CameraCalibration) -> bool:
    """Inside the triangular "my lane" region (narrow at the horizon, wide at
    the bottom), centred on 0.5 + center_offset."""
    return abs(cx - (0.5 + cam.center_offset)) <= corridor_halfwidth(cy, cam)


def side_of_road(cx: float, cam: CameraCalibration) -> str:
    """`left` | `right` of the road centre line (frame centre + center_offset)."""
    return "left" if cx < 0.5 + cam.center_offset else "right"


def is_wrong_side(side: str, drive_side: str) -> bool:
    """Stopped on the oncoming lane's side: left in right-hand traffic, right in left-hand traffic."""
    return side != drive_side


def is_masked(nbox: Sequence[float], mask_regions: Iterable[Sequence[float]], hood_bottom_ratio: float) -> bool:
    """Box centre inside a masked rectangle (OSD text, timestamp …) or in the
    bottom `hood_bottom_ratio` band (own bonnet)."""
    cx, cy = centre(nbox)
    if hood_bottom_ratio > 0 and cy > 1.0 - hood_bottom_ratio:
        return True
    return any(r[0] <= cx <= r[2] and r[1] <= cy <= r[3] for r in mask_regions)
