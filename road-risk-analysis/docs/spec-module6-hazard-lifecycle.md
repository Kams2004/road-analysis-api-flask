# Module 6 — Hazard lifecycle

Hazards are persisted clusters of accepted AI detections (`app/modules/m06_hazards/hazard_service.py`,
see `spec-module6-hazard-validation.md`). The **lifecycle** decides how long a hazard stays
active and visible to drivers: it is re-sighted in later videos, resolved after valid
"not seen" video passes, voted on by drivers, expired after a time without sightings, and
recognised when it keeps coming back at the same place.

Code: `app/modules/m06_hazards/lifecycle/` — `settings.py`, `policy.py`, `timeutil.py`,
`events.py`, `sightings.py`, `video_pass.py`, `votes.py`, `expiry.py`, `recurring.py`,
`service.py`; Celery tasks in `app/workers/hazard_tasks.py`.

Everything is generic per hazard type. The defaults keep potholes, speed bumps and traffic
signs persistent (as before) and make `parked_vehicle` transient.

## 1. Time

Every lifecycle instant is a naive **UTC** datetime. The *recording time* of a detection is,
in order: `detections.recorded_at` (video recording time, timezone-aware), the OSD timestamp
`captured_at` (read as local time in `timezone` when `osd_time_is_local`), then `created_at`
(processing time, an upper bound).

`last_seen_at` is the recording time of the latest sighting — not when the video was
processed — so a video uploaded days later never makes a hazard look fresh.

## 2. Policies

`policy = policies[type]` (else `default_policy`) ⊕ `subtype_overrides[subtype]`
⊕ heavy vehicle (`vehicle_class` ∈ `heavy_vehicle_classes`, read from the detection's
`ai_details.vehicle_class`) → `expiry_hours = heavy_vehicle_expiry_hours`.
A hazard's subtype is its own (distinct-subtype types) or its most recent member's; the
vehicle class is a heavy class if any member is heavy.

| Field | Meaning | Default (TypePolicy) |
|---|---|---|
| `expiry_hours` | hours after `last_seen_at` before it expires; `null` = never | `null` |
| `heavy_vehicle_expiry_hours` | expiry for trucks/buses; `null` = no special case | `null` |
| `absence_resolution` | resolve after valid "not seen" passes | `false` |
| `absences_to_resolve` | valid passes since the last sighting needed | 2 |
| `require_same_direction` | unknown original heading → pass not valid | `false` |
| `pass_daylight_only` | passes count only in `daylight_hours` | `false` |
| `votes_enabled` | drivers may vote | `true` |
| `not_there_to_resolve` | distinct "not there" voters since the last sighting | 2 |
| `still_there_extends` | a "still there" vote is a sighting | `false` |
| `reopen_on_sighting` | a new accepted sighting reopens the resolved hazard | `false` |
| `reopen_window_hours` | … only within this many hours of the resolution evidence; `null` = the expiry hours (no expiry = no limit) | `null` |
| `recurring_enabled` | flag recurring spots | `false` |
| `show_to_drivers` | returned by the mobile queries | `true` |
| `alert_priority` | `low` / `normal` / `high`, returned to the app | `normal` |
| `subtype_overrides` | `{subtype: {field: value}}` partial policies | `{}` |

Default policies:

| Type | Expiry | Absence resolution | Votes | Other |
|---|---|---|---|---|
| `pothole`, `speed_bump` | never | off (repairs are slow, a missed pothole is common) | on, 3 "not there" | — |
| `traffic_sign` | never | on, 3 passes, `require_same_direction` | on, 3 | — |
| `parked_vehicle` | `blocking` 2 h (priority high), `wrong_side` 4 h (high), `roadside` 6 h (low, shown), other 6 h; trucks/buses 24 h | on, 2 passes | on, 2 | `still_there_extends`, `reopen_on_sighting`, `recurring_enabled` |
| any other (`default_policy`) | never | off | on, 3 | — |

## 3. Settings — section `hazard_lifecycle`

`GET /platform-settings/` and `PUT /platform-settings/hazard_lifecycle` (permission
`settings`). `policies` is merged per type and per field: `{"policies": {"parked_vehicle":
{"absences_to_resolve": 3}}}` changes that one value; `subtype_overrides` merge per subtype
(`null` removes one).

