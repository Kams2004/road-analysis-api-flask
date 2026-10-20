-- Parked / roadside vehicle detection (Module 5, app/modules/m05_ai_detection/parked_vehicle)
-- and the recording time of every detection. Applied automatically at startup
-- by app/core/db/upgrade.py (PARKED_VEHICLE_COLUMNS); kept here for manual / review
-- purposes. Idempotent.
--
--   recorded_at   video recording time of the detection (timezone-aware): the
--                 OSD date-time of the stored frame (camera local time,
--                 settings OSD_TIMEZONE), else extrapolated from the nearest
--                 frame of the same video whose OSD time was read; NULL when
--                 the video never showed a readable time. All detection types.
--   ai_details    evidence of rule-based pipelines — parked vehicles:
--                 {verdict, subtype, severity, vehicle_class, score,
--                  score_components, motion, fallback, reasons[{code, value,
--                  threshold, passed, detail}], features{…}, target_position,
--                  road_distance_m, camera_profile, rules_version}.
--                 Detections have no severity column: parked-vehicle severity
--                 is ai_details->>'severity'.
--
-- Detection type 'parked_vehicle', subtypes 'blocking' | 'wrong_side' |
-- 'roadside'. Settings: system_settings section `parked_vehicle` (no DDL).

ALTER TABLE detections ADD COLUMN IF NOT EXISTS recorded_at TIMESTAMPTZ;
ALTER TABLE detections ADD COLUMN IF NOT EXISTS ai_details JSON;
CREATE INDEX IF NOT EXISTS ix_detections_recorded_at ON detections (recorded_at);

-- Recording time of every trajectory fix (same rule as detections.recorded_at);
-- the hazard lifecycle's video passes use it in preference to
-- "recording start + video_time_s".
ALTER TABLE job_track_points ADD COLUMN IF NOT EXISTS recorded_at TIMESTAMPTZ;

-- Rejection reasons of the parked-vehicle rules (also seeded at startup by
-- app/main.py SEED_REASONS).
INSERT INTO rejection_reasons (id, detection_type, code, label, description, is_custom, created_at)
SELECT gen_random_uuid()::text, 'parked_vehicle', v.code, v.label, v.description, FALSE, NOW()
FROM (VALUES
  ('ai_off_road',       'Off the road',          'Automatically rejected: the vehicle stands too far from the road (yard, garage, fuel station, car park)'),
  ('ai_not_grounded',   'Not a real vehicle',    'Automatically rejected: the vehicle does not stand on the ground (billboard / poster, car on a carrier, reflection)'),
  ('ai_traffic_queue',  'Traffic queue',         'Automatically rejected: vehicle stopped in traffic ahead of us, not parked'),
  ('ai_moving_vehicle', 'Moving vehicle',        'Rejected: the vehicle was moving (same direction or oncoming), not parked')
) AS v(code, label, description)
WHERE NOT EXISTS (SELECT 1 FROM rejection_reasons r WHERE r.code = v.code AND r.detection_type = 'parked_vehicle');
