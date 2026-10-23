"""
Video consistency of a detection — "a real sign / pothole stays at the same
place" (accept blockers of app.modules.m05_ai_detection.ai_validation).

The processor feeds every detector box of a sampled frame to a `Tracker`
(one per model): a box joins the open track of the same label whose last box
overlaps it (IoU ≥ MATCH_IOU, or centres closer than the box size) seen at
most `max_gap` sampled frames ago; otherwise it opens a new track. A track
not seen for more than `max_gap` sampled frames is finished and becomes ONE
detection (its best-confidence sighting). Then `assess()` says whether it may
be auto-accepted:

  not_static     seen in fewer than `min_frames` sampled frames (a sticker on
                 a passing truck, a flash, a bird …), or its estimated world
                 position drifted more than `max_drift_m` while the vehicle
                 moved — an object that moves with traffic (vehicle sticker,
                 motorcycle, pedestrian) keeps the same apparent size while
                 the vehicle advances, so its estimated position follows the
                 vehicle;
  off_road_area  potholes only: box centre above `min_y_ratio` × frame height
                 (potholes lie on the road surface, in the lower part of the
                 image).

Pothole false-positive rules — `pothole_rule_rejects()`, auto-REJECT codes
(rejection reasons seeded for type pothole), applied by the processor through
ai_validation.rule_reject (manual mode → review hint only):

  ai_off_road       box centre above the road-area line (as off_road_area)
                    when ai_validation.pothole_off_road_action = "reject"
                    (trees, sky, roadside above the horizon);
  ai_box_too_large  box wider than `max_width_ratio` of the frame, or larger
                    than `max_area_ratio` of it, or (`reject_edge_touching`)
                    touching the left / right border within EDGE_MARGIN while
                    wider than EDGE_MIN_WIDTH — the legacy detector's large
                    boxes over the roadside (red earth, bushes, building
                    edge), whose centre is often low enough to pass the
                    road-area line.

Position estimate (cheap, no calibration): vehicle GPS fix of the sighting
+ a range along the vehicle heading, from a flat-road pinhole model —
ground objects (pothole, speed bump) from the box bottom below the horizon,
upright objects (signs, lights) from the apparent box height of a ~0.75 m
plate. A static object's estimates stay within |1 − k|·Δ of each other (k =
range-scale error, Δ = vehicle displacement); an object moving with the
vehicle drifts by ≈ Δ. Without GPS movement (no fix, stale fix, vehicle
stopped) the drift rule is not applied. Single-image jobs skip the frame
count rule. Pure code, unit-tested in tests/test_sign_pipeline.py.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

NOT_STATIC = "not_static"
OFF_ROAD_AREA = "off_road_area"
AI_OFF_ROAD = "ai_off_road"              # pothole rule rejects (rejection codes)
AI_BOX_TOO_LARGE = "ai_box_too_large"
EDGE_MARGIN = 0.01          # box within 1 % of the left / right border "touches" it
EDGE_MIN_WIDTH = 0.25       # … and is a roadside box when also wider than 25 % of the frame

MATCH_IOU = 0.2
MAX_GAP = 2                 # sampled frames a track may miss before it is finished
MAX_OPEN_TRACKS = 30        # per model — bounds the frames kept in memory
FOCAL = 1.2                 # normalised focal length (≈ 45° vertical field of view)
CAM_HEIGHT_M = 1.4
HORIZON = 0.40              # horizon line, fraction of the frame height
UPRIGHT_HEIGHT_M = 0.75     # typical sign plate / light housing height
MIN_MOVE_M = 1.0            # vehicle displacement below which the drift rule is skipped
GROUND = frozenset({"pothole", "speed_bump"})


def iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _close(a, b, ref_size: bool = False) -> bool:
    """Centres closer than the box size (the larger of both boxes, or the
    reference box `a` only with `ref_size`)."""
    ca, cb = ((a[0] + a[2]) / 2, (a[1] + a[3]) / 2), ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
    size = max(a[2] - a[0], a[3] - a[1]) if ref_size else max(a[2] - a[0], a[3] - a[1], b[2] - b[0], b[3] - b[1])
    return math.hypot(ca[0] - cb[0], ca[1] - cb[1]) <= size


def _area(b) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


@dataclass
class Sighting:
    frame_num: int
    box: tuple
    conf: float
    lat: Optional[float] = None
    lon: Optional[float] = None


@dataclass
class Track:
    label: str
    sightings: list[Sighting] = field(default_factory=list)
    best_conf: float = -1.0
    best: Any = None            # processor payload of the best-confidence sighting (frame copy, osd …)

    @property
    def last(self) -> Sighting:
        return self.sightings[-1]


def predicted_box(track: "Track", frame_num: int) -> tuple:
    """Last box of the track shifted / scaled linearly to `frame_num` from its
    last two sightings (constant image velocity) — the last box when the
    track has a single sighting."""
    last = track.last
    if len(track.sightings) < 2 or frame_num <= last.frame_num:
        return last.box
    prev = track.sightings[-2]
    dt = last.frame_num - prev.frame_num
    if dt <= 0:
        return last.box
    k = (frame_num - last.frame_num) / dt
    return tuple(b + (b - a) * k for a, b in zip(prev.box, last.box))


class Tracker:
    """Box tracks of one model across sampled frames.

    `match_iou` is the IoU a new box needs with a track's last box (or, with
    `predict`, with the box extrapolated from the track's image velocity —
    vehicles passed at speed sweep across the frame between two sampled
    frames); a box whose centre is closer than the box size also matches.
    Among candidates the best IoU wins, then the closest centre."""

    def __init__(self, max_gap: int = MAX_GAP, max_open: int = MAX_OPEN_TRACKS,
                 match_iou: float = MATCH_IOU, predict: bool = False,
                 max_scale_change: Optional[float] = None):
        self.max_gap, self.max_open = max_gap, max_open
        self.match_iou, self.predict = match_iou, predict
        # size gate (vehicles): a box whose area differs from the reference box by
        # more than this factor per sampled step never continues the track, and the
        # "centres closer than the box size" rule uses the REFERENCE box size — so a
        # car leaving by the side cannot hand its track over to a small, far car
        # (ID switch that mixes a parked and a moving vehicle in one track).
        self.max_scale_change = max_scale_change
        self.open: list[Track] = []

    def _ref_boxes(self, t: "Track", frame_num: int) -> list:
        return [t.last.box, predicted_box(t, frame_num)] if self.predict else [t.last.box]

    def _match(self, t: "Track", s: Sighting) -> tuple[float, float]:
        """(best IoU, −centre distance) of a sighting against a track."""
        refs = self._ref_boxes(t, s.frame_num)
        best_iou = max(iou(r, s.box) for r in refs)
        cs = ((s.box[0] + s.box[2]) / 2, (s.box[1] + s.box[3]) / 2)
        dist = min(math.hypot((r[0] + r[2]) / 2 - cs[0], (r[1] + r[3]) / 2 - cs[1]) for r in refs)
        return best_iou, -dist

    def _accepts(self, t: "Track", s: Sighting, step: int) -> bool:
        """Size gate + (IoU ≥ match_iou or close centres) of a candidate pair."""
        refs = self._ref_boxes(t, s.frame_num)
        if self.max_scale_change:
            steps = max(1.0, (s.frame_num - t.last.frame_num) / max(step, 1))
            limit = self.max_scale_change * steps          # linear: a long gap must not open the gate
            a_s = _area(s.box)
            if not any(1 / limit <= a_s / max(_area(r), 1e-6) <= limit for r in refs):
                return False
            return max(iou(r, s.box) for r in refs) >= self.match_iou or \
                any(_close(r, s.box, ref_size=True) for r in refs)
        return max(iou(r, s.box) for r in refs) >= self.match_iou or any(_close(r, s.box) for r in refs)

    def _candidates(self, s: Sighting, label: str, step: int) -> list["Track"]:
        horizon = s.frame_num - (self.max_gap + 1) * step
        return [t for t in self.open if t.label == label and horizon < t.last.frame_num < s.frame_num]

    def _attach(self, track: Optional["Track"], s: Sighting, label: str, payload_fn) -> "Track":
        if track is None:
            track = Track(label=label)
            self.open.append(track)
        track.sightings.append(s)
        if s.conf > track.best_conf:
            track.best_conf, track.best = s.conf, payload_fn()
        return track

    def observe(self, s: Sighting, label: str, step: int, payload_fn) -> Track:
        """Attach a sighting; `payload_fn()` builds the payload only when this
        sighting becomes the track's best (avoids copying every frame)."""
        candidates = self._candidates(s, label, step)
        track = max(candidates, key=lambda t: self._match(t, s), default=None)
        if track is not None and not self._accepts(track, s, step):
            track = None
        return self._attach(track, s, label, payload_fn)

    def observe_frame(self, items: list, step: int) -> list["Track"]:
        """Attach all the sightings of ONE frame [(sighting, label, payload_fn)]
        at once: pairs are assigned globally, best IoU (then closest centre)
        first, one sighting per track — box order no longer decides which of
        two neighbouring vehicles takes a track."""
        pairs = []
        for i, (s, label, _fn) in enumerate(items):
            for t in self._candidates(s, label, step):
                if self._accepts(t, s, step):
                    pairs.append((self._match(t, s), i, t))
        pairs.sort(key=lambda p: p[0], reverse=True)
        chosen: dict[int, Track] = {}
        used: set[int] = set()
        for _score, i, t in pairs:
            if i in chosen or id(t) in used:
                continue
            chosen[i] = t
            used.add(id(t))
        return [self._attach(chosen.get(i), s, label, fn) for i, (s, label, fn) in enumerate(items)]

    def expire(self, frame_num: int, step: int) -> list[Track]:
        """Finish tracks not seen for more than max_gap sampled frames (and the
        oldest ones beyond max_open)."""
        horizon = frame_num - self.max_gap * step
        done = [t for t in self.open if t.last.frame_num < horizon]
        keep = [t for t in self.open if t.last.frame_num >= horizon]
        if len(keep) > self.max_open:
            keep.sort(key=lambda t: t.last.frame_num)
            done += keep[:len(keep) - self.max_open]
            keep = keep[len(keep) - self.max_open:]
        self.open = keep
        return done

    def flush(self) -> list[Track]:
        done, self.open = self.open, []
        return done


