-- Module 7 — video/data ingestion & processing jobs (docs/spec-module7-jobs.md)
--
-- Applied automatically at API startup by app/core/db/upgrade.py (idempotent);
-- this file mirrors it for review / manual runs. New TABLES
-- (cloud_connections, stream_sources, job_events) are created by SQLAlchemy
-- create_all — their DDL is listed here for reference.

-- ── Job status (run outside a transaction on PostgreSQL < 12) ──────────────
ALTER TYPE jobstatus ADD VALUE IF NOT EXISTS 'cancelled';

-- ── New tables ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cloud_connections (
    id                   VARCHAR PRIMARY KEY,
    provider             VARCHAR NOT NULL,                    -- google_drive | onedrive
    name                 VARCHAR NOT NULL,
    account_email        VARCHAR,
    folder_id            VARCHAR,                             -- NULL = drive root
    folder_name          VARCHAR,
    status               VARCHAR NOT NULL DEFAULT 'pending_auth', -- pending_auth | active | error | disabled
    refresh_token_enc    VARCHAR,                             -- Fernet(SECRET_KEY)
    created_by           VARCHAR,
    created_at           TIMESTAMP NOT NULL DEFAULT now(),
    last_sync_at         TIMESTAMP,
    last_sync_status     VARCHAR,                             -- ok | error
    last_sync_error      VARCHAR,
    last_sync_files      INTEGER NOT NULL DEFAULT 0,
    files_imported_total INTEGER NOT NULL DEFAULT 0,
    enabled              BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS stream_sources (
    id                 VARCHAR PRIMARY KEY,
    name               VARCHAR NOT NULL,
    url                VARCHAR NOT NULL,                      -- rtsp / rtmp / http / hls
    camera_type        VARCHAR,
    status             VARCHAR NOT NULL DEFAULT 'paused',     -- active | paused | error
    segment_seconds    INTEGER NOT NULL DEFAULT 60,
    frame_skip         INTEGER,                               -- NULL = processing.frame_skip_by_source.stream
    enabled_models     JSON,
    last_segment_at    TIMESTAMP,
    last_error         VARCHAR,
    segments_processed INTEGER NOT NULL DEFAULT 0,
    created_by         VARCHAR,
    created_at         TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS job_events (
    id      SERIAL PRIMARY KEY,
    job_id  VARCHAR NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    at      TIMESTAMP NOT NULL DEFAULT now(),
    event   VARCHAR NOT NULL,   -- created|queued|started|progress_failed|failed|cancel_requested|cancelled|retried|done
    actor   VARCHAR,            -- e-mail or "system"
    details JSON
);
CREATE INDEX IF NOT EXISTS ix_job_events_job_id ON job_events (job_id);

-- ── jobs: source, lifecycle, metrics ───────────────────────────────────────
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS source_type VARCHAR NOT NULL DEFAULT 'upload';
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS source_ref VARCHAR;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS source_name VARCHAR;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS connection_id VARCHAR REFERENCES cloud_connections(id) ON DELETE SET NULL;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS stream_id VARCHAR REFERENCES stream_sources(id) ON DELETE SET NULL;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS source_path VARCHAR;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS submitted_by VARCHAR;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS started_at TIMESTAMP;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS cancel_requested BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMP;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS cancelled_by VARCHAR;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 1;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS max_attempts INTEGER NOT NULL DEFAULT 3;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS celery_task_id VARCHAR;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS avg_confidence DOUBLE PRECISION;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS frame_skip INTEGER;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS detections_by_model JSON;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS fps DOUBLE PRECISION;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS duration_s DOUBLE PRECISION;
CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs (status);
CREATE INDEX IF NOT EXISTS ix_jobs_created_at ON jobs (created_at);
CREATE INDEX IF NOT EXISTS ix_jobs_connection_id ON jobs (connection_id);
CREATE INDEX IF NOT EXISTS ix_jobs_stream_id ON jobs (stream_id);
-- A cloud file is imported at most once per connection (sync dedup)
CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_connection_source_ref ON jobs (connection_id, source_ref);
