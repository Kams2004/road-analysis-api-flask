"""
Time-based expiry.

    expires_at = last_seen_at + policy.expiry_hours        (None = never)

A hazard whose expires_at has passed is resolved with source `expiry` by
the periodic sweep (Celery beat, every hazard_lifecycle.expiry_check_minutes)
and is hidden from drivers as soon as expires_at passes, even before the
sweep runs. Stale evidence — a detection whose recording time is already
older than the expiry when it is accepted (late-processed video) — opens or
keeps the hazard as history only: it is resolved `expiry` at once and never
shown (service.on_attached).
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.hazard import Hazard, HazardStatus
from app.services.hazards.lifecycle.settings import HazardLifecycleSettings, TypePolicy

logger = logging.getLogger(__name__)


# ── pure rules ────────────────────────────────────────────────────────────────

def compute_expires_at(last_seen_at: Optional[datetime], expiry_hours: Optional[float]) -> Optional[datetime]:
    if expiry_hours is None or last_seen_at is None:
        return None
    return last_seen_at + timedelta(hours=float(expiry_hours))


def is_expired(expires_at: Optional[datetime], now: Optional[datetime] = None) -> bool:
    return expires_at is not None and expires_at <= (now or datetime.utcnow())


def is_stale_evidence(recorded_at: Optional[datetime], expiry_hours: Optional[float],
                      now: Optional[datetime] = None) -> bool:
    """True when evidence recorded at `recorded_at` is already past the expiry."""
    return is_expired(compute_expires_at(recorded_at, expiry_hours), now)


def type_can_expire(policy: TypePolicy) -> bool:
    """Whether any subtype / vehicle class of this type has an expiry."""
    if policy.expiry_hours is not None or policy.heavy_vehicle_expiry_hours is not None:
        return True
    return any(o.get("expiry_hours") is not None or o.get("heavy_vehicle_expiry_hours") is not None
               for o in policy.subtype_overrides.values())


# ── periodic sweep ───────────────────────────────────────────────────────────

async def sweep(db: AsyncSession, cfg: Optional[HazardLifecycleSettings] = None,
                now: Optional[datetime] = None) -> int:
    """Refresh expires_at of every active hazard from the CURRENT policy (so a
    settings change applies within one period) and resolve the expired ones.
    Commits. Returns the number of hazards resolved."""
    from app.services import hazard_service
    from app.services.hazards.lifecycle import service
    from app.services.hazards.lifecycle.policy import policy_for_hazard, type_policy

    cfg = cfg or await service.load_settings(db, fresh=True)
    now = now or datetime.utcnow()
    types = (await db.execute(
        select(Hazard.type).where(Hazard.status == HazardStatus.active.value).distinct()
    )).scalars().all()
    resolved = 0
    for det_type in types:
        if not type_can_expire(type_policy(cfg, det_type)):
            await db.execute(update(Hazard).where(
                Hazard.status == HazardStatus.active.value, Hazard.type == det_type,
                Hazard.expires_at.isnot(None)).values(expires_at=None))
            continue
        hazards = (await db.execute(select(Hazard).where(
            Hazard.status == HazardStatus.active.value, Hazard.type == det_type))).scalars().all()
        for hazard in hazards:
            members = await hazard_service.members_of(db, hazard.id)
            pol = policy_for_hazard(cfg, hazard, members)
            hazard.expires_at = compute_expires_at(hazard.last_seen_at, pol.expiry_hours)
            if is_expired(hazard.expires_at, now):
                await service.resolve(db, hazard, "expiry", evidence_at=hazard.expires_at, members=members,
                                      note=f"Not seen for {pol.expiry_hours:g} h (expired).",
                                      details={"expiry_hours": pol.expiry_hours,
                                               "last_seen_at": hazard.last_seen_at.isoformat()
                                               if hazard.last_seen_at else None})
                resolved += 1
    await db.commit()
    if resolved:
        logger.info(f"[hazard-expiry] {resolved} hazard(s) resolved as expired")
    return resolved
