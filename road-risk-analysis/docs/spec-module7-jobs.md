# Module 7 — Video/data ingestion & processing jobs (implementation spec)

Baseline: RRAP-07-001 … 07-014. Backend `backend/app` (FastAPI + Celery/Redis worker `app/workers`), web `web`.

## 1. Job record (07-005, 07-006, 07-010)

Add to `jobs` (idempotent `ALTER TABLE … ADD COLUMN IF NOT EXISTS` in the startup upgrade module `app/core/db/upgrade.py`, documented in `backend/migrations/add_module7_jobs.sql`):
- `source_type` enum `upload | google_drive | onedrive | stream` (existing rows → upload); `source_ref` (external file id / stream id), `source_name` (display name), `connection_id` (FK cloud_connections, nullable), `stream_id` (FK stream_sources, nullable), `source_path` (local temp path of the media while retained), `submitted_by` (e-mail or "system").
- `status` enum add `cancelled` (keep pending/processing/done/failed).
- `started_at`, `finished_at` (exists), `cancel_requested` bool, `cancelled_at`, `cancelled_by`.
- `attempts` int default 1, `max_attempts` (from settings at creation), `celery_task_id`.
- Metrics: `avg_confidence` float, `frame_skip` int (sampling actually used), `detections_by_model` JSON (`{"pothole": 3, …}`), `fps`, `duration_s` (video length).
- Computed in API output: `processing_seconds` (finished/now − started_at), `progress` (processed/total_frames).
- New table `job_events`: id, job_id (index), at, event (`created|queued|started|progress_failed|failed|cancel_requested|cancelled|retried|done`), actor, details JSON. Every lifecycle change writes one; user actions also write `audit_logs`.

## 2. Processing behaviour

- **Async + queue (07-007/07-008)** stays Celery. Store `celery_task_id`.
- **Configurable capacity C (07-009/07-014)**: settings section `processing.max_concurrent_jobs` (default 2). Enforced with a Redis slot counter/lock: a task that starts while C jobs are already `processing` re-queues itself (`self.retry(countdown=10)`, not counted as an attempt) and the job stays `pending`. Worker `--concurrency` is the hard upper bound — make it an env var `WORKER_CONCURRENCY` (default 4) in docker-compose.yml. `GET /jobs/capacity` → `{max_concurrent_jobs, running, pending, worker_concurrency}`.
- **Frame sampling (07-013)**: settings `processing.frame_skip_default` (5), `processing.frame_skip_by_source` (`{"upload":5,"google_drive":5,"onedrive":5,"stream":10}`), `processing.frame_skip_by_model` (optional per model; the processor runs each model only on frames where `frame_index % model_skip == 0`). Job submit may pass `frame_skip` (1..60) overriding the default (jobs:add). Value used is stored on the job.
- **Failure handling (07-010)**: any exception → job `failed` with readable `error` (and `job_events`) once retries are exhausted; transient retries keep `pending` and log an event. A beat task every 5 min marks jobs stuck in `processing` longer than `STALE_JOB_MINUTES` as failed (existing watchdog only runs at startup → schedule it).
- **Media retention**: keep the source file at `source_path` until the job is `done` (then delete). Failed/cancelled jobs keep it for `processing.retain_failed_hours` (default 72, beat cleanup task) so they can be retried. Stream/cloud segments: same rule (07-011: job remains monitorable after media deletion).
- **Cancel (07-012)**: `POST /jobs/{id}/cancel` (jobs:edit). pending → revoke Celery task, status `cancelled`. processing → set `cancel_requested`; processor checks it every ~25 frames (cheap DB/Redis check), stops, keeps detections created so far, status `cancelled`. done/failed/cancelled → 409 `invalid_transition`. Audited.
- **Retry (07-012)**: `POST /jobs/{id}/retry` (jobs:edit) for `failed|cancelled` while `attempts < max_attempts` (settings `processing.max_attempts`, default 3) and source media still available (or re-downloadable for cloud sources) → reset counters, `attempts+1`, status pending, new Celery task. Else 409 with code `retry_limit` / `source_unavailable`. Audited. Detections from the previous attempt of the same job are deleted before re-processing (keep validated ones? → NO: block retry with 409 `has_reviewed_detections` if any detection of the job was already reviewed).
- Existing `DELETE /jobs/{id}` stays (jobs:delete) but refuses while processing (409) and is audited.

## 3. Cloud storage sources (07-002, 07-004)

Settings section `cloud` (secrets encrypted like other sections): `google_client_id`, `google_client_secret`, `microsoft_client_id`, `microsoft_client_secret`, `microsoft_tenant` (default `common`), `sync_interval_minutes` (default 15). Redirect URI shown to the admin: `{WEB_BASE_URL}/settings/cloud/callback`.

Table `cloud_connections`: id, provider (`google_drive|onedrive`), name, account_email, folder_id, folder_name, status (`pending_auth|active|error|disabled`), refresh_token_enc, created_by, created_at, last_sync_at, last_sync_status (`ok|error`), last_sync_error, last_sync_files, files_imported_total, enabled bool.

