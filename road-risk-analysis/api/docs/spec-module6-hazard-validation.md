# Module 6 — Hazard management, clustering, validation & evidence (implementation spec)

Baseline: RRAP-06-001 … 06-020 (+ 05-003/05-009/05-010 detection traceability).
Backend = FastAPI app in `api/app`. Web = `api/road-risk-analysis-interface`.

## 1. Detection state machine (06-005, 06-008, 06-012, 06-018)

`Detection.review_status` values (keep existing string values, add new ones):

| value | label | meaning |
|---|---|---|
| `location_pending` | Location review | detection has no confirmed location (OCR missing/invalid or only an approximate/interpolated proposal) |
| `pending` | Pending review | confirmed location, waiting for a human decision |
| `validated` | Accepted | accepted as predicted |
| `corrected` | Corrected | accepted after the validator changed the class/subtype OR the location was manually corrected |
| `rejected` | Rejected | not a hazard (human or automatic rule) |
| `resolved` | Resolved | accepted hazard later resolved (cluster/hazard resolution) |

Allowed transitions (anything else → HTTP 409 `{"code":"invalid_transition"}` and an audit entry with outcome=failure):

- `location_pending → pending` — location confirmed (map pin / trajectory point / OCR text / accept proposed position)
- `location_pending → rejected`
- `pending → validated | corrected | rejected`
- `validated | corrected → resolved` (hazard resolution)
- `validated | corrected | rejected | resolved → pending` (or `location_pending` if location not confirmed) — ONLY through **reopen** (`POST /detections/{id}/reopen`, mandatory `reason`), permission `validations:delete` (= "reverse a finalized decision"). The old `PATCH /detections/{id}/review/undo` must be removed (or made an alias of reopen with the same rules).

Every transition writes a `detection_reviews` history row AND an `audit_logs` row (`action="detection.<transition>"`).

New table `detection_reviews`: id, detection_id (FK, index), from_status, to_status, action (`accept|correct|reject|reopen|location_fix|auto_reject|auto_accept|resolve|create`), actor_type (user|system), actor (e-mail or "system"), note, details JSON (e.g. previous/new class, previous/new lat/lon, method, reason code), created_at.

## 2. Detection fields (add)

- Location: `location_status` enum `confirmed | approximate | missing`; `location_source` enum `ocr | interpolated | map_pin | trajectory | ocr_text | none`; `country` (str), `city` (str) from structured reverse geocoding.
- Traceability (05-003/05-010): `bbox_x1, bbox_y1, bbox_x2, bbox_y2` (float, pixels), `video_time_s` (float, seconds from video start), `model_name` (str, e.g. `pothole`), `model_version` (str: weights file name + first 12 chars of its SHA-256, computed once at model load).
- Class correction: `predicted_type`, `predicted_subtype` (original model output, set at creation); `type`/`subtype` hold the current (possibly corrected) class.
- Rejection (06-017): `rejection_code` (str, matches `rejection_reasons.code`), `rejection_source` (`human|auto`).
- `hazard_id` (FK hazards.id, nullable, index).
- Startup migration: the project uses `create_all` + hand-written SQL in `api/migrations/`. New COLUMNS on existing tables are NOT added by create_all → add an idempotent startup step (`ALTER TABLE … ADD COLUMN IF NOT EXISTS …`) run from `init_db` (new module `app/db/upgrade.py`), and also document it in `api/migrations/add_module6_validation.sql`. Back-fill: rows with latitude NULL → `location_status='missing'`, `review_status` pending→`location_pending`; rows with `gps_interpolated=true` → `approximate` (and pending→location_pending); others → `confirmed`, `location_source='ocr'`. `predicted_type/subtype` ← type/subtype when NULL.

## 3. Creation rules in the video processor (06-007, 06-012, 06-013, 06-020)

When the processor stores a detection:
- `location_status`: confirmed if OCR fix valid; `approximate` if `gps_interpolated`; `missing` if no lat/lon. Non-confirmed → `review_status=location_pending`.
- Confidence routing from settings section `validation` (see §6): `confidence < auto_reject_below` → `rejected`, `rejection_source=auto`, `rejection_code="low_confidence"` (seed this reason); if `auto_accept_above` is set and confidence ≥ it AND location confirmed → `validated` with actor "system" (default: disabled = null). Otherwise pending/location_pending. Per-type overrides allowed.
- Context clip: ±`clip_seconds` (default **5**) around the event for every `location_pending` detection (not only when lat is None).
- Store bbox, video_time_s, model_name, model_version.
- Vehicle trajectory (06-014): record every valid OCR GPS fix of the job in new table `job_track_points` (id, job_id index, frame_number, video_time_s, latitude, longitude, speed_kmh, source='ocr'). Insert in batches.

