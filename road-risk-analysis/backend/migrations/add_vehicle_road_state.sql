-- Migration: create vehicle_road_states table
-- Run once on existing databases; SQLAlchemy create_all handles new installs.
--
-- One row per vehicle — the latest road-aware position summary only.
-- Full road geometry / upcoming segments are NOT persisted here; they're
-- cheap to recompute from OSRM and served from the Redis-cached
-- RoadAwareVehicleState (see app/services/road_state_service.py).

CREATE TABLE IF NOT EXISTS vehicle_road_states (
    vehicle_id         VARCHAR PRIMARY KEY REFERENCES vehicles(id),
    road_segment_id    VARCHAR,
    road_name          VARCHAR,
    direction          VARCHAR NOT NULL DEFAULT 'UNKNOWN',
    matched_latitude   DOUBLE PRECISION,
    matched_longitude  DOUBLE PRECISION,
    match_quality      VARCHAR NOT NULL,
    match_confidence   DOUBLE PRECISION,
    matched_at         TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMP NOT NULL DEFAULT NOW()
);
