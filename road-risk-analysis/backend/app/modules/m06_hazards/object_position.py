"""
Estimated ground position of the detected OBJECT (docs/CLUSTERS.md § Object
position).

`detections.latitude / longitude` is the position of the recording VEHICLE at
the stored frame (the OSD GPS fix) — kept as evidence. The same pothole seen
from 60 m on one pass and from 10 m on another lands 50 m apart there, more
than a rural cluster radius. Clustering therefore uses the object position:

    object = vehicle fix + range ahead along the heading + lateral offset

  heading   bearing of the vehicle's own GPS track around the frame
            (`heading_from_fixes`: first → last OCR fix within ±HEADING_WINDOW_S
            of the frame, at least HEADING_MIN_MOVE_M apart; interpolated fixes
            are never used);
  range     flat-road pinhole model of app.modules.m05_ai_detection.parked_vehicle.geometry
            — ground objects (pothole, speed bump) from the box bottom below the
            horizon, upright objects (signs, lights) from the apparent height
            of a ~0.75 m plate (static_check.UPRIGHT_HEIGHT_M), parked vehicles
            from their own pipeline's estimate (ai_details.target_position) or
            the class height; clamped to [MIN_RANGE_M, MAX_RANGE_M];
  lateral   box centre offset from the optical axis at that range, clamped to
            ±MAX_LATERAL_M;
  camera    the parked-vehicle camera calibration (settings `parked_vehicle`
            → `camera`, per-vehicle `camera_profiles` keyed by the OSD vehicle
            id): horizon_y, camera_height_m, vertical_fov_deg, center_offset.

`object_position_source` = `estimated` when all of that is known, else
`vehicle` (object_latitude / object_longitude NULL → the vehicle position is
used). Pure functions + the back-fill of existing rows (no I/O besides the DB).
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from app.modules.m03_maps.osrm.geo import bearing_deg, haversine_m
from app.modules.m05_ai_detection.parked_vehicle import geometry as pv_geo
from app.modules.m05_ai_detection.parked_vehicle.settings import CameraCalibration, ParkedVehicleSettings
from app.modules.m05_ai_detection.static_check import UPRIGHT_HEIGHT_M

logger = logging.getLogger(__name__)

ESTIMATED = "estimated"
VEHICLE = "vehicle"

GROUND_TYPES = frozenset({"pothole", "speed_bump"})
UPRIGHT_TYPES = frozenset({"traffic_sign", "traffic_light"})
PARKED_VEHICLE = "parked_vehicle"

HEADING_WINDOW_S = 4.0       # fixes used for the heading: frame time ± this
HEADING_MIN_MOVE_M = 3.0     # displacement below which the heading is unknown (vehicle stopped)
MIN_RANGE_M = 1.0
MAX_RANGE_M = 50.0           # far estimates are dominated by the horizon error: clamped
MAX_LATERAL_M = 15.0


@dataclass
class Estimate:
    latitude: Optional[float]
    longitude: Optional[float]
    source: Optional[str]          # estimated | vehicle | None (no vehicle position)
    range_m: Optional[float] = None
    heading_deg: Optional[float] = None


def heading_from_fixes(fixes: Sequence[tuple[float, Optional[float], Optional[float]]], t: Optional[float],
                       *, window_s: float = HEADING_WINDOW_S, min_move_m: float = HEADING_MIN_MOVE_M
                       ) -> Optional[float]:
    """Vehicle heading (degrees clockwise from north) at video time `t` from
    its GPS fixes `(t, lat, lon)`: bearing from the first to the last fix
    within t ± window_s; None when unknown (no time, too few fixes, stopped)."""
    if t is None:
        return None
    pts = sorted((p for p in fixes if p[1] is not None and p[2] is not None and abs(p[0] - t) <= window_s),
                 key=lambda p: p[0])
    if len(pts) < 2:
        return None
    a, b = pts[0], pts[-1]
    if haversine_m(a[1], a[2], b[1], b[2]) < min_move_m:
        return None
    return bearing_deg(a[1], a[2], b[1], b[2])


def object_range(det_type: str, box: Sequence[float], frame_w: int, frame_h: int, cam: CameraCalibration,
                 *, vehicle_height_m: Optional[float] = None) -> Optional[float]:
    """Distance (m) ahead of the camera, flat-road pinhole model; None when unknown."""
    if frame_w <= 0 or frame_h <= 0:
        return None
    nbox = pv_geo.normalise_box(box, frame_w, frame_h)
    if det_type in GROUND_TYPES:
        return pv_geo.range_from_bottom(nbox[3], cam)
    if det_type in UPRIGHT_TYPES:
        return pv_geo.range_from_height(nbox[3] - nbox[1], UPRIGHT_HEIGHT_M, cam)
    if det_type == PARKED_VEHICLE and vehicle_height_m:
        return pv_geo.target_range(nbox, vehicle_height_m, cam)
    return None


def estimate(det_type: str, box: Optional[Sequence[float]], frame_w: Optional[int], frame_h: Optional[int],
             lat: Optional[float], lon: Optional[float], heading_deg: Optional[float], cam: CameraCalibration,
             *, vehicle_height_m: Optional[float] = None, target: Optional[dict] = None) -> Estimate:
    """Object ground position of a detection (see module docstring).
    `target`: a position already estimated by the type's pipeline
    ({latitude, longitude}, parked vehicles)."""
    if lat is None or lon is None:
        return Estimate(None, None, None)
    if target and target.get("latitude") is not None and target.get("longitude") is not None:
        return Estimate(float(target["latitude"]), float(target["longitude"]), ESTIMATED, None, heading_deg)
    fallback = Estimate(None, None, VEHICLE, None, heading_deg)
    if heading_deg is None or box is None or any(v is None for v in box) or not frame_w or not frame_h:
        return fallback
    r = object_range(det_type, box, frame_w, frame_h, cam, vehicle_height_m=vehicle_height_m)
    if r is None or not math.isfinite(r):
        return fallback
    r = min(max(r, MIN_RANGE_M), MAX_RANGE_M)
    cx = (box[0] + box[2]) / 2 / frame_w
    lateral = pv_geo.lateral_offset(cx, r, frame_w / frame_h, cam)
    lateral = min(max(lateral, -MAX_LATERAL_M), MAX_LATERAL_M)
    plat, plon = pv_geo.world_position(lat, lon, heading_deg, r, lateral)
    return Estimate(plat, plon, ESTIMATED, round(r, 1), heading_deg)


def for_detection(det: Any, fixes: Sequence[tuple[float, Optional[float], Optional[float]]],
                  pvs: ParkedVehicleSettings) -> Estimate:
    """Estimate of a stored detection (or any object with the same fields)
    from its job's GPS fixes `(video_time_s, lat, lon)` (OCR fixes only)."""
    box = [det.bbox_x1, det.bbox_y1, det.bbox_x2, det.bbox_y2]
    if getattr(det, "corrected_bbox", None):
        box = list(det.corrected_bbox)
    details = getattr(det, "ai_details", None) or {}
    vclass = details.get("vehicle_class") if isinstance(details, dict) else None
    return estimate(det.type, box, det.frame_width, det.frame_height, det.latitude, det.longitude,
                    heading_from_fixes(fixes, det.video_time_s), pvs.camera_for(det.vehicle_id),
                    vehicle_height_m=pvs.class_heights_m.get(vclass) if vclass else None,
                    target=details.get("target_position") if isinstance(details, dict) else None)


