"""
Per-track feature extraction of the parked-vehicle pipeline (pure).

Inputs: the sightings of ONE vehicle track (`VehicleSighting`: frame, pixel
box, detector confidence, COCO class, video time, the OSD of that frame) and
the ego path of the recording vehicle around it (`EgoPath`: every sampled
frame's OSD fix / speed). Output: `TrackFeatures`, the numbers the rules
(rules.py) and the score (scoring.py) are computed from — also stored on the
detection (`ai_details.features`) so a validator sees why it was flagged.

Ego motion, three modes (best available):
  gps        ≥ 2 distinct OCR fixes around the track: ego position at any
             time = local linear least-squares fit of the fixes within
             ±LOCAL_WINDOW_S (smooths the metre-level GPS noise; dashcam GPS
             repeats the same fix for ~1 s, so only the first frame of each
             fix is kept), heading = direction of the fitted velocity; the
             ego distance comes from the OSD speeds when the OSD shows them
             (smoother than differentiated GPS), else from the smoothed path;
  odometry   no usable fixes but OSD speeds: straight-line dead reckoning
             (distance = ∫ speed dt), heading = straight ahead;
  none       neither — only the prototype's image heuristics can be used.

Target world position per sighting = ego position + range ahead (contact
line, else apparent height × class height) + lateral offset. A weighted
least-squares fit of those positions over time gives the target's velocity:
`target_speed_kmh`, `world_drift_m` (= speed × duration) and its signed
component along the ego heading (`drift_along_m` > 0 same direction,
< 0 oncoming). Sightings farther than `max_range_m` are not used (range error
grows with distance); each is weighted 1 / range.
"""
from __future__ import annotations

import bisect
import math
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Optional, Sequence

from app.services.static_check import Sighting

from . import geometry as geo
from .settings import CameraCalibration, ParkedVehicleSettings

GPS_JUMP_KMH = 200.0         # a fix implying more than this from the previous one is an OCR glitch
EXTRAPOLATE_S = 2.0          # ego position may be extrapolated this far past the first / last fix
LOCAL_WINDOW_S = 3.0         # ego position at t = linear fit of the fixes within t ± this
MIN_HEADING_SPEED_MS = 0.5   # fitted speed below which the local heading is not trusted
MIN_HEADING_MOVE_M = 2.0     # displacement below which the overall heading is not trusted
PATH_STEP_S = 0.5            # integration step of the smoothed path length
MIN_FIT_POINTS = 3


@dataclass
class VehicleSighting(Sighting):
    """One detector box of a vehicle track + the OSD of its frame."""
    t_s: float = 0.0                       # video time
    cls: str = "car"                       # COCO class name
    speed_kmh: Optional[float] = None      # OSD speed of the frame
    frame_w: int = 0
    frame_h: int = 0
    timestamp: Optional[datetime] = None   # OSD date-time of the frame


@dataclass(frozen=True)
class EgoSample:
    t_s: float
    lat: Optional[float]
    lon: Optional[float]
    speed_kmh: Optional[float]


class EgoPath:
    """OSD fixes / speeds of the recording vehicle, one per sampled frame,
    kept for the last `keep_s` seconds of video (bounded memory)."""

    def __init__(self, keep_s: float = 600.0):
        self.keep_s = keep_s
        self.samples: list[EgoSample] = []

    def add(self, t_s: float, lat: Optional[float], lon: Optional[float], speed_kmh: Optional[float],
            interpolated: bool = False) -> None:
        """Interpolated (GPS-voter) positions are not used for ego motion."""
        if interpolated:
            lat = lon = None
        self.samples.append(EgoSample(t_s, lat, lon, speed_kmh))
        if self.samples[0].t_s < t_s - self.keep_s:
            cut = bisect.bisect_left([s.t_s for s in self.samples], t_s - self.keep_s)
            del self.samples[:cut]

    def window(self, t0: float, t1: float, pad: float = 3.0) -> list[EgoSample]:
        return [s for s in self.samples if t0 - pad <= s.t_s <= t1 + pad]


