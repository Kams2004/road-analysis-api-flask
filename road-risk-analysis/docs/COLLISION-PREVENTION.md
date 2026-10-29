# Collision prevention (Module 10)

Connected vehicles (the VIASafe mobile app, the demo simulator) send a position fix every few
seconds. VIASafe predicts each vehicle's path along the road network for the next 15 seconds,
compares it with the paths of the vehicles around it, and warns both drivers when they are
about to collide. Every alert says why.

The engine has two layers:

1. **Physics decides whether there is a conflict.** It uses distance and time to the closest
   approach, how the two paths relate on the road, and the braking needed behind a stopped
   vehicle. It is exact and explainable, and it never looks at history.
2. **Context only modulates.** A *Context Risk Index* C ∈ [0, 1] at the conflict point
   (accident history of the place, speeding, potholes, reported accidents or roadwork,
   night, GPS quality — nothing about who drives: 3.4) makes alerts fire **earlier** (thresholds widen by up to
   +50 %) and, when C ≥ 0.7, **one level louder**. Context never creates an alert by
   itself and never lowers one.

Code: `backend/app/modules/m10_collision/`. Settings: *Platform settings → section
`collision`* (the API is `PUT /platform-settings/collision`). An admin's change reaches every
process within about 10 s.

---

## 1. Pipeline and latency

```
phone ──POST /vehicles/{id}/ping──▶ API (204 at once)
                                     │ background task, same process
                                     ▼
  vehicle_service      live state in Redis (vehicles:live:geo + vehicle:live:{id}, TTL 45 s);
                       speed / heading smoothed over the last 3 pings (vehicle:motion:{id})
  road_state_service   OSRM map matching → RoadAwareVehicleState          (≈ 5–50 ms)
  trajectory_service   kinematic prediction along the road, horizons 1…15 s
  collision_service    candidates (GEOSEARCH 700 m → approaching ones, 30 nearest)
                       → per pair: detector (layer 1) → context (layer 2, Redis only)
  alert_service        alert lifecycle (NEW / ESCALATED / DE_ESCALATED / RESOLVED)
                       → PUBLISH collision:alerts:{vehicle_id} (both vehicles)
                                     │ Redis pub/sub (any API process)
                                     ▼
  WS /alerts/ws        forwarded to the subscribed socket at once ──▶ phone
  GET /alerts/{id}     polling fallback (same records)
```

**Latency budget** (one ping of vehicle A, B already known):

| Step | Typical |
|------|---------|
| ping upload (mobile network) | 100–500 ms |
| map matching (OSRM, local) | 5–50 ms |
| prediction, detection, context, alert (all in-process + Redis) | 2–10 ms per pair |
| pub/sub → WebSocket → phone | < 100 ms + downlink |

So a transition reaches the phone about **0.2–0.7 s after the ping that causes it**. With
polling every 4 s it took 2 s more on average. `detection_latency_ms` on each alert is the time
from the pair's newest telemetry fix to the evaluation. It is computed from the device
timestamp, so a phone with a wrong clock skews it. `GET /collision/metrics` gives the median
and 90th percentile. The ping interval (3 s) sets the floor: a manoeuvre is seen at the
earliest at the next fix.

---

## 2. Layer 1 — physics

`collision/detector.py` (pure). A pair is rated only when **all** gates pass:

1. **Closing**: the closest point of approach (CPA) lies in the future, within
   `max_horizon_s`, and the gap shrinks at least `min_closing_speed_mps`.
2. **Road relation**, which is geometric (see 2.2). Unrelated roads that are both reliably
   matched → no alert (MONITOR only if the CPA is within `unrelated_monitor_distance_m`).
3. **Not passing**: the vehicles are not opposite-direction traffic in separate lanes. That
   means lateral separation from GPS vs the centreline ≥ `lateral_margin_m`, or the two
   carriageways of a divided road.
4. **Stopped vehicles** (see 2.1).

The level is then the most severe one whose **distance at CPA and time to CPA** both hold. The
defaults are:

