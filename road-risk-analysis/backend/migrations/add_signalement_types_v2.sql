-- Additional signalement types for the mobile report menu.
--
-- Applied automatically at API startup by app/core/db/upgrade.py (idempotent);
-- this file mirrors it for review / manual runs.
-- (run outside a transaction on PostgreSQL < 12)
ALTER TYPE signalementtype ADD VALUE IF NOT EXISTS 'vehicule_panne';
ALTER TYPE signalementtype ADD VALUE IF NOT EXISTS 'dos_d_ane';
ALTER TYPE signalementtype ADD VALUE IF NOT EXISTS 'route_glissante';
ALTER TYPE signalementtype ADD VALUE IF NOT EXISTS 'eboulement';
ALTER TYPE signalementtype ADD VALUE IF NOT EXISTS 'travaux';
ALTER TYPE signalementtype ADD VALUE IF NOT EXISTS 'animaux';
ALTER TYPE signalementtype ADD VALUE IF NOT EXISTS 'vehicule_dangereux';
