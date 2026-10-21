# Hazard clusters — cluster-first detections

**One real hazard = one cluster, reviewed once.** Detections no longer pile up as separate
review items each time the same road (or the same video) is processed.

```
detection ──▶ cluster ──▶ review once ──▶ hazard ──▶ re-sightings auto ──▶ resolution
 (created)   (attach)    (accept/reject   (HZ code)   (validated by        (absence passes /
                          the cluster)                  the system)          votes / expiry / manual)
```

## 1. Attach at creation

Every detection that is **not rejected** and has a **usable position** (confirmed OCR GPS
or approximate / interpolated GPS) joins a cluster as soon as the processor stores it
(`hazard_service.attach`):

* radius = the Cluster Configuration resolved at the detection's position for its type
  (zone → urban / rural profile → per-type value);
* cluster = the **nearest open cluster** of the same type (same subtype for
  `validation.distinct_subtype_types`, e.g. traffic signs) within that radius — a
  candidate or a confirmed hazard, whatever the review status of its members;
* none → a new cluster: a **candidate** (`status=candidate`, code `CL-000123`) for an
  unreviewed detection, a confirmed **hazard** (`status=active`, code `HZ-000123`) for one
  the AI already accepted.

Detections without a position (`location_pending`, GPS missing) join when a validator
fixes the position (`POST /detections/{id}/location`); a class correction or a new
position re-evaluates the cluster. A rejected detection leaves its cluster; a candidate left
without members is deleted. Aggregates (centroid — from confirmed positions when there are
any —, counts, first / last seen, confidence, cover image, address, zone) cover every
non-rejected member; `member_counts` gives them by review status.

After attaching, the cluster's centre has moved: any other **open cluster** of the same type
(same subtype rule) whose centre is within the **larger of the two radii** is **merged** into
it (`hazard_service.merge_nearby`): confirmed (`active`) before candidate, then the oldest is
kept with its code; members, timeline and votes move; `merged` event (+ audit
`hazard.merge`); a candidate merged into a confirmed hazard has its unreviewed members
auto-validated as re-sightings.

The per-job geo dedup of the processor still suppresses repeats inside one video; the
clusters handle repeats across videos.

### Object position

`detections.latitude / longitude` is the **recording vehicle's GPS fix** at the stored frame
(evidence, unchanged). The same pothole seen from 60 m on one pass and from 10 m on another
would land 50 m apart — beyond a rural radius. Every detection therefore also gets an
**estimated ground position of the object** (`object_latitude`, `object_longitude`,
`object_position_source`; `app/modules/m06_hazards/object_position.py`):

```
object = vehicle fix + range ahead along the heading + lateral offset
```

* **heading** — bearing of the vehicle's own OCR GPS fixes from frame time − 4 s to + 4 s
  (≥ 3 m apart; interpolated fixes never used). Processor: the ego track of the job;
  back-fill: `job_track_points`.
* **range** — flat-road pinhole model (`parked_vehicle/geometry.py`): ground objects
  (pothole, speed bump) from the box bottom below the horizon, upright objects (signs,
  lights) from the apparent height of a ~0.75 m plate, parked vehicles from their pipeline's
  own estimate (`ai_details.target_position`) or the class height; clamped to 1–50 m.
* **lateral** — box centre offset from the optical axis at that range, clamped to ±15 m.
* **camera** — the parked-vehicle calibration: settings `parked_vehicle.camera`
  (`horizon_y` 0.45, `camera_height_m` 1.4, `vertical_fov_deg` 60, `center_offset`) with the
  per-vehicle override `parked_vehicle.camera_profiles[<OSD vehicle id>]`. Calibrate those
  per dashcam to improve every estimate.

`object_position_source` = `estimated`, or `vehicle` when heading / box / camera is unknown
(object columns NULL → the vehicle position is used), NULL = not computed yet. All the
clustering distances use the object position when there is one: attach, cluster centroid
and address, merges, reopening resolved hazards, "seen nearby" in video passes,
`recluster-detections`. A validator's position fix keeps the estimate's offset (trajectory
point / OCR text) or replaces it with the pin (map pin). `DetectionOut` exposes the three
fields (optional).

### Observation identity

The same footage processed several times (the full video, then clips cut from it, a
re-upload under another name …) yields the **same observation**, not new sightings. Two
detections are the same observation (`app/modules/m06_hazards/observations.py`) when they
have:

* the same **type** (current or predicted class) — and subtype for distinct-subtype types;
* the same **camera** — OSD vehicle id (both unknown counts as the same);
* the same **instant** — `recorded_at` (OSD camera time) within ±1 s; when neither has one
  (OSD time unreadable), the same OSD GPS reading (the two coordinate fields of
  `raw_gps_text`); one with and one without a time are different;
* the same **frame region** — normalised box IoU ≥ 0.3, or centres < 3 % of the frame apart.

The job / file name plays no part. The processor looks it up for every finished track (one
query: type + `recorded_at` ± 1 s, index `ix_detections_type_recorded_at`; without a time,
type + same GPS text) against every stored detection, whatever its review status; a repeat
is **not stored** (no evidence uploaded) and is counted as `duplicates_skipped` in the job's
`done` event details.

## 2. Candidates are not hazards

`candidate` and `dismissed` rows live in the `hazards` table (plain-string status, no
migration) but are **never** shown outside the Validations screen: the hazard list,
summary, places, detail, timeline, votes, resolve / reopen (`404`), recurring spots, the
dashboard map, the maps / mobile endpoints (`/detections/nearby`, `/along-route`,
`/detections/clusters` — accepted detections only) all filter on
`PUBLIC_STATUSES = (active, resolved)`. A detection of a candidate has `hazard_code=null`
and `hazard_status=candidate`. Candidates take `number = -nextval(hazard_candidates_seq)`,
so they never consume HZ codes; the HZ number is assigned at confirmation.