| Level | Distance at CPA | Time to CPA |
|-------|-----------------|-------------|
| CRITICAL | ≤ 15 m | ≤ 4 s |
| HIGH_WARNING | ≤ 30 m | ≤ 8 s |
| WARNING | ≤ 60 m | ≤ 15 s |
| MONITOR | ≤ 120 m | — |

These limits are scaled by `scenario_multipliers` (head-on / crossing / same direction).

### 2.1 Reliability rules (layer 1)

| Rule | Why | Settings |
|------|-----|----------|
| **Stopped vehicle in the path** (one vehicle ≤ `stationary_speed_mps` — or held still by the display layer: no trusted motion, inside its GPS uncertainty — its own heading ignored as GPS noise; encounter `stationary`). The level comes from the deceleration the mover needs to stop `queue_safety_gap_m` short of it once the alert is delivered and the driver reacts (`queue_reaction_time_s`, at full speed): **a = v² / (2·(gap − safety gap − v·t_react))**. A stopped vehicle beside the path (another lane, the shoulder) gives no alert. It is capped by the distance/time level. | A queue at a red light was CRITICAL: the CPA with a stopped car is always ~0 m. A parked phone's GPS heading made a gentle approach "head-on". Without the reaction time, 54 km/h towards a stopped car warned only ~45 m before it. | `queue_comfort_decel_mps2` 3.0 (below: no alert; above: WARNING), `queue_high_decel_mps2` 4.5 (HIGH_WARNING), `queue_critical_decel_mps2` 6.0 (CRITICAL), `queue_safety_gap_m` 2, `queue_reaction_time_s` 1.5 |
| **Two stopped vehicles** never conflict. | Parked or queued cars with jittering GPS. | `stationary_speed_mps` 1.0 |
| **Same road is geometric.** It needs predicted paths within `road_link_margin_m`, headings within `heading_tolerance_deg` of 0° or 180°, **and** a supporting signal: a shared matched segment, or the same road name (then paths may be up to `same_road_name_margin_m` apart). A name alone no longer counts. | Two streets with one name, or service roads. | `road_link_margin_m` 8, `heading_tolerance_deg` 30, `same_road_name_margin_m` 15 |
| **Divided carriageway**: opposite headings on connected paths ≥ `divided_min_separation_m` apart → passing, no alert. | Dual carriageways (N3 …). | `divided_min_separation_m` 5 |
| **Missing GPS accuracy** counts as `assumed_accuracy_m` instead of "untrustworthy". | A ping without `accuracy_m` disabled lane separation, so all oncoming traffic became CRITICAL. | `assumed_accuracy_m` 10 |
| **Speed / heading smoothing** over the last 3 pings. Consistent braking or turning → newest value; jitter → median (speed) or weighted circular mean (heading); a stopped phone keeps the last heading it had while moving. The raw values stay in `raw_speed_mps` / `raw_heading`. | One bad fix was fed straight into the prediction. | — |
| **Each pair once per pair of fixes.** The result is cached under `collision:pair:{a}:{b}` with both telemetry timestamps. The other vehicle's pipeline reuses it until either vehicle sends a newer fix. A 2 s NX lock (`collision:pair_lock:…`) makes a concurrent pipeline wait up to 0.3 s for the result. | Each pair used to be computed twice. | `pair_cache_ttl_s` 5 (0 = off) |
| **Candidate cap**: of the GEOSEARCH hits (700 m), only vehicles approaching (closing speed > 0 from the live speed/heading, or unknown) or within the widest MONITOR reach (120 m × multiplier × 1.5), and only the 30 nearest. Non-approaching vehicles beyond that are *cleared*: their alert resolves at once. | Bounded work in dense traffic. | `candidate_radius_m` 700, `candidate_cap` 30 |

The simulator now sends `accuracy_m: 5`.

---

## 3. Layer 2 — Context Risk Index

`collision/context.py` (pure) and `context_service.py` (Redis). The settings are nested in
`collision.context`.

### 3.1 Factors (each 0 … 1)

