-- Migration: per-device secret of a live vehicle (app/modules/m10_collision/device_auth.py).
-- Mirrors app/core/db/upgrade.py COLLISION_CONTEXT_STATEMENTS (run automatically at start-up). Idempotent.
-- With an API key, calls about a vehicle need its owner's X-Device-Secret; null = registered before.
ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS device_secret_hash VARCHAR;
