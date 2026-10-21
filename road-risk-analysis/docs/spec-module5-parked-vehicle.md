# Module 5 — Parked / roadside vehicle detection

Requirements RRAP-05-001 / 05-003 / 05-015. Code: `app/modules/m05_ai_detection/parked_vehicle/`
(pure, unit-tested in `tests/test_parked_vehicle.py`), driven by `app/modules/m07_ingestion/pipeline.py`.

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
   - the box centre is outside every OSD-overlay rectangle of the frame's equipment layout
     (`mask_regions_by_layout[osd.layout]`: `total` = the three text rows on top, `besafe` /
     `ola_energy` = GPS + name lines on top and the date / plate line at the bottom; `mask_regions`
     for an unknown layout) and above the bottom `hood_mask_bottom_ratio` band (our own bonnet).

   Every kept `car` box also feeds the job's **camera self-calibration** (`calibration.py`): for a
   grounded car, `y_bottom = horizon + (H_cam / H_car) · h`, so a trimmed straight-line fit over the
   car boxes gives the horizon and the lens height of THIS dashcam (`auto_calibrate`, used from
   `auto_calibrate_min_boxes` boxes on). Finished vehicle tracks are held back (at most 80) until
   it has converged, then judged with it.

   Masked boxes are counted in the job event (`vehicle_boxes_masked`).
2. **Tracking.** The pipeline uses `static_check.Tracker` with motion prediction: a box matches
   the track's last box, or that box extrapolated with the track's image velocity
   (`tracker_match_iou`, or the centres are closer than the reference box size). The boxes of a
   frame are assigned globally (best IoU first, `observe_frame`), and a **size gate**
   (`tracker_max_scale_change` per sampled step) stops a big car leaving by the side from handing
   its track over to a small far car — an ID switch that used to mix a parked and a moving vehicle
   in one track. A track ends after `tracker_max_gap` missed sampled frames. The per-model `processing.frame_skip_by_model.vehicles`
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
   - **Closing ratio** (rule 2b): −Δrange / our own travelled distance, fitted over the sightings
     (weight 1 / range): **parked ≈ 1**, a vehicle **ahead of us at our speed ≈ 0**, **oncoming > 1**
     (≈ 2 at equal speeds). Computed from the contact-line range (`closing_ratio`, decides) and
     from the apparent height (`closing_ratio_height`, box growth, informational — class heights
     are unknown for trucks). It needs our travel only (OSD speed, else the GPS path), no heading,
     so curves and GPS heading noise do not matter. `followed`: ≥ `followed_min_corridor_share` of
     the sightings in our corridor and the box area changing less than
     `followed_max_scale_change` while we drive ≥ `closing_min_ego_m`.
   - **Lateral offset** (`lateral_m`, median over the sightings closer than 40 m, display aspect
     16:9 — the dashcams record non-square pixels).
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
| 5 | `traffic_queue` | `oncoming_lane` | still, but inside the oncoming lane: 0 < offset towards the oncoming side < `oncoming_lane_max_offset_m` — a jam on the other side (a parked car stands at the road edge, farther out) |
| 5 | `traffic_queue` | `queue_ego_stop`, `queue_peers` | ≥ 2 sightings in our corridor while our speed < `queue_ego_speed_kmh`; or the vehicle is stationary in our corridor and, together with the other stationary in-corridor vehicles seen at the same time, they number ≥ `queue_min_vehicles` |
| 2b | `moving` | `closing_ratio` (`box_growth_ratio` when the contact line is unusable), `followed_vehicle` | closing ratio outside [`closing_ratio_min`, `closing_ratio_max`] (below = ahead of us / same direction, above = oncoming), or a followed vehicle |
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