def apply(det: Any, est: Estimate) -> None:
    det.object_latitude, det.object_longitude = est.latitude, est.longitude
    det.object_position_source = est.source


def position(det: Any) -> Optional[tuple[float, float]]:
    """Position used for clustering: the estimated object position when there
    is one, else the vehicle position; None when neither is known."""
    olat, olon = getattr(det, "object_latitude", None), getattr(det, "object_longitude", None)
    if olat is not None and olon is not None:
        return olat, olon
    if det.latitude is None or det.longitude is None:
        return None
    return det.latitude, det.longitude


def on_location_fix(det: Any, old_lat: Optional[float], old_lon: Optional[float], method: str) -> None:
    """The vehicle position was fixed by a validator: an estimate keeps its
    offset from the vehicle (shifted by the same amount); a map pin marks the
    object itself (no estimate: the pin is used)."""
    if (method != "map_pin" and det.object_position_source == ESTIMATED and det.object_latitude is not None
            and old_lat is not None and old_lon is not None and det.latitude is not None):
        det.object_latitude += det.latitude - old_lat
        det.object_longitude += det.longitude - old_lon
        return
    det.object_latitude = det.object_longitude = None
    det.object_position_source = VEHICLE if det.latitude is not None else None


# ── back-fill of existing detections ─────────────────────────────────────────

async def backfill(db, *, dry_run: bool = False, only_missing: bool = True) -> dict:
    """Estimate the object position of stored detections (python -m app.cli
    dedupe-observations). Returns {checked, estimated, vehicle}. The caller
    commits (one transaction per job)."""
    from sqlalchemy import select
    from app.core.settings_service import get_section
    from app.modules.m06_hazards.models.detection import Detection
    from app.modules.m07_ingestion.models.job_track_point import JobTrackPoint
    pvs: ParkedVehicleSettings = await get_section(db, "parked_vehicle")  # type: ignore[assignment]
    q = select(Detection).where(Detection.latitude.isnot(None), Detection.longitude.isnot(None))
    if only_missing:
        q = q.where(Detection.object_position_source.is_(None))
    dets = (await db.execute(q.order_by(Detection.job_id))).scalars().all()
    out = {"checked": 0, ESTIMATED: 0, VEHICLE: 0}
    fixes_by_job: dict[str, list] = {}
    for det in dets:
        if det.job_id not in fixes_by_job:
            rows = (await db.execute(select(JobTrackPoint.video_time_s, JobTrackPoint.latitude,
                                            JobTrackPoint.longitude).where(
                JobTrackPoint.job_id == det.job_id, JobTrackPoint.source == "ocr",
                JobTrackPoint.video_time_s.isnot(None)))).all()
            fixes_by_job = {det.job_id: [tuple(r) for r in rows]}      # one job at a time (ordered)
        est = for_detection(det, fixes_by_job[det.job_id], pvs)
        out["checked"] += 1
        if est.source:
            out[est.source] += 1
        if not dry_run:
            apply(det, est)
    await db.flush()
    return out
