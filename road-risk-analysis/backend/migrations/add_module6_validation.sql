-- Module 6 — hazard validation, clustering & evidence (docs/spec-module6-hazard-validation.md)
--
-- Applied automatically at API startup by app/core/db/upgrade.py (idempotent);
-- this file mirrors it for review / manual runs. New TABLES (hazards,
-- cluster_areas, detection_reviews, job_track_points) are created by
-- SQLAlchemy create_all — their DDL is listed here for reference.

-- ── Review statuses (run outside a transaction on PostgreSQL < 12) ─────────
ALTER TYPE reviewstatus ADD VALUE IF NOT EXISTS 'location_pending';
ALTER TYPE reviewstatus ADD VALUE IF NOT EXISTS 'corrected';

-- ── New tables ──────────────────────────────────────────────────────────────
CREATE SEQUENCE IF NOT EXISTS hazards_number_seq;
CREATE TABLE IF NOT EXISTS hazards (
    id                VARCHAR PRIMARY KEY,
    number            INTEGER NOT NULL UNIQUE DEFAULT nextval('hazards_number_seq'),
    code              VARCHAR NOT NULL UNIQUE,           -- HZ-000001
    type              VARCHAR NOT NULL,
    subtype           VARCHAR,
    status            VARCHAR NOT NULL DEFAULT 'active', -- active | resolved
    latitude          DOUBLE PRECISION NOT NULL,
    longitude         DOUBLE PRECISION NOT NULL,
    radius_m          DOUBLE PRECISION NOT NULL,
    country           VARCHAR,
    city              VARCHAR,
    location_name     VARCHAR,
    detection_count   INTEGER NOT NULL DEFAULT 0,
    avg_confidence    DOUBLE PRECISION NOT NULL DEFAULT 0,
    max_confidence    DOUBLE PRECISION NOT NULL DEFAULT 0,
    first_detected_at TIMESTAMP,
    last_detected_at  TIMESTAMP,
    resolved_at       TIMESTAMP,
    resolved_by       VARCHAR,
    resolution_note   VARCHAR,
    created_at        TIMESTAMP NOT NULL,
    updated_at        TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_hazards_type   ON hazards (type);
CREATE INDEX IF NOT EXISTS ix_hazards_status ON hazards (status);

CREATE TABLE IF NOT EXISTS cluster_areas (
    id               SERIAL PRIMARY KEY,
    name             VARCHAR NOT NULL,
    country          VARCHAR,
    city             VARCHAR,
    center_lat       DOUBLE PRECISION NOT NULL,
    center_lon       DOUBLE PRECISION NOT NULL,
    area_radius_km   DOUBLE PRECISION NOT NULL,
    cluster_radius_m DOUBLE PRECISION NOT NULL,
    type_radius      JSON,                                -- {"pothole": 25}
    is_active        BOOLEAN NOT NULL DEFAULT TRUE,
    created_at       TIMESTAMP NOT NULL,
    updated_at       TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS detection_reviews (
    id           VARCHAR PRIMARY KEY,
    detection_id VARCHAR NOT NULL REFERENCES detections(id) ON DELETE CASCADE,
    from_status  VARCHAR,
    to_status    VARCHAR NOT NULL,
    action       VARCHAR NOT NULL,  -- accept|correct|reject|reopen|location_fix|auto_reject|auto_accept|resolve|unresolve|create
    actor_type   VARCHAR NOT NULL,  -- user | system
    actor        VARCHAR,
    note         VARCHAR,
    details      JSON,
    created_at   TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_detection_reviews_detection_id ON detection_reviews (detection_id);

CREATE TABLE IF NOT EXISTS job_track_points (
    id           SERIAL PRIMARY KEY,
    job_id       VARCHAR NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    frame_number INTEGER NOT NULL,
    video_time_s DOUBLE PRECISION,
    latitude     DOUBLE PRECISION NOT NULL,
    longitude    DOUBLE PRECISION NOT NULL,
    speed_kmh    DOUBLE PRECISION,
    source       VARCHAR NOT NULL DEFAULT 'ocr'
);
CREATE INDEX IF NOT EXISTS ix_job_track_points_job_id ON job_track_points (job_id);

-- ── New detection columns ───────────────────────────────────────────────────
ALTER TABLE detections ADD COLUMN IF NOT EXISTS location_status VARCHAR;   -- confirmed | approximate | missing
ALTER TABLE detections ADD COLUMN IF NOT EXISTS location_source VARCHAR;   -- ocr | interpolated | map_pin | trajectory | ocr_text | none
ALTER TABLE detections ADD COLUMN IF NOT EXISTS country VARCHAR;
ALTER TABLE detections ADD COLUMN IF NOT EXISTS city VARCHAR;
ALTER TABLE detections ADD COLUMN IF NOT EXISTS bbox_x1 DOUBLE PRECISION;
ALTER TABLE detections ADD COLUMN IF NOT EXISTS bbox_y1 DOUBLE PRECISION;
ALTER TABLE detections ADD COLUMN IF NOT EXISTS bbox_x2 DOUBLE PRECISION;
ALTER TABLE detections ADD COLUMN IF NOT EXISTS bbox_y2 DOUBLE PRECISION;
ALTER TABLE detections ADD COLUMN IF NOT EXISTS video_time_s DOUBLE PRECISION;
ALTER TABLE detections ADD COLUMN IF NOT EXISTS model_name VARCHAR;
ALTER TABLE detections ADD COLUMN IF NOT EXISTS model_version VARCHAR;     -- weights.pt@sha256[:12]
ALTER TABLE detections ADD COLUMN IF NOT EXISTS predicted_type VARCHAR;
ALTER TABLE detections ADD COLUMN IF NOT EXISTS predicted_subtype VARCHAR;
ALTER TABLE detections ADD COLUMN IF NOT EXISTS rejection_code VARCHAR;
ALTER TABLE detections ADD COLUMN IF NOT EXISTS rejection_source VARCHAR;  -- human | auto
ALTER TABLE detections ADD COLUMN IF NOT EXISTS hazard_id VARCHAR REFERENCES hazards(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS ix_detections_hazard_id ON detections (hazard_id);
CREATE INDEX IF NOT EXISTS ix_detections_review_status ON detections (review_status);

-- ── Back-fill (each row touched once) ───────────────────────────────────────
UPDATE detections SET location_status = 'missing', location_source = 'none',
       review_status = CASE WHEN review_status = 'pending' THEN 'location_pending'::reviewstatus ELSE review_status END
 WHERE location_status IS NULL AND (latitude IS NULL OR longitude IS NULL);
UPDATE detections SET location_status = 'approximate', location_source = 'interpolated',
       review_status = CASE WHEN review_status = 'pending' THEN 'location_pending'::reviewstatus ELSE review_status END
 WHERE location_status IS NULL AND gps_interpolated;
UPDATE detections SET location_status = 'confirmed', location_source = 'ocr'
 WHERE location_status IS NULL;
UPDATE detections SET predicted_type = type, predicted_subtype = subtype
 WHERE predicted_type IS NULL;

-- Hazards are then built from the accepted detections at the next startup
-- (when the table is empty), or explicitly: python -m app.cli rebuild-hazards
