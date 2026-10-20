"""
Community "still there / not there" votes on signalements (Waze-style).

A vote counts only when the voter sent a position within
community.vote_max_distance_m of the report. Each voter (the signed-in user,
else the device id) has at most one counted vote per report — a later
counted vote replaces the earlier one (kept with reason "duplicate"). Votes
that do not count are still stored (reason "too_far" / "no_position") so
administrators can spot false testifiers.

Counted votes drive the report: present_count / absent_count (mirrored into
the legacy confirmations / not_there_votes), freshness (each counted
"present" keeps it visible community.confirm_extends_minutes after the
vote) and hiding (status annule, restorable by an admin) once absent_count
reaches community.hide_after_absent_votes.

Pass-bys (signalement_passes): the app reports when a device comes within
vote_max_distance_m of a displayed report. Devices that passed within
community.ignore_window_minutes and never voted are "ignores"; at
community.ignore_threshold ignores the report is flagged `uncertain` (unless
it has keep_visible_present_votes counted "present" votes) and, with
community.hide_when_ignored, hidden from the app.

This is the community validation mechanism; AI detections are validated
separately (AI confidence/severity routing + human review).
"""
from datetime import datetime, timedelta
from typing import Iterable, Optional

from fastapi import Request
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.m11_community.models.signalement import Signalement, SignalementStatus
from app.modules.m11_community.models.signalement_vote import INVALID_REASONS, SignalementPass, SignalementVote
from app.core import audit
from app.modules.m03_maps.osrm.geo import haversine_m
from app.core.settings_service import CommunitySettings

AUTO_CANCEL_ACTOR = "community"


# ── pure helpers ─────────────────────────────────────────────────────────────

def judge_vote(distance_m: Optional[float], max_distance_m: float) -> tuple[bool, Optional[str]]:
    """(counted, reason) for a vote before duplicate handling."""
    if distance_m is None:
        return False, "no_position"
    if distance_m > max_distance_m:
        return False, "too_far"
    return True, None


def vote_distance(sig: Signalement, latitude: Optional[float], longitude: Optional[float]) -> Optional[float]:
    if latitude is None or longitude is None:
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return round(haversine_m(sig.latitude, sig.longitude, latitude, longitude), 1)


def expires_at(sig: Signalement, cfg: CommunitySettings) -> datetime:
    type_value = sig.type.value if hasattr(sig.type, "value") else str(sig.type)
    # A merged re-report (clustering) restarts the freshness like a new report.
    reported = max(t for t in (sig.reported_at, getattr(sig, "last_reported_at", None)) if t is not None)
    base = reported + timedelta(hours=cfg.expiry_hours(type_value))
    if sig.last_confirmed_at is not None:
        extended = sig.last_confirmed_at + timedelta(minutes=cfg.confirm_extends_minutes)
        return max(base, extended)
    return base


def is_fresh(sig: Signalement, cfg: CommunitySettings, now: Optional[datetime] = None) -> bool:
    return expires_at(sig, cfg) > (now or datetime.utcnow())


def is_visible(sig: Signalement, cfg: CommunitySettings, now: Optional[datetime] = None) -> bool:
    """Shown to mobile users: active, not expired, not hidden as ignored."""
    if sig.status != SignalementStatus.actif or not is_fresh(sig, cfg, now):
        return False
    return not (cfg.hide_when_ignored and sig.uncertain)


def annotate(sigs: Iterable[Signalement], cfg: CommunitySettings, now: Optional[datetime] = None) -> None:
    """Attach the computed expires_at / expired (read by SignalementOut)."""
    now = now or datetime.utcnow()
    for sig in sigs:
        exp = expires_at(sig, cfg)
        sig.expires_at = exp      # type: ignore[attr-defined]  — transient, not a column
        sig.expired = exp <= now  # type: ignore[attr-defined]


def voter_filter(user_id: Optional[str], device_id: Optional[str]):
    """SQL condition selecting one voter's rows, or None when unidentifiable."""
    if user_id:
        return SignalementVote.voter_user_id == user_id
    if device_id:
        return and_(SignalementVote.voter_user_id.is_(None), SignalementVote.voter_device_id == device_id)
    return None


