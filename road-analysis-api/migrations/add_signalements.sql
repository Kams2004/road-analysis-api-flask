-- Migration: create signalements table
-- Run once on existing databases; SQLAlchemy create_all handles new installs.

CREATE TYPE IF NOT EXISTS signalementstype AS ENUM (
    'embouteillage','police','accident','danger',
    'route_fermee','voie_bloquee','probleme_de_carte',
    'mauvais_temps','prix_carburant','assistance_route','debogage'
);

CREATE TYPE IF NOT EXISTS signalementstatus AS ENUM ('actif','annule','rejete');

CREATE TABLE IF NOT EXISTS signalements (
    id               VARCHAR PRIMARY KEY,
    type             signalementstype  NOT NULL,
    status           signalementstatus NOT NULL DEFAULT 'actif',
    latitude         DOUBLE PRECISION  NOT NULL,
    longitude        DOUBLE PRECISION  NOT NULL,
    description      TEXT,
    image_url        VARCHAR,
    reported_by      VARCHAR,
    moderated_by     VARCHAR,
    moderated_at     TIMESTAMP,
    moderation_note  TEXT,
    reported_at      TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_signalements_status   ON signalements(status);
CREATE INDEX IF NOT EXISTS ix_signalements_type     ON signalements(type);
CREATE INDEX IF NOT EXISTS ix_signalements_latlon   ON signalements(latitude, longitude);