@dataclass
class EgoMotion:
    """Ego trajectory over a track (see module docstring for the modes)."""
    mode: str                                              # gps | odometry | none
    fixes: list[tuple[float, float, float]] = field(default_factory=list)  # (t, east, north) local metres
    origin: Optional[tuple[float, float]] = None           # lat/lon of the local frame origin
    speeds: list[tuple[float, float]] = field(default_factory=list)        # (t, OSD km/h)

    # ── positions ────────────────────────────────────────────────────────────
    def _local_fit(self, t: float) -> Optional[tuple[float, float, float, float]]:
        """(east, north, v_east, v_north) at t from a linear least-squares fit
        of the fixes within t ± LOCAL_WINDOW_S (widened once when too few)."""
        f = self.fixes
        if not f or t < f[0][0] - EXTRAPOLATE_S or t > f[-1][0] + EXTRAPOLATE_S:
            return None
        for half in (LOCAL_WINDOW_S, 2 * LOCAL_WINDOW_S, math.inf):
            pts = [p for p in f if abs(p[0] - t) <= half]
            if len(pts) >= 2:
                break
        else:
            return (f[0][1], f[0][2], 0.0, 0.0) if abs(t - f[0][0]) <= EXTRAPOLATE_S else None
        n = len(pts)
        tm = sum(p[0] for p in pts) / n
        em, nm = sum(p[1] for p in pts) / n, sum(p[2] for p in pts) / n
        stt = sum((p[0] - tm) ** 2 for p in pts)
        if stt <= 1e-9:
            return em, nm, 0.0, 0.0
        ve = sum((p[0] - tm) * (p[1] - em) for p in pts) / stt
        vn = sum((p[0] - tm) * (p[2] - nm) for p in pts) / stt
        return em + ve * (t - tm), nm + vn * (t - tm), ve, vn

    def position(self, t: float) -> Optional[tuple[float, float]]:
        """Local (east, north) at video time t (gps) or (right, forward) (odometry)."""
        if self.mode == "odometry":
            return 0.0, self.travelled(self.speeds[0][0], t)
        if self.mode != "gps":
            return None
        fit = self._local_fit(t)
        return (fit[0], fit[1]) if fit is not None else None

    def heading(self, t: float) -> Optional[float]:
        """Degrees clockwise from north (gps), 0 (odometry: straight ahead)."""
        if self.mode == "odometry":
            return 0.0
        if self.mode != "gps":
            return None
        fit = self._local_fit(t)
        if fit is not None and math.hypot(fit[2], fit[3]) >= MIN_HEADING_SPEED_MS:
            return math.degrees(math.atan2(fit[2], fit[3])) % 360
        de, dn = self.fixes[-1][1] - self.fixes[0][1], self.fixes[-1][2] - self.fixes[0][2]
        return math.degrees(math.atan2(de, dn)) % 360 if math.hypot(de, dn) >= MIN_HEADING_MOVE_M else None

    # ── distance & speed ─────────────────────────────────────────────────────
    def travelled(self, t0: float, t1: float) -> float:
        """Odometry distance (m) between t0 and t1 from the OSD speeds (trapezoids)."""
        pts = [(t, v) for t, v in self.speeds if t0 <= t <= t1]
        if len(pts) < 2:
            return 0.0
        return sum((b[0] - a[0]) * (a[1] + b[1]) / 2 / 3.6 for a, b in zip(pts, pts[1:]))

    def distance(self, t0: float, t1: float) -> Optional[float]:
        """Ego distance (m) travelled between t0 and t1: OSD speeds when shown,
        else the smoothed GPS path; None when unknown."""
        if self.mode == "odometry" or len([1 for t, _ in self.speeds if t0 <= t <= t1]) >= 2:
            return self.travelled(t0, t1)
        if self.mode != "gps":
            return None
        steps = max(1, math.ceil((t1 - t0) / PATH_STEP_S))
        pts = [self.position(t0 + (t1 - t0) * k / steps) for k in range(steps + 1)]
        if any(p is None for p in pts):
            return None
        return sum(math.hypot(q[0] - p[0], q[1] - p[1]) for p, q in zip(pts, pts[1:]))  # type: ignore[index]

    def speed_at(self, t: float) -> Optional[float]:
        """Ego speed (km/h) at t: nearest OSD speed within 1.5 s, else local GPS speed."""
        near = [(abs(ts - t), v) for ts, v in self.speeds if abs(ts - t) <= 1.5]
        if near:
            return min(near)[1]
        if self.mode == "gps":
            d = self.distance(t - 1.0, t + 1.0)
            return d / 2.0 * 3.6 if d is not None else None
        return None

    def median_speed(self, t0: float, t1: float) -> Optional[float]:
        vals = [v for t, v in self.speeds if t0 <= t <= t1]
        if vals:
            return float(statistics.median(vals))
        d = self.distance(t0, t1)
        return d / (t1 - t0) * 3.6 if d is not None and t1 > t0 else None


