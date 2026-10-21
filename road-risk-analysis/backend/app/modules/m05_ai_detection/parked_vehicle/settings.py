"""
Settings section `parked_vehicle` (system_settings, editable from the web
console through GET/PUT /settings/parked_vehicle) — every threshold of the
parked / roadside vehicle pipeline. See the package docstring for the rules
and docs/spec-module5-parked-vehicle.md for the meaning of each field.

Camera calibration: `camera` holds the defaults for every dashcam; an entry
of `camera_profiles` (keyed by the OSD vehicle id, e.g. "LTD123AB") overrides
only the fields it sets for that vehicle's camera.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# COCO class names the pipeline can use (the stock YOLOv8 detector).
COCO_VEHICLE_CLASSES = ("car", "motorcycle", "bus", "truck")
VERDICTS = ("parked", "moving", "traffic_queue", "off_road", "not_grounded", "insufficient_evidence", "too_far")
SUBTYPES = ("blocking", "wrong_side", "roadside")
SEVERITY_LEVELS = ("low", "medium", "high", "critical")
# Verdicts that may be stored as auto-rejected when store_rejected_for_review is on.
REVIEWABLE_REJECTS = ("off_road", "not_grounded", "traffic_queue", "moving")
SCORE_COMPONENTS = ("detector", "evidence", "target_speed", "ego_motion", "ground_contact", "road_distance",
                    "proximity")

_DEFAULT_WEIGHTS = {"detector": 0.25, "evidence": 0.15, "target_speed": 0.25, "ego_motion": 0.10,
                    "ground_contact": 0.10, "road_distance": 0.10, "proximity": 0.05}


def _check_rects(name: str, v: list[list[float]]) -> None:
    for i, r in enumerate(v):
        if len(r) != 4 or not all(0 <= x <= 1 for x in r) or r[0] >= r[2] or r[1] >= r[3]:
            raise ValueError(f"{name}[{i}] must be [x1, y1, x2, y2] in 0..1 with x1 < x2 and y1 < y2")


class CameraCalibration(BaseModel):
    """Flat-road pinhole model of one dashcam (all ratios of the frame)."""
    horizon_y:        float = Field(0.45, ge=0.05, le=0.95)   # vanishing line, fraction of the frame height
    center_offset:    float = Field(0.0, ge=-0.4, le=0.4)     # road centre / optical axis minus 0.5, fraction of width
    camera_height_m:  float = Field(1.4, gt=0.2, le=5.0)      # lens height above the road
    vertical_fov_deg: float = Field(21.0, gt=10, lt=170)      # vertical field of view (measured on the fleet's
                                                              # dashcams: 18–31°, tools/parked_eval/calibrate.py)
    display_aspect:   Optional[float] = Field(16 / 9, gt=0.5, le=4.0)  # picture width / height as displayed
                                                                      # (None = pixel ratio of the frame)
    corridor_halfwidth_at_horizon: float = Field(0.08, ge=0, le=0.5)  # "my lane" half width at the horizon
    corridor_halfwidth_at_bottom:  float = Field(0.42, ge=0, le=0.5)  # … and at the bottom of the frame

    @model_validator(mode="after")
    def _corridor(self):
        if self.corridor_halfwidth_at_bottom < self.corridor_halfwidth_at_horizon:
            raise ValueError("corridor_halfwidth_at_bottom must be ≥ corridor_halfwidth_at_horizon")
        return self


class CameraOverride(BaseModel):
    """Partial calibration of one vehicle's camera (unset = the default)."""
    horizon_y:        Optional[float] = Field(None, ge=0.05, le=0.95)
    center_offset:    Optional[float] = Field(None, ge=-0.4, le=0.4)
    camera_height_m:  Optional[float] = Field(None, gt=0.2, le=5.0)
    vertical_fov_deg: Optional[float] = Field(None, gt=10, lt=170)
    display_aspect:   Optional[float] = Field(None, gt=0.5, le=4.0)
    corridor_halfwidth_at_horizon: Optional[float] = Field(None, ge=0, le=0.5)
    corridor_halfwidth_at_bottom:  Optional[float] = Field(None, ge=0, le=0.5)


