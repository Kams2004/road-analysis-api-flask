-- Community "still there / not there" votes on signalements, with the
-- voter's position (only votes cast within community.vote_max_distance_m
-- count; the others are kept for review as invalid / false testimony).
--
-- Applied automatically at API startup by app/db/upgrade.py (idempotent);
-- this file mirrors it for review / manual runs.

CREATE TABLE IF NOT EXISTS signalement_votes (
    id               VARCHAR PRIMARY KEY,
    signalement_id   VARCHAR NOT NULL REFERENCES signalements(id) ON DELETE CASCADE,
    voter_user_id    VARCHAR REFERENCES users(id) ON DELETE SET NULL,
    voter_device_id  VARCHAR,                       -- vehicle / device id sent by the app
    vote             VARCHAR NOT NULL,              -- present | absent
    latitude         DOUBLE PRECISION,
    longitude        DOUBLE PRECISION,
    distance_m       DOUBLE PRECISION,              -- voter → report, metres
    counted          BOOLEAN NOT NULL DEFAULT FALSE,
    reason           VARCHAR,                       -- NULL | too_far | duplicate | no_position
    created_at       TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_signalement_votes_signalement_id  ON signalement_votes (signalement_id);
CREATE INDEX IF NOT EXISTS ix_signalement_votes_voter_user_id   ON signalement_votes (voter_user_id);
CREATE INDEX IF NOT EXISTS ix_signalement_votes_voter_device_id ON signalement_votes (voter_device_id);

-- Counted-vote tallies and freshness on the report itself
ALTER TABLE signalements ADD COLUMN IF NOT EXISTS present_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE signalements ADD COLUMN IF NOT EXISTS absent_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE signalements ADD COLUMN IF NOT EXISTS invalid_vote_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE signalements ADD COLUMN IF NOT EXISTS last_confirmed_at TIMESTAMP;
CREATE INDEX IF NOT EXISTS ix_signalements_status ON signalements (status);

-- Pass-by impressions: a device came within vote_max_distance_m of a
-- displayed report. Devices that pass without voting are "ignores";
-- community.ignore_threshold ignores within ignore_window_minutes mark the
-- report uncertain (hidden from the app when community.hide_when_ignored).
CREATE TABLE IF NOT EXISTS signalement_passes (
    id              VARCHAR PRIMARY KEY,
    signalement_id  VARCHAR NOT NULL REFERENCES signalements(id) ON DELETE CASCADE,
    device_id       VARCHAR,
    user_id         VARCHAR REFERENCES users(id) ON DELETE SET NULL,
    latitude        DOUBLE PRECISION,
    longitude       DOUBLE PRECISION,
    distance_m      DOUBLE PRECISION,
    counted         BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_signalement_passes_signalement_id ON signalement_passes (signalement_id);
CREATE INDEX IF NOT EXISTS ix_signalement_passes_device_id      ON signalement_passes (device_id);

ALTER TABLE signalements ADD COLUMN IF NOT EXISTS ignore_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE signalements ADD COLUMN IF NOT EXISTS uncertain BOOLEAN NOT NULL DEFAULT FALSE;
