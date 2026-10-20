# Module 5 — Parked / roadside vehicle detection

Requirements RRAP-05-001 / 05-003 / 05-015. Code: `app/services/detection/parked_vehicle/`
(pure, unit-tested in `tests/test_parked_vehicle.py`), driven by `app/workers/processor.py`.

**No training.** A stock COCO-pretrained YOLOv8s finds vehicles. Whether a vehicle is *parked* is
decided by physics and rules over its track. One frame cannot tell a parked car from a car stopped
at a light or a car driving next to us. Several seconds of motion can.

## 1. Model

| | |
|---|---|
| Slot / job model key | `vehicles` (opt-in: `POST /jobs/?models=pothole,signs,vehicles`; not in the default set) |
| Weights | `MODEL_VEHICLES`, default `/models/model_vehicles.pt` (`ai/models/` is mounted read-only on `/models`) |
| Model card | `model_vehicles.json` next to the weights: COCO-pretrained YOLOv8s, no fine-tuning, classes used, AGPL-3.0 note |
| Detection type | `parked_vehicle`, subtypes `blocking` · `wrong_side` · `roadside` |
| Missing weights | the model is unavailable, as for any other slot |

To fetch the weights on a machine with Internet access:

```bash
python -c "from ultralytics import YOLO; YOLO('yolov8s.pt')" && cp yolov8s.pt ai/models/model_vehicles.pt
```

**Licence:** Ultralytics YOLOv8 code and weights are AGPL-3.0. If you serve them over a network,
you must offer the corresponding source under AGPL-3.0, or hold an Ultralytics Enterprise
licence.

`GET /models/` lists the slot with its version (`model_vehicles.pt@sha256[:12]`) and its card.
The `parked_vehicle` thresholds it shows come from the settings (§5).

## 2. Pipeline

1. **Detector filter** (processor). The pipeline keeps only these boxes:
   - the class is in `vehicle_classes` (COCO names);
   - the confidence is ≥ `detector_confidence`;
   - the box centre is outside every `mask_regions` rectangle (OSD overlay) and above the bottom
     `hood_mask_bottom_ratio` band (our own bonnet).

   Masked boxes are counted in the job event (`vehicle_boxes_masked`).
2. **Tracking.** The pipeline uses `static_check.Tracker` with motion prediction: a box matches
   the track's last box, or that box extrapolated with the track's image velocity
   (`tracker_match_iou`, or the centres are closer than the box size). A track ends after
   `tracker_max_gap` missed sampled frames. The per-model `processing.frame_skip_by_model.vehicles`
   applies. In parallel, the processor keeps the **ego path** of every sampled frame: the OSD GPS
   fix (GPS-voter interpolated fixes are excluded) and the OSD speed.
3. **Features** (`features.extract`):
   - **Ego motion**:
     - `gps` mode: local linear fit of the 1 Hz fixes within ±3 s, which smooths the
       metre-level GPS noise. The heading comes from the fitted velocity. The distance comes from
       the OSD speeds when they are available.
     - `odometry` mode: OSD speed only, straight line.
     - `none` mode: no GPS fix and no OSD speed.
   - **Target world position** for every sighting closer than `max_range_m`: ego position + range
     ahead + lateral offset. The range comes from the contact line (box bottom) on a flat-road
     pinhole model, or from the apparent height × the class height when the bottom is cut by the
     frame. A weighted (1/range) least-squares fit of these positions gives the **target speed**,
     the **world drift** (speed × duration) and its **sign** along our heading.
   - **Prototype image heuristics**: lateral drift, exit by a side, scale growth, growth rate.
   - **Closest sighting** (largest box): area, in corridor, side of the road.
   - **Ground contact**: contact-line range compared with the class-height range, on uncut
     sightings closer than `ground_contact_max_range_m`.
   - **Queue evidence**: sightings in our corridor while our speed < `queue_ego_speed_kmh`.
4. **Rules** (`rules.judge`) → verdict + reasons (§3).
5. **Score** (`scoring.score`) → `verifier_score` (§4).
6. **Severity** (`severity.severity`) → `ai_details.severity`.
7. **Storage** (`stage.finish`, §5).

## 3. Rules and verdicts

Each rule writes a reason `{code, value, threshold, passed, detail}` into `ai_details.reasons`.
`passed: null` means the rule was skipped or its input is unknown; a skipped rule never counts
as a failure. The verdict is the first rule that fails, in this order:

| # | Verdict | Reason code(s) | Rule |
|---|---|---|---|
| 7 | `too_far` | `closest_box_area` | closest box area / frame area < `min_box_area_at_closest`: we never came close enough |
| 6 | `insufficient_evidence` | `sightings`, `track_seconds`, `mean_confidence` | fewer than `min_sightings`, shorter than `min_track_seconds`, or mean detector confidence < `min_mean_confidence` |
| 5 | `traffic_queue` | `queue_ego_stop`, `queue_peers` | ≥ 2 sightings in our corridor while our speed < `queue_ego_speed_kmh`; or the vehicle is stationary in our corridor and, together with the other stationary in-corridor vehicles seen at the same time, they number ≥ `queue_min_vehicles` |
| 2 | `moving` | `target_speed`, `world_drift` | target speed > `max_target_speed_kmh` + `range_error_ratio` × ego speed, or drift > `max_world_drift_m` + `range_error_ratio` × ego distance. `ai_details.motion` = `same_direction` / `oncoming`. Fallback without ego motion: see below |
| 1 | `insufficient_evidence` | `ego_distance`, `ego_speed` | we drove < `min_ego_distance_m`, or our median speed was < `min_ego_speed_kmh`: we cannot tell a parked car from stopped traffic (a moving target does not need this) |
| 3 | `not_grounded` | `ground_contact` | share of agreeing close sightings < `min_ground_contact_ratio` (agree = \|r_bottom − r_height\| / mean ≤ `ground_contact_tolerance`; a bottom above the horizon never agrees): billboard / poster car, car on a carrier, reflection |
| 4 | `off_road` | `road_distance` | OSRM `/nearest` distance of the estimated position > `max_road_distance_m`: yard, garage, fuel station, car park |
| 10 | `parked` | `in_corridor`, `road_side` (informational) | subtype `blocking` if in our corridor at the closest sighting; else `wrong_side` if on the oncoming lane's side for `drive_side`; else `roadside` |

**Why the range-error tolerance.** A static object whose range is mis-scaled by a factor k
appears to drift by |1 − k| × our displacement. A vehicle moving with us drifts by about our
whole displacement. An oncoming vehicle drifts by more, in the opposite direction.

**Image-only fallback (rule 2 without ego motion).** This applies when there is no GPS fix and
no OSD speed, or no usable world position. The prototype's heuristics decide instead:
- `lateral_drift` ≥ `fallback_min_lateral_drift`;
- `growth_rate` ≤ `fallback_max_growth_rate`;
- `exit_or_growth`: the box leaves by a side (`fallback_edge_margin`) or grows ≥
  `fallback_min_scale_growth`.

Such a verdict is never auto-accepted. The score is capped at `fallback_score_cap` and the
accept blocker `fallback_heuristics` turns any auto-accept into review.

**Road distance unknown.** The reason is `road_distance_unknown` and the rule is skipped when:
- `road_check` is off;
- there is no GPS-estimated position (odometry or fallback mode);
- OSRM is unreachable. After the first failure, OSRM is not queried again for the rest of the
  job.

OSRM is queried only for tracks that would otherwise be `parked` or `off_road`.

## 4. Score

The score is `verifier_score`, between 0 and 1, deterministic and monotonic:
`Σ wᵢ·cᵢ / Σ wᵢ` with the weights `score_weights`. Each component is non-decreasing in the
evidence it measures.

| Component | Value | Unknown |
|---|---|---|
| `detector` | (mean conf − `detector_confidence`) / (1 − `detector_confidence`) | — |
| `evidence` | ½·min(1, n / 2·`min_sightings`) + ½·min(1, s / 2·`min_track_seconds`) | — |
| `target_speed` | 1 − speed / allowed speed (fallback: 1 − growth / max) | — |
| `ego_motion` | min(1, ego distance / 2·`min_ego_distance_m`) | fallback: 0 |
| `ground_contact` | share of agreeing sightings | 0.5 |
| `road_distance` | 1 − distance / `max_road_distance_m` | 0.5 |
| `proximity` | min(1, closest area / 4·`min_box_area_at_closest`) | — |

## 5. Storage and AI validation

- **`parked`**: stored as type `parked_vehicle` with the subtype. It goes through the normal AI
  validation routing (`app.services.ai_validation`) with `verifier_score` = the score. The
  `auto_accept` / `auto_reject` values on the score come from `ai_validation.type_overrides
  .parked_vehicle`, else from `auto_accept_score` / `auto_reject_score` of this section (shown as
  source `card` in `GET /models/`). `detector_accept` / `detector_reject` on the best-sighting
  confidence come from the model card (0.6 / 0.35), else from section `validation`. Location
  rules, audit sampling and modes are unchanged.