def ego_motion(samples: Sequence[EgoSample]) -> EgoMotion:
    """Build the ego trajectory from OSD samples (sorted by time)."""
    samples = sorted(samples, key=lambda s: s.t_s)
    speeds = [(s.t_s, float(s.speed_kmh)) for s in samples if s.speed_kmh is not None]
    raw = [s for s in samples if s.lat is not None and s.lon is not None]
    fixes: list[tuple[float, float, float]] = []
    origin = None
    if raw:
        origin = (raw[0].lat, raw[0].lon)
        last_pos = None
        for s in raw:
            pos = (s.lat, s.lon)
            if pos == last_pos:            # same fix repeated by the dashcam → keep its first frame
                continue
            e, n = geo.enu(origin[0], origin[1], s.lat, s.lon)  # type: ignore[arg-type]
            if fixes:
                pt, pe, pn = fixes[-1]
                dt = s.t_s - pt
                if dt <= 0 or math.hypot(e - pe, n - pn) / dt * 3.6 > GPS_JUMP_KMH:
                    continue
            fixes.append((s.t_s, e, n))
            last_pos = pos
    if len(fixes) >= 2:
        return EgoMotion("gps", fixes, origin, speeds)
    if len(speeds) >= 2:
        return EgoMotion("odometry", [], None, speeds)
    return EgoMotion("none", fixes, origin, speeds)


@dataclass
class TrackFeatures:
    """Everything the rules / score look at (JSON-able via as_dict())."""
    vehicle_class: str
    sightings: int
    duration_s: float
    mean_confidence: float
    max_confidence: float
    t_start: float
    t_end: float
    # ego motion
    ego_mode: str
    ego_distance_m: Optional[float]
    ego_speed_kmh: Optional[float]           # median over the track
    # world stability
    positions_used: int = 0
    target_speed_kmh: Optional[float] = None
    world_drift_m: Optional[float] = None
    drift_along_m: Optional[float] = None    # + same direction as us, − towards us
    target_lat: Optional[float] = None
    target_lon: Optional[float] = None
    # image heuristics (prototype)
    lateral_drift: float = 0.0
    exits_via_side: bool = False
    scale_growth: float = 1.0
    growth_rate: float = 0.0
    # closest sighting
    closest_area: float = 0.0
    closest_cx: float = 0.5
    closest_cy: float = 0.5
    closest_frame: int = 0
    closest_range_m: Optional[float] = None
    in_corridor_at_closest: bool = False
    side_at_closest: str = "right"
    # ground contact
    ground_contact_ratio: Optional[float] = None
    ground_contact_error: Optional[float] = None
    ground_checked: int = 0
    # traffic queue
    queue_stop_sightings: int = 0
    queue_min_ego_speed_kmh: Optional[float] = None

    def as_dict(self) -> dict:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in asdict(self).items()}


def _fit_velocity(points: list[tuple[float, float, float, float]]) -> Optional[tuple[float, float]]:
    """Weighted least-squares velocity (m/s east, north) of (t, x, y, w) points."""
    if len(points) < MIN_FIT_POINTS:
        return None
    sw = sum(p[3] for p in points)
    tm = sum(p[0] * p[3] for p in points) / sw
    xm = sum(p[1] * p[3] for p in points) / sw
    ym = sum(p[2] * p[3] for p in points) / sw
    stt = sum(p[3] * (p[0] - tm) ** 2 for p in points)
    if stt <= 1e-9:
        return None
    vx = sum(p[3] * (p[0] - tm) * (p[1] - xm) for p in points) / stt
    vy = sum(p[3] * (p[0] - tm) * (p[2] - ym) for p in points) / stt
    return vx, vy


def majority_class(sightings: Sequence[VehicleSighting]) -> str:
    """Class with the highest summed confidence (a truck flickering to car keeps `truck`)."""
    votes: dict[str, float] = {}
    for s in sightings:
        votes[s.cls] = votes.get(s.cls, 0.0) + s.conf
    return max(votes, key=lambda c: votes[c])


