-- Module 5 — Driver Behaviour Analytics reads historical_events (single event
-- store, shared with Module 4) by type + period, driver and vehicle.
-- Applied automatically at start-up by app/core/db/upgrade.py.
CREATE INDEX IF NOT EXISTS ix_historical_events_type_occurred ON historical_events (event_type, occurred_at);
CREATE INDEX IF NOT EXISTS ix_historical_events_driver ON historical_events (driver_external_id);
CREATE INDEX IF NOT EXISTS ix_historical_events_vehicle ON historical_events (vehicle_external_id);