- **`store_rejected_for_review` = true**: the verdicts listed in `review_verdicts` are stored as
  automatic rejections, for a human to audit:

  | Verdict | Rejection code |
  |---|---|
  | `off_road` | `ai_off_road` |
  | `not_grounded` | `ai_not_grounded` |
  | `traffic_queue` | `ai_traffic_queue` |
  | `moving` (only when listed) | `ai_moving_vehicle` |

  Manual mode and audit sampling still apply (`ai_validation.rule_reject`). These rejection
  reasons are seeded for type `parked_vehicle`.
- **Everything else** (`moving`, `too_far`, `insufficient_evidence`) is not stored. It is counted
  in the job events `done` / `cancelled` as `details.vehicle_verdicts`, e.g.
  `{"parked": 3, "moving": 41, "too_far": 12}`.
- **Geo-dedup**: about 55 m per job, applied to stored tracks only.

Stored on the detection:
- `verifier_model` = `parked_vehicle_rules:v1@<sha256(settings)[:12]>`: which rule set and which
  settings judged it.
- `ai_details`: verdict, subtype, **severity** (detections have no severity column),
  vehicle_class, score and components, motion, fallback, reasons, features, target_position,
  road_distance_m, camera_profile.
- `recorded_at` (§7).

`validation.distinct_subtype_types` now defaults to `["traffic_sign", "parked_vehicle"]`, so a
`blocking` vehicle never merges into the same hazard as a `roadside` one. A database that
already stores this setting keeps its value: add `parked_vehicle` from the console.

**Severity.** `severity_by_subtype` (blocking → high, wrong_side → medium, roadside → low), plus
`heavy_vehicle_bump` levels (capped at `critical`) when the vehicle class is in
`heavy_vehicle_classes`.

## 6. Settings — section `parked_vehicle`

Edit with `GET /settings/` and `PUT /settings/parked_vehicle`. The nested maps `camera`,
`score_weights`, `class_heights_m` and `severity_by_subtype` are merged key by key on PUT.

