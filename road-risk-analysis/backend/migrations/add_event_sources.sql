-- Migration: several event providers a day (YYYY-MM-DD_<source>.csv) and a processing hour per
-- events source (app/modules/m08_historical/event_feed.py). Mirrors app/core/db/upgrade.py. Idempotent.
ALTER TABLE cloud_connections ADD COLUMN IF NOT EXISTS event_process_hour INTEGER;   -- UTC, null = 4
ALTER TABLE event_files ADD COLUMN IF NOT EXISTS source VARCHAR;                     -- null = ymane (legacy names)