def _enu(lat0: float, lon0: float, lat: float, lon: float) -> tuple[float, float]:
    """Local east/north metres of (lat, lon) around (lat0, lon0)."""
    r = 6_371_000.0
    return (math.radians(lon - lon0) * r * math.cos(math.radians(lat0)), math.radians(lat - lat0) * r)


enu = _enu   # public name (app.modules.m05_ai_detection.parked_vehicle reuses it)


def estimate_range(det_type: str, box, frame_h: int) -> Optional[float]:
    """Distance ahead of the camera (m), flat-road pinhole model; None when unknown."""
    if frame_h <= 0:
        return None
    if det_type in GROUND:
        below = box[3] / frame_h - HORIZON
        return CAM_HEIGHT_M * FOCAL / below if below > 0.02 else None
    h = (box[3] - box[1]) / frame_h
    return UPRIGHT_HEIGHT_M * FOCAL / h if h > 0.005 else None


def drift_m(det_type: str, sightings: list[Sighting], frame_h: int) -> tuple[Optional[float], float]:
    """(max drift of the estimated object position from the first estimate,
    vehicle displacement) — drift None when it cannot be told."""
    fixes = [s for s in sightings if s.lat is not None and s.lon is not None]
    if len(fixes) < 2:
        return None, 0.0
    lat0, lon0 = fixes[0].lat, fixes[0].lon
    east, north = _enu(lat0, lon0, fixes[-1].lat, fixes[-1].lon)  # type: ignore[arg-type]
    moved = math.hypot(east, north)
    if moved < MIN_MOVE_M:
        return None, moved
    he, hn = east / moved, north / moved
    points = []
    for s in fixes:
        r = estimate_range(det_type, s.box, frame_h)
        if r is None:
            continue
        ve, vn = _enu(lat0, lon0, s.lat, s.lon)  # type: ignore[arg-type]
        points.append((ve + he * r, vn + hn * r))
    if len(points) < 2:
        return None, moved
    return max(math.hypot(p[0] - points[0][0], p[1] - points[0][1]) for p in points[1:]), moved