| Setting | Default | Meaning |
|---|---|---|
| `vehicle_classes` | `["car","bus","truck"]` | COCO classes kept (`motorcycle` available, off) |
| `detector_confidence` | 0.35 | detector box confidence floor |
| `mask_regions` | `[[0,0,1,0.235]]` | normalised `[x1,y1,x2,y2]` rectangles ignored (OSD text rows) |
| `hood_mask_bottom_ratio` | 0.10 | bottom band ignored (own bonnet) |
| `tracker_max_gap` | 3 | sampled frames a vehicle may be missed |
| `tracker_match_iou` | 0.10 | IoU with the (predicted) last box to continue a track |
| `min_sightings` | 5 | rule 6 |
| `min_track_seconds` | 2.5 | rule 6 |
| `min_mean_confidence` | 0.5 | rule 6 |
| `min_box_area_at_closest` | 0.008 | rule 7 (share of the frame area) |
| `min_ego_distance_m` | 30 | rule 1 |
| `min_ego_speed_kmh` | 15 | rule 1 (median over the track) |
| `max_target_speed_kmh` | 3 | rule 2 base limit |
| `max_world_drift_m` | 8 | rule 2 base limit |
| `range_error_ratio` | 0.25 | rule 2: share of the ego speed / distance added to the limits |
| `max_range_m` | 80 | sightings farther than this are not used for world positions |
| `fallback_min_lateral_drift` | 0.12 | fallback heuristics |
| `fallback_edge_margin` | 0.18 | fallback heuristics |
| `fallback_max_growth_rate` | 2.0 | fallback heuristics, per second |
| `fallback_min_scale_growth` | 3.0 | fallback heuristics |
| `fallback_score_cap` | 0.6 | maximum score without ego motion |
| `ground_contact_tolerance` | 0.4 | rule 3, relative range disagreement |
| `min_ground_contact_ratio` | 0.5 | rule 3, share of sightings that must agree |
| `ground_contact_max_range_m` | 40 | rule 3, farther sightings are not judged |
| `class_heights_m` | car 1.5, bus 3.0, truck 3.2, motorcycle 1.2 | apparent-height range |
| `road_check` | true | rule 4, OSRM `/nearest` (`OSRM_BASE_URL`) |
| `max_road_distance_m` | 12 | rule 4 |
| `queue_ego_speed_kmh` | 5 | rule 5 |
| `queue_min_vehicles` | 2 | rule 5 |
| `camera` | horizon_y 0.45, center_offset 0.0, camera_height_m 1.4, vertical_fov_deg 60, corridor_halfwidth_at_horizon 0.08, corridor_halfwidth_at_bottom 0.42 | default calibration |
| `camera_profiles` | `{}` | `{OSD vehicle id: partial camera override}` |
| `drive_side` | `right` | traffic side (Cameroon); `left` swaps wrong_side and roadside |
| `score_weights` | detector 0.25, evidence 0.15, target_speed 0.25, ego_motion 0.10, ground_contact 0.10, road_distance 0.10, proximity 0.05 | §4 |
| `auto_accept_score` | 0.80 | score ≥ this may auto-accept (with the mode's other conditions) |
| `auto_reject_score` | null | score ≤ this auto-rejects (`ai_verifier`); off by default |
| `severity_by_subtype` | blocking high, wrong_side medium, roadside low | severity |
| `heavy_vehicle_classes` | `["bus","truck"]` | heavy-vehicle classes |
| `heavy_vehicle_bump` | 1 | severity levels added for heavy vehicles |
| `store_rejected_for_review` | false | rule 12 |
| `review_verdicts` | `["off_road","not_grounded","traffic_queue"]` | verdicts stored as auto-rejected (`moving` allowed) |

Related settings elsewhere:
- `MODEL_VEHICLES` and `OSD_TIMEZONE` (default `Africa/Douala`) in the environment / `app.core.config`.
- `processing.frame_skip_by_model.vehicles`.

## 7. Recording time (`recorded_at`)

This applies to every detection type and to every `job_track_points` fix. The value is the OSD
date-time of the frame. When the frame's OSD time is unreadable, it is the nearest OSD time of
the same video ± the video-time difference. When the video never showed a readable time, it is
NULL. It is stored timezone-aware (`TIMESTAMPTZ`), with the OSD read as `OSD_TIMEZONE` local
time. The source time of the job is not known to the platform, so it is not used.

The hazard lifecycle's video passes use the track point's `recorded_at` first. Older jobs fall
back to recording start + `video_time_s`.

DB changes: `app/db/upgrade.py` (`PARKED_VEHICLE_COLUMNS`) with the mirror
`migrations/add_parked_vehicle.sql`:
- `detections.recorded_at`, with an index;
- `detections.ai_details`;
- `job_track_points.recorded_at`;
- the four rejection reasons.

## 8. Calibrating a camera profile

Use one frame of a straight, flat road from that vehicle's video.

1. **`horizon_y`**: the y of the vanishing point of the lane lines, as a fraction of the frame
   height.
2. **`center_offset`**: the x of that vanishing point − 0.5. Positive when the camera is mounted
   left of the road axis.
3. **`camera_height_m`**: the lens height above the road, measured on the vehicle (about 1.3 m
   for a car, 2.0–2.5 m for a truck or bus).
4. **`vertical_fov_deg`**: from the dashcam's specification. To check it, park a car at a known
   distance d. Its box bottom y_b should satisfy
   `d ≈ camera_height_m · 0.5 / tan(fov/2) / (y_b − horizon_y)`. Adjust the FOV until it does.
5. **Corridor half widths**: the half width of our lane at the horizon and at the bottom of the
   frame. A car you know is on the shoulder but reads as `blocking` means the corridor is too
   wide.

Save the profile:

```json
PUT /settings/parked_vehicle
{"camera_profiles": {"LTD123AB": {"horizon_y": 0.52, "camera_height_m": 2.2}}}
```

Check the result in `ai_details`:
- `features.ground_contact_error` should be < 0.2 on real parked cars;
- `target_speed` should be close to 0;
- `camera_profile` names the profile that was used.

## 9. Limits

- **Monocular range.** Accuracy depends on the calibration and on a flat road. Slopes and pitch
  (braking) bias the ranges. `range_error_ratio` absorbs part of this. Do not trust tracks
  farther than about 60 m.
- **Slow traffic.** A vehicle moving slower than `max_target_speed_kmh` + `range_error_ratio` ×
  our speed (about 12 km/h when we drive at 36 km/h) can pass as parked. Lower
  `range_error_ratio` once the cameras are calibrated.
- **One pass.** The pipeline cannot tell a broken-down car from a parked one. It cannot tell a
  car stopped at a light from a parked one when we stop too (that case is `traffic_queue` or
  `insufficient_evidence`).
- **Corridor and side.** These are image geometry, not lane detection. They break on curves,
  multi-lane or one-way roads and off-centre mounts (use `center_offset`). On single-track roads
  the wide default corridor favours `blocking`.
- **OSRM.** OSRM snaps to the road centreline, so a parked car is normally 3–8 m away. With the
  public demo server, `road_check` is best effort.
- **Classes.** COCO classes are generic. Minibuses are often `car` or `bus`; three-wheelers are
  not covered.
