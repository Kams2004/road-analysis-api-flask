-- Module 6 — hazard lifecycle (app/modules/m06_hazards/lifecycle/): re-sightings,
-- "not seen" video passes, driver votes, expiry and recurring spots.
--
-- Applied automatically at API startup by app/core/db/upgrade.py (idempotent);
-- this file mirrors it for review / manual runs.

ALTER TABLE hazards ADD COLUMN IF NOT EXISTS resolution_source VARCHAR;      -- manual | video_absence | votes | expiry
ALTER TABLE hazards ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMP;         -- recording time of the latest sighting (UTC)
ALTER TABLE hazards ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP;           -- NULL = never expires
ALTER TABLE hazards ADD COLUMN IF NOT EXISTS absence_count INTEGER NOT NULL DEFAULT 0;  -- consecutive valid "not seen" passes
ALTER TABLE hazards ADD COLUMN IF NOT EXISTS recurring_spot BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE hazards ADD COLUMN IF NOT EXISTS recurrence_count INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS ix_hazards_last_seen_at ON hazards (last_seen_at);
CREATE INDEX IF NOT EXISTS ix_hazards_expires_at ON hazards (expires_at);

-- Timeline of a hazard
CREATE TABLE IF NOT EXISTS hazard_events (
    id            VARCHAR PRIMARY KEY,
    hazard_id     VARCHAR NOT NULL REFERENCES hazards(id) ON DELETE CASCADE,
    kind          VARCHAR NOT NULL,     -- seen | not_seen | vote_still_there | vote_not_there | expired | resolved | reopened
    source        VARCHAR,              -- video | review | vote | expiry | manual | video_absence | votes | system
    job_id        VARCHAR,
    detection_id  VARCHAR,
    user_id       VARCHAR,
    details       JSON,
    recorded_at   TIMESTAMP,            -- when the evidence happened (recording / vote time, UTC)
    created_at    TIMESTAMP NOT NULL DEFAULT now()
);
ALTER TABLE hazard_events ADD COLUMN IF NOT EXISTS detection_id VARCHAR;
CREATE INDEX IF NOT EXISTS ix_hazard_events_hazard_id_created_at ON hazard_events (hazard_id, created_at);
CREATE INDEX IF NOT EXISTS ix_hazard_events_job_id ON hazard_events (job_id);
CREATE INDEX IF NOT EXISTS ix_hazard_events_detection_id ON hazard_events (detection_id);

-- Driver votes (counted or refused)
CREATE TABLE IF NOT EXISTS hazard_votes (
    id          VARCHAR PRIMARY KEY,
    hazard_id   VARCHAR NOT NULL REFERENCES hazards(id) ON DELETE CASCADE,
    user_id     VARCHAR REFERENCES users(id) ON DELETE SET NULL,
    device_id   VARCHAR,
    vote        VARCHAR NOT NULL,       -- still_there | not_there
    latitude    DOUBLE PRECISION,
    longitude   DOUBLE PRECISION,
    distance_m  DOUBLE PRECISION,
    counted     BOOLEAN NOT NULL DEFAULT FALSE,
    reason      VARCHAR,                -- NULL | too_far | cooldown
    created_at  TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_hazard_votes_hazard_id ON hazard_votes (hazard_id);
CREATE INDEX IF NOT EXISTS ix_hazard_votes_user_id ON hazard_votes (user_id);

-- Back-fill (idempotent): last sighting = latest member recording time
-- (OSD time is local Africa/Douala time; else processing time).
UPDATE hazards h SET last_seen_at = sub.t
FROM (SELECT hazard_id,
             MAX(COALESCE((captured_at AT TIME ZONE 'Africa/Douala') AT TIME ZONE 'UTC', created_at)) AS t
      FROM detections WHERE hazard_id IS NOT NULL GROUP BY hazard_id) sub
WHERE h.id = sub.hazard_id AND h.last_seen_at IS NULL;

UPDATE hazards SET resolution_source = 'manual' WHERE status = 'resolved' AND resolution_source IS NULL;