| Setting | Default | Meaning |
|---|---|---|
| `expiry_check_minutes` | 5 | period of the expiry sweep |
| `timezone` | `Africa/Douala` | local time for daylight and OSD times |
| `daylight_hours` | `[6, 18]` | `[start, end)` local hours |
| `osd_time_is_local` | `true` | OSD `captured_at` is camera local time |
| `pass_max_distance_m` | 30 | closest confirmed GPS fix within this distance |
| `pass_max_speed_kmh` | 60 | maximum speed at that fix |
| `pass_heading_tolerance_deg` | 60 | maximum angle to the original sighting heading |
| `pass_require_recording_time` | `true` | unknown recording time → not a valid pass (else processing time is used) |
| `type_models` | `pothole→[pothole]`, `traffic_sign→[signs]`, `speed_bump→[speedbump]`, `parked_vehicle→[vehicles, parked_vehicle]` | job models that can see each type |
| `vote_max_distance_m` | 300 | voter within this distance of the hazard |
| `vote_cooldown_minutes` | 60 | one counted vote per user per hazard per period |
| `recurring_min_occurrences` | 3 | resolved hazards needed for a recurring spot |
| `recurring_window_days` | 30 | look-back window |
| `recurring_radius_m` | `null` | `null` = the hazard's cluster radius |
| `heavy_vehicle_classes` | `["truck", "bus"]` | classes using `heavy_vehicle_expiry_hours` |
| `default_policy`, `policies` | see §2 | |

## 4. Rules

### 4.1 Sighting (`seen`)
* An **accepted** detection joins its hazard through `hazard_service.attach` (unchanged
  clustering) and counts as a sighting.
* When a job is **done**, each of its detections that is **not rejected** (pending review
  included) with a confirmed or approximate position is matched to the nearest **active**
  hazard of the same type within the cluster radius (same subtype for distinct-subtype
  types) and counts as a sighting.
* Effect: `last_seen_at` = its recording time when newer; `absence_count` recounted;
  `expires_at` recomputed; one `seen` event per detection (idempotent).

### 4.2 Accepted detection near a resolved hazard (no active one)
With the most recently resolved hazard of the same kind within the radius:
* recorded **before** the resolution evidence → joins it as **history** (resolved at once,
  never shown);
* newer and policy `reopen_on_sighting`, within `reopen_window_hours` → that hazard is
  **reopened** (`reopened` event, members un-resolved, source `sighting`);
* otherwise a **new hazard** opens (previous behaviour).

### 4.3 "Not seen" video pass (`not_seen`)
After a job is `done` (`job_service.schedule_hazard_lifecycle` → Celery
`evaluate_job_hazards`; the periodic tick catches jobs whose task could not be queued), for
each active hazard whose policy has `absence_resolution` and whose type's model ran in the
job, the trajectory is a **valid pass** when, at its closest **confirmed** (OCR) GPS fix:

1. the fix is ≤ `pass_max_distance_m` from the hazard;
2. the job produced **no detection** of that type within the hazard radius (that is a
   sighting, any status — even rejected — prevents an absence);