# ── database operations ──────────────────────────────────────────────────────

async def my_vote(db: AsyncSession, signalement_id: str, user_id: Optional[str], device_id: Optional[str]) -> Optional[str]:
    cond = voter_filter(user_id, device_id)
    if cond is None:
        return None
    return (await db.execute(
        select(SignalementVote.vote)
        .where(SignalementVote.signalement_id == signalement_id, SignalementVote.counted.is_(True), cond)
        .order_by(SignalementVote.created_at.desc()).limit(1)
    )).scalar_one_or_none()


async def recount(db: AsyncSession, sig: Signalement) -> None:
    rows = (await db.execute(
        select(SignalementVote.vote, SignalementVote.counted, SignalementVote.reason, func.count())
        .where(SignalementVote.signalement_id == sig.id)
        .group_by(SignalementVote.vote, SignalementVote.counted, SignalementVote.reason)
    )).all()
    present = sum(n for vote, counted, _, n in rows if counted and vote == "present")
    absent = sum(n for vote, counted, _, n in rows if counted and vote == "absent")
    invalid = sum(n for _, counted, reason, n in rows if not counted and reason in INVALID_REASONS)
    sig.present_count, sig.absent_count, sig.invalid_vote_count = present, absent, invalid
    sig.confirmations, sig.not_there_votes = present, absent


async def recount_ignores(db: AsyncSession, sig: Signalement, cfg: CommunitySettings,
                          now: Optional[datetime] = None) -> None:
    """ignore_count = devices with a counted pass inside the ignore window
    that never voted on this report; sets `uncertain` accordingly."""
    now = now or datetime.utcnow()
    since = now - timedelta(minutes=cfg.ignore_window_minutes)
    passes = (await db.execute(
        select(SignalementPass.device_id, SignalementPass.user_id)
        .where(SignalementPass.signalement_id == sig.id, SignalementPass.counted.is_(True),
               SignalementPass.created_at >= since)
    )).all()
    voters = (await db.execute(
        select(SignalementVote.voter_device_id, SignalementVote.voter_user_id)
        .where(SignalementVote.signalement_id == sig.id)
    )).all()
    voted_devices = {d for d, _ in voters if d}
    voted_users = {u for _, u in voters if u}
    ignorers = {
        device or f"user:{user}"
        for device, user in passes
        if not ((device and device in voted_devices) or (user and user in voted_users))
    }
    was = bool(sig.uncertain)
    sig.ignore_count = len(ignorers)
    sig.uncertain = (sig.ignore_count >= cfg.ignore_threshold
                     and (sig.present_count or 0) < cfg.keep_visible_present_votes)
    if sig.uncertain and not was:
        audit.record_system(db, "signalement.uncertain", object_type="signalement", object_id=sig.id,
                            details={"ignore_count": sig.ignore_count, "threshold": cfg.ignore_threshold,
                                     "window_minutes": cfg.ignore_window_minutes,
                                     "hidden": cfg.hide_when_ignored})


async def record_pass(
    db: AsyncSession,
    sig: Signalement,
    latitude: Optional[float],
    longitude: Optional[float],
    device_id: Optional[str],
    cfg: CommunitySettings,
    *,
    principal=None,
) -> dict:
    """Record a pass-by impression (one per device per report per ignore
    window). The caller commits. Returns {pass_row, counted, distance_m, duplicate}."""
    now = datetime.utcnow()
    user = getattr(principal, "user", None)
    user_id = user.id if user is not None else None
    device_id = (device_id or "").strip()[:128] or None
    if device_id is None and user_id is None:
        raise ValueError("device_id is required")

    distance = vote_distance(sig, latitude, longitude)
    counted = distance is not None and distance <= cfg.vote_max_distance_m
    since = now - timedelta(minutes=cfg.ignore_window_minutes)
    who = SignalementPass.device_id == device_id if device_id else SignalementPass.user_id == user_id
    existing = (await db.execute(
        select(SignalementPass)
        .where(SignalementPass.signalement_id == sig.id, who, SignalementPass.created_at >= since)
        .order_by(SignalementPass.created_at.desc()).limit(1)
    )).scalar_one_or_none()

    duplicate = existing is not None
    if existing is None:
        row = SignalementPass(signalement_id=sig.id, device_id=device_id, user_id=user_id,
                              latitude=latitude, longitude=longitude, distance_m=distance,
                              counted=counted, created_at=now)
        db.add(row)
    else:
        row = existing
        if counted and not existing.counted:   # first pass was out of range — upgrade it
            row.latitude, row.longitude, row.distance_m, row.counted = latitude, longitude, distance, True
        counted = row.counted
        distance = row.distance_m
    await db.flush()
    await recount_ignores(db, sig, cfg, now)
    return {"pass_row": row, "counted": counted, "distance_m": distance, "duplicate": duplicate}


