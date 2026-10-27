-- Cloud sources & imports (Module 7, RRAP-07-002/004)
--
-- Applied automatically at API startup by app/core/db/upgrade.py
-- (CLOUD_IMPORT_COLUMNS, idempotent); this file mirrors it for review /
-- manual runs. The two new TABLES are created by SQLAlchemy create_all —
-- their DDL is listed here for reference.

CREATE TABLE IF NOT EXISTS cloud_import_batches (
    id               VARCHAR PRIMARY KEY,
    connection_id    VARCHAR REFERENCES cloud_connections(id) ON DELETE SET NULL,
    provider         VARCHAR NOT NULL,                 -- google_drive | onedrive
    connection_name  VARCHAR,
    origin           VARCHAR NOT NULL DEFAULT 'manual', -- manual | sync (watched folder)
    models           VARCHAR,                          -- comma list
    frame_skip       INTEGER,
    options          JSON,                             -- {include_subfolders, reimport, folder_ids}
    item_count       INTEGER NOT NULL DEFAULT 0,
    cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
    created_by       VARCHAR,
    created_at       TIMESTAMP NOT NULL DEFAULT now(),
    finished_at      TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_cloud_import_batches_connection_id ON cloud_import_batches (connection_id);
CREATE INDEX IF NOT EXISTS ix_cloud_import_batches_created_at ON cloud_import_batches (created_at);

CREATE TABLE IF NOT EXISTS cloud_import_items (
    id                  VARCHAR PRIMARY KEY,
    batch_id            VARCHAR NOT NULL REFERENCES cloud_import_batches(id) ON DELETE CASCADE,
    connection_id       VARCHAR REFERENCES cloud_connections(id) ON DELETE SET NULL,
    file_id             VARCHAR NOT NULL,              -- provider file id
    version             VARCHAR,                       -- md5 / cTag / modified time (dedupe)
    name                VARCHAR NOT NULL,
    path                VARCHAR,
    mime_type           VARCHAR,
    checksum            VARCHAR,                       -- md5, verified after download
    modified_at         VARCHAR,
    status              VARCHAR NOT NULL DEFAULT 'queued',
        -- queued | downloading | uploading_to_storage | queued_for_processing
        -- | processing | done | failed | skipped_duplicate | cancelled
    bytes_total         BIGINT,
    bytes_done          BIGINT NOT NULL DEFAULT 0,
    speed_bps           DOUBLE PRECISION,
    attempts            INTEGER NOT NULL DEFAULT 0,
    error               VARCHAR,
    dest_path           VARCHAR,
    job_id              VARCHAR REFERENCES jobs(id) ON DELETE SET NULL,
    duplicate_of_job_id VARCHAR,
    celery_task_id      VARCHAR,
    created_at          TIMESTAMP NOT NULL DEFAULT now(),
    started_at          TIMESTAMP,
    finished_at         TIMESTAMP,
    updated_at          TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_cloud_import_items_batch_id ON cloud_import_items (batch_id);
CREATE INDEX IF NOT EXISTS ix_cloud_import_items_status ON cloud_import_items (status);
CREATE INDEX IF NOT EXISTS ix_cloud_import_items_job_id ON cloud_import_items (job_id);

-- ── upgrade of existing databases ──────────────────────────────────────────
-- A cloud file may be imported again when its content changed: the unique
-- (connection_id, source_ref) index of add_module7_jobs.sql becomes a plain one.
DROP INDEX IF EXISTS uq_jobs_connection_source_ref;
CREATE INDEX IF NOT EXISTS ix_jobs_connection_source_ref ON jobs (connection_id, source_ref);

-- Watched folder = automatic import, now an explicit option of the connection.
ALTER TABLE cloud_connections ADD COLUMN IF NOT EXISTS auto_import BOOLEAN;
UPDATE cloud_connections SET auto_import = enabled WHERE auto_import IS NULL;
ALTER TABLE cloud_connections ALTER COLUMN auto_import SET DEFAULT FALSE;
ALTER TABLE cloud_connections ADD COLUMN IF NOT EXISTS auto_import_models JSON;
ALTER TABLE cloud_connections ADD COLUMN IF NOT EXISTS auto_import_frame_skip INTEGER;
-- Folders bookmarked in the browser (e.g. opened from a OneDrive link): [{id, name, breadcrumbs, link}]
ALTER TABLE cloud_connections ADD COLUMN IF NOT EXISTS saved_folders JSON;
CREATE INDEX IF NOT EXISTS ix_cloud_import_items_conn_file ON cloud_import_items (connection_id, file_id);
