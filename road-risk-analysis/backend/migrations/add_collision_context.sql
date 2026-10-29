-- Migration: collision prevention — context & reliability (docs/COLLISION-PREVENTION.md)
-- Mirrors app/core/db/upgrade.py COLLISION_CONTEXT_STATEMENTS (run automatically at start-up);
-- SQLAlchemy create_all creates collision_alert_feedback on start-up as well. Idempotent.

-- Optional fleet identity of a registered vehicle → its driving-behaviour score (RISKY_DRIVER)
ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS plate VARCHAR;
ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS driver_external_id VARCHAR;

-- Driver feedback on alerts (POST /alerts/{conflict_id}/feedback), one row per conflict × vehicle
CREATE TABLE IF NOT EXISTS collision_alert_feedback (
    id                  VARCHAR PRIMARY KEY,
    conflict_id         VARCHAR NOT NULL,
    vehicle_id          VARCHAR NOT NULL,
    verdict             VARCHAR NOT NULL,           -- false_alert | useful
    comment             TEXT,
    level               VARCHAR,
    physical_level      VARCHAR,
    context_index       DOUBLE PRECISION,
    context_level_boost INTEGER,
    other_vehicle_id    VARCHAR,
    submitted_by        VARCHAR,
    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_collision_feedback_conflict_vehicle UNIQUE (conflict_id, vehicle_id)
);
CREATE INDEX IF NOT EXISTS ix_collision_feedback_updated_at ON collision_alert_feedback (updated_at);
