"""
Idempotent in-place schema upgrade, run by init_db after create_all.

create_all creates missing TABLES but never alters existing ones, so new
COLUMNS / enum values on tables that already exist are added here with
`IF NOT EXISTS` statements — safe on every boot and on a fresh database
(where create_all already produced them and every statement is a no-op).
Each block is mirrored in backend/migrations/ for manual / review purposes.
"""
import logging

from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

# ── Module 6 — hazard validation (migrations/add_module6_validation.sql) ─────
# ALTER TYPE … ADD VALUE cannot be used in the transaction that adds it, so
# enum values run on their own autocommit connection first.
MODULE6_ENUM_VALUES = [
    "ALTER TYPE reviewstatus ADD VALUE IF NOT EXISTS 'location_pending'",
    "ALTER TYPE reviewstatus ADD VALUE IF NOT EXISTS 'corrected'",
]

MODULE6_COLUMNS = [
    "ALTER TABLE detections ADD COLUMN IF NOT EXISTS location_status VARCHAR",
    "ALTER TABLE detections ADD COLUMN IF NOT EXISTS location_source VARCHAR",
    "ALTER TABLE detections ADD COLUMN IF NOT EXISTS country VARCHAR",
    "ALTER TABLE detections ADD COLUMN IF NOT EXISTS city VARCHAR",
    "ALTER TABLE detections ADD COLUMN IF NOT EXISTS bbox_x1 DOUBLE PRECISION",
    "ALTER TABLE detections ADD COLUMN IF NOT EXISTS bbox_y1 DOUBLE PRECISION",
    "ALTER TABLE detections ADD COLUMN IF NOT EXISTS bbox_x2 DOUBLE PRECISION",
    "ALTER TABLE detections ADD COLUMN IF NOT EXISTS bbox_y2 DOUBLE PRECISION",
    "ALTER TABLE detections ADD COLUMN IF NOT EXISTS video_time_s DOUBLE PRECISION",
    "ALTER TABLE detections ADD COLUMN IF NOT EXISTS model_name VARCHAR",
    "ALTER TABLE detections ADD COLUMN IF NOT EXISTS model_version VARCHAR",
    "ALTER TABLE detections ADD COLUMN IF NOT EXISTS predicted_type VARCHAR",
    "ALTER TABLE detections ADD COLUMN IF NOT EXISTS predicted_subtype VARCHAR",
    "ALTER TABLE detections ADD COLUMN IF NOT EXISTS rejection_code VARCHAR",
    "ALTER TABLE detections ADD COLUMN IF NOT EXISTS rejection_source VARCHAR",
    "ALTER TABLE detections ADD COLUMN IF NOT EXISTS hazard_id VARCHAR REFERENCES hazards(id) ON DELETE SET NULL",
    "CREATE INDEX IF NOT EXISTS ix_detections_hazard_id ON detections (hazard_id)",
    "CREATE INDEX IF NOT EXISTS ix_detections_review_status ON detections (review_status)",
]

# Back-fill rows created before Module 6. Guarded by `location_status IS
# NULL` / `predicted_type IS NULL`, so each row is touched exactly once.
MODULE6_BACKFILL = [
    """UPDATE detections SET location_status = 'missing', location_source = 'none',
              review_status = CASE WHEN review_status = 'pending' THEN 'location_pending'::reviewstatus
                                   ELSE review_status END
       WHERE location_status IS NULL AND (latitude IS NULL OR longitude IS NULL)""",
    """UPDATE detections SET location_status = 'approximate', location_source = 'interpolated',
              review_status = CASE WHEN review_status = 'pending' THEN 'location_pending'::reviewstatus
                                   ELSE review_status END
       WHERE location_status IS NULL AND gps_interpolated""",
    """UPDATE detections SET location_status = 'confirmed', location_source = 'ocr'
       WHERE location_status IS NULL""",
    """UPDATE detections SET predicted_type = type, predicted_subtype = subtype
       WHERE predicted_type IS NULL""",
]

# ── Module 7 — ingestion & processing jobs (migrations/add_module7_jobs.sql) ─
# New tables (cloud_connections, stream_sources, job_events) come from create_all.
MODULE7_ENUM_VALUES = [
    "ALTER TYPE jobstatus ADD VALUE IF NOT EXISTS 'cancelled'",
]