| Factor | Value | Reason codes | Default weight |
|--------|-------|--------------|----------------|
| `history` | Σ type weight × severity multiplier × recency × n / `history_saturation` (20). This is the place's *current risk* (Historical analysis), with **HARD_BRAKING and HARSH_CORNERING ×2**: `history_type_weights` ACCIDENT 5, HARD_BRAKING 3, HARSH_CORNERING 3, HARD_ACCELERATION 1.2, SPEEDING 1. It covers the last `history_window_days` (365), with a recency half-life of `history_half_life_days` (90) and the severity multipliers of `driver_behaviour` (Exception patterns settings). | `HISTORY_HARSH_BRAKING`, `HISTORY_HARSH_CORNERING`, `HISTORY_ACCIDENTS`, `HISTORY_EVENTS` (dominant type) | 0.35 |
| `habitual_speeding` | Recency- and severity-weighted SPEEDING events of the place / `speeding_saturation` (10) | `HABITUAL_SPEEDING` | 0.15 |
| `vehicle_speeding` | Uses the **local limit**, which is the median `speed_limit_mps` of the place's SPEEDING events. When the cell has fewer than `speed_limit_min_events` (3), the cell and its 8 neighbours are pooled. A party faster than limit × (1 + `speeding_tolerance_pct` 10 %) scores 0.1 → 1 at +40 % (`speeding_full_excess_pct` 30). | `VEHICLE_SPEEDING` | 0.30 |
| `hazards` | Validated (`active`) hazards of `hazard_types` (pothole, speed_bump) within `hazard_radius_m` (100 m) / `hazard_saturation` (2) | `POTHOLE_NEARBY`, `SPEED_BUMP_NEARBY` | 0.15 |
| `reports` | Visible community reports (active, not expired, not merged) within `report_radius_m` (300 m). The sum of `report_kind_weights`: accident 1.0, travaux / route_fermee / voie_bloquee 0.8, inondation 0.8, embouteillage 0.6, other dangers 0.6 (`report_kinds` maps each report type). | `ACCIDENT_REPORTED`, `ROADWORK_REPORTED`, `FLOODING_REPORTED`, `CONGESTION_REPORTED`, `HAZARD_REPORTED` | 0.30 |
| `night` | Local time (`driver_behaviour.timezone` — Exception patterns settings —, Africa/Douala) from `night_start_hour` (19) to `night_end_hour` (6) | `NIGHT` | 0.10 |
| `poor_gps` | A party *reports* accuracy > `poor_gps_accuracy_m` (20 m); an assumed accuracy never counts | `POOR_GPS` | 0.10 |
| `stale_data` | A party's fix is older than `collision.fresh_after_s` (4 s) | `STALE_DATA` | 0.10 |