## 4. Hazards = persisted clusters (06-001…06-004, 06-009…06-011, 06-019)

New table `hazards`: `id` (uuid), `code` (human id `HZ-000001`, unique, sequential), `type`, `subtype` (nullable), `status` (`active|resolved`), `latitude`, `longitude` (centroid of member detections), `radius_m` (radius rule applied), `country`, `city`, `location_name`, `detection_count`, `avg_confidence`, `max_confidence`, `first_detected_at`, `last_detected_at`, `resolved_at`, `resolved_by`, `resolution_note`, `created_at`, `updated_at`.

Assignment (service `app/services/hazard_service.py`), run whenever a detection enters `validated`/`corrected` with `location_status=confirmed`:
1. radius = matching `cluster_areas` rule (§5) for the detection position and type, else global `ClusterConfig.radius_m`.
2. candidate = nearest ACTIVE hazard with same `type` within radius; if the type is listed in `validation.distinct_subtype_types` (default `["traffic_sign"]`) the subtype must also match (06-011: two different signs a few metres apart stay separate hazards).
3. attach (update centroid, counts, confidence stats, last_detected_at) or create a new hazard.
When a detection leaves accepted states (reopen) → detach and recompute; delete hazard if empty.
Resolve hazard → its accepted detections become `resolved` (transition + history), hazard keeps resolution info (06-003). Reopen hazard (permission detections:edit) → active again, detections back to validated.
Back-fill CLI: `python -m app.cli rebuild-hazards`; also run automatically at startup when `hazards` is empty and accepted detections exist.

Downstream exposure (06-019): `query_nearby`, `query_along_route`, `/detections/clusters` and anything used by the mobile app must only return detections with status in (`validated`,`corrected`) and `location_status='confirmed'`.

## 5. City/area cluster radius (06-010)

New table `cluster_areas`: id, name, country (nullable), city (nullable), center_lat, center_lon, area_radius_km (area extent), cluster_radius_m, type_radius JSON (optional per-type override, e.g. `{"pothole": 25}`), is_active, created_at, updated_at. A detection matches an area when within `area_radius_km` of its centre (smallest matching area wins); otherwise default radius. CRUD under `/cluster-areas` (module `settings`: view/edit). Audit changes.

## 6. Settings section `validation` (06-007) in `settings_service.SECTIONS`

```
auto_reject_below: float = 0.30        # 0..1
auto_accept_above: float | None = None # disabled by default
type_overrides: dict[str, {auto_reject_below?, auto_accept_above?}] = {}
clip_seconds: int = 5                  # 1..30
distinct_subtype_types: list[str] = ["traffic_sign"]
```
Edited from Settings → Platform (new card), permission settings:edit, audited (already handled by `PUT /platform-settings/{section}`).

## 7. API contract (all under existing auth/permission guards)