def off_road(box, frame_h: int, min_y_ratio: float) -> bool:
    """Box centre above the road-area line."""
    return frame_h > 0 and ((box[1] + box[3]) / 2) / frame_h < min_y_ratio


def assess(det_type: str, track: Track, frame_h: int, *, min_frames: int, max_drift_m: float,
           video: bool = True, min_y_ratio: Optional[float] = None) -> list[tuple[str, str]]:
    """Accept blockers [(code, detail)] of a finished track (empty = may be auto-accepted)."""
    blockers: list[tuple[str, str]] = []
    if det_type == "pothole" and min_y_ratio is not None:
        best = max(track.sightings, key=lambda s: s.conf)
        if off_road(best.box, frame_h, min_y_ratio):
            y = ((best.box[1] + best.box[3]) / 2) / frame_h
            blockers.append((OFF_ROAD_AREA, f"box centre at {y:.2f} of the frame height, above {min_y_ratio:.2f}"))
    n = len(track.sightings)
    if video and n < min_frames:
        blockers.append((NOT_STATIC, f"seen in {n} frame{'s' if n != 1 else ''} < {min_frames}"))
    else:
        d, moved = drift_m(det_type, track.sightings, frame_h)
        if d is not None and d > max_drift_m:
            blockers.append((NOT_STATIC, f"position drifted {d:.0f} m > {max_drift_m:g} m "
                                         f"while the vehicle moved {moved:.0f} m"))
    return blockers


