-- Two-stage pothole pipeline (detector + crop classifier) and the AI
-- validation mode (RRAP-05-010/011/012/016, RRAP-06-007, RRAP-13-001/016).
-- Applied automatically at startup by app/db/upgrade.py (AI_VALIDATION_COLUMNS);
-- kept here for manual / review purposes. Idempotent.
--
--   model_card_version     detector model card "name:version" (model_version stays file@sha256[:12])
--   verifier_score         crop classifier P(class) — NULL when no classifier is installed
--   verifier_model         classifier weights file@sha256[:12]
--   verifier_card_version  classifier model card "name:version"
--   ai_decision            auto_accept | auto_reject | review (NULL = created before this feature)
--   ai_decision_reason     short explanation (thresholds that fired, manual-mode suggestion …)
--   audit_sample           automatic decision left pending for a human audit
--
-- The runtime settings live in system_settings (section `ai_validation`), no DDL needed.

ALTER TABLE detections ADD COLUMN IF NOT EXISTS model_card_version VARCHAR;
ALTER TABLE detections ADD COLUMN IF NOT EXISTS verifier_score DOUBLE PRECISION;
ALTER TABLE detections ADD COLUMN IF NOT EXISTS verifier_model VARCHAR;
ALTER TABLE detections ADD COLUMN IF NOT EXISTS verifier_card_version VARCHAR;
ALTER TABLE detections ADD COLUMN IF NOT EXISTS ai_decision VARCHAR;
ALTER TABLE detections ADD COLUMN IF NOT EXISTS ai_decision_reason VARCHAR;
ALTER TABLE detections ADD COLUMN IF NOT EXISTS audit_sample BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS ix_detections_ai_decision ON detections (ai_decision);
CREATE INDEX IF NOT EXISTS ix_detections_audit_sample ON detections (audit_sample) WHERE audit_sample;

-- Rejection reason used when the crop classifier rejects a detection
-- (also seeded at startup by app/main.py SEED_REASONS).
INSERT INTO rejection_reasons (id, detection_type, code, label, description, is_custom, created_at)
SELECT gen_random_uuid()::text, 'all', 'ai_verifier', 'Rejected by AI verifier',
       'Automatically rejected: the crop classifier judged it is not this hazard type', FALSE, NOW()
WHERE NOT EXISTS (SELECT 1 FROM rejection_reasons WHERE code = 'ai_verifier' AND detection_type = 'all');
