-- Migration: add enabled_models to jobs, raw_gps_text + context_clip_url to detections
-- Safe to run multiple times.

DO $$
BEGIN
    -- jobs
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'jobs' AND column_name = 'enabled_models'
    ) THEN
        ALTER TABLE jobs ADD COLUMN enabled_models VARCHAR;
    END IF;

    -- detections
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'detections' AND column_name = 'raw_gps_text'
    ) THEN
        ALTER TABLE detections ADD COLUMN raw_gps_text VARCHAR;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'detections' AND column_name = 'context_clip_url'
    ) THEN
        ALTER TABLE detections ADD COLUMN context_clip_url VARCHAR;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'detections' AND column_name = 'location_name'
    ) THEN
        ALTER TABLE detections ADD COLUMN location_name VARCHAR;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'detections' AND column_name = 'rpm'
    ) THEN
        ALTER TABLE detections ADD COLUMN rpm INTEGER;
    END IF;
END $$;