3. speed ≤ `pass_max_speed_kmh` (OSD speed, else derived from neighbouring fixes);
4. travel heading within `pass_heading_tolerance_deg` of the heading at one of the hazard's
   original sightings (from the members' own job trajectories); if no original heading is
   known, either direction is accepted unless `require_same_direction`;
5. recording time known and **after** `last_seen_at` (recording time of a fix = job
   recording start + `video_time_s`; the start is the median of the job's detections'
   recording time − `video_time_s`);
6. with `pass_daylight_only`, inside `daylight_hours` local time.

A valid pass is one `not_seen` event (once per job and hazard). `absence_count` = valid
passes recorded after `last_seen_at`, so any newer sighting resets it and late-processed
videos count in recording order. At `absences_to_resolve` → resolved, source
`video_absence`.

### 4.4 Driver votes
`still_there` / `not_there` from a signed-in mobile user within `vote_max_distance_m`, one
counted vote per user per hazard per `vote_cooldown_minutes`, hazard active and not
expired, policy `votes_enabled`. Refused votes for distance / cooldown are stored
(`counted=false`, `reason`), every vote is audited (`hazard.vote`). Drivers never see counts.
* `still_there` → `vote_still_there` event; with `still_there_extends`, `last_seen_at` = now
  (expiry extended, absences reset).
* `not_there` → `vote_not_there` event; distinct voters with a counted "not there" since the
  last sighting ≥ `not_there_to_resolve` → resolved, source `votes`.

### 4.5 Expiry
`expires_at = last_seen_at + expiry_hours` (`null` = never). A hazard is hidden from drivers
as soon as `expires_at` passes; the Celery beat `hazard_lifecycle_tick` (every minute, runs
at most every `expiry_check_minutes`) recomputes `expires_at` from the current policy and
resolves the expired ones (`expired` + `resolved` events, source `expiry`).

**Stale evidence**: an accepted detection whose recording time is already older than its
expiry (a late-processed video) opens or keeps the hazard as **history only** — it is
resolved `expiry` in the same transaction and never alerted.

### 4.6 Recurring spots
When a hazard opens with `recurring_enabled`, the hazards of the same type **resolved** in
the last `recurring_window_days` within the radius are counted: ≥ `recurring_min_occurrences`
→ `recurring_spot = true`, `recurrence_count` = that number.

### 4.7 Resolution
`resolve(hazard, source)` resolves every accepted member exactly like the manual resolve
(`validated|corrected → resolved`, history row, `detection.resolve` audit), sets
`resolution_source`, `resolved_at`, `resolved_by` (`system` for automatic sources) and writes
a `resolved` event plus a `hazard.resolve` system audit entry for automatic sources.
`POST /hazards/{id}/reopen` reopens manually (`last_seen_at` = now).

### 4.8 Drivers / alerts
`POST /detections/nearby` and `/detections/along-route` (the mobile app's hazard sources)
return only accepted detections (resolved members are excluded as before) whose hazard is
active and not expired and whose type/subtype policy has `show_to_drivers`; each item carries
`alert_priority`.

## 5. Data

`hazards` gains `last_seen_at`, `expires_at`, `absence_count`, `resolution_source`
(`manual|video_absence|votes|expiry`), `recurring_spot`, `recurrence_count`.
`hazard_events` (`id, hazard_id, kind, source, job_id, detection_id, user_id, details,
recorded_at, created_at`) — kinds `seen`, `not_seen`, `vote_still_there`, `vote_not_there`,
`expired`, `resolved`, `reopened`. `hazard_votes` (`hazard_id, user_id, device_id, vote,
latitude, longitude, distance_m, counted, reason, created_at`). Job event `hazard_lifecycle`
records each job evaluation summary.

Schema: `app/core/db/upgrade.py` (idempotent, at startup; back-fills `last_seen_at` from the
members and `resolution_source='manual'` for already-resolved hazards), mirrored in
`migrations/add_hazard_lifecycle.sql`. `python -m app.cli rebuild-hazards` recreates hazards
without lifecycle history (events and votes are dropped with the hazards).

## 6. API

| Endpoint | Who | |
|---|---|---|
| `POST /hazards/{id}/votes` | mobile app (API key + signed-in user Bearer), or `detections:edit` | body `{vote, latitude, longitude, device_id?}` → **201** `{"message": "Thanks, your vote has been recorded."}`; errors `401 not_authenticated`, `404`, `409 hazard_resolved`, `409 votes_disabled`, `422 too_far`, `429 cooldown` |
| `GET /hazards/{id}/events` | `detections:view` | timeline, newest first, filter `kind`, paging |
| `GET /hazards/recurring-spots` | `detections:view` | filters `type`, `bbox=min_lat,min_lon,max_lat,max_lon`, `window_days`, `min_occurrences` |
| `GET /hazards/`, `GET /hazards/{id}` | `detections:view` | + `last_seen_at`, `expires_at`, `absence_count`, `resolution_source`, `recurring_spot`, `recurrence_count`; filters `resolution_source`, `recurring_spot`; sorts `last_seen_at`, `expires_at` |

## 7. Examples

**Parked car, two passes.** 10:00 a car blocking the lane is detected and accepted → hazard
HZ-000120 opens, `last_seen_at` 10:00, `expires_at` 12:00. A video recorded 10:20 passes
9 m away at 38 km/h, same direction, no vehicle detected → `not_seen`, `absence_count` 1.
A video recorded 10:40, same → `absence_count` 2 → resolved `video_absence`; it disappears
from the app. At 10:45 a new video shows the car again → the hazard is reopened
(`reopen_on_sighting`, within 2 h of the 10:40 evidence).

**Pass that does not count.** Driven at 75 km/h, or westbound when the car was seen
eastbound, or recorded before 10:00, or a car detected (even pending review) → no event,
the counter stays.

**Driver votes.** Driver A (40 m away) votes `still_there` → `last_seen_at` = now, expiry
extended. A votes again 10 min later → 429 `cooldown`. Drivers B and C vote `not_there`
from 30 m → resolved `votes`. Driver D, 2 km away → 422 `too_far` (stored, not counted).

**Expiry.** A roadside car seen at 08:00 and never again → hidden at 14:00, resolved
`expiry` by the next sweep. A broken-down truck: 24 h.

**Late video.** A video recorded yesterday 15:00 is processed today; its blocking car
(2 h expiry) is auto-accepted → the hazard is created already resolved `expiry`
(`seen`, `expired`, `resolved` events) and is never shown or alerted.

**Recurring spot.** Cars were parked and gone at the same corner on 3 days of the last 30 →
the 4th hazard opens with `recurring_spot = true`, `recurrence_count = 3`; the corner is
listed by `GET /hazards/recurring-spots?type=parked_vehicle`.
