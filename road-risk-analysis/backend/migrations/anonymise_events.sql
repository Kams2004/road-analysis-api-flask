-- Migration: anonymous exceptions and vehicles — nothing concerning a driver or a vehicle is kept
-- (app/modules/m08_historical/anonymity.py). Mirrors app/core/db/upgrade.py (ANONYMISE_*,
-- anonymise_statements), which applies it automatically at start-up. Idempotent: each block runs
-- only while the identity columns still exist; the whole file can be re-run safely.
--
-- IRREVERSIBLE: the driver ids / names and vehicle ids / plates / unit ids are erased.
-- The values are overwritten before the columns are dropped (DROP COLUMN alone leaves them in the
-- rows on disk). Old row versions remain until VACUUM reclaims them (VACUUM FULL, or a dump / restore,
-- rewrites the tables); backups and WAL archives taken before keep them.

BEGIN;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema()
             AND table_name = 'historical_events' AND column_name IN ('driver_external_id', 'vehicle_external_id')) THEN
    -- raw: only the whitelisted, non-identifying source fields (anonymity.RAW_KEYS); json or jsonb column
    EXECUTE format(
      'UPDATE historical_events SET raw = COALESCE((SELECT jsonb_object_agg(lower(j.key), j.value) '
      'FROM jsonb_each(CASE WHEN jsonb_typeof(raw::jsonb) = ''object'' THEN raw::jsonb ELSE ''{}''::jsonb END) AS j '
      'WHERE lower(j.key) IN (%s)), ''{}''::jsonb)::%s',
      $k$
      'exceptionid', 'exception_type_name', 'startdatetime', 'enddatetime', 'exceptionday', 'startgps',
      'endgps', 'vmax', 'threshold', 'maxvalue', 'level', 'totalduration',
      'totaldistance', 'distanceunderexception', 'timeexceeded', 'status', 'requiredbreak', 'numberofbreak',
      'maxbreak', 'invaliddata', 'datasource', 'treated', 'mergetreated', 'preventivedatetime',
      'preventivethreshold', 'customer_affiliate_name', 'transporter_name', 'isrestored', 'restauredate', 'aireason',
      'aidetectionvalid', 'exceptiontype', 'affiliateid', 'transporterid'
      $k$,
      (SELECT data_type FROM information_schema.columns WHERE table_schema = current_schema()
       AND table_name = 'historical_events' AND column_name = 'raw'));
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema()
             AND table_name = 'historical_events' AND column_name = 'driver_external_id') THEN
    UPDATE historical_events SET driver_external_id = NULL WHERE driver_external_id IS NOT NULL;
    ALTER TABLE historical_events DROP COLUMN driver_external_id;      -- drops ix_historical_events_driver
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema()
             AND table_name = 'historical_events' AND column_name = 'vehicle_external_id') THEN
    UPDATE historical_events SET vehicle_external_id = NULL WHERE vehicle_external_id IS NOT NULL;
    ALTER TABLE historical_events DROP COLUMN vehicle_external_id;     -- drops ix_historical_events_vehicle
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema()
             AND table_name = 'vehicles' AND column_name = 'plate') THEN
    UPDATE vehicles SET plate = NULL WHERE plate IS NOT NULL;
    ALTER TABLE vehicles DROP COLUMN plate;
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema()
             AND table_name = 'vehicles' AND column_name = 'driver_external_id') THEN
    UPDATE vehicles SET driver_external_id = NULL WHERE driver_external_id IS NOT NULL;
    ALTER TABLE vehicles DROP COLUMN driver_external_id;
  END IF;
END $$;

-- Daily summaries keyed by driver / vehicle: dropped. Their anonymous successors (event_rollup_day,
-- event_rollup_hour, event_rollup_cell — app/modules/m08_historical/models/event_rollup.py) are created by
-- the API at start-up and rebuilt from the events (worker task rebuild_event_rollups).
DROP TABLE IF EXISTS event_daily;
DROP TABLE IF EXISTS event_daily_hour;
DROP TABLE IF EXISTS event_daily_cell;

COMMIT;

-- Redis (done by the API at start-up — app/modules/m10_collision/vehicle_service.forget_identities):
--   HDEL vehicle:live:<id> plate driver_external_id     (every live vehicle hash)
--   DEL collision:ctx:drivers collision:ctx:drivers:tmp
