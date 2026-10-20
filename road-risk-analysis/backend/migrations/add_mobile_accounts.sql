-- Mobile accounts & Google sign-in (docs/spec-mobile-app.md, "Backend — mobile accounts")
--
-- Applied automatically at API startup by app/core/db/upgrade.py (idempotent);
-- this file mirrors it for review / manual runs.

-- ── New signalement types for the mobile "Report a hazard" screen ───────────
-- (run outside a transaction on PostgreSQL < 12)
ALTER TYPE signalementtype ADD VALUE IF NOT EXISTS 'nid_de_poule';
ALTER TYPE signalementtype ADD VALUE IF NOT EXISTS 'obstacle';
ALTER TYPE signalementtype ADD VALUE IF NOT EXISTS 'inondation';
ALTER TYPE signalementtype ADD VALUE IF NOT EXISTS 'autre';

-- ── Users: self-registered mobile accounts and Google identity ─────────────
ALTER TABLE users ADD COLUMN IF NOT EXISTS account_type VARCHAR NOT NULL DEFAULT 'staff';  -- staff | mobile
ALTER TABLE users ADD COLUMN IF NOT EXISTS google_sub VARCHAR;                             -- Google "sub" claim
CREATE UNIQUE INDEX IF NOT EXISTS ix_users_google_sub ON users (google_sub);