## 3. Review by cluster

`GET /hazards/review-queue` (validations:view) lists the clusters that have detections
awaiting a decision — one row per cluster with its unreviewed detections (best first),
cover image and counts, plus `candidates` (count of candidate clusters) and `unclustered`
(unreviewed detections without a position).

* `POST /hazards/{id}/accept` {note?, label?, severity_score?} (validations:edit) accepts
  every `pending` member through the normal workflow (history, audit, training sample);
  the first acceptance **confirms** the candidate (new HZ code, `confirmed` timeline event
  with `previous_code`); the remaining members with an approximate position are then
  auto-validated as re-sightings. Corrections (class, box, position) stay per detection.
* `POST /hazards/{id}/reject` {reason_code, note?} rejects every unreviewed member with the
  same reason (kinds false_positive / real_redundant / unusable apply as for one
  detection); an empty candidate is deleted (`hazard_status=deleted`).
* Individual decisions are still possible (`GET /detections/queue?hazard_id=…`); accepting
  one detection of a candidate also confirms it.

Both are audited (`hazard.accept_cluster`, `hazard.reject_cluster`, plus the per-detection
entries). The web Validations page has a **Group by cluster** mode (default on).

## 4. Re-sightings are automatic

A detection that joins a **confirmed** hazard is decided `validated` by the system, history
action `resight`, note `matches confirmed hazard HZ-…`, and recorded as a `seen` event — it
never enters the review queue (`corrected` if a human had fixed its position). Training
data counts it as an accepted sample decided by the system. Switch it off per type with
`hazard_lifecycle.policies.<type>.auto_confirm_resightings=false` (then the detection stays
pending inside the hazard and shows in the grouped queue under the HZ code).

An unreviewed detection never reopens a resolved hazard: it opens a candidate (a recording
older than the resolution joins the resolved hazard as history, auto-validated, when
re-sightings are auto-confirmed).

## 5. Resolution

| How | Confirmed hazard | Candidate |
|---|---|---|
| Video absence — N valid "not seen" passes (all types; pothole & speed bump 3, same direction; traffic sign 3; parked vehicle 2) | `resolved`, source `video_absence` | `dismissed`; unreviewed members auto-rejected `not_seen_again` (unusable → excluded from training) |
| Driver votes ("not there") | `resolved`, source `votes` | — (not visible to drivers) |
| Expiry (`expiry_hours`, parked vehicles) | `resolved`, source `expiry` | — (candidates never expire by time) |
| Manual (Hazard Track) | `resolved`, source `manual` | reject the cluster |

A pass counts only when the job ran a model of that type, the track is valid (distance,
speed, heading, recording time after the last sighting, daylight if required), the camera
**approached** the spot with it ahead (`pass_require_approach`, `pass_approach_min_m`) and
the job did not produce the hazard's own detections. The timeline shows "Not seen in pass
1/3 (job X)" from the `not_seen` event details (`pass_number`, `threshold`, `job_name`).

## 6. Same video twice

Uploads are fingerprinted (SHA-256 of the content, streamed while receiving); cloud / SFTP
imports hash the downloaded file. When a job with the same fingerprint is done, queued or
processing, `POST /jobs/` answers `409 duplicate_video` {job_id, job_status, filename,
processed_at, fingerprint} unless `force=true` (the web asks "open it / process again");
imports mark the item `skipped_duplicate` with `duplicate_of_job_id` unless the batch
re-imports. Even when forced, cluster-first attachment prevents duplicate hazards.
(`jobs.content_fingerprint`, added by the startup schema upgrade.)

## 7. Existing data

```
python -m app.cli recluster-detections --dry-run --verbose   # report only (rolled back)
python -m app.cli recluster-detections                        # apply
```

Merges duplicate open clusters (same type / subtype within the radius → into the
confirmed one, else the oldest; kept code unchanged; members, events and votes move;
`merged` event), detaches rejected detections, attaches every clusterable detection,
auto-validates re-sightings of confirmed hazards. Idempotent; short transactions, no DDL
(safe while jobs are processing). Reports clusters by status, review queue size and
duplicate pairs (< 30 m, same type, different clusters) before / after.

Clusters are merged when their centres are within the larger of their two radii (the same
rule as after an attach); the report includes `cluster_pairs_within_radius` (0 after a run).

```
python -m app.cli dedupe-observations --dry-run --verbose --show HZ-001054   # report only
python -m app.cli dedupe-observations                                         # apply
```

1. adds the object-position columns / index when missing (no lock once they exist);
2. **duplicate observations** (identity rule above, grouped transitively): per group the
   **earliest created detection a person reviewed** is kept (its decision, history and
   training sample stay intact), else the earliest created; the others are deleted — hazard
   events pointing to them are re-pointed to the kept one, their validation label moves (or
   goes), their training sample goes (its frame is re-synced, its crop removed); evidence
   objects in MinIO are removed only when no remaining detection or training frame refers to
   them; differing human decisions in one group are reported as conflicts;
3. back-fills the object positions, recomputes every cluster centre;
4. runs `recluster-detections` (merges, attachments, re-sightings).

Idempotent; short transactions (the dry run is one rolled-back transaction). Reports
detections, clusters by status, duplicate observation groups, cluster pairs within radius
and object-position sources before / after.

`python -m app.cli rebuild-hazards` still rebuilds everything from scratch (accepted
detections → hazards, unreviewed → candidates; lifecycle history is lost).