def box_ratios(box, frame_w: int, frame_h: int) -> dict[str, Any]:
    """Geometry of a box relative to the frame: width / area share, centre
    height (0 = top, 1 = bottom) and whether it touches the left / right border."""
    x1, y1, x2, y2 = box
    if frame_w <= 0 or frame_h <= 0:
        return {"width_ratio": 0.0, "area_ratio": 0.0, "centre_y_ratio": None, "touches_side": False}
    w, h = max(0.0, x2 - x1), max(0.0, y2 - y1)
    margin = EDGE_MARGIN * frame_w
    return {"width_ratio": round(w / frame_w, 4), "area_ratio": round(w * h / (frame_w * frame_h), 4),
            "centre_y_ratio": round(((y1 + y2) / 2) / frame_h, 4),
            "touches_side": bool(x1 <= margin or x2 >= frame_w - margin)}


def pothole_rule_check(box, frame_w: int, frame_h: int, *, min_y_ratio: Optional[float],
                       off_road_action: str = "reject", max_width_ratio: float = 1.0,
                       max_area_ratio: float = 1.0, reject_edge_touching: bool = False,
                       ) -> tuple[list[tuple[str, str]], list[dict[str, Any]]]:
    """(rejects [(rejection code, detail)], reasons) of a pothole box — see the
    module docstring. `reasons` lists every rule evaluated in the generic
    ai_details form {code, value, threshold, passed, detail}. `off_road_action`
    "review" leaves the road-area line to assess() (accept blocker only); a
    ratio ≥ 1 disables its rule."""
    r = box_ratios(box, frame_w, frame_h)
    if r["centre_y_ratio"] is None:
        return [], []
    rejects: list[tuple[str, str]] = []
    reasons: list[dict[str, Any]] = []
    if off_road_action == "reject" and min_y_ratio is not None:
        ok = r["centre_y_ratio"] >= min_y_ratio
        detail = (f"box centre at {r['centre_y_ratio']:.3f} of the frame height, "
                  f"{'below' if ok else 'above'} the road-area line {min_y_ratio:.2f}")
        reasons.append({"code": "pothole_road_area", "value": r["centre_y_ratio"], "threshold": min_y_ratio,
                        "passed": ok, "detail": detail})
        if not ok:
            rejects.append((AI_OFF_ROAD, detail))
    too_large = []
    if max_width_ratio < 1:
        ok = r["width_ratio"] <= max_width_ratio
        reasons.append({"code": "pothole_box_width", "value": r["width_ratio"], "threshold": max_width_ratio,
                        "passed": ok, "detail": f"box width {r['width_ratio']:.0%} of the frame"})
        if not ok:
            too_large.append(f"width {r['width_ratio']:.0%} of the frame > {max_width_ratio:.0%}")
    if max_area_ratio < 1:
        ok = r["area_ratio"] <= max_area_ratio
        reasons.append({"code": "pothole_box_area", "value": r["area_ratio"], "threshold": max_area_ratio,
                        "passed": ok, "detail": f"box area {r['area_ratio']:.0%} of the frame"})
        if not ok:
            too_large.append(f"area {r['area_ratio']:.0%} of the frame > {max_area_ratio:.0%}")
    if reject_edge_touching:
        hit = r["touches_side"] and r["width_ratio"] > EDGE_MIN_WIDTH
        reasons.append({"code": "pothole_side_edge", "value": r["width_ratio"] if r["touches_side"] else 0.0,
                        "threshold": EDGE_MIN_WIDTH, "passed": not hit,
                        "detail": ("touches the left/right image side" if r["touches_side"] else
                                   "does not touch the image sides") + f", {r['width_ratio']:.0%} wide"})
        if hit:
            too_large.append(f"touches the image side and is {r['width_ratio']:.0%} wide > {EDGE_MIN_WIDTH:.0%}")
    if too_large:
        rejects.append((AI_BOX_TOO_LARGE, ", ".join(too_large)))
    return rejects, reasons


