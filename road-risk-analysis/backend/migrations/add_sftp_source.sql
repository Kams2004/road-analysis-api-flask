-- SFTP video sources: server profile on cloud_connections (provider = 'sftp').
-- Applied automatically at API start-up (app/core/db/upgrade.py, SFTP_COLUMNS).
-- No password column: passwords live only in infra/secrets/sftp.env.
ALTER TABLE cloud_connections ADD COLUMN IF NOT EXISTS sftp_host VARCHAR;
ALTER TABLE cloud_connections ADD COLUMN IF NOT EXISTS sftp_port INTEGER;
ALTER TABLE cloud_connections ADD COLUMN IF NOT EXISTS sftp_username VARCHAR;
ALTER TABLE cloud_connections ADD COLUMN IF NOT EXISTS sftp_base_path VARCHAR;
ALTER TABLE cloud_connections ADD COLUMN IF NOT EXISTS sftp_password_key VARCHAR;
ALTER TABLE cloud_connections ADD COLUMN IF NOT EXISTS sftp_host_key VARCHAR;
ALTER TABLE cloud_connections ADD COLUMN IF NOT EXISTS sftp_host_key_fp VARCHAR;
ALTER TABLE cloud_connections ADD COLUMN IF NOT EXISTS sftp_host_key_at TIMESTAMP;
