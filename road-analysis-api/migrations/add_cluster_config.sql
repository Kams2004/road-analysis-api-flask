CREATE TABLE IF NOT EXISTS cluster_config (
    id       INTEGER PRIMARY KEY DEFAULT 1,
    radius_m DOUBLE PRECISION NOT NULL DEFAULT 50.0
);

-- Seed the single config row if it doesn't exist yet
INSERT INTO cluster_config (id, radius_m)
VALUES (1, 50.0)
ON CONFLICT (id) DO NOTHING;