**C = min(1, Σ weight × factor)**, computed at the predicted conflict point (fallback: the
vehicle's position). Reasons are listed in order of contribution, largest first. Reasons below
`min_reason_contribution` (0.02) are omitted.

Some examples:
- A harsh-braking hot spot alone (history 1.0) gives C = 0.35. Thresholds widen by 17.5 %, with
  no boost.
- That hot spot plus a vehicle 40 % over the local limit gives 0.65. Add night (0.75) → the level
  is boosted.

### 3.2 Effect on the level

```
scale = 1 + max_widening × C                       (max_widening 0.5 → ×1 … ×1.5)
widened = level with every distance / TTC threshold × scale
final = min(CRITICAL, max(physical, widened) + (1 if C ≥ boost_threshold else 0))
        — unless physical = SAFE: then final = SAFE
```

- `max_boost_steps` is 0 or 1. `enabled: false` turns layer 2 off entirely.
- A conflict rated by the stopped-vehicle rule is not widened, because its scale is a
  deceleration, not a distance. It can still be boosted.
- With no context data (C = 0) the results are exactly those of layer 1.

### 3.3 Precomputation (no database on the hot path)

Beat task `refresh_collision_context` (`app/workers/collision_tasks.py`, queue `processing`,
checked every minute). It rebuilds when the last build is older than `refresh_interval_s`
(300 s), **or** when `events:data_version` changed, i.e. after an event import or a
summary rebuild, so new events are reflected within a minute. A Redis lock prevents two
builds running at once.

- Fleet exceptions are read one month at a time, grouped per ~110 m square × day × type ×
  severity. The daily summaries (`event_rollup_cell`) are used when they are built, the raw
  events otherwise. Each square goes to the 150 m cell (`HISTORICAL_GRID_CELL_SIZE_M`) of its
  mean position.
- Local limits come from one grouped query (square × limit km/h). Hazards and reports are each
  copied into every cell their radius can reach, with their position, so the exact distance is
  checked at lookup.
- Only cells with data are written, at most `max_cells` (200 000), keeping the riskiest.

| Redis key | Content | TTL |
|-----------|---------|-----|
| `collision:ctx:cell:{150m:lat_idx:lon_idx}` | `PlaceCell` JSON: `history`, `history_counts`, `history_dominant`, `speeding`, `local_limit_mps`, `limit_samples`, `hazards [[type,lat,lon]]`, `reports [[type,kind,lat,lon]]`, `score` (place part at the cell centre), `reasons` | max(900 s, 2.5 × refresh) |
| `collision:ctx:meta` | `{built_at, data_version, cells, tz, rollups, duration_ms}` | same |
| `collision:ctx:lock` | build in progress | 600 s |

**Hot path, per conflicting pair:** one `GET` of the cell and the meta (cached in-process for
60 s). If Redis fails, the place context is skipped and the physical level is kept.

### 3.4 Anonymity

Nothing in the context identifies a driver or a vehicle. A party is its current trip only
(speed against the local limit, GPS accuracy, data age); the place factors come from anonymous
fleet exceptions (`app/modules/m08_historical/anonymity.py`). The former `driver` factor
(`RISKY_DRIVER`: a phone's plate linked to a fleet behaviour score) was removed with the plate
field of the app: `POST /vehicles/register` ignores the `plate` / `driver_external_id` sent by
older app versions, `vehicles.plate` / `vehicles.driver_external_id` were erased and dropped
(`app/core/db/upgrade.py`, `migrations/anonymise_events.sql`), and the API start-up removes the
plate / driver id left in the live vehicle hashes and the `collision:ctx:drivers` hash. A stored
`context.weights.driver` is ignored.

---

## 4. Alert output

`ConflictAlert` (REST and WebSocket) keeps every earlier field and adds:

| Field | Meaning |
|-------|---------|
| `physical_level` | Level from layer 1 alone |
| `level` | Final level (≥ `physical_level`; `SAFE` whenever `physical_level` is `SAFE`) |
| `context_index` | C, 0–1 |
| `context_level_boost` | 0 or 1 |
| `context_reasons` | `[{code, label, factor, value, contribution, params}]`, largest contribution first |
| `detection_latency_ms` | Newest fix of the pair → evaluation |

`reason` gains "— risky context (…)" when the context mattered.

Example reason:

```json
{"code": "HISTORY_HARSH_BRAKING", "label": "Frequent harsh braking at this place (18 in 12 months)",
 "factor": "history", "value": 1.0, "contribution": 0.35,
 "params": {"events": 25, "dominant_type": "HARD_BRAKING", "dominant_events": 18, "months": 12,
            "by_type": {"HARD_BRAKING": 18, "ACCIDENT": 2}}}
```

`params` by code:

| Code | `params` |
|------|----------|
| `HISTORY_*` | `events`, `dominant_type`, `dominant_events`, `months`, `by_type` |
| `HABITUAL_SPEEDING` | `events`, `local_limit_kmh` |
| `VEHICLE_SPEEDING` | `vehicle_id`, `speed_kmh`, `local_limit_kmh`, `excess_pct` |
| `POTHOLE_NEARBY` / `SPEED_BUMP_NEARBY` / `*_REPORTED` | `count`, `nearest_m`, `radius_m` |
| `NIGHT` | `local_time` |
| `POOR_GPS` | `vehicle_id`, `accuracy_m` |
| `STALE_DATA` | `vehicle_id`, `age_s` |

The app should translate by `code`; `label` is an English default.

---

## 5. Endpoints

| Endpoint | Who | What |
|----------|-----|------|
| `POST /vehicles/register` | API key | Anonymous: no plate / driver (sent by older apps: ignored) |
| `POST /vehicles/{id}/ping` | API key | Unchanged; send `accuracy_m` when known |
| `GET /alerts/{vehicle_id}` | API key | Current alerts (polling fallback) |
| `WS /alerts/ws` | API key, or user with `collisions:view` | Instant delivery (5.1) |
| `POST /alerts/{conflict_id}/feedback` | API key, or user with `collisions:add` | `{vehicle_id, verdict: "false_alert" \| "useful", comment?}` |
| `GET /collision/metrics?period=1h\|6h\|24h\|7d` | user with `collisions:view` | Alerts per hour and level, context boosts, latency, feedback |

### 5.1 WebSocket `/alerts/ws`

Connect to `wss://api-roadrisk.camtrack.net/alerts/ws?vehicle_id={id}`. Caddy upgrades
WebSockets by default. Send the same headers as REST: `X-API-Key: <key>`, plus
`Authorization: Bearer <token>` optionally. React Native passes them as
`new WebSocket(url, null, {headers: {...}})`. Keys are never accepted in the URL, because URLs
end up in proxy logs.

Client → server:

```json
{"vehicle_id": "5d0c3b1e-…"}            // subscribe, or switch vehicle (instead of ?vehicle_id=)
{"type": "ping"}                        // optional; answered by {"type": "pong", "server_time": "…"}
```

Server → client (JSON text frames):

```json
{"type": "snapshot", "vehicle_id": "A", "server_time": "2026-10-29T10:00:00+00:00",
 "alerts": [ConflictAlert, …]}            // on every (re)subscription, = GET /alerts/{vehicle_id}
{"type": "alert", "vehicle_id": "A", "other_vehicle_id": "B", "alert": ConflictAlert}
                                         // every transition: NEW, ESCALATED, DE_ESCALATED, RESOLVED
{"type": "ping", "server_time": "…"}     // after 20 s without any other message
{"type": "error", "code": "invalid_api_key" | "not_authenticated" |
         "session_expired" | "forbidden" | "vehicle_id_required", "message": "…"}   // then close
```

Close codes:
- 4401: not authenticated
- 4403: forbidden
- 4400: no `vehicle_id` within 10 s

Client advice:
- Reconnect with backoff (1 s → 30 s).
- Treat each `snapshot` as the full truth and replace the local list.
- Apply each `alert` by `conflict_id` (`RESOLVED` → remove).
- Keep `GET /alerts/{vehicle_id}` polling only while the socket is down.
- No `ping` for 45 s → reconnect.

The socket is multi-process safe: whichever process evaluated a transition publishes it on
`collision:alerts:{vehicle_id}`, and every socket subscribes to its vehicle's channel.

### 5.2 Feedback and metrics

Feedback is stored in `collision_alert_feedback`, one row per conflict × vehicle (answering
again replaces the verdict). The alert's `level`, `physical_level`, `context_index` and boost
are copied into the row, because the alert itself expires from Redis.

Metrics:
- Each raised alert (NEW or ESCALATED) increments `collision:metrics:h:{YYYYMMDDHH}`:
  `raised:{LEVEL}`, `boosted` (one-step boost), and `context_raised` (final level above the
  physical level).
- Latencies are kept in `collision:metrics:lat:{hour}` (≤ 1000 per hour).
- Both are kept 8 days.
- The false-alert rate is false_alert / all answers in the period.

---

## 6. Calibration

```
python -m app.cli collision-calibration [--holdout-days 90] [--min-events 5] [--json]
```

This is an out-of-sample check that the **place index** (history + habitual speeding) points
at the right places:

1. Build the index from the events **before** the last `--holdout-days`.
2. Count harsh-braking and accident events **during** the holdout per cell.
3. Compare the index of "hot" cells (≥ `--min-events`) with the others.

The report gives both mean indexes, their ratio, and the AUC: the probability that a hot cell
ranks above a cold one, where 0.5 means no signal. The verdict is `ok` when AUC ≥ 0.6 and hot >
other. `--holdout-days 0` runs an in-sample check, which is optimistic by construction. Run it
after changing `history_*` settings. The pure part is `context.calibration_report`, tested on
synthetic data.

---

## 7. Settings reference (`collision`)

New keys (all validated; the earlier keys are unchanged):

| Key | Default | Range |
|-----|---------|-------|
| `assumed_accuracy_m` | 10 | 0–100 |
| `stationary_speed_mps` | 1.0 | 0–5 |
| `queue_safety_gap_m` | 2 | 0–20 |
| `queue_comfort_decel_mps2` / `queue_high_decel_mps2` / `queue_critical_decel_mps2` | 3 / 4.5 / 6 | increasing, ≤ 15 |
| `queue_reaction_time_s` | 1.5 | 0 – 5 |
| `heading_tolerance_deg` | 30 | 5–60 |
| `same_road_name_margin_m` | 15 | 0–100 |
| `divided_min_separation_m` | 5 | 1–100 |
| `candidate_cap` | 30 | 1–500 |
| `pair_cache_ttl_s` | 5 | 0–60 |
| `context.enabled` | true | |
| `context.max_widening` | 0.5 | 0–1 |
| `context.boost_threshold` | 0.7 | 0–1 |
| `context.max_boost_steps` | 1 | 0–1 |
| `context.weights.*` | see 3.1 | 0–1 each |
| `context.history_window_days` / `history_half_life_days` / `history_saturation` | 365 / 90 / 20 | |
| `context.history_type_weights` | see 3.1 | 0–100 |
| `context.speeding_saturation` / `speed_limit_min_events` / `speeding_tolerance_pct` / `speeding_full_excess_pct` | 10 / 3 / 10 / 30 | |
| `context.hazard_types` / `hazard_radius_m` / `hazard_saturation` | pothole, speed_bump / 100 / 2 | |
| `context.report_radius_m` / `report_kinds` / `report_kind_weights` | 300 / see 3.1 | |
| `context.night_start_hour` / `night_end_hour` | 19 / 6 | |
| `context.poor_gps_accuracy_m` | 20 | |
| `context.refresh_interval_s` / `max_cells` | 300 / 200 000 | |

Saving a partial `context` changes only the keys it names; the nested dictionaries are merged
key by key.

Migration: `backend/migrations/add_collision_context.sql`, applied automatically at start-up
by `app/core/db/upgrade.py`; the `collision_alert_feedback` table comes from `create_all`. (The
`vehicles.plate` / `vehicles.driver_external_id` columns it once added are erased and dropped by
`migrations/anonymise_events.sql`.)

---

## 8. Limits

- **No lanes, no elevation.** Lane separation is inferred from GPS vs the road centreline. It
  can only be resolved with good accuracy (≤ `lateral_max_accuracy_m`). Over- and underpasses
  rely on the "unrelated roads" gate.
- **Divided roads** are recognised only when OSM maps the two carriageways as separate ways at
  least 5 m apart.
- **Stopped-vehicle rule**: a stopped car in the *adjacent* lane is rated like one in your lane
  (still by deceleration, so usually silent).
- **Context is correlational.** Fleet exceptions come from a few fleets' trackers and say where
  *those* drivers brake hard. They do not give an exposure-normalised crash rate. Validate
  with the calibration command before increasing the weights.
- **The local limit** is inferred from SPEEDING events only. Places where nobody speeds have
  none, so no `VEHICLE_SPEEDING` there.
- **Precomputation runs on the `processing` queue**, shared with video jobs. A long video
  backlog delays the 5-minute refresh. The previous map stays valid for 15 minutes and then
  simply expires (C falls back to live conditions only).
- **Detection latency** relies on device clocks.
- **Metrics** are hourly and kept 8 days; there is no long-term alert archive.
- **The web console** does not show the new `collision.context` keys yet; they can be changed
  through `PUT /platform-settings/collision`.
