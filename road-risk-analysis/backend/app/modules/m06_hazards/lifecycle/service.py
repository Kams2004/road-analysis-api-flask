"""
Hazard lifecycle orchestration.

  resolve(hazard, source, …)   active → resolved; every accepted member
                               detection is resolved exactly like the manual
                               resolve (history row + `detection.resolve`
                               audit; actor "system" for automatic sources),
                               plus a `resolved` event and, for automatic
                               sources, a `hazard.resolve` system audit entry.
  reopen(hazard, …)            resolved → active; resolved members return to
                               their accepted status (`unresolve`).
  apply_sighting(hazard, det)  a later detection of the same kind near an
                               ACTIVE hazard: last_seen_at = its recording
                               time when newer, absences recounted, expiry
                               extended, `seen` event (once per detection).
  on_attached / place_new      hooks of hazard_service.attach (accepted
                               detections): sighting, recurring flag, stale
                               evidence, reopen-on-sighting.
  filter_for_drivers           mobile / alert visibility.

The absence counter is DERIVED, not incremented: absence_count = valid
"not seen" passes recorded after last_seen_at, so a sighting resets it and
late-processed videos are counted in recording order. "not there" votes are
counted the same way (since the last sighting).
"""
import logging
from datetime import datetime
from typing import Any, Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.m06_hazards.models.detection import ACCEPTED_STATUSES, Detection, ReviewStatus
from app.modules.m06_hazards.models.hazard import Hazard, HazardStatus, ResolutionSource
from app.modules.m06_hazards.models.hazard_lifecycle import HazardEvent, HazardVote
from app.modules.m06_hazards.lifecycle import events
from app.modules.m06_hazards.lifecycle.expiry import compute_expires_at, is_expired
from app.modules.m06_hazards.lifecycle.policy import (
    EffectivePolicy, policy_for_hazard, resolve_policy, vehicle_class_of,
)
from app.modules.m06_hazards.lifecycle.settings import HazardLifecycleSettings
from app.modules.m06_hazards.lifecycle.timeutil import recording_time

logger = logging.getLogger(__name__)

AUTOMATIC_SOURCES = (ResolutionSource.video_absence.value, ResolutionSource.votes.value,
                     ResolutionSource.expiry.value)


async def load_settings(db: AsyncSession, fresh: bool = False) -> HazardLifecycleSettings:
    from app.core.settings_service import get_cached, get_section
    if fresh:
        return await get_section(db, "hazard_lifecycle")  # type: ignore[return-value]
    return await get_cached(db, "hazard_lifecycle")  # type: ignore[return-value]


def det_time(det, cfg: HazardLifecycleSettings) -> Optional[datetime]:
    return recording_time(det, cfg.timezone, cfg.osd_time_is_local)


# ── pure helpers ─────────────────────────────────────────────────────────────

def placement(det_recorded_at: Optional[datetime], resolution_evidence_at: Optional[datetime],
              reopen_on_sighting: bool, reopen_window_hours: Optional[float] = None) -> str:
    """What an accepted detection near a RESOLVED hazard (and no active one) does:
      history  recorded before the resolution evidence → joins the resolved
               hazard as history (resolved at once, never shown);
      reopen   newer, the policy reopens on a sighting and it comes within
               reopen_window_hours of the resolution evidence (None = no limit);
      new      otherwise a new hazard is opened (hazard_service default)."""
    if det_recorded_at is not None and resolution_evidence_at is not None \
            and det_recorded_at <= resolution_evidence_at:
        return "history"
    if not reopen_on_sighting:
        return "new"
    if reopen_window_hours is not None and det_recorded_at is not None and resolution_evidence_at is not None \
            and (det_recorded_at - resolution_evidence_at).total_seconds() > reopen_window_hours * 3600:
        return "new"
    return "reopen"


