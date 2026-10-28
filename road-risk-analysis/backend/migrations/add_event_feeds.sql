-- Daily driving-event files from an SFTP "events" source (app/modules/m08_historical/event_feed.py).
-- Applied automatically at API start-up (app/core/db/upgrade.py, EVENT_FEED_COLUMNS); event_files
-- is created by create_all (app/modules/m08_historical/models/event_file.py).
ALTER TABLE cloud_connections ADD COLUMN IF NOT EXISTS purpose VARCHAR NOT NULL DEFAULT 'videos';
ALTER TABLE cloud_connections ADD COLUMN IF NOT EXISTS event_alert_hour INTEGER;      -- NULL = 10 (UTC)
ALTER TABLE cloud_connections ADD COLUMN IF NOT EXISTS event_alert_emails JSON;
ALTER TABLE historical_events ADD COLUMN IF NOT EXISTS country VARCHAR;
ALTER TABLE historical_events ADD COLUMN IF NOT EXISTS transporter VARCHAR;
ALTER TABLE historical_events ADD COLUMN IF NOT EXISTS ended_at TIMESTAMP;
ALTER TABLE historical_events ADD COLUMN IF NOT EXISTS duration_s DOUBLE PRECISION;
ALTER TABLE historical_events ADD COLUMN IF NOT EXISTS distance_km DOUBLE PRECISION;
ALTER TABLE historical_events ADD COLUMN IF NOT EXISTS event_file_id VARCHAR;
CREATE INDEX IF NOT EXISTS ix_historical_events_country_occurred ON historical_events (country, occurred_at);
CREATE INDEX IF NOT EXISTS ix_historical_events_file ON historical_events (event_file_id);
