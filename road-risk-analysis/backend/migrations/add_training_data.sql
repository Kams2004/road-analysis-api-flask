-- Migration: live training data (docs/TRAINING-DATA.md).
-- Applied automatically at boot by app/core/db/upgrade.py (TRAINING_DATA_COLUMNS)
-- and create_all (the two tables); mirrored here for manual review.

ALTER TABLE detections ADD COLUMN IF NOT EXISTS frame_clean_url VARCHAR;
ALTER TABLE detections ADD COLUMN IF NOT EXISTS frame_width INTEGER;
ALTER TABLE detections ADD COLUMN IF NOT EXISTS frame_height INTEGER;
ALTER TABLE detections ADD COLUMN IF NOT EXISTS corrected_bbox JSON;

CREATE TABLE IF NOT EXISTS training_samples (
    id                 VARCHAR PRIMARY KEY REFERENCES detections(id) ON DELETE CASCADE,
    detection_type     VARCHAR NOT NULL,
    frame_key          VARCHAR NOT NULL,
    group_key          VARCHAR NOT NULL,
    job_id             VARCHAR NOT NULL,
    source_name        VARCHAR,
    decision           VARCHAR NOT NULL,          -- accepted | corrected | rejected
    state              VARCHAR NOT NULL,          -- active | superseded
    decided_by         VARCHAR NOT NULL,          -- human | system
    reviewer           VARCHAR,
    reviewed_at        TIMESTAMP,
    reason_code        VARCHAR,
    corrections        JSON,
    class_name         VARCHAR,
    class_index        INTEGER,
    classifier_class   VARCHAR NOT NULL,
    bbox_x1            DOUBLE PRECISION,
    bbox_y1            DOUBLE PRECISION,
    bbox_x2            DOUBLE PRECISION,
    bbox_y2            DOUBLE PRECISION,
    box_corrected      BOOLEAN NOT NULL,
    frame_number       INTEGER,
    video_time_s       DOUBLE PRECISION,
    recorded_at        TIMESTAMPTZ,
    latitude           DOUBLE PRECISION,
    longitude          DOUBLE PRECISION,
    confidence         DOUBLE PRECISION,
    model_version      VARCHAR,
    model_card_version VARCHAR,
    crop_key           VARCHAR,
    sync_status        VARCHAR NOT NULL,          -- pending | synced | failed
    sync_attempts      INTEGER NOT NULL,
    sync_error         VARCHAR,
    synced_at          TIMESTAMP,
    created_at         TIMESTAMP NOT NULL,
    updated_at         TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_training_samples_detection_type ON training_samples (detection_type);
CREATE INDEX IF NOT EXISTS ix_training_samples_frame_key ON training_samples (frame_key);
CREATE INDEX IF NOT EXISTS ix_training_samples_job_id ON training_samples (job_id);
CREATE INDEX IF NOT EXISTS ix_training_samples_state ON training_samples (state);
CREATE INDEX IF NOT EXISTS ix_training_samples_sync_status ON training_samples (sync_status);
CREATE INDEX IF NOT EXISTS ix_training_samples_updated_at ON training_samples (updated_at);

CREATE TABLE IF NOT EXISTS training_frames (
    id               SERIAL PRIMARY KEY,
    detection_type   VARCHAR NOT NULL,
    frame_key        VARCHAR NOT NULL,
    group_key        VARCHAR NOT NULL,
    job_id           VARCHAR NOT NULL,
    frame_number     INTEGER,
    video_time_s     DOUBLE PRECISION,
    image_width      INTEGER,
    image_height     INTEGER,
    source_object    VARCHAR,
    image_key        VARCHAR,
    label_key        VARCHAR,
    labels           JSON,
    n_boxes          INTEGER NOT NULL,
    status           VARCHAR NOT NULL,            -- active | empty | no_image
    review_status    VARCHAR NOT NULL,            -- needs_box_review | box_reviewed
    reviewed_boxes   JSON,
    box_reviewer     VARCHAR,
    box_reviewed_at  TIMESTAMP,
    dirty            BOOLEAN NOT NULL,            -- objects must be (re)written by the sync
    sync_attempts    INTEGER NOT NULL,
    sync_error       VARCHAR,
    synced_at        TIMESTAMP,
    created_at       TIMESTAMP NOT NULL,
    updated_at       TIMESTAMP NOT NULL,
    CONSTRAINT uq_training_frames_type_key UNIQUE (detection_type, frame_key)
);
CREATE INDEX IF NOT EXISTS ix_training_frames_detection_type ON training_frames (detection_type);
CREATE INDEX IF NOT EXISTS ix_training_frames_job_id ON training_frames (job_id);
CREATE INDEX IF NOT EXISTS ix_training_frames_review_status ON training_frames (review_status);
CREATE INDEX IF NOT EXISTS ix_training_frames_updated_at ON training_frames (updated_at);
CREATE INDEX IF NOT EXISTS ix_training_frames_dirty ON training_frames (dirty);
