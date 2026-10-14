-- Migration: create vehicles table
-- Run once on existing databases; SQLAlchemy create_all handles new installs.
--
-- This table is the durable vehicle registry only (identity + last_seen).
-- Live position/speed/heading are kept in Redis (see app/services/vehicle_service.py)
-- and are never persisted here.

-- Postgres has no CREATE TYPE ... IF NOT EXISTS; this is the standard
-- idempotent-enum-creation workaround.
DO $$ BEGIN
    CREATE TYPE vehicletype AS ENUM ('bus','taxi','truck','private','moto');
EXCEPTION
    WHEN duplicate_object THEN null;
END $$;

CREATE TABLE IF NOT EXISTS vehicles (
    id           VARCHAR PRIMARY KEY,
    vehicle_type vehicletype NOT NULL DEFAULT 'private',
    created_at   TIMESTAMP NOT NULL DEFAULT NOW(),
    last_seen    TIMESTAMP
);