def driver_visible(hazard: Optional[Hazard], pol: EffectivePolicy, now: Optional[datetime] = None) -> bool:
    """Shown in the mobile app / alerts: the policy shows the type, and the
    hazard (when the detection belongs to one) is active and not expired."""
    if not pol.show_to_drivers:
        return False
    if hazard is None:
        return True
    return hazard.status == HazardStatus.active.value and not is_expired(hazard.expires_at, now)


def count_since(times: Iterable[Optional[datetime]], since: Optional[datetime]) -> int:
    return sum(1 for t in times if t is not None and (since is None or t > since))


# ── state updates ────────────────────────────────────────────────────────────

def refresh_expiry(hazard: Hazard, pol: EffectivePolicy) -> None:
    hazard.expires_at = compute_expires_at(hazard.last_seen_at, pol.expiry_hours)


async def recount_absences(db: AsyncSession, hazard: Hazard) -> int:
    await db.flush()
    times = (await db.execute(select(HazardEvent.recorded_at).where(
        HazardEvent.hazard_id == hazard.id, HazardEvent.kind == "not_seen"))).scalars().all()
    hazard.absence_count = count_since(times, hazard.last_seen_at)
    return hazard.absence_count


async def not_there_count(db: AsyncSession, hazard: Hazard) -> int:
    """Distinct voters with a counted "not there" vote since the last sighting."""
    await db.flush()
    q = select(func.count(func.distinct(func.coalesce(HazardVote.user_id, HazardVote.device_id)))).where(
        HazardVote.hazard_id == hazard.id, HazardVote.vote == "not_there", HazardVote.counted.is_(True))
    if hazard.last_seen_at is not None:
        q = q.where(HazardVote.created_at > hazard.last_seen_at)
    if hazard.resolved_at is None:
        # votes cast before a reopen do not count again
        reopened = await events.last_of(db, hazard.id, "reopened")
        if reopened is not None:
            q = q.where(HazardVote.created_at > reopened.created_at)
    return int((await db.execute(q)).scalar() or 0)


async def policy_of(db: AsyncSession, cfg: HazardLifecycleSettings, hazard: Hazard,
                    members: Optional[list] = None) -> EffectivePolicy:
    from app.modules.m06_hazards import hazard_service
    if members is None:
        members = await hazard_service.members_of(db, hazard.id)
    return policy_for_hazard(cfg, hazard, members)


async def apply_sighting(db: AsyncSession, hazard: Hazard, det: Detection, cfg: HazardLifecycleSettings, *,
                         source: str, created: bool = False, members: Optional[list] = None,
                         pol: Optional[EffectivePolicy] = None) -> bool:
    """Record `det` as a sighting of the ACTIVE `hazard`. Idempotent per
    detection. Returns True when it moved last_seen_at forward."""
    if await events.exists(db, hazard.id, ("seen",), detection_id=det.id):
        return False
    pol = pol or await policy_of(db, cfg, hazard, members)
    when = det_time(det, cfg)
    newer = when is not None and (hazard.last_seen_at is None or when > hazard.last_seen_at)
    if newer:
        hazard.last_seen_at = when
    events.record(db, hazard.id, "seen", source=source, job_id=det.job_id, detection_id=det.id, recorded_at=when,
                  details={"created": created, "review_status": _status(det), "confidence": det.confidence,
                           "subtype": det.subtype, "newer": newer})
    await recount_absences(db, hazard)
    refresh_expiry(hazard, pol)
    hazard.updated_at = datetime.utcnow()
    return newer


def _status(det) -> Optional[str]:
    rs = det.review_status
    return rs.value if isinstance(rs, ReviewStatus) else rs


# ── resolve / reopen ─────────────────────────────────────────────────────────

