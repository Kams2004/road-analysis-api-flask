-- Migration: create datasets and dataset_samples tables

CREATE TABLE IF NOT EXISTS datasets (
    id             TEXT PRIMARY KEY,
    detection_type TEXT NOT NULL,
    version        INTEGER NOT NULL,
    total_samples  INTEGER NOT NULL DEFAULT 0,
    class_counts   JSONB NOT NULL DEFAULT '{}',
    manifest_url   TEXT,
    metadata_url   TEXT,
    created_by     TEXT NOT NULL,
    created_at     TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE (detection_type, version)
);

CREATE TABLE IF NOT EXISTS dataset_samples (
    id                   TEXT PRIMARY KEY,
    dataset_id           TEXT NOT NULL REFERENCES datasets(id),
    validation_label_id  TEXT NOT NULL REFERENCES validation_labels(id),
    label                TEXT NOT NULL,
    severity_score       SMALLINT NOT NULL DEFAULT 0,
    model_confidence     FLOAT NOT NULL,
    crop_url             TEXT
);

CREATE INDEX IF NOT EXISTS ix_datasets_detection_type ON datasets(detection_type);
CREATE INDEX IF NOT EXISTS ix_dataset_samples_dataset_id ON dataset_samples(dataset_id);
