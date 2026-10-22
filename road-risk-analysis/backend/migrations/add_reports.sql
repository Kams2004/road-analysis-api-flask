-- Module 14 — generated PDF reports (files in the MinIO bucket `reports`).
-- The table is created at start-up by create_all (app/modules/m14_reports/models/report.py).
CREATE TABLE IF NOT EXISTS generated_reports (
    id VARCHAR PRIMARY KEY,
    report_type VARCHAR NOT NULL,
    title VARCHAR NOT NULL,
    filters JSON NOT NULL,
    summary JSON NOT NULL,
    status VARCHAR NOT NULL,
    error VARCHAR,
    object_name VARCHAR,
    size_bytes INTEGER,
    page_count INTEGER,
    methodology JSON NOT NULL,
    created_by VARCHAR,
    created_at TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_generated_reports_created_at ON generated_reports (created_at);