A vehicle that looks still by these heuristics is **`insufficient_evidence`**, never `parked`
(`fallback_parked` = false, default): without our own speed a car driving ahead of us or a slow
oncoming car cannot be told from a parked one — this was the main source of "the vehicle in
front is flagged as parked". With `fallback_parked` = true (old behaviour) it is `parked`, the
score is capped at `fallback_score_cap` and the accept blocker `fallback_heuristics` turns any
auto-accept into review.

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
  validation routing (`app.modules.m05_ai_detection.ai_validation`) with `verifier_score` = the score. The
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
- `verifier_model` = `parked_vehicle_rules:v2@<sha256(settings)[:12]>`: which rule set and which
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
| `mask_regions` | `[[0,0,1,0.235]]` | normalised `[x1,y1,x2,y2]` rectangles ignored (OSD text rows) for an unknown layout |
| `mask_regions_by_layout` | total `[[0,0,1,0.235]]`; besafe, ola_energy `[[0,0,1,0.18],[0,0.87,1,1]]` | masks per OSD layout (`OSDData.layout`) |
| `hood_mask_bottom_ratio` | 0.10 | bottom band ignored (own bonnet) |
| `tracker_max_gap` | 3 | sampled frames a vehicle may be missed |
| `tracker_match_iou` | 0.10 | IoU with the (predicted) last box to continue a track |
| `tracker_max_scale_change` | 2.5 | box-area ratio allowed per sampled step (× steps missed): ID-switch gate |
| `min_sightings` | 5 | rule 6 |
| `min_track_seconds` | 2.5 | rule 6 |
| `min_mean_confidence` | 0.5 | rule 6 |
| `min_box_area_at_closest` | 0.008 | rule 7 (share of the frame area) |
| `min_ego_distance_m` | 20 | rule 1 (was 30) |
| `min_ego_speed_kmh` | 15 | rule 1 (median over the track) |
| `max_target_speed_kmh` | 3 | rule 2 base limit |
| `max_world_drift_m` | 8 | rule 2 base limit |
| `range_error_ratio` | 0.25 | rule 2: share of the ego speed / distance added to the limits |
| `max_range_m` | 80 | sightings farther than this are not used for world positions / the closing ratio |
| `closing_check` | true | rule 2b on/off |
| `closing_ratio_min` / `closing_ratio_max` | 0.5 / 1.8 | rule 2b window (parked ≈ 1) |
| `closing_min_ego_m` | 10 | our travel needed over the fitted sightings |
| `closing_min_points` | 4 | sightings needed for the fit |
| `followed_min_corridor_share` / `followed_max_scale_change` | 0.6 / 1.5 | "followed vehicle" |
| `world_drift_check` | true | rule 2 (world-position fit) on/off |
| `fallback_parked` | false | without ego motion a still-looking vehicle is `insufficient_evidence` (true = old capped `parked`) |
| `fallback_min_lateral_drift` | 0.12 | fallback heuristics |
| `fallback_edge_margin` | 0.18 | fallback heuristics |
| `fallback_max_growth_rate` | 2.0 | fallback heuristics, per second |
| `fallback_min_scale_growth` | 3.0 | fallback heuristics |
| `fallback_score_cap` | 0.6 | maximum score without ego motion |
| `ground_contact_tolerance` | 0.4 | rule 3, relative range disagreement |
| `min_ground_contact_ratio` | 0.5 | rule 3, share of sightings that must agree |
| `ground_contact_max_range_m` | 40 | rule 3, farther sightings are not judged |
| `class_heights_m` | car 1.5, bus 3.0, truck 3.2, motorcycle 1.2 | apparent-height range |
| `class_height_ranges_m` | car 1.3–2.0, bus 2.4–3.6, truck 1.6–4.2, motorcycle 1.0–1.7 | rule 3: the contact line agrees when it falls inside the height span (a COCO `truck` is anything from a pickup to a semi) |
| `road_check` | true | rule 4, OSRM `/nearest` (`OSRM_BASE_URL`) |
| `max_road_distance_m` | 12 | rule 4 |
| `queue_ego_speed_kmh` | 5 | rule 5 |
| `queue_min_vehicles` | 2 | rule 5 |
| `oncoming_lane_max_offset_m` | 4.5 | rule 5b, 0 = off |
| `camera` | horizon_y 0.45, center_offset 0.0, camera_height_m 1.4, vertical_fov_deg **21** (was 60), display_aspect 16/9, corridor_halfwidth_at_horizon 0.08, corridor_halfwidth_at_bottom 0.42 | default calibration |
| `camera_by_layout` | besafe: horizon_y 0.57, camera_height_m 1.9, vertical_fov_deg 45 | per OSD layout (equipment) override |
| `camera_profiles` | `{}` | `{OSD vehicle id: partial camera override}` — wins over the layout and over the self-calibration |
| `auto_calibrate` / `auto_calibrate_min_boxes` | true / 40 | per-job horizon + lens height from the car boxes |
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