async def resolve(db: AsyncSession, hazard: Hazard, source: str, *, actor=None, note: Optional[str] = None,
                  details: Optional[dict[str, Any]] = None, evidence_at: Optional[datetime] = None,
                  members: Optional[list] = None) -> int:
    """active → resolved. Returns the number of member detections resolved
    (0 and no change when the hazard is not active). The caller commits; a
    user-initiated (manual) resolve is audited by the route."""
    from app.core import audit
    from app.modules.m06_hazards import detection_workflow as wf, hazard_service
    if hazard.status != HazardStatus.active.value:
        return 0
    actor = actor or wf.SYSTEM
    if members is None:
        members = await hazard_service.members_of(db, hazard.id)
    det_note = note or f"hazard {hazard.code} resolved ({source})"
    n = 0
    for det in members:
        if det.review_status in ACCEPTED_STATUSES:
            await wf.resolve(db, det, actor, note=det_note)
            n += 1
    now = datetime.utcnow()
    hazard.status = HazardStatus.resolved.value
    hazard.resolved_at = now
    hazard.resolved_by = actor.name
    hazard.resolution_note = note
    hazard.resolution_source = source
    hazard.updated_at = now
    info = {"code": hazard.code, "detections": n, "source": source, **(details or {})}
    if source == ResolutionSource.expiry.value:
        events.record(db, hazard.id, "expired", source="expiry", recorded_at=hazard.expires_at or evidence_at,
                      details={k: v for k, v in info.items() if k not in ("code",)})
    user = getattr(getattr(actor, "principal", None), "user", None)
    events.record(db, hazard.id, "resolved", source=source, user_id=user.id if user else None,
                  recorded_at=evidence_at or now, details={**info, "by": actor.name, "note": note})
    if actor.type == "system":
        audit.record_system(db, "hazard.resolve", object_type="hazard", object_id=hazard.id, details=info)
    logger.info(f"[hazard] {hazard.code} resolved ({source}) — {n} detection(s)")
    return n


async def reopen(db: AsyncSession, hazard: Hazard, *, source: str, reason: Optional[str] = None, actor=None,
                 seen_at: Optional[datetime] = None, cfg: Optional[HazardLifecycleSettings] = None,
                 details: Optional[dict[str, Any]] = None) -> dict:
    """resolved → active. Returns the previous resolution. The caller commits;
    a manual reopen is audited by the route."""
    from app.core import audit
    from app.modules.m06_hazards import detection_workflow as wf, hazard_service
    actor = actor or wf.SYSTEM
    cfg = cfg or await load_settings(db)
    previous = {"resolved_at": hazard.resolved_at.isoformat() if hazard.resolved_at else None,
                "resolved_by": hazard.resolved_by, "resolution_note": hazard.resolution_note,
                "resolution_source": hazard.resolution_source}
    members = await hazard_service.members_of(db, hazard.id)
    n = 0
    for det in members:
        if det.review_status == ReviewStatus.resolved:
            await wf.unresolve(db, det, actor, note=reason)
            n += 1
    hazard.status = HazardStatus.active.value
    hazard.resolved_at = hazard.resolved_by = hazard.resolution_note = hazard.resolution_source = None
    when = seen_at or datetime.utcnow()
    if hazard.last_seen_at is None or when > hazard.last_seen_at:
        hazard.last_seen_at = when
    hazard.updated_at = datetime.utcnow()
    user = getattr(getattr(actor, "principal", None), "user", None)
    info = {"code": hazard.code, "detections": n, "reason": reason, "previous": previous, **(details or {})}
    events.record(db, hazard.id, "reopened", source=source, user_id=user.id if user else None,
                  recorded_at=when, details=info)
    await recount_absences(db, hazard)
    refresh_expiry(hazard, policy_for_hazard(cfg, hazard, members))
    if actor.type == "system":
        audit.record_system(db, "hazard.reopen", object_type="hazard", object_id=hazard.id, details=info)
    return previous


async def resolution_evidence_at(db: AsyncSession, hazard: Hazard) -> Optional[datetime]:
    """When the evidence that resolved the hazard happened (last pass
    recording time, vote time, expiry instant, manual resolve time)."""
    ev = await events.last_of(db, hazard.id, "resolved")
    if ev is not None and ev.recorded_at is not None:
        return ev.recorded_at
    return hazard.resolved_at


