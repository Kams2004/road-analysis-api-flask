-- Two-stage traffic-sign pipeline (detector traffic_sign/traffic_light +
-- crop classifier of VIASafe sign codes). Applied automatically at startup by
-- app/db/upgrade.py (SIGN_CLASSIFIER_COLUMNS); kept here for manual / review
-- purposes. Idempotent.
--
--   class_candidates   classifier top-3 for a traffic-sign crop:
--                      [{"code": "speed_limit_50", "score": 0.82}, …] (NULL for
--                      other types and for the legacy single-stage sign model)
--
-- Sign subtypes stay in detections.subtype (type 'traffic_sign'; traffic
-- lights are subtype 'traffic_light'; an uncertain classifier gives
-- 'unknown_sign'). Per-class thresholds live in the classifier model card and
-- in system_settings section `ai_validation` (type_overrides.traffic_sign.per_class).

ALTER TABLE detections ADD COLUMN IF NOT EXISTS class_candidates JSON;

-- Rejection reason used when the sign classifier's reject class (card
-- thresholds.reject_class, e.g. `not_a_sign`) wins (also seeded at startup by
-- app/main.py SEED_REASONS).
INSERT INTO rejection_reasons (id, detection_type, code, label, description, is_custom, created_at)
SELECT gen_random_uuid()::text, 'traffic_sign', 'ai_not_a_sign', 'Not a traffic sign',
       'Automatically rejected: the sign classifier judged the crop is not a traffic sign (sticker, billboard, person, vehicle, tree …)', FALSE, NOW()
WHERE NOT EXISTS (SELECT 1 FROM rejection_reasons WHERE code = 'ai_not_a_sign' AND detection_type = 'traffic_sign');

-- New settings of section `ai_validation` (no DDL, defaults apply):
--   min_frames_for_auto_accept (2), max_static_drift_m (8), pothole_min_y_ratio (0.45)
