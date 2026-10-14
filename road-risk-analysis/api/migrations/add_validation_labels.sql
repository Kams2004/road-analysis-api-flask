-- Migration: add crop_url to detections + create validation_labels table

ALTER TABLE detections ADD COLUMN IF NOT EXISTS crop_url TEXT;

CREATE TABLE IF NOT EXISTS validation_labels (
    id               TEXT PRIMARY KEY,
    detection_id     TEXT NOT NULL UNIQUE REFERENCES detections(id),
    detection_type   TEXT NOT NULL,
    label            TEXT NOT NULL,
    severity_score   SMALLINT NOT NULL DEFAULT 0 CHECK (severity_score BETWEEN 0 AND 3),
    model_confidence FLOAT NOT NULL,
    crop_url         TEXT,
    labeled_by       TEXT NOT NULL,
    labeled_at       TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_validation_labels_detection_type ON validation_labels(detection_type);
CREATE INDEX IF NOT EXISTS ix_validation_labels_label          ON validation_labels(label);
CREATE INDEX IF NOT EXISTS ix_validation_labels_severity_score ON validation_labels(severity_score);
