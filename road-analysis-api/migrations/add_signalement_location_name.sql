-- Add location_name to signalements (reverse-geocoded on creation)
ALTER TABLE signalements ADD COLUMN IF NOT EXISTS location_name VARCHAR;