# ── hazard_service.attach hooks (accepted detections) ────────────────────────

async def latest_resolved_near(db: AsyncSession, det: Detection, ctx) -> Optional[Hazard]:
    """The most recently resolved hazard `det` would have joined."""
    from app.modules.m06_hazards import hazard_service
    if det.latitude is None or det.longitude is None:
        return None
    radius = ctx.radius_for(det.latitude, det.longitude, det.type)
    dlat, dlon = hazard_service._bbox(det.latitude, det.longitude, radius)
    rows = (await db.execute(select(Hazard).where(
        Hazard.status == HazardStatus.resolved.value, Hazard.type == det.type,
        Hazard.latitude.between(det.latitude - dlat, det.latitude + dlat),
        Hazard.longitude.between(det.longitude - dlon, det.longitude + dlon),
    ).order_by(Hazard.resolved_at.desc().nullslast()))).scalars().all()
    for h in rows:   # newest resolution first
        if hazard_service.pick_hazard([h], det.latitude, det.longitude, det.type, det.subtype,
                                      radius, ctx.distinct_subtype_types) is not None:
            return h
    return None


async def place_new(db: AsyncSession, det: Detection, ctx, *, accepted: bool = True) -> tuple[Optional[Hazard], str]:
    """No open cluster for `det`: (resolved hazard, "history" | "reopen") when
    it belongs to a resolved one, else (None, "new"). An UNREVIEWED detection
    never reopens a hazard (it opens a candidate instead); it joins a
    resolved hazard as history only when recorded before the resolution and
    the policy auto-confirms re-sightings (it is then auto-validated)."""
    cfg = await load_settings(db)
    resolved = await latest_resolved_near(db, det, ctx)
    if resolved is None:
        return None, "new"
    pol = resolve_policy(cfg, det.type, det.subtype, vehicle_class_of(det))
    mode = placement(det_time(det, cfg), await resolution_evidence_at(db, resolved), pol.reopen_on_sighting,
                     pol.reopen_window_hours)
    if not accepted and (mode == "reopen" or not pol.auto_confirm_resightings):
        return None, "new"
    if mode == "reopen":
        await reopen(db, resolved, source="sighting", seen_at=det_time(det, cfg), cfg=cfg,
                     reason=f"Seen again (detection {det.id})", details={"detection_id": det.id, "job_id": det.job_id})
    return (resolved, mode) if mode != "new" else (None, "new")


async def on_attached(db: AsyncSession, hazard: Hazard, det: Detection, *, created: bool) -> None:
    """After an accepted detection joined (or opened) an ACTIVE hazard."""
    from app.modules.m06_hazards import hazard_service
    from app.modules.m06_hazards.lifecycle import recurring
    cfg = await load_settings(db)
    members = await hazard_service.members_of(db, hazard.id)
    pol = policy_for_hazard(cfg, hazard, members)
    if created and pol.recurring_enabled:
        await recurring.flag_new_hazard(db, hazard, cfg)
    await apply_sighting(db, hazard, det, cfg, source="review", created=created, members=members, pol=pol)
    now = datetime.utcnow()
    if is_expired(hazard.expires_at, now):
        # Stale evidence (late-processed video) or an already-expired hazard:
        # history only, never shown to drivers.
        when = det_time(det, cfg)
        await resolve(db, hazard, ResolutionSource.expiry.value, evidence_at=hazard.expires_at, members=members,
                      note="Stale evidence: recorded longer ago than the expiry." if created
                      else f"Not seen for {pol.expiry_hours:g} h (expired).",
                      details={"stale_evidence": created, "detection_id": det.id,
                               "expiry_hours": pol.expiry_hours,
                               "evidence_age_hours": round((now - when).total_seconds() / 3600, 2) if when else None})