async def cast_vote(
    db: AsyncSession,
    sig: Signalement,
    vote: str,
    latitude: Optional[float],
    longitude: Optional[float],
    device_id: Optional[str],
    cfg: CommunitySettings,
    *,
    principal=None,
    request: Optional[Request] = None,
) -> dict:
    """Record one vote and apply its effects. The caller commits.
    Returns {vote_row, counted, reason, distance_m, my_vote, auto_cancelled}."""
    now = datetime.utcnow()
    user = getattr(principal, "user", None)
    user_id = user.id if user is not None else None
    device_id = (device_id or "").strip()[:128] or None

    distance = vote_distance(sig, latitude, longitude)
    counted, reason = judge_vote(distance, cfg.vote_max_distance_m)

    replaced: Optional[SignalementVote] = None
    cond = voter_filter(user_id, device_id)
    if counted and cond is not None:
        replaced = (await db.execute(
            select(SignalementVote)
            .where(SignalementVote.signalement_id == sig.id, SignalementVote.counted.is_(True), cond)
            .order_by(SignalementVote.created_at.desc()).limit(1)
        )).scalar_one_or_none()
        if replaced is not None:
            replaced.counted = False
            replaced.reason = "duplicate"

    row = SignalementVote(
        signalement_id=sig.id, voter_user_id=user_id, voter_device_id=device_id, vote=vote,
        latitude=latitude, longitude=longitude, distance_m=distance,
        counted=counted, reason=reason, created_at=now,
    )
    db.add(row)
    await db.flush()

    if counted and vote == "present":
        sig.last_confirmed_at = now
    await recount(db, sig)
    await recount_ignores(db, sig, cfg, now)

    auto_cancelled = False
    # Only a counted "absent" vote can cancel — a report an admin restored
    # is not taken down again by an unrelated "present" vote.
    if (counted and vote == "absent" and sig.status == SignalementStatus.actif
            and sig.absent_count >= cfg.hide_after_absent_votes):
        sig.status = SignalementStatus.annule
        sig.moderated_by = AUTO_CANCEL_ACTOR
        sig.moderated_at = now
        sig.moderation_note = (f"Hidden by the community: {sig.absent_count} 'not there' "
                               f"vote(s) (threshold {cfg.hide_after_absent_votes}).")
        auto_cancelled = True
        audit.record_system(db, "signalement.auto_cancel", object_type="signalement", object_id=sig.id,
                            details={"absent_count": sig.absent_count, "threshold": cfg.hide_after_absent_votes})

    details = {"vote": vote, "counted": counted, "reason": reason, "distance_m": distance,
               "device_id": device_id, "max_distance_m": cfg.vote_max_distance_m}
    if replaced is not None:
        details["replaced_vote"] = {"id": replaced.id, "vote": replaced.vote}
    audit.record(db, request, "signalement.vote.replaced" if replaced is not None else "signalement.vote",
                 principal=principal, outcome="success" if counted else "failure",
                 object_type="signalement", object_id=sig.id, details=details,
                 actor_label=None if user is not None else (f"device:{device_id}" if device_id else None))

    current = vote if counted else await my_vote(db, sig.id, user_id, device_id)
    return {"vote_row": row, "counted": counted, "reason": reason, "distance_m": distance,
            "my_vote": current, "auto_cancelled": auto_cancelled}