MODULE7_COLUMNS = [
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS source_type VARCHAR NOT NULL DEFAULT 'upload'",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS source_ref VARCHAR",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS source_name VARCHAR",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS connection_id VARCHAR REFERENCES cloud_connections(id) ON DELETE SET NULL",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS stream_id VARCHAR REFERENCES stream_sources(id) ON DELETE SET NULL",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS source_path VARCHAR",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS submitted_by VARCHAR",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS started_at TIMESTAMP",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS cancel_requested BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMP",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS cancelled_by VARCHAR",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS max_attempts INTEGER NOT NULL DEFAULT 3",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS celery_task_id VARCHAR",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS avg_confidence DOUBLE PRECISION",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS frame_skip INTEGER",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS detections_by_model JSON",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS fps DOUBLE PRECISION",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS duration_s DOUBLE PRECISION",
    "CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs (status)",
    "CREATE INDEX IF NOT EXISTS ix_jobs_created_at ON jobs (created_at)",
    "CREATE INDEX IF NOT EXISTS ix_jobs_connection_id ON jobs (connection_id)",
    "CREATE INDEX IF NOT EXISTS ix_jobs_stream_id ON jobs (stream_id)",
    # Lookup of the jobs of a cloud file. Not unique any more: a file whose
    # content changed may be imported again (dedupe by version lives in
    # cloud_import_items — see CLOUD_IMPORT_COLUMNS).
    "CREATE INDEX IF NOT EXISTS ix_jobs_connection_source_ref ON jobs (connection_id, source_ref)",
]

# ── Mobile accounts (migrations/add_mobile_accounts.sql) ─────────────────────
MOBILE_ENUM_VALUES = [
    "ALTER TYPE signalementtype ADD VALUE IF NOT EXISTS 'nid_de_poule'",
    "ALTER TYPE signalementtype ADD VALUE IF NOT EXISTS 'obstacle'",
    "ALTER TYPE signalementtype ADD VALUE IF NOT EXISTS 'inondation'",
    "ALTER TYPE signalementtype ADD VALUE IF NOT EXISTS 'autre'",
    # Mobile report menu v2 (migrations/add_signalement_types_v2.sql)
    "ALTER TYPE signalementtype ADD VALUE IF NOT EXISTS 'vehicule_panne'",
    "ALTER TYPE signalementtype ADD VALUE IF NOT EXISTS 'dos_d_ane'",
    "ALTER TYPE signalementtype ADD VALUE IF NOT EXISTS 'route_glissante'",
    "ALTER TYPE signalementtype ADD VALUE IF NOT EXISTS 'eboulement'",
    "ALTER TYPE signalementtype ADD VALUE IF NOT EXISTS 'travaux'",
    "ALTER TYPE signalementtype ADD VALUE IF NOT EXISTS 'animaux'",
    "ALTER TYPE signalementtype ADD VALUE IF NOT EXISTS 'vehicule_dangereux'",
]

MOBILE_COLUMNS = [
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS account_type VARCHAR NOT NULL DEFAULT 'staff'",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS google_sub VARCHAR",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_google_sub ON users (google_sub)",
]

# ── Community votes (migrations/add_signalement_votes.sql) ──────────────────
# The signalement_votes / signalement_passes tables come from create_all.
COMMUNITY_COLUMNS = [
    "ALTER TABLE signalements ADD COLUMN IF NOT EXISTS present_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE signalements ADD COLUMN IF NOT EXISTS absent_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE signalements ADD COLUMN IF NOT EXISTS invalid_vote_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE signalements ADD COLUMN IF NOT EXISTS last_confirmed_at TIMESTAMP",
    "ALTER TABLE signalements ADD COLUMN IF NOT EXISTS ignore_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE signalements ADD COLUMN IF NOT EXISTS uncertain BOOLEAN NOT NULL DEFAULT FALSE",
    "CREATE INDEX IF NOT EXISTS ix_signalements_status ON signalements (status)",
]

# ── Two-stage pipeline & AI validation mode (migrations/add_ai_validation.sql) ─
AI_VALIDATION_COLUMNS = [
    "ALTER TABLE detections ADD COLUMN IF NOT EXISTS model_card_version VARCHAR",
    "ALTER TABLE detections ADD COLUMN IF NOT EXISTS verifier_score DOUBLE PRECISION",
    "ALTER TABLE detections ADD COLUMN IF NOT EXISTS verifier_model VARCHAR",
    "ALTER TABLE detections ADD COLUMN IF NOT EXISTS verifier_card_version VARCHAR",
    "ALTER TABLE detections ADD COLUMN IF NOT EXISTS ai_decision VARCHAR",
    "ALTER TABLE detections ADD COLUMN IF NOT EXISTS ai_decision_reason VARCHAR",
    "ALTER TABLE detections ADD COLUMN IF NOT EXISTS audit_sample BOOLEAN NOT NULL DEFAULT FALSE",
    "CREATE INDEX IF NOT EXISTS ix_detections_ai_decision ON detections (ai_decision)",
    "CREATE INDEX IF NOT EXISTS ix_detections_audit_sample ON detections (audit_sample) WHERE audit_sample",
]

