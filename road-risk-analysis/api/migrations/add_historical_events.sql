-- Migration: create historical_events table (collision-prevention Step 6.1)
-- Run once on existing databases; SQLAlchemy create_all handles new installs.
--
-- Durable storage, unlike the Redis-only live vehicle state used by Steps
-- 2-5 — historical events accumulate indefinitely for Step 6.2+ training.

CREATE TABLE IF NOT EXISTS historical_events (
    id                   VARCHAR PRIMARY KEY,

    source               VARCHAR NOT NULL,
    source_event_id      VARCHAR NOT NULL,
    event_type           VARCHAR NOT NULL,

    occurred_at          TIMESTAMP NOT NULL,

    latitude             DOUBLE PRECISION NOT NULL,
    longitude            DOUBLE PRECISION NOT NULL,

    vehicle_external_id  VARCHAR,
    driver_external_id   VARCHAR,

    speed_mps            DOUBLE PRECISION,
    speed_limit_mps      DOUBLE PRECISION,
    severity             VARCHAR,

    grid_cell_id         VARCHAR NOT NULL,
    matched_latitude     DOUBLE PRECISION,
    matched_longitude    DOUBLE PRECISION,
    road_name            VARCHAR,
    road_segment_id      VARCHAR,
    match_confidence     DOUBLE PRECISION,

    raw                  JSONB NOT NULL DEFAULT '{}',

    imported_at          TIMESTAMP NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_historical_event_source UNIQUE (source, source_event_id)
);

CREATE INDEX IF NOT EXISTS ix_historical_events_grid_cell   ON historical_events(grid_cell_id);
CREATE INDEX IF NOT EXISTS ix_historical_events_occurred_at ON historical_events(occurred_at);
