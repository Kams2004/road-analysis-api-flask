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


class CameraCalibration(BaseModel):
    """Flat-road pinhole model of one dashcam (all ratios of the frame)."""
    horizon_y:        float = Field(0.45, ge=0.05, le=0.95)   # vanishing line, fraction of the frame height
    center_offset:    float = Field(0.0, ge=-0.4, le=0.4)     # road centre / optical axis minus 0.5, fraction of width
    camera_height_m:  float = Field(1.4, gt=0.2, le=5.0)      # lens height above the road
    vertical_fov_deg: float = Field(60.0, gt=10, lt=170)      # vertical field of view
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
    corridor_halfwidth_at_horizon: Optional[float] = Field(None, ge=0, le=0.5)
    corridor_halfwidth_at_bottom:  Optional[float] = Field(None, ge=0, le=0.5)


class ParkedVehicleSettings(BaseModel):
    """Parked / roadside vehicle detection (Module 5, RRAP-05-001/003/015) —
    detector filter, tracking, the "is it parked?" rules, scoring, severity
    and storage. Read by the video worker once per job."""
    # ── detector & masks ────────────────────────────────────────────────────
    vehicle_classes:        list[str] = ["car", "bus", "truck"]   # COCO names; motorcycle off by default
    detector_confidence:    float = Field(0.35, ge=0.05, le=0.99)  # box confidence floor
    mask_regions:           list[list[float]] = [[0.0, 0.0, 1.0, 0.235]]  # [x1, y1, x2, y2] normalised (OSD overlay)
    hood_mask_bottom_ratio: float = Field(0.10, ge=0, le=0.5)      # bottom band = own bonnet
    # ── tracking ────────────────────────────────────────────────────────────
    tracker_max_gap:        int   = Field(3, ge=1, le=20)          # sampled frames a vehicle may be missed
    tracker_match_iou:      float = Field(0.10, ge=0.0, le=0.9)    # IoU with the (predicted) last box
    # ── evidence (rule 6) and proximity (rule 7) ────────────────────────────
    min_sightings:          int   = Field(5, ge=2, le=200)
    min_track_seconds:      float = Field(2.5, ge=0.2, le=120)
    min_mean_confidence:    float = Field(0.5, ge=0, le=1)
    min_box_area_at_closest: float = Field(0.008, ge=0, le=1)      # w×h / frame area at the closest sighting
    # ── ego motion (rule 1) ─────────────────────────────────────────────────
    min_ego_distance_m:     float = Field(30.0, ge=0, le=2000)
    min_ego_speed_kmh:      float = Field(15.0, ge=0, le=200)
    # ── world stability (rule 2) ────────────────────────────────────────────
    max_target_speed_kmh:   float = Field(3.0, ge=0, le=100)
    max_world_drift_m:      float = Field(8.0, ge=0, le=500)
    range_error_ratio:      float = Field(0.25, ge=0, le=1)        # monocular range-scale error tolerated
    max_range_m:            float = Field(80.0, gt=5, le=500)      # sightings farther are not used for drift
    # image-only fallback (no GPS / OSD speed) — prototype heuristics, score capped
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
    # ── road distance (rule 4, OSRM /nearest) ───────────────────────────────
    road_check:             bool  = True
    max_road_distance_m:    float = Field(12.0, gt=0, le=500)
    # ── traffic queue (rule 5) ──────────────────────────────────────────────
    queue_ego_speed_kmh:    float = Field(5.0, ge=0, le=100)
    queue_min_vehicles:     int   = Field(2, ge=2, le=50)
    # ── camera (rule 11) and road side (rule 10) ────────────────────────────
    camera:                 CameraCalibration = Field(default_factory=CameraCalibration)
    camera_profiles:        dict[str, CameraOverride] = {}
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
        for i, r in enumerate(v):
            if len(r) != 4 or not all(0 <= x <= 1 for x in r) or r[0] >= r[2] or r[1] >= r[3]:
                raise ValueError(f"mask_regions[{i}] must be [x1, y1, x2, y2] in 0..1 with x1 < x2 and y1 < y2")
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

    def camera_for(self, vehicle_id: Optional[str]) -> CameraCalibration:
        """Default calibration with the vehicle's profile applied (rule 11)."""
        override = self.camera_profiles.get(vehicle_id or "") if vehicle_id else None
        if override is None:
            return self.camera
        patch = {k: v for k, v in override.model_dump().items() if v is not None}
        return CameraCalibration(**{**self.camera.model_dump(), **patch})

    def weight(self, component: str) -> float:
        """Score weight (components missing from score_weights weigh 0)."""
        return float(self.score_weights.get(component, 0.0))