def pothole_rule_rejects(box, frame_w: int, frame_h: int, **rules) -> list[tuple[str, str]]:
    """Rejection rules of a pothole box [(rejection code, detail)], empty = none
    (keyword arguments of pothole_rule_check)."""
    return pothole_rule_check(box, frame_w, frame_h, **rules)[0]


def pothole_rule_kwargs(ai) -> dict[str, Any]:
    """pothole_rule_check() keyword arguments from settings section
    `ai_validation` (AiValidationSettings or any object with its fields)."""
    return {"min_y_ratio": ai.pothole_min_y_ratio, "off_road_action": ai.pothole_off_road_action,
            "max_width_ratio": ai.pothole_max_width_ratio, "max_area_ratio": ai.pothole_max_area_ratio,
            "reject_edge_touching": ai.pothole_reject_edge_touching}


def large_box_to_review(rule_reject: Optional[tuple[str, str]], classifier_score: Optional[float],
                        review_score: float) -> bool:
    """A pothole box rejected only as too large / roadside goes to human review instead when the crop
    classifier is at least `review_score` sure it is a pothole (ai_validation.pothole_large_box_review_score;
    1 = never). The road-area rule (ai_off_road) is not overridden."""
    return (rule_reject is not None and rule_reject[0] == AI_BOX_TOO_LARGE and classifier_score is not None
            and review_score < 1 and classifier_score >= review_score)
