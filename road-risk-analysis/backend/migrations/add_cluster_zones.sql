-- Global Cluster Configuration (Module 6, RRAP-06-009/010/011).
-- Mirrors app/db/upgrade.py CLUSTER_ZONE_COLUMNS; the API applies it on
-- every start (idempotent). The settings row `clustering` and the legacy
-- cluster_config / cluster_areas conversion are done once by
-- app.services.clustering.service.migrate_legacy (Python, at start-up).

CREATE TABLE IF NOT EXISTS cluster_zones (
    id                 SERIAL PRIMARY KEY,
    name               VARCHAR NOT NULL,
    kind               VARCHAR NOT NULL,              -- agglomeration | road_section | custom
    environment        VARCHAR NOT NULL,              -- urban | rural
    geometry           JSON NOT NULL,                 -- GeoJSON Polygon/MultiPolygon or LineString
    corridor_width_m   DOUBLE PRECISION,              -- road sections: total corridor width
    bbox_min_lat       DOUBLE PRECISION NOT NULL,
    bbox_min_lon       DOUBLE PRECISION NOT NULL,
    bbox_max_lat       DOUBLE PRECISION NOT NULL,
    bbox_max_lon       DOUBLE PRECISION NOT NULL,
    area_km2           DOUBLE PRECISION NOT NULL DEFAULT 0,
    center_lat         DOUBLE PRECISION,
    center_lon         DOUBLE PRECISION,
    circle_radius_km   DOUBLE PRECISION,
    source             VARCHAR NOT NULL DEFAULT 'drawn', -- nominatim | drawn | route | legacy_area
    source_ref         VARCHAR,
    source_meta        JSON,
    default_radius_m   DOUBLE PRECISION,              -- NULL = inherit the environment profile
    type_radius        JSON,
    signalement_radius JSON,
    priority           INTEGER NOT NULL DEFAULT 0,
    is_active          BOOLEAN NOT NULL DEFAULT TRUE,
    country            VARCHAR,
    city               VARCHAR,
    created_by         VARCHAR,
    created_at         TIMESTAMP NOT NULL DEFAULT now(),
    updated_at         TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_cluster_zones_kind ON cluster_zones (kind);
CREATE INDEX IF NOT EXISTS ix_cluster_zones_environment ON cluster_zones (environment);
CREATE INDEX IF NOT EXISTS ix_cluster_zones_is_active ON cluster_zones (is_active);
CREATE INDEX IF NOT EXISTS ix_cluster_zones_bbox ON cluster_zones
    (bbox_min_lat, bbox_max_lat, bbox_min_lon, bbox_max_lon);

-- Zone / environment used when a hazard was created ("Douala · urban · 25 m").
ALTER TABLE hazards ADD COLUMN IF NOT EXISTS cluster_zone_id INTEGER;
ALTER TABLE hazards ADD COLUMN IF NOT EXISTS cluster_zone_name VARCHAR;
ALTER TABLE hazards ADD COLUMN IF NOT EXISTS cluster_environment VARCHAR;

-- Community reports merged by the same configuration.
ALTER TABLE signalements ADD COLUMN IF NOT EXISTS merged_into_id VARCHAR;
ALTER TABLE signalements ADD COLUMN IF NOT EXISTS report_count INTEGER NOT NULL DEFAULT 1;
ALTER TABLE signalements ADD COLUMN IF NOT EXISTS last_reported_at TIMESTAMP;
ALTER TABLE signalements ADD COLUMN IF NOT EXISTS cluster_zone_id INTEGER;
ALTER TABLE signalements ADD COLUMN IF NOT EXISTS cluster_environment VARCHAR;
ALTER TABLE signalements ADD COLUMN IF NOT EXISTS cluster_radius_m DOUBLE PRECISION;
CREATE INDEX IF NOT EXISTS ix_signalements_merged_into_id ON signalements (merged_into_id);
