"""
Parked / roadside vehicle detection — Module 5 (RRAP-05-001/003/015).

No training: a stock COCO-pretrained YOLOv8 (slot `vehicles`,
MODEL_VEHICLES, default /models/model_vehicles.pt) finds vehicles; whether a
vehicle is PARKED is decided by physics and rules over its track, because a
single frame cannot tell a parked car from one stopped at a light or driving
next to us. Detection type `parked_vehicle`, subtypes `blocking` |
`wrong_side` | `roadside`. Settings section `parked_vehicle`
(ParkedVehicleSettings); spec: docs/spec-module5-parked-vehicle.md.

Pipeline (app/modules/m07_ingestion/pipeline.py drives it, everything here is pure):

  1. detector filter (processor): boxes of `vehicle_classes` (COCO names;
     motorcycle off by default) with confidence ≥ `detector_confidence`;
     boxes whose centre is in a `mask_regions` rectangle (OSD overlay) or in
     the bottom `hood_mask_bottom_ratio` band (own bonnet) are ignored
     (geometry.is_masked);
  2. tracking: static_check.Tracker with motion prediction (vehicles sweep
     across the frame), `tracker_max_gap`, `tracker_match_iou`; the ego path
     (OSD fix + speed of every sampled frame) is kept alongside (features.EgoPath);
  3. features (features.extract): duration, sightings, confidences, ego
     distance / speed (GPS, else OSD-speed odometry), target world positions
     → target speed / drift (+ direction), the prototype's image heuristics,
     closest box, corridor / side, ground contact, queue evidence;
  4. rules (rules.judge) → verdict parked | moving | traffic_queue | off_road
     | not_grounded | insufficient_evidence | too_far, with a reason
     {code, value, threshold, passed} per rule:
       1 ego motion      ego_distance ≥ min_ego_distance_m, ego_speed ≥ min_ego_speed_kmh
       2 world stability target_speed / world_drift within limits (image
                         heuristics when ego motion is unknown — capped score,
                         never auto-accepted)
       3 ground contact  contact-line range ≈ class-height range
       4 road distance   OSRM snap ≤ max_road_distance_m (skipped if unknown)
       5 traffic queue   in our corridor while we stop / ≥ queue_min_vehicles
       6 evidence        min_sightings, min_track_seconds, min_mean_confidence
       7 proximity       closest box area ≥ min_box_area_at_closest
      10 subtype         blocking (corridor) / wrong_side (drive_side) / roadside
      11 camera          `camera` defaults + `camera_profiles[vehicle_id]`
  5. score (scoring.score) 0..1, weighted, monotonic → verifier_score;
  6. severity (severity.severity) per subtype, heavy-vehicle bump;
  7. stage.finish → store (parked; or rejected verdicts for review with
     codes ai_off_road / ai_not_grounded / ai_traffic_queue /
     ai_moving_vehicle) or only count.

Public API below; tests in tests/test_parked_vehicle.py.
"""
from .features import EgoPath, EgoSample, TrackFeatures, VehicleSighting, ego_motion, extract
from .rules import (
    BLOCKING, INSUFFICIENT, MOVING, NOT_GROUNDED, OFF_ROAD, PARKED, ROADSIDE, TOO_FAR, TRAFFIC_QUEUE, WRONG_SIDE,
    Judgement, Reason, judge,
)
from .settings import CameraCalibration, CameraOverride, ParkedVehicleSettings
from .stage import (
    DETECTION_TYPE, MODEL_KEY, REJECTION_CODES, RULES_VERSION, Evaluation, PeerSummary, count_queue_peers,
    evaluate, finish, merge_policy_cards, needs_road_check, peer_summary, prepare, rules_version,
    score_thresholds,
)

__all__ = [
    "BLOCKING", "INSUFFICIENT", "MOVING", "NOT_GROUNDED", "OFF_ROAD", "PARKED", "ROADSIDE", "TOO_FAR",
    "TRAFFIC_QUEUE", "WRONG_SIDE", "DETECTION_TYPE", "MODEL_KEY", "REJECTION_CODES", "RULES_VERSION",
    "CameraCalibration", "CameraOverride", "EgoPath", "EgoSample", "Evaluation", "Judgement",
    "ParkedVehicleSettings", "PeerSummary", "Reason", "TrackFeatures", "VehicleSighting",
    "count_queue_peers", "ego_motion", "evaluate", "extract", "finish", "judge", "merge_policy_cards",
    "needs_road_check", "peer_summary", "prepare", "rules_version", "score_thresholds",
]
