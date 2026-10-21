-- Migration: observation identity & object position (docs/CLUSTERS.md).
-- Applied automatically at boot by app/core/db/upgrade.py (OBJECT_POSITION_STATEMENTS);
-- mirrored here for manual review.
--
-- detections.latitude / longitude stay the position of the recording VEHICLE
-- (evidence); object_latitude / object_longitude are the estimated ground
-- position of the detected object (vehicle fix + range along the GPS heading),
-- used by the clustering. object_position_source: estimated | vehicle | NULL.
ALTER TABLE detections ADD COLUMN IF NOT EXISTS object_latitude DOUBLE PRECISION;
ALTER TABLE detections ADD COLUMN IF NOT EXISTS object_longitude DOUBLE PRECISION;
ALTER TABLE detections ADD COLUMN IF NOT EXISTS object_position_source VARCHAR;

-- Duplicate-observation lookups (same type, same recording instant ± 1 s).
CREATE INDEX IF NOT EXISTS ix_detections_type_recorded_at ON detections (type, recorded_at);

-- Existing data: `python -m app.cli dedupe-observations [--dry-run]` removes the
-- duplicate observations (same footage processed several times), back-fills the
-- object positions and re-clusters.
