-- Dashcam overlay layouts (RRAP-05-014): requested and used OSD layout of a job.
-- (Also applied at startup by app/core/db/upgrade.py.)
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS osd_layout_requested VARCHAR;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS osd_layout VARCHAR;