# ── Two-stage traffic-sign pipeline (migrations/add_sign_classifier.sql) ─────
SIGN_CLASSIFIER_COLUMNS = [
    "ALTER TABLE detections ADD COLUMN IF NOT EXISTS class_candidates JSON",
]


# ── Hazard lifecycle (migrations/add_hazard_lifecycle.sql) ───────────────────
# The hazard_events / hazard_votes tables come from create_all.
HAZARD_LIFECYCLE_COLUMNS = [
    "ALTER TABLE hazards ADD COLUMN IF NOT EXISTS resolution_source VARCHAR",
    "ALTER TABLE hazards ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMP",
    "ALTER TABLE hazards ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP",
    "ALTER TABLE hazards ADD COLUMN IF NOT EXISTS absence_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE hazards ADD COLUMN IF NOT EXISTS recurring_spot BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE hazards ADD COLUMN IF NOT EXISTS recurrence_count INTEGER NOT NULL DEFAULT 0",
    "CREATE INDEX IF NOT EXISTS ix_hazards_last_seen_at ON hazards (last_seen_at)",
    "CREATE INDEX IF NOT EXISTS ix_hazards_expires_at ON hazards (expires_at)",
    "ALTER TABLE hazard_events ADD COLUMN IF NOT EXISTS detection_id VARCHAR",
    "CREATE INDEX IF NOT EXISTS ix_hazard_events_detection_id ON hazard_events (detection_id)",
]

# Back-fill: last sighting = latest member recording time (OSD time is the
# camera's local time, Africa/Douala; else processing time). Resolved
# hazards resolved before the lifecycle existed were resolved by hand.
# Guarded by IS NULL, so each row is touched once.
HAZARD_LIFECYCLE_BACKFILL = [
    """UPDATE hazards h SET last_seen_at = sub.t
       FROM (SELECT hazard_id,
                    MAX(COALESCE((captured_at AT TIME ZONE 'Africa/Douala') AT TIME ZONE 'UTC', created_at)) AS t
             FROM detections WHERE hazard_id IS NOT NULL GROUP BY hazard_id) sub
       WHERE h.id = sub.hazard_id AND h.last_seen_at IS NULL""",
    """UPDATE hazards SET resolution_source = 'manual'
       WHERE status = 'resolved' AND resolution_source IS NULL""",
]

# ── Parked vehicles & recording time (migrations/add_parked_vehicle.sql) ────
# recorded_at: video recording time of the detection (OSD date-time of the
# stored frame, timezone-aware), every detection type. ai_details: the
# evidence of rule-based pipelines (parked vehicles: verdict, reasons,
# features, severity). job_track_points.recorded_at: same clock for each fix.
PARKED_VEHICLE_COLUMNS = [
    "ALTER TABLE detections ADD COLUMN IF NOT EXISTS recorded_at TIMESTAMPTZ",
    "ALTER TABLE detections ADD COLUMN IF NOT EXISTS ai_details JSON",
    "CREATE INDEX IF NOT EXISTS ix_detections_recorded_at ON detections (recorded_at)",
    "ALTER TABLE job_track_points ADD COLUMN IF NOT EXISTS recorded_at TIMESTAMPTZ",
]

# ── Structured reverse geocoding (migrations/add_structured_address.sql) ────
# Street / quartier / arrondissement / town / region + a short display label
# on detections and hazards (app.services.geocoding). detections.geocoded_at
# NULL = never looked up → candidate of the throttled back-fill
# (app.services.geocoding_backfill), never a bulk call from here.
ADDRESS_COLUMNS = [
    *(f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS {c} VARCHAR"
      for t in ("detections", "hazards")
      for c in ("road", "locality", "district", "town", "region", "display_location")),
    "ALTER TABLE detections ADD COLUMN IF NOT EXISTS geocoded_at TIMESTAMP",
    "CREATE INDEX IF NOT EXISTS ix_detections_town ON detections (town)",
    "CREATE INDEX IF NOT EXISTS ix_detections_locality ON detections (locality)",
    "CREATE INDEX IF NOT EXISTS ix_hazards_town ON hazards (town)",
    "CREATE INDEX IF NOT EXISTS ix_hazards_locality ON hazards (locality)",
]