Detections (module `detections` unless noted):
- `GET /detections/` — filters: `review_status` (comma list), `type`, `subtype`, `job_id`, `hazard_id`, `country`, `city`, `location_status`, `date_from`, `date_to` (captured_at/created_at), `min_confidence`, `search` (id prefix, location_name, raw_gps_text), `sort` (`created_at|captured_at|confidence|type`, prefix `-` for desc, default `-created_at`), `skip`, `limit` (max 200). Returns `{total, items}`; items include all new fields + `hazard_code`.
- `GET /detections/queue` (validations:view) — the validation queue: statuses `pending,location_pending` by default (param `status`), same filters/sort; each item also includes `cluster`: `{hazard_id, hazard_code, detection_count}` of the nearest active hazard of the same type within radius (what it WOULD join) or null.
- `GET /detections/{id}` — includes `history` (list of detection_reviews, newest first) and `hazard` summary.
- `POST /detections/{id}/accept` (validations:edit) body `{label?, severity_score?, note?}` → `validated`.
- `POST /detections/{id}/correct` (validations:edit) body `{type, subtype?, label?, severity_score?, note?}` → `corrected` (keeps predicted_type).
- `POST /detections/{id}/reject` (validations:edit) body `{reason_code, note?}` (reason_code must exist in rejection_reasons) → `rejected`, `rejection_source=human`.
- `POST /detections/{id}/reopen` (validations:delete) body `{reason}` → pending/location_pending.
- `POST /detections/{id}/location` (validations:edit) body `{method: "map_pin"|"trajectory_point"|"ocr_text"|"accept_proposed", latitude?, longitude?, track_point_id?, raw_gps_text?, note?}` → sets coordinates (validated range), `location_status=confirmed`, `location_source`, structured reverse geocode, history with previous coords; `location_pending → pending`. Allowed on pending too (then later accept counts as `corrected`).
- `GET /detections/{id}/trajectory?window_s=60` (validations:view) → `{detection_time_s, points:[{id, video_time_s, frame_number, latitude, longitude, speed_kmh}]}` from job_track_points within ±window.
- `GET /detections/rejected` (validations:view) — rejected dataset with filters (type, reason_code, rejection_source, reviewer, date range) + `GET /detections/rejected/export?format=csv|json` (datasets:view) streaming download with: id, job_id, type, subtype, predicted_type, confidence, model_name, model_version, bbox, crop_url, image_url, rejection_code, rejection label, rejection_source, reviewed_by, reviewed_at, note, captured_at, latitude, longitude.
- Keep `PATCH /detections/{id}/review` and `PATCH /detections/{id}/location` working as thin compatibility wrappers over the new logic (same rules), so older clients don't break.

Hazards (module `detections`):
- `GET /hazards/` — filters `status` (active|resolved), `type`, `subtype`, `country`, `city`, `date_from`, `date_to` (first/last detected), `search` (code, location_name), `sort` (`last_detected_at|first_detected_at|detection_count|avg_confidence|code`, `-` desc), `skip`, `limit` (≤200) → `{total, items}`.
- `GET /hazards/{id}` — hazard + member detections (id, status, confidence, captured_at, crop_url, image_url).
- `POST /hazards/{id}/resolve` (detections:edit) `{note?}`; `POST /hazards/{id}/reopen` (detections:edit) `{reason}`.
- `GET /hazards/summary` — counts by status/type for the dashboard.

Cluster areas: `GET/POST /cluster-areas/`, `PATCH/DELETE /cluster-areas/{id}` (settings view/edit).

## 8. Web screens

- **Validations** (`/validations`): tabs *Pending review*, *Location review*, *Rejected*; filters (type, confidence range, date, country/city, search) with server pagination; item panel shows evidence (annotated frame, crop, ±5 s clip if any), class, confidence + model/version, bbox drawn on the frame, location (status badge confirmed/approximate/missing + source), cluster info ("joins HZ-000123 · 4 detections" or "new hazard"), history timeline. Actions per permission: Accept, Correct (type/subtype dropdown), Reject (reason select from rejection_reasons — required), Reopen (with reason; on Rejected tab and in detail of finalized items, needs validations:delete). Approximate/missing location → *Location review* panel: Leaflet map (use `useMapTiles`) with draggable pin (pre-placed on the proposed position if any), the job trajectory as a polyline with clickable points (nearest point to event highlighted), buttons "Use this point", "Accept proposed position" (only when approximate), and OCR text entry as a fallback; on save the item moves to Pending.
- **Hazard Track** (`/detections` page, nav label stays "Detections"): tabs *Confirmed hazards* (active), *Resolved*, *All detections*; filters date range, country, city, type, search by ID/location; sortable columns; server pagination; hazard row → detail drawer with members (thumbnails), map mini-preview, resolution info, actions Resolve / Reopen (detections:edit). Rejected-data export button (CSV/JSON) for datasets:view.
- **Settings → Platform**: new cards *Validation rules* (§6) and *Cluster areas* table with add/edit/delete (name, country, city, centre lat/lon — pick on a small map, area radius km, cluster radius m, per-type overrides).
- All strings EN/FR/ES (new keys in a NEW file `lib/i18n/extra/module6.ts`, registered in `lib/i18n/index.tsx` EXTRA list), permissions via `useAuth().can`.