API (module `jobs`; view/add/edit/delete):
- `GET /cloud-connections/`; `POST /cloud-connections/` `{provider, name, folder_id?}` → creates `pending_auth` + returns `{connection, authorize_url}` (OAuth state = signed connection id + nonce, stored in Redis 15 min).
- `POST /cloud-connections/oauth/complete` `{code, state}` → exchanges the code (httpx, no SDK), stores encrypted refresh token, fetches account e-mail, status active.
- `PATCH /cloud-connections/{id}` (name, folder_id, enabled), `DELETE`, `POST /cloud-connections/{id}/sync` → enqueues a sync now.
- `GET /cloud-connections/{id}/folders?parent=` → list folders to choose `folder_id`.
- Sync task (beat every `sync_interval_minutes` + manual): list video files in the folder (Google Drive v3 `files.list` q=`'<folder>' in parents and mimeType contains 'video/' and trashed=false`; OneDrive Graph `/me/drive/items/{folder}/children` filtering `file.mimeType` video/*), skip files already imported (unique `(connection_id, source_ref)` on jobs), download to TEMP_DIR, create job `source_type=google_drive|onedrive`, enqueue. Update last_sync_* fields and job_events/audit.
- OAuth endpoints: Google auth `https://accounts.google.com/o/oauth2/v2/auth` (scope `https://www.googleapis.com/auth/drive.readonly openid email`, `access_type=offline`, `prompt=consent`), token `https://oauth2.googleapis.com/token`. Microsoft auth `https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize` (scope `offline_access Files.Read User.Read`), token `…/oauth2/v2.0/token`.

## 4. Stream sources (07-003, 07-011)

Table `stream_sources`: id, name, url (rtsp/rtmp/http/hls), camera_type, status (`active|paused|error`), segment_seconds (default 60), frame_skip (nullable → processing default for streams), enabled_models (list), last_segment_at, last_error, segments_processed, created_by, created_at.
API (module `streams`): CRUD `/streams/`, `POST /streams/{id}/start|pause`, `POST /streams/{id}/test` (open with OpenCV, read one frame, return ok/error + resolution).
Capture: beat task every minute → for each active stream with no capture running (Redis lock), Celery task `capture_stream_segment` reads `segment_seconds` of video with `cv2.VideoCapture(url)` into an mp4 in TEMP_DIR, creates a job `source_type=stream, stream_id, source_name="<stream> HH:MM"`, enqueues processing. Errors → stream `error` with last_error. Jobs from streams appear in the Jobs list like any other (filter by source).

## 5. Jobs API summary

- `POST /jobs/` (jobs:add) multipart `file`, query `models`, `frame_skip?` → job (source upload, submitted_by=user).
- `GET /jobs/` filters `status` (comma list), `source_type`, `search` (filename/source_name/id), `date_from`, `date_to`, `sort` (`-created_at` default), `skip`, `limit` ≤200 → `{total, items}` (**breaking change from list → object; update web callers**).
- `GET /jobs/{id}` → job + `events` (job_events newest first).
- `POST /jobs/{id}/cancel`, `POST /jobs/{id}/retry`, `DELETE /jobs/{id}`, `GET /jobs/capacity`.
- `GET /jobs/summary` → counts per status for the last 24 h / total.

## 6. Settings section `processing`

```
max_concurrent_jobs: int = 2 (1..32)
max_attempts: int = 3 (1..10)
frame_skip_default: int = 5 (1..60)
frame_skip_by_source: dict[str,int] = {"upload":5,"google_drive":5,"onedrive":5,"stream":10}
frame_skip_by_model: dict[str,int] = {}
retain_failed_hours: int = 72
```

## 7. Web screens

- **Upload** (`/upload`): multi-file drag&drop (exists) + model selection + optional frame sampling override; shows created jobs with links.
- **Jobs** (`/jobs`): capacity banner (`running / C · pending N`), status filter chips with counts, source filter, search, server pagination; columns: id/name, source (icon + name), status badge (pending/processing/done/failed/cancelled), progress bar with frames processed/total, detections, avg confidence, started, duration, attempts; row actions Cancel / Retry / Delete per permission; detail drawer with metrics, per-model detection counts, error, events timeline, link to its detections (`/detections?job_id=`). Auto-refresh every 5 s while anything is pending/processing.
- **Stream Sources** (`/streams`): replace the mock with the real API: list, add/edit dialog (name, URL, camera type, segment length, models, frame sampling), test connection, start/pause, delete, status + last segment/error, recent jobs of that stream.
- **Settings → Platform**: cards *Processing* (§6) and *Cloud storage* (OAuth client credentials + redirect URI to copy) — and a **Cloud connections** card/list (connect Google Drive / OneDrive → redirect to provider; page `/settings/cloud/callback` completes the flow; choose folder; enable/disable; sync now; last sync status/error/files).
- All strings EN/FR/ES in a NEW file `lib/i18n/extra/module7.ts` registered in `lib/i18n/index.tsx`.
