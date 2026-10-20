"""
Driver votes on hazards — "still there" / "not there" (same spirit as the
community votes on signalements, app.services.signalement_votes).

Eligibility: a signed-in mobile user, the hazard active (not expired) and its
policy votes_enabled, the voter within hazard_lifecycle.vote_max_distance_m
of the hazard, and at most one counted vote per user per hazard per
vote_cooldown_minutes. Refused votes for distance / cooldown are still
stored (counted=false, reason) for administrators; drivers are only ever
told "thanks, recorded" — never any count.

Effects of a counted vote:
  still_there  `vote_still_there` event; with policy still_there_extends it
               is a sighting (last_seen_at = now → absences reset, expiry
               extended).
  not_there    `vote_not_there` event; distinct voters with a counted
               "not there" since the last sighting ≥ policy
               not_there_to_resolve → resolved with source `votes`.
"""
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.hazard import Hazard, HazardStatus, ResolutionSource
from app.models.hazard_lifecycle import HazardVote
from app.services.hazards.lifecycle import events
from app.services.hazards.lifecycle.expiry import is_expired
from app.services.hazards.lifecycle.settings import HazardLifecycleSettings
from app.services.osrm.geo import haversine_m

THANKS = "Thanks, your vote has been recorded."


# ── pure rules ────────────────────────────────────────────────────────────────

def vote_distance(hazard, latitude: Optional[float], longitude: Optional[float]) -> Optional[float]:
    if latitude is None or longitude is None:
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return round(haversine_m(hazard.latitude, hazard.longitude, latitude, longitude), 1)


def judge_vote(*, hazard_active: bool, votes_enabled: bool, distance_m: Optional[float], max_distance_m: float,
               last_vote_at: Optional[datetime], cooldown_minutes: int,
               now: Optional[datetime] = None) -> Optional[str]:
    """None when the vote counts, else the refusal code (checked in this
    order): hazard_resolved, votes_disabled, too_far, cooldown."""
    now = now or datetime.utcnow()
    if not hazard_active:
        return "hazard_resolved"
    if not votes_enabled:
        return "votes_disabled"
    if distance_m is None or distance_m > max_distance_m:
        return "too_far"
    if last_vote_at is not None and cooldown_minutes > 0 \
            and now - last_vote_at < timedelta(minutes=cooldown_minutes):
        return "cooldown"
    return None


def should_resolve(not_there_voters: int, threshold: int) -> bool:
    return not_there_voters >= threshold


# ── database ─────────────────────────────────────────────────────────────────

async def last_counted_vote_at(db: AsyncSession, hazard_id: str, user_id: str) -> Optional[datetime]:
    return (await db.execute(
        select(HazardVote.created_at).where(HazardVote.hazard_id == hazard_id, HazardVote.user_id == user_id,
                                            HazardVote.counted.is_(True))
        .order_by(HazardVote.created_at.desc()).limit(1)
    )).scalar_one_or_none()


async def cast_vote(db: AsyncSession, hazard: Hazard, vote: str, latitude: Optional[float],
                    longitude: Optional[float], cfg: HazardLifecycleSettings, *, principal,
                    device_id: Optional[str] = None, request: Optional[Request] = None,
                    now: Optional[datetime] = None) -> dict:
    """Record a vote and apply it. The caller commits (also on refusal: refused
    too_far / cooldown votes are stored). Returns {counted, reason, resolved}."""
    from app.services import audit
    from app.services.hazards.lifecycle import service
    now = now or datetime.utcnow()
    user = principal.user
    device_id = (device_id or "").strip()[:128] or None
    pol = await service.policy_of(db, cfg, hazard)
    active = hazard.status == HazardStatus.active.value and not is_expired(hazard.expires_at, now)
    distance = vote_distance(hazard, latitude, longitude)
    reason = judge_vote(hazard_active=active, votes_enabled=pol.votes_enabled, distance_m=distance,
                        max_distance_m=cfg.vote_max_distance_m,
                        last_vote_at=await last_counted_vote_at(db, hazard.id, user.id),
                        cooldown_minutes=cfg.vote_cooldown_minutes, now=now)
    details = {"vote": vote, "distance_m": distance, "max_distance_m": cfg.vote_max_distance_m,
               "device_id": device_id, "reason": reason}
    if reason in ("hazard_resolved", "votes_disabled"):
        audit.record(db, request, "hazard.vote", principal=principal, outcome="failure",
                     object_type="hazard", object_id=hazard.id, details=details)
        return {"counted": False, "reason": reason, "resolved": False}

    counted = reason is None
    db.add(HazardVote(hazard_id=hazard.id, user_id=user.id, device_id=device_id, vote=vote,
                      latitude=latitude, longitude=longitude, distance_m=distance,
                      counted=counted, reason=reason, created_at=now))
    audit.record(db, request, "hazard.vote", principal=principal, outcome="success" if counted else "failure",
                 object_type="hazard", object_id=hazard.id, details=details)
    if not counted:
        return {"counted": False, "reason": reason, "resolved": False}

    resolved = False
    if vote == "still_there":
        events.record(db, hazard.id, "vote_still_there", source="vote", user_id=user.id, recorded_at=now,
                      details={"distance_m": distance, "extends": pol.still_there_extends})
        if pol.still_there_extends and (hazard.last_seen_at is None or now > hazard.last_seen_at):
            hazard.last_seen_at = now
            await service.recount_absences(db, hazard)
            service.refresh_expiry(hazard, pol)
            hazard.updated_at = now
    else:
        events.record(db, hazard.id, "vote_not_there", source="vote", user_id=user.id, recorded_at=now,
                      details={"distance_m": distance})
        voters = await service.not_there_count(db, hazard)
        if should_resolve(voters, pol.not_there_to_resolve):
            await service.resolve(db, hazard, ResolutionSource.votes.value, evidence_at=now,
                                  note=f"{voters} driver(s) reported it is no longer there.",
                                  details={"not_there_votes": voters, "threshold": pol.not_there_to_resolve})
            resolved = True
    return {"counted": True, "reason": None, "resolved": resolved}