async def on_history_member(db: AsyncSession, hazard: Hazard, det: Detection) -> None:
    """A detection older than its resolved hazard's resolution: kept as
    history — resolved at once (an unreviewed one is first auto-validated as
    a re-sighting of the confirmed hazard)."""
    from app.modules.m06_hazards import detection_workflow as wf
    cfg = await load_settings(db)
    if not await events.exists(db, hazard.id, ("seen",), detection_id=det.id):
        events.record(db, hazard.id, "seen", source="review", job_id=det.job_id, detection_id=det.id,
                      recorded_at=det_time(det, cfg), details={"history": True, "newer": False})
    if det.review_status in (ReviewStatus.pending, ReviewStatus.location_pending):
        await wf.resight(db, det, hazard)
    if det.review_status in ACCEPTED_STATUSES:
        await wf.resolve(db, det, wf.SYSTEM, note=f"hazard {hazard.code} already resolved — recorded before "
                                                   "its resolution (history)")


async def on_candidate_member(db: AsyncSession, hazard: Hazard, det: Detection) -> None:
    """An unreviewed detection joined (or opened) a CANDIDATE cluster: it is a
    sighting of the cluster (last_seen_at, absences recounted) — candidates
    never expire by time, they are dismissed by absence passes only."""
    cfg = await load_settings(db)
    await apply_sighting(db, hazard, det, cfg, source="video")
    hazard.expires_at = None


async def dismiss(db: AsyncSession, hazard: Hazard, source: str, *, details: Optional[dict[str, Any]] = None,
                  evidence_at: Optional[datetime] = None, members: Optional[list] = None) -> int:
    """candidate → dismissed (it disappeared before anyone confirmed it). Its
    unreviewed detections are rejected automatically with reason
    `not_seen_again` (kind unusable: excluded from training) and stay
    attached as history. Never shown outside the validation screen. Returns
    the number of detections rejected."""
    from app.core import audit
    from app.modules.m06_hazards import detection_workflow as wf, hazard_service
    if hazard.status != HazardStatus.candidate.value:
        return 0
    if members is None:
        members = await hazard_service.members_of(db, hazard.id)
    now = datetime.utcnow()
    hazard.status = HazardStatus.dismissed.value
    hazard.resolved_at, hazard.resolved_by, hazard.resolution_source = now, "system", source
    hazard.updated_at = now
    n = 0
    for det in members:
        if det.review_status in (ReviewStatus.pending, ReviewStatus.location_pending):
            await wf.auto_reject(db, det, NOT_SEEN_AGAIN,
                                 note=f"cluster {hazard.code} not seen again ({source}) — never confirmed")
            n += 1
    info = {"code": hazard.code, "source": source, "rejected": n, **(details or {})}
    events.record(db, hazard.id, "dismissed", source=source, recorded_at=evidence_at or now, details=info)
    audit.record_system(db, "hazard.dismiss", object_type="hazard", object_id=hazard.id, details=info)
    logger.info(f"[hazard] candidate {hazard.code} dismissed ({source}) — {n} detection(s) rejected")
    return n


NOT_SEEN_AGAIN = "not_seen_again"


# ── driver visibility ────────────────────────────────────────────────────────

async def filter_for_drivers(db: AsyncSession, dets: list[Detection],
                             now: Optional[datetime] = None) -> list[Detection]:
    """Drop detections whose hazard is resolved / expired or whose policy
    hides them from drivers; annotate `alert_priority` (transient)."""
    if not dets:
        return dets
    cfg = await load_settings(db)
    ids = {d.hazard_id for d in dets if d.hazard_id}
    hazards = {h.id: h for h in (await db.execute(select(Hazard).where(Hazard.id.in_(ids)))).scalars().all()} \
        if ids else {}
    now = now or datetime.utcnow()
    out = []
    for d in dets:
        pol = resolve_policy(cfg, d.type, d.subtype, vehicle_class_of(d))
        if driver_visible(hazards.get(d.hazard_id) if d.hazard_id else None, pol, now):
            d.alert_priority = pol.alert_priority  # type: ignore[attr-defined] — transient, not a column
            out.append(d)
    return out