# Calibrated on the evaluation videos (tools/parked_eval/calibrate.py, see
# docs/spec-module5-parked-vehicle.md §10): horizon + camera height from the
# car boxes (y_bottom = horizon + H_cam/H_car · h), range scale from the
# labelled parked vehicles against our OSD travel.
# `total` cameras are mounted too differently (horizon 0.34 … 0.65) for a layout
# default: the per-job self-calibration (calibration.py) provides it.
_LAYOUT_CAMERAS: dict[str, dict] = {
    "besafe": {"horizon_y": 0.57, "camera_height_m": 1.9, "vertical_fov_deg": 45.0},   # Mint0639, 704×576
}


class ParkedVehicleSettings(BaseModel):
    """Parked / roadside vehicle detection (Module 5, RRAP-05-001/003/015) —
    detector filter, tracking, the "is it parked?" rules, scoring, severity
    and storage. Read by the video worker once per job."""
    # ── detector & masks ────────────────────────────────────────────────────
    vehicle_classes:        list[str] = ["car", "bus", "truck"]   # COCO names; motorcycle off by default
    detector_confidence:    float = Field(0.35, ge=0.05, le=0.99)  # box confidence floor
    mask_regions:           list[list[float]] = [[0.0, 0.0, 1.0, 0.235]]  # [x1, y1, x2, y2] normalised (OSD overlay)
    # per OSD layout (equipment profile, OSDData.layout) — replaces mask_regions for that layout
    mask_regions_by_layout: dict[str, list[list[float]]] = Field(default_factory=lambda: {
        # from the OSD layout ROIs (osd_layouts.BUILTIN_PROFILES) + a margin
        "total":      [[0.0, 0.0, 1.0, 0.235]],                      # 3 text lines top-left + top-right
        "besafe":     [[0.0, 0.0, 1.0, 0.18], [0.0, 0.87, 1.0, 1.0]],  # GPS + name on top, date / plate at the bottom
        "ola_energy": [[0.0, 0.0, 1.0, 0.18], [0.0, 0.87, 1.0, 1.0]],  # same arrangement
    })
    hood_mask_bottom_ratio: float = Field(0.10, ge=0, le=0.5)      # bottom band = own bonnet
    # ── tracking ────────────────────────────────────────────────────────────
    tracker_max_gap:        int   = Field(3, ge=1, le=20)          # sampled frames a vehicle may be missed
    tracker_match_iou:      float = Field(0.10, ge=0.0, le=0.9)    # IoU with the (predicted) last box
    tracker_max_scale_change: float = Field(2.5, ge=1.1, le=20)    # box area ratio allowed per sampled step (ID-switch gate)
    # ── evidence (rule 6) and proximity (rule 7) ────────────────────────────
    min_sightings:          int   = Field(5, ge=2, le=200)
    min_track_seconds:      float = Field(2.5, ge=0.2, le=120)
    min_mean_confidence:    float = Field(0.5, ge=0, le=1)
    min_box_area_at_closest: float = Field(0.008, ge=0, le=1)      # w×h / frame area at the closest sighting
    # ── ego motion (rule 1) ─────────────────────────────────────────────────
    min_ego_distance_m:     float = Field(20.0, ge=0, le=2000)
    min_ego_speed_kmh:      float = Field(15.0, ge=0, le=200)
    # ── world stability (rule 2) ────────────────────────────────────────────
    max_target_speed_kmh:   float = Field(3.0, ge=0, le=100)
    max_world_drift_m:      float = Field(8.0, ge=0, le=500)
    range_error_ratio:      float = Field(0.25, ge=0, le=1)        # monocular range-scale error tolerated
    max_range_m:            float = Field(80.0, gt=5, le=500)      # sightings farther are not used for drift
    # closing ratio (rule 2b): −d(range)/d(our travel) — parked ≈ 1, followed ≈ 0, oncoming > 1
    closing_check:          bool  = True
    closing_ratio_min:      float = Field(0.5, ge=0, le=2)         # below → moving with us / ahead
    closing_ratio_max:      float = Field(1.8, ge=0.5, le=5)       # above → oncoming
    closing_min_ego_m:      float = Field(10.0, ge=1, le=500)      # our travel needed over the fitted sightings
    closing_min_points:     int   = Field(4, ge=3, le=100)
    followed_min_corridor_share: float = Field(0.6, ge=0, le=1)    # "followed": share of sightings in our corridor
    followed_max_scale_change:   float = Field(1.5, ge=1, le=10)   # … and box area ratio last / first within this
    world_drift_check:      bool  = True                           # rule 2a (world-position fit, needs GPS heading)
    # image-only fallback (no GPS / OSD speed) — prototype heuristics, score capped
    fallback_parked:        bool  = False                          # False: without ego motion a still-looking
                                                                   # vehicle is insufficient_evidence, never parked
    fallback_min_lateral_drift: float = Field(0.12, ge=0, le=1)
    fallback_edge_margin:       float = Field(0.18, ge=0, le=0.5)
    fallback_max_growth_rate:   float = Field(2.0, ge=0, le=50)    # (scale growth − 1) / s
    fallback_min_scale_growth:  float = Field(3.0, ge=1, le=100)
    fallback_score_cap:         float = Field(0.6, ge=0, le=1)
    # ── ground contact (rule 3) ─────────────────────────────────────────────
    ground_contact_tolerance:   float = Field(0.4, gt=0, le=5)
    min_ground_contact_ratio:   float = Field(0.5, ge=0, le=1)     # share of sightings that must agree
    ground_contact_max_range_m: float = Field(40.0, gt=2, le=200)  # farther sightings are too sensitive to judge
    class_heights_m: dict[str, float] = {"car": 1.5, "bus": 3.0, "truck": 3.2, "motorcycle": 1.2}
    # plausible height span per class for the ground-contact rule (COCO `truck` = pickup … semi)
    class_height_ranges_m: dict[str, list[float]] = Field(default_factory=lambda: {
        "car": [1.3, 2.0], "bus": [2.4, 3.6], "truck": [1.6, 4.2], "motorcycle": [1.0, 1.7]})
    # ── road distance (rule 4, OSRM /nearest) ───────────────────────────────
    road_check:             bool  = True
    max_road_distance_m:    float = Field(12.0, gt=0, le=500)
    # ── traffic queue (rule 5) ──────────────────────────────────────────────
    queue_ego_speed_kmh:    float = Field(5.0, ge=0, le=100)
    queue_min_vehicles:     int   = Field(2, ge=2, le=50)
    oncoming_lane_max_offset_m: float = Field(4.5, ge=0, le=20)    # still vehicle this close on the oncoming
                                                                   # side = stopped traffic (0 = rule off)
    # ── camera (rule 11) and road side (rule 10) ────────────────────────────
    camera:                 CameraCalibration = Field(default_factory=CameraCalibration)
    auto_calibrate:         bool  = True     # per-job horizon + lens height from the car boxes (calibration.py)
    auto_calibrate_min_boxes: int = Field(40, ge=5, le=5000)          # car boxes needed before it is used
    camera_profiles:        dict[str, CameraOverride] = {}           # per OSD vehicle id (wins, no auto-calibration)
    camera_by_layout:       dict[str, CameraOverride] = Field(default_factory=lambda: {
        k: CameraOverride(**v) for k, v in _LAYOUT_CAMERAS.items()})  # per OSD layout (equipment)
    drive_side:             Literal["right", "left"] = "right"
    # ── scoring & decision ──────────────────────────────────────────────────
    score_weights:          dict[str, float] = Field(default_factory=lambda: dict(_DEFAULT_WEIGHTS))
    auto_accept_score:      Optional[float] = Field(0.80, ge=0, le=1)   # verifier_score ≥ → may auto-accept
    auto_reject_score:      Optional[float] = Field(None, ge=0, le=1)   # verifier_score ≤ → auto-reject
    # ── severity ────────────────────────────────────────────────────────────
    severity_by_subtype:    dict[str, str] = {"blocking": "high", "wrong_side": "medium", "roadside": "low"}
    heavy_vehicle_classes:  list[str] = ["bus", "truck"]
    heavy_vehicle_bump:     int   = Field(1, ge=0, le=3)
    # ── storage (rule 12) ───────────────────────────────────────────────────
    store_rejected_for_review: bool = False
    review_verdicts:        list[str] = ["off_road", "not_grounded", "traffic_queue"]

    @field_validator("vehicle_classes", "heavy_vehicle_classes")
    @classmethod
    def _coco(cls, v: list[str]) -> list[str]:
        v = [c.strip().lower() for c in v]
        unknown = set(v) - set(COCO_VEHICLE_CLASSES)
        if unknown:
            raise ValueError(f"unknown vehicle class(es) {sorted(unknown)}; allowed {list(COCO_VEHICLE_CLASSES)}")
        return list(dict.fromkeys(v))

    @field_validator("mask_regions")
    @classmethod
    def _rects(cls, v: list[list[float]]) -> list[list[float]]:
        _check_rects("mask_regions", v)
        return v

    @field_validator("mask_regions_by_layout")
    @classmethod
    def _layout_rects(cls, v: dict[str, list[list[float]]]) -> dict[str, list[list[float]]]:
        for name, rects in v.items():
            _check_rects(f"mask_regions_by_layout.{name}", rects)
        return v

    @model_validator(mode="after")
    def _consistent(self):
        if not self.vehicle_classes:
            raise ValueError("vehicle_classes must not be empty")
        for name, h in self.class_heights_m.items():
            if not 0.3 <= h <= 6:
                raise ValueError(f"class_heights_m.{name} must be between 0.3 and 6 m")
        missing = set(self.vehicle_classes) - set(self.class_heights_m)
        if missing:
            raise ValueError(f"class_heights_m: no height for {sorted(missing)}")
        unknown = set(self.score_weights) - set(SCORE_COMPONENTS)
        if unknown:
            raise ValueError(f"score_weights: unknown component(s) {sorted(unknown)}; allowed {list(SCORE_COMPONENTS)}")
        if any(w < 0 for w in self.score_weights.values()) or sum(self.score_weights.values()) <= 0:
            raise ValueError("score_weights must be ≥ 0 with a positive sum")
        bad = {k: v for k, v in self.severity_by_subtype.items() if k not in SUBTYPES or v not in SEVERITY_LEVELS}
        if bad:
            raise ValueError(f"severity_by_subtype: subtypes {list(SUBTYPES)}, levels {list(SEVERITY_LEVELS)}")
        bad_v = set(self.review_verdicts) - set(REVIEWABLE_REJECTS)
        if bad_v:
            raise ValueError(f"review_verdicts: allowed {list(REVIEWABLE_REJECTS)}")
        if (self.auto_accept_score is not None and self.auto_reject_score is not None
                and self.auto_accept_score <= self.auto_reject_score):
            raise ValueError("auto_accept_score must be greater than auto_reject_score")
        return self

    @classmethod
    def merge_patch(cls, current: dict, patch: dict) -> dict:
        """PUT /settings/parked_vehicle: nested maps (`camera`, `score_weights`,
        `class_heights_m`, `severity_by_subtype`) are merged key by key, so a
        patch {"camera": {"horizon_y": 0.5}} keeps the other camera fields;
        every other field (camera_profiles included) is replaced as sent."""
        out = {**current, **patch}
        for key in ("camera", "score_weights", "class_heights_m", "severity_by_subtype"):
            if isinstance(patch.get(key), dict) and isinstance(current.get(key), dict):
                out[key] = {**current[key], **patch[key]}
        return out

    def camera_for(self, vehicle_id: Optional[str], layout: Optional[str] = None) -> CameraCalibration:
        """Default calibration, then the OSD layout's (equipment) override, then
        the vehicle's profile (rule 11)."""
        overrides = [self.camera_by_layout.get(layout) if layout else None,
                     self.camera_profiles.get(vehicle_id) if vehicle_id else None]
        if not any(overrides):
            return self.camera
        merged = self.camera.model_dump()
        for o in overrides:
            if o is not None:
                merged.update({k: v for k, v in o.model_dump().items() if v is not None})
        return CameraCalibration(**merged)

    def masks_for(self, layout: Optional[str]) -> list[list[float]]:
        """Mask rectangles of an OSD layout (mask_regions when the layout has none)."""
        return self.mask_regions_by_layout.get(layout, self.mask_regions) if layout else self.mask_regions

    def class_height_range(self, cls: str) -> tuple[float, float]:
        """(min, max) height of a class (class_height_ranges_m, else the class height)."""
        r = self.class_height_ranges_m.get(cls)
        if r and len(r) == 2 and 0 < r[0] <= r[1]:
            return float(r[0]), float(r[1])
        h = self.class_heights_m.get(cls, 1.5)
        return h, h

    def weight(self, component: str) -> float:
        """Score weight (components missing from score_weights weigh 0)."""
        return float(self.score_weights.get(component, 0.0))