# ── Global Cluster Configuration (migrations/add_cluster_zones.sql) ────────
# The cluster_zones table comes from create_all; the settings row
# `clustering` and the legacy-area → zone conversion are done once by
# app.services.clustering.service.migrate_legacy at start-up.
CLUSTER_ZONE_COLUMNS = [
    "ALTER TABLE hazards ADD COLUMN IF NOT EXISTS cluster_zone_id INTEGER",
    "ALTER TABLE hazards ADD COLUMN IF NOT EXISTS cluster_zone_name VARCHAR",
    "ALTER TABLE hazards ADD COLUMN IF NOT EXISTS cluster_environment VARCHAR",
    "ALTER TABLE signalements ADD COLUMN IF NOT EXISTS merged_into_id VARCHAR",
    "ALTER TABLE signalements ADD COLUMN IF NOT EXISTS report_count INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE signalements ADD COLUMN IF NOT EXISTS last_reported_at TIMESTAMP",
    "ALTER TABLE signalements ADD COLUMN IF NOT EXISTS cluster_zone_id INTEGER",
    "ALTER TABLE signalements ADD COLUMN IF NOT EXISTS cluster_environment VARCHAR",
    "ALTER TABLE signalements ADD COLUMN IF NOT EXISTS cluster_radius_m DOUBLE PRECISION",
    "CREATE INDEX IF NOT EXISTS ix_signalements_merged_into_id ON signalements (merged_into_id)",
    "CREATE INDEX IF NOT EXISTS ix_cluster_zones_bbox ON cluster_zones "
    "(bbox_min_lat, bbox_max_lat, bbox_min_lon, bbox_max_lon)",
]


# ── Cloud sources & imports (migrations/add_cloud_imports.sql) ─────────────
# cloud_import_batches / cloud_import_items come from create_all. Connections
# made before keep being synced: auto_import is back-filled from `enabled`.
CLOUD_IMPORT_COLUMNS = [
    "DROP INDEX IF EXISTS uq_jobs_connection_source_ref",
    "ALTER TABLE cloud_connections ADD COLUMN IF NOT EXISTS auto_import BOOLEAN",
    "UPDATE cloud_connections SET auto_import = enabled WHERE auto_import IS NULL",
    "ALTER TABLE cloud_connections ALTER COLUMN auto_import SET DEFAULT FALSE",
    "ALTER TABLE cloud_connections ADD COLUMN IF NOT EXISTS auto_import_models JSON",
    "ALTER TABLE cloud_connections ADD COLUMN IF NOT EXISTS auto_import_frame_skip INTEGER",
    "CREATE INDEX IF NOT EXISTS ix_cloud_import_items_conn_file ON cloud_import_items (connection_id, file_id)",
]

# ── SFTP video source (migrations/add_sftp_source.sql) ───────────────────────
SFTP_COLUMNS = [
    "ALTER TABLE cloud_connections ADD COLUMN IF NOT EXISTS sftp_host VARCHAR",
    "ALTER TABLE cloud_connections ADD COLUMN IF NOT EXISTS sftp_port INTEGER",
    "ALTER TABLE cloud_connections ADD COLUMN IF NOT EXISTS sftp_username VARCHAR",
    "ALTER TABLE cloud_connections ADD COLUMN IF NOT EXISTS sftp_base_path VARCHAR",
    "ALTER TABLE cloud_connections ADD COLUMN IF NOT EXISTS sftp_password_key VARCHAR",
    "ALTER TABLE cloud_connections ADD COLUMN IF NOT EXISTS sftp_host_key VARCHAR",
    "ALTER TABLE cloud_connections ADD COLUMN IF NOT EXISTS sftp_host_key_fp VARCHAR",
    "ALTER TABLE cloud_connections ADD COLUMN IF NOT EXISTS sftp_host_key_at TIMESTAMP",
]

# ── Training data (migrations/add_training_data.sql) ────────────────────────
# training_samples / training_frames come from create_all.
TRAINING_DATA_COLUMNS = [
    "ALTER TABLE detections ADD COLUMN IF NOT EXISTS frame_clean_url VARCHAR",
    "ALTER TABLE detections ADD COLUMN IF NOT EXISTS frame_width INTEGER",
    "ALTER TABLE detections ADD COLUMN IF NOT EXISTS frame_height INTEGER",
    "ALTER TABLE detections ADD COLUMN IF NOT EXISTS corrected_bbox JSON",
]

async def run(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        for stmt in MODULE6_ENUM_VALUES + MODULE7_ENUM_VALUES + MOBILE_ENUM_VALUES:
            await conn.exec_driver_sql(stmt)
    async with engine.begin() as conn:
        for stmt in (MODULE6_COLUMNS + MODULE6_BACKFILL + MODULE7_COLUMNS + MOBILE_COLUMNS + COMMUNITY_COLUMNS
                     + AI_VALIDATION_COLUMNS + SIGN_CLASSIFIER_COLUMNS
                     + HAZARD_LIFECYCLE_COLUMNS + HAZARD_LIFECYCLE_BACKFILL + PARKED_VEHICLE_COLUMNS
                     + ADDRESS_COLUMNS + CLUSTER_ZONE_COLUMNS + CLOUD_IMPORT_COLUMNS
                     + TRAINING_DATA_COLUMNS + SFTP_COLUMNS):
            await conn.exec_driver_sql(stmt)
    logger.info("Schema upgrade checked")