def extract(sightings: Sequence[VehicleSighting], ego: EgoMotion, s: ParkedVehicleSettings,
            cam: CameraCalibration) -> TrackFeatures:
    """Features of one finished track (see module docstring)."""
    sts = sorted(sightings, key=lambda x: x.frame_num)
    cls = majority_class(sts)
    obj_h = s.class_heights_m.get(cls, 1.5)
    t0, t1 = sts[0].t_s, sts[-1].t_s
    confs = [x.conf for x in sts]
    nboxes = [geo.normalise_box(x.box, x.frame_w, x.frame_h) for x in sts]

    f = TrackFeatures(
        vehicle_class=cls, sightings=len(sts), duration_s=t1 - t0,
        mean_confidence=sum(confs) / len(confs), max_confidence=max(confs), t_start=t0, t_end=t1,
        ego_mode=ego.mode, ego_distance_m=ego.distance(t0, t1), ego_speed_kmh=ego.median_speed(t0, t1),
    )

    # image heuristics of the prototype (used alone when ego motion is unknown)
    (fx, _), (lx, _) = geo.centre(nboxes[0]), geo.centre(nboxes[-1])
    f.lateral_drift = lx - fx
    f.exits_via_side = lx < s.fallback_edge_margin or lx > 1 - s.fallback_edge_margin
    f.scale_growth = geo.area(nboxes[-1]) / max(geo.area(nboxes[0]), 1e-6)
    f.growth_rate = (f.scale_growth - 1) / f.duration_s if f.duration_s > 0 else 0.0

    # closest sighting = largest box
    ci = max(range(len(sts)), key=lambda i: geo.area(nboxes[i]))
    cx, cy = geo.centre(nboxes[ci])
    f.closest_area, f.closest_cx, f.closest_cy, f.closest_frame = geo.area(nboxes[ci]), cx, cy, sts[ci].frame_num
    f.closest_range_m = geo.target_range(nboxes[ci], obj_h, cam)
    f.in_corridor_at_closest = geo.in_corridor(cx, cy, cam)
    f.side_at_closest = geo.side_of_road(cx, cam)

    # world positions → target velocity
    points: list[tuple[float, float, float, float]] = []
    for x, nb in zip(sts, nboxes):
        r = geo.target_range(nb, obj_h, cam)
        if r is None or r > s.max_range_m:
            continue
        pos, hd = ego.position(x.t_s), ego.heading(x.t_s)
        if pos is None or hd is None:
            continue
        aspect = x.frame_w / x.frame_h if x.frame_h else 16 / 9
        lat_m = geo.lateral_offset(geo.centre(nb)[0], r, aspect, cam)
        e, n = geo.offset_enu(pos[0], pos[1], hd, r, lat_m)
        points.append((x.t_s, e, n, 1.0 / max(r, 5.0)))
    f.positions_used = len(points)
    v = _fit_velocity(points)
    if v is not None:
        span = points[-1][0] - points[0][0]
        speed = math.hypot(*v)
        f.target_speed_kmh = speed * 3.6
        f.world_drift_m = speed * span
        hd = ego.heading((t0 + t1) / 2) if ego.mode == "gps" else 0.0
        if hd is not None:
            fwd = (math.sin(math.radians(hd)), math.cos(math.radians(hd)))
            f.drift_along_m = (v[0] * fwd[0] + v[1] * fwd[1]) * span
        if ego.mode == "gps" and ego.origin is not None:
            sw = sum(p[3] for p in points)
            me, mn = sum(p[1] * p[3] for p in points) / sw, sum(p[2] * p[3] for p in points) / sw
            f.target_lat = ego.origin[0] + math.degrees(mn / 6_371_000.0)
            f.target_lon = ego.origin[1] + math.degrees(me / (6_371_000.0 * math.cos(math.radians(ego.origin[0]))))

    # ground contact: contact-line range vs apparent-height range
    errors, passes, checked = [], 0, 0
    for nb in nboxes:
        if geo.is_truncated_bottom(nb):
            continue
        rh = geo.range_from_height(nb[3] - nb[1], obj_h, cam)
        if rh is None or rh > s.ground_contact_max_range_m:
            continue
        checked += 1
        rb = geo.range_from_bottom(nb[3], cam)
        if rb is None:                         # bottom at / above the horizon although close: floating
            errors.append(math.inf)
            continue
        err = geo.ground_contact_error(rb, rh)
        errors.append(err)
        passes += err <= s.ground_contact_tolerance
    f.ground_checked = checked
    if checked:
        f.ground_contact_ratio = passes / checked
        med = statistics.median(errors)
        f.ground_contact_error = med if math.isfinite(med) else 99.0

    # traffic queue: ahead of us in our corridor while we (almost) stop
    slow = []
    for x, nb in zip(sts, nboxes):
        if not geo.in_corridor(*geo.centre(nb), cam):
            continue
        v_ego = x.speed_kmh if x.speed_kmh is not None else ego.speed_at(x.t_s)
        if v_ego is not None and v_ego < s.queue_ego_speed_kmh:
            slow.append(v_ego)
    f.queue_stop_sightings = len(slow)
    f.queue_min_ego_speed_kmh = min(slow) if slow else None
    return f