DB changes: `app/core/db/upgrade.py` (`PARKED_VEHICLE_COLUMNS`) with the mirror
`migrations/add_parked_vehicle.sql`:
- `detections.recorded_at`, with an index;
- `detections.ai_details`;
- `job_track_points.recorded_at`;
- the four rejection reasons.

## 8. Calibrating a camera profile

Order of precedence: `camera` defaults → `camera_by_layout[osd layout]` → the job's
self-calibrated horizon + lens height (`auto_calibrate`) → `camera_profiles[vehicle id]` (a
measured profile disables the self-calibration for that vehicle). The self-calibration cannot
see the focal length: `vertical_fov_deg` stays the layout / default value. To measure it for a
camera, label a few parked vehicles of its video in `tools/parked_eval` and run `calibrate.py`
(§10): it fits the range scale K = H_cam·f against our OSD travel.

By hand, use one frame of a straight, flat road from that vehicle's video.

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

## 10. Evaluation harness (`tools/parked_eval/`)

The harness replays real video segments through **the job worker's own code**:
`pipeline._JobContext.observe_osd` (ego path), `pipeline._vehicle_tracker` + `_observe_vehicles`
(filters, masks, self-calibration, tracking), `pipeline._vehicle_batch` (tracks held back until
calibrated) and `pipeline._judge_vehicle_track` (`pv.prepare` → OSRM → `pv.finish`, queue peers).
Only the detector and the OSD reader are cached, so rules / settings can be re-judged in seconds.

| File | Role |
|---|---|
| `segments.json` | the segments (video, start, duration) |
| `extract.py` | stage A, heavy (API image): sampled frames (frame_skip 5) → vehicles detector at conf 0.05, all 4 COCO vehicle classes + the job's OSD reader (once per second of video) → `cache/<segment>.json` |
| `evaluate.py` | stage B, light (test venv or API image): replay → `out/<tag>/tracks.json` (per track: verdict, subtype, score, motion, every reason, every feature incl. ego speed source, closing ratios, world drift, ground contact, corridor, side, closest box, lateral offset, camera used), `report.md` / `report.json` (confusion matrix, precision / recall of `parked`), `--sheets` contact sheets, `--video` annotated video |
| `label.py` | labels a run's tracks → `labels/<segment>.json` (`parked`, `moving_same_dir`, `oncoming`, `queue`, `crossing`, `ignore`); a label stores the vehicle's sightings, so it survives tracker / rule changes |
| `calibrate.py` | horizon + lens height per video from the car boxes, range scale K from the labelled parked vehicles → vertical FOV |
| `bench_models.py` | CPU cost per sampled frame of every detector + the OSD reader |
| `run_docker.sh` | runs a script in `road-analysis-api_api` (`--cpus 4 -m 4g`, backend mounted read-only) |

```bash
tools/parked_eval/run_docker.sh extract.py                       # once (≈ 5–8 min per 60 s segment)
python=~/.cache/viasafe-testvenv/bin/python
$python tools/parked_eval/evaluate.py --tag after --sheets        # seconds
$python tools/parked_eval/evaluate.py --tag before --backend <export of the previous backend>
$python tools/parked_eval/evaluate.py --tag x --settings patch.json            # try settings
$python tools/parked_eval/evaluate.py --tag x --no-ego-layouts besafe          # replay without OSD GPS/speed
$python tools/parked_eval/label.py <segment> --tag after 12=parked:"note" 15=oncoming
python3 tools/parked_eval/calibrate.py
```

