-- Migration: rejection reason kinds (docs/TRAINING-DATA.md, "Rejection kinds").
-- Applied automatically at boot by app/core/db/upgrade.py (REJECTION_KIND_COLUMNS);
-- mirrored here for manual review. The seeded reasons get their kind from
-- app.main.SEED_REASONS at start-up; existing training samples of rejected
-- detections are re-filed by `python -m app.cli resync-training-rejections`
-- (also run once automatically at start-up for unclassified samples).

ALTER TABLE rejection_reasons ADD COLUMN IF NOT EXISTS kind VARCHAR NOT NULL DEFAULT 'false_positive';
    -- false_positive | real_redundant | unusable
ALTER TABLE training_samples ADD COLUMN IF NOT EXISTS rejection_kind VARCHAR;
ALTER TABLE training_samples ADD COLUMN IF NOT EXISTS excluded BOOLEAN NOT NULL DEFAULT FALSE;
