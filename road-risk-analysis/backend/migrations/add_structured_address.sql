-- Structured reverse geocoding of detections and hazards (Hazard Track:
-- readable locations instead of raw GPS). Applied automatically at startup by
-- app/core/db/upgrade.py (ADDRESS_COLUMNS); kept here for manual / review purposes.
-- Idempotent.
--
--   road              street name                         "Rue Joss"
--   locality          quartier / neighbourhood / suburb   "Akwa"
--   district          arrondissement / city district      "Douala Ier"
--   town              city / town / village               "Douala"
--   region            state / region                      "Littoral"
--   display_location  short label (road, locality, town)  "Rue Joss, Akwa, Douala"
--   detections.geocoded_at  when the address was looked up; NULL = never
--                     (back-fill candidate: POST /hazards/geocode/backfill or
--                     `python -m app.cli geocode-backfill`, throttled ≤ 1 req/s).
--
-- country / city / location_name stay as before (city = town). A hazard's
-- address is the one of its most central geocoded member.

ALTER TABLE detections ADD COLUMN IF NOT EXISTS road VARCHAR;
ALTER TABLE detections ADD COLUMN IF NOT EXISTS locality VARCHAR;
ALTER TABLE detections ADD COLUMN IF NOT EXISTS district VARCHAR;
ALTER TABLE detections ADD COLUMN IF NOT EXISTS town VARCHAR;
ALTER TABLE detections ADD COLUMN IF NOT EXISTS region VARCHAR;
ALTER TABLE detections ADD COLUMN IF NOT EXISTS display_location VARCHAR;
ALTER TABLE detections ADD COLUMN IF NOT EXISTS geocoded_at TIMESTAMP;
ALTER TABLE hazards ADD COLUMN IF NOT EXISTS road VARCHAR;
ALTER TABLE hazards ADD COLUMN IF NOT EXISTS locality VARCHAR;
ALTER TABLE hazards ADD COLUMN IF NOT EXISTS district VARCHAR;
ALTER TABLE hazards ADD COLUMN IF NOT EXISTS town VARCHAR;
ALTER TABLE hazards ADD COLUMN IF NOT EXISTS region VARCHAR;
ALTER TABLE hazards ADD COLUMN IF NOT EXISTS display_location VARCHAR;
CREATE INDEX IF NOT EXISTS ix_detections_town ON detections (town);
CREATE INDEX IF NOT EXISTS ix_detections_locality ON detections (locality);
CREATE INDEX IF NOT EXISTS ix_hazards_town ON hazards (town);
CREATE INDEX IF NOT EXISTS ix_hazards_locality ON hazards (locality);