**Data set (2026-10-21).** 19 segments of 60–90 s from the six videos (LTTR 458 AV, LT-183-GO,
TEMCM111, TEMCM127 — `total` layout, 720×576 / 720×288 — and the two Mint0639 — `besafe`,
704×576): town streets with parked rows, a market, a fuel station, open road, followed trucks /
tankers / a bus, oncoming traffic, an oncoming jam, stops behind trucks. 176 vehicles labelled by
looking at the frames: 40 parked, 8 followed / same direction, 54 oncoming, 15 stopped traffic
(queue / oncoming jam), 2 crossing, 57 `ignore` (ambiguous, or parked while we never moved — not
judgeable). Precision / recall are per vehicle: a vehicle counts as flagged when any of its
tracks is stored as `parked`.

**Before** (commit ce09a20 rules and defaults; same detector, OSD and labels):

| truth \ verdict | parked | moving | traffic_queue | insufficient_evidence | too_far | not_grounded | no_track |
|---|---|---|---|---|---|---|---|
| parked | **3** | 30 | 0 | 4 | 1 | 0 | 2 |
| moving_same_dir | 0 | 8 | 0 | 0 | 0 | 0 | 0 |
| oncoming | 0 | 34 | 10 | 10 | 0 | 0 | 0 |
| queue | 0 | 8 | 1 | 5 | 0 | 0 | 1 |

precision 1.00 (3 / 3), **recall 0.075** (3 / 40). Without the OSD (GPS / speed unreadable — the
Mint0639 `besafe` videos before the new OSD reader): the image-only fallback flags a vehicle
driving ahead of us as `parked` (precision 0.75, recall 0.075).

**After** (this version, defaults):

| truth \ verdict | parked | moving | traffic_queue | insufficient_evidence | too_far | not_grounded | no_track |
|---|---|---|---|---|---|---|---|
| parked | **24** | 5 | 0 | 8 | 1 | 1 | 1 |
| moving_same_dir | 0 | 7 | 0 | 0 | 1 | 0 | 0 |
| oncoming | 0 | 28 | 12 | 14 | 0 | 0 | 0 |
| queue | 1 | 1 | 6 | 7 | 0 | 0 | 0 |
| crossing | 0 | 1 | 1 | 0 | 0 | 0 | 0 |

**precision 0.96** (24 / 25), **recall 0.60** (24 / 40); no followed / oncoming vehicle is flagged.
Per video: TEMCM127 12 / 12 parked found, LT-183-GO 9 / 18, Mint0639 1 / 4, TEMCM111 1 / 4,
LTTR 1 / 2. The one false positive is a vehicle of an oncoming jam whose track mixes two cars.
Without any OSD speed / GPS nothing is flagged (`insufficient_evidence`). Misses: tracks shorter
than `min_track_seconds` at 50–60 km/h (8), closing ratio outside the window on noisy contact
lines (5), boxes too small or never tracked (2), one truck failing ground contact.

**Root causes found (and fixed).**
1. *Camera calibration.* The default pinhole camera (horizon 0.45, 60° vertical FOV, 1.4 m) was
   off by a factor 2–3 in range: measured on the videos, horizons are 0.34–0.65 of the frame and
   the vertical FOV 18–31° (`total` dashcams) / ≈ 45° (`besafe`). With ranges 2–3× too short, a
   parked car seemed to approach at ⅓–½ of our speed (→ `moving`, the parked vehicles "not taken
   into consideration") and an oncoming car at about our speed (→ looks parked). Fixed by the
   per-job self-calibration of horizon / lens height, the 21° default, the `besafe` layout
   profile, the 16:9 display aspect for lateral offsets, and judging tracks only once calibrated.
2. *No ego motion → parked.* Without OSD speed / GPS the image heuristics accepted vehicles
   driving ahead of us and slow oncoming ones as parked. Now `insufficient_evidence`.
3. *Followed / oncoming vehicles.* The world-position fit depends on the GPS heading; the
   closing ratio does not, and separates parked (≈ 1), ahead / same direction (≈ 0) and oncoming
   (≈ 2). Still vehicles inside the oncoming lane (a jam) are `traffic_queue`.
4. *ID switches.* A large box leaving by the side could continue on a small far box ("centres
   closer than the larger box"): size gate + per-frame global assignment.
5. *Masks.* The OSD masks follow the equipment layout (`besafe` has a bottom text line).
6. *Ground contact* for `truck` used one height (3.2 m) for pickups and semis alike → height span.
