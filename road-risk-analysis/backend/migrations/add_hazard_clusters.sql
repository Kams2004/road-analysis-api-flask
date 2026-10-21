-- Migration: cluster-first hazards (docs/CLUSTERS.md).
-- Applied automatically at boot by app/core/db/upgrade.py (HAZARD_CLUSTER_STATEMENTS);
-- mirrored here for manual review.
--
-- hazards.status is a plain string: the new values `candidate` (cluster of
-- unreviewed AI detections, code CL-…, never shown outside the validation
-- screen) and `dismissed` (candidate that disappeared unconfirmed) need no DDL.
-- Candidates take number = -nextval('hazard_candidates_seq') so they never
-- consume HZ codes; confirming one assigns nextval('hazards_number_seq').
CREATE SEQUENCE IF NOT EXISTS hazard_candidates_seq;

-- Content fingerprint ("sha256:<hex>") of a job's media: re-processing the same
-- video is refused with 409 duplicate_video unless forced.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS content_fingerprint VARCHAR;

-- Existing data: `python -m app.cli recluster-detections [--dry-run]` attaches the
-- detections to clusters, merges duplicate hazards and auto-validates the
-- re-sightings of confirmed hazards.
