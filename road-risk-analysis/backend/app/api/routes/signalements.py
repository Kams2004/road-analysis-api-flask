import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Request, UploadFile, File, Form
from sqlalchemy import case, select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.signalement import Signalement, SignalementType, SignalementStatus
from app.models.signalement_vote import INVALID_REASONS, SignalementPass, SignalementVote
from app.models.user import User
from app.schemas.schemas import (
    SignalementOut, SignalementListOut, SignalementDetailOut,
    SignalementModerationIn, SignalementNearbyIn, AlongRouteQueryIn,
    SignalementVoteIn, SignalementLegacyVoteIn, SignalementVoteResultOut,
    SignalementVoteOut, SignalementVoteListOut, FlaggedVoterOut,
    SignalementPassIn, SignalementPassResultOut, SignalementPassOut,
)
from app.services.signalement_service import (
    find_merge_target, query_signalements_nearby, query_signalements_along_route,
)
from app.services.clustering import service as clustering_service
from app.services.minio_service import upload_signalement_image, upload_signalement_audio
from app.services.geocoding import reverse_geocode
from app.services import audit, signalement_votes as votes
from app.services.mobile_config import mobile_radius, route_corridor
from app.services.settings_service import CommunitySettings, get_cached

from app.api.deps import Principal, actor_name, api_error, get_principal
from app.api.openapi import err, errors, legacy

router = APIRouter()

_SIG_ID = Path(..., description="Report (signalement) id (UUID).",
               examples=["0b6f7a52-3c1e-4d7b-9a35-2f1e8c4d9a10"])
_NOT_FOUND = legacy(404, "Signalement not found")
_INACTIVE = err(409, "signalement_inactive", "This report is no longer active.", report_status="annule")


# ── Mobile: create a signalement (multipart to allow optional image) ──────────

@router.post("/", response_model=SignalementOut, status_code=201, summary="Create a community report")
async def create_signalement(
    type:            SignalementType   = Form(..., description="Report type (see `SignalementType`).",
                                              examples=["police"]),
    latitude:        float             = Form(..., description="Report position, WGS84 latitude (degrees).",
                                              examples=[4.0511]),
    longitude:       float             = Form(..., description="Report position, WGS84 longitude (degrees).",
                                              examples=[9.7679]),
    description:     Optional[str]     = Form(None, description="Free-text comment from the reporter."),
    reported_by:     Optional[str]     = Form(None, description=(
        "Reporter name. Ignored when a user is signed in (their e-mail is used instead).")),
    blocked_bearing: Optional[float]   = Form(None, description=(
        "For closures/blocked lanes: direction of travel that is blocked, degrees clockwise from north "
        "(0–360).")),
    image:           Optional[UploadFile] = File(None, description="Optional photo (stored in object storage)."),
    audio:           Optional[UploadFile] = File(None, description="Optional voice note (stored in object storage)."),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """
    **Mobile — report a road event** (`multipart/form-data`, so a photo and/or a voice note can be
    attached). The report starts `actif`, with its place name reverse-geocoded from the position and
    `expires_at` computed from the type's expiry (`community.expiry_hours_by_type`). When a user is
    signed in (API key + Bearer), they are recorded as the reporter whatever `reported_by` says.

    **Clustering** — the radius comes from the global Cluster Configuration (`GET /clustering/config`)
    resolved at the position for the report type (zone → urban / out-of-agglomeration profile →
    per-type value). When `merge_signalements` is on and an active, fresh report of the same type lies
    within that radius, the new report is stored but merged into it: `merged_into_id` = the existing
    report (which gets `report_count + 1` and restarts its freshness), and the new one is not listed
    by the app's nearby / along-route searches. `cluster_radius_m` / `cluster_environment` /
    `cluster_zone_id` record the resolution.
    """
    # A signed-in user (mobile app: API key + Bearer) is the reporter,
    # whatever free text the client sent.
    reported_by = actor_name(principal, reported_by)
    image_url = None
    if image:
        data = await image.read()
        ext  = (image.filename or "img.jpg").rsplit(".", 1)[-1].lower()
        object_name = f"signalements/{uuid.uuid4().hex}.{ext}"
        image_url = upload_signalement_image(data, object_name, image.content_type or "image/jpeg")

    audio_url = None
    if audio:
        data = await audio.read()
        ext  = (audio.filename or "audio.m4a").rsplit(".", 1)[-1].lower()
        object_name = f"signalements/audio/{uuid.uuid4().hex}.{ext}"
        audio_url = upload_signalement_audio(data, object_name, audio.content_type or "audio/m4a")

    sig = Signalement(
        type=type,
        latitude=latitude,
        longitude=longitude,
        description=description,
        reported_by=reported_by,
        image_url=image_url,
        audio_url=audio_url,
        blocked_bearing=blocked_bearing,
        location_name=reverse_geocode(latitude, longitude),
        reported_at=datetime.utcnow(),
    )
    # Clustering — the global Cluster Configuration, per report type.
    res = await clustering_service.resolve(db, latitude, longitude, "signalement", type.value)
    sig.cluster_zone_id, sig.cluster_environment, sig.cluster_radius_m = res.zone_id, res.environment, res.radius_m
    cfg = await _community(db)
    snapshot = await clustering_service.get_snapshot(db)
    target = None
    if snapshot.settings.merge_signalements:
        target = await find_merge_target(db, type.value, latitude, longitude, res.radius_m, cfg)
    if target is not None:
        sig.merged_into_id = target.id
        target.report_count = (target.report_count or 1) + 1
        target.last_reported_at = sig.reported_at
    db.add(sig)
    await db.commit()
    await db.refresh(sig)
    votes.annotate([sig], cfg)
    return sig


# ── Mobile: list signalements in a radius around the user's position ──────────

async def _community(db: AsyncSession) -> CommunitySettings:
    return await get_cached(db, "community")  # type: ignore[return-value]


async def _fresh_active(db: AsyncSession, ids: list[str]) -> list[Signalement]:
    """What the app displays among `ids`: active, not expired, not hidden as ignored."""
    if not ids:
        return []
    cfg = await _community(db)
    rows = (await db.execute(select(Signalement).where(Signalement.id.in_(ids)))).scalars().all()
    now = datetime.utcnow()
    # Reports merged into another one (clustering) are represented by it.
    items = [s for s in rows if votes.is_visible(s, cfg, now) and not s.merged_into_id]
    votes.annotate(items, cfg, now)
    return items


@router.post("/nearby", response_model=SignalementListOut, summary="List visible reports around a position")
async def nearby_signalements(
    body: SignalementNearbyIn,
    db: AsyncSession = Depends(get_db),
):
    """
    **Mobile** — reports within `radius_m` of the position (default and maximum:
    `mobile.visibility_radius_m`). Only what the app should display: status `actif`, not expired and —
    when `community.hide_when_ignored` is on — not `uncertain`. Read-only despite the POST.
    """
    radius = await mobile_radius(db, body.radius_m)
    ids = await query_signalements_nearby(db, body.latitude, body.longitude, radius)
    items = await _fresh_active(db, ids)
    return SignalementListOut(total=len(items), items=items)


# ── Mobile: signalements along a planned route ────────────────────────────────

@router.post("/along-route", response_model=SignalementListOut, summary="List visible reports along a route",
             responses=errors(legacy(422, "At least 2 waypoints required.")))
async def signalements_along_route(
    body: AlongRouteQueryIn,
    db: AsyncSession = Depends(get_db),
):
    """
    **Mobile** — reports within `corridor_m` metres of the polyline through `waypoints` (default:
    `mobile.route_corridor_m`). Same visibility rules as `POST /signalements/nearby`. Read-only despite
    the POST.
    """
    if len(body.waypoints) < 2:
        raise HTTPException(422, "At least 2 waypoints required.")
    pairs = [(wp.latitude, wp.longitude) for wp in body.waypoints]
    ids   = await query_signalements_along_route(db, pairs, await route_corridor(db, body.corridor_m))
    items = await _fresh_active(db, ids)
    return SignalementListOut(total=len(items), items=items)


# ── Community voting ─────────────────────────────────────────────────────────

async def _vote(
    signalement_id: str, vote: str, latitude: Optional[float], longitude: Optional[float],
    device_id: Optional[str], db: AsyncSession, principal: Principal, request: Request,
) -> dict:
    sig = await db.get(Signalement, signalement_id, with_for_update=True)
    if not sig:
        raise HTTPException(404, "Signalement not found")
    if sig.status != SignalementStatus.actif:
        raise api_error(409, "signalement_inactive", "This report is no longer active.", report_status=sig.status.value)
    cfg = await _community(db)
    result = await votes.cast_vote(db, sig, vote, latitude, longitude, device_id, cfg,
                                   principal=principal, request=request)
    await db.commit()
    await db.refresh(sig)
    votes.annotate([sig], cfg)
    return {**result, "signalement": sig}


@router.post("/{signalement_id}/vote", response_model=SignalementVoteResultOut,
             summary="Vote on a report (still there / not there)",
             responses=errors(_NOT_FOUND, _INACTIVE))
async def vote_signalement(
    request: Request,
    body: SignalementVoteIn,
    signalement_id: str = _SIG_ID,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """
    **Mobile — "Still there?" vote** (`present` / `absent`). Every vote is stored, but it is
    **counted** only when the voter's position is within `community.vote_max_distance_m` metres of the
    report; otherwise `counted=false` with `reason` `too_far` or `no_position`. One counted vote per
    voter (the signed-in user, else `device_id`): a later counted vote replaces the earlier one, which is
    kept with `reason=duplicate`.

    Effects of a counted vote: `present` refreshes the report (`last_confirmed_at`, visible at least
    `community.confirm_extends_minutes` more); `absent` votes reaching `community.hide_after_absent_votes`
    set the status to `annule` (moderated by `community`, restorable). Each vote writes an audit entry
    (`signalement.vote` / `signalement.vote.replaced`).
    """
    r = await _vote(signalement_id, body.vote, body.latitude, body.longitude, body.device_id,
                    db, principal, request)
    return SignalementVoteResultOut(signalement=SignalementOut.model_validate(r["signalement"]),
                                    my_vote=r["my_vote"], counted=r["counted"],
                                    reason=r["reason"], distance_m=r["distance_m"])


@router.post("/{signalement_id}/confirm", response_model=SignalementOut, deprecated=True,
             summary="Confirm a report (legacy)", responses=errors(_NOT_FOUND, _INACTIVE))
async def confirm_signalement(
    request: Request,
    signalement_id: str = _SIG_ID,
    body: Optional[SignalementLegacyVoteIn] = Body(None),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """
    **Deprecated** — use `POST /signalements/{signalement_id}/vote` with `{"vote": "present"}`.

    Same rules as the vote endpoint; the body is optional (older app versions send none) and, without
    a position, the vote is stored but not counted (`no_position`). Returns the report only.
    """
    body = body or SignalementLegacyVoteIn()
    r = await _vote(signalement_id, "present", body.latitude, body.longitude, body.device_id,
                    db, principal, request)
    return r["signalement"]


@router.post("/{signalement_id}/not-there", response_model=SignalementOut, deprecated=True,
             summary="Report a report as gone (legacy)", responses=errors(_NOT_FOUND, _INACTIVE))
async def not_there_signalement(
    request: Request,
    signalement_id: str = _SIG_ID,
    body: Optional[SignalementLegacyVoteIn] = Body(None),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """
    **Deprecated** — use `POST /signalements/{signalement_id}/vote` with `{"vote": "absent"}`.

    Same rules as the vote endpoint; the body is optional and, without a position, the vote is stored
    but not counted (`no_position`). Returns the report only.
    """
    body = body or SignalementLegacyVoteIn()
    r = await _vote(signalement_id, "absent", body.latitude, body.longitude, body.device_id,
                    db, principal, request)
    return r["signalement"]


@router.post("/{signalement_id}/pass", response_model=SignalementPassResultOut,
             summary="Record that a device drove past a report",
             responses=errors(_NOT_FOUND, _INACTIVE,
                              err(422, "device_id_required", "device_id is required")))
async def pass_signalement(
    body: SignalementPassIn,
    signalement_id: str = _SIG_ID,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """
    **Mobile — pass-by impression**, sent when the device comes within
    `community.vote_max_distance_m` metres of a displayed report. Recorded once per device (or signed-in
    user) per report per `community.ignore_window_minutes` (`duplicate=true` afterwards; an out-of-range
    first pass is upgraded by a later in-range one). Devices that pass within the window and never vote
    are *ignores*: at `community.ignore_threshold` ignores the report becomes `uncertain` (unless it has
    `community.keep_visible_present_votes` counted "present" votes) and, with
    `community.hide_when_ignored`, disappears from the app. `device_id` is required for anonymous callers.
    """
    sig = await db.get(Signalement, signalement_id, with_for_update=True)
    if not sig:
        raise HTTPException(404, "Signalement not found")
    if sig.status != SignalementStatus.actif:
        raise api_error(409, "signalement_inactive", "This report is no longer active.", report_status=sig.status.value)
    cfg = await _community(db)
    try:
        r = await votes.record_pass(db, sig, body.latitude, body.longitude, body.device_id, cfg, principal=principal)
    except ValueError as exc:
        raise api_error(422, "device_id_required", str(exc))
    await db.commit()
    await db.refresh(sig)
    votes.annotate([sig], cfg)
    return SignalementPassResultOut(signalement=SignalementOut.model_validate(sig), counted=r["counted"],
                                    distance_m=r["distance_m"], duplicate=r["duplicate"])


def _voter_label(v: SignalementVote, email: Optional[str]) -> Optional[str]:
    return email or v.voter_device_id


@router.get("/votes/flagged", response_model=list[FlaggedVoterOut], summary="List voters with invalid votes")
async def flagged_voters(
    min_invalid: int = Query(1, ge=1, description="Minimum number of invalid votes for a voter to be listed."),
    limit: int = Query(50, ge=1, le=500, description="Maximum number of voters returned (1–500)."),
    db: AsyncSession = Depends(get_db),
):
    """
    **Admin** — voters (signed-in users or anonymous devices) with invalid votes, i.e. cast too far
    from the report (`too_far`) or without a position (`no_position`): candidates for false testimony.
    Sorted by number of invalid votes, then most recent vote.
    """
    invalid = case((SignalementVote.reason.in_(INVALID_REASONS), 1), else_=0)
    too_far = case((SignalementVote.reason == "too_far", 1), else_=0)
    rows = (await db.execute(
        select(
            SignalementVote.voter_user_id, SignalementVote.voter_device_id, User.email,
            func.sum(invalid), func.sum(too_far), func.count(),
            func.count(func.distinct(SignalementVote.signalement_id)), func.max(SignalementVote.created_at),
        )
        .outerjoin(User, User.id == SignalementVote.voter_user_id)
        .group_by(SignalementVote.voter_user_id, SignalementVote.voter_device_id, User.email)
        .having(func.sum(invalid) >= min_invalid)
        .order_by(func.sum(invalid).desc(), func.max(SignalementVote.created_at).desc())
        .limit(limit)
    )).all()
    return [FlaggedVoterOut(voter=email or device, voter_user_id=uid, voter_email=email, voter_device_id=device,
                            invalid_votes=int(inv or 0), too_far_votes=int(far or 0), total_votes=int(total),
                            reports=int(reports), last_vote_at=last)
            for uid, device, email, inv, far, total, reports, last in rows]


@router.get("/{signalement_id}/votes", response_model=SignalementVoteListOut,
            summary="List the votes and pass-bys of a report", responses=errors(_NOT_FOUND))
async def list_votes(signalement_id: str = _SIG_ID, db: AsyncSession = Depends(get_db)):
    """
    **Admin** — every vote on a report, newest first, invalid and replaced ones included (`counted`,
    `reason`, distance to the report), plus the recorded pass-bys (`passes`, with `voted=false` for
    ignores) and the report's current tallies.
    """
    sig = await db.get(Signalement, signalement_id)
    if not sig:
        raise HTTPException(404, "Signalement not found")
    cfg = await _community(db)
    rows = (await db.execute(
        select(SignalementVote, User.email)
        .outerjoin(User, User.id == SignalementVote.voter_user_id)
        .where(SignalementVote.signalement_id == signalement_id)
        .order_by(SignalementVote.created_at.desc())
    )).all()
    items = [SignalementVoteOut(
        id=v.id, vote=v.vote, voter_user_id=v.voter_user_id, voter_email=email,
        voter_device_id=v.voter_device_id, voter=_voter_label(v, email),
        latitude=v.latitude, longitude=v.longitude, distance_m=v.distance_m,
        counted=v.counted, reason=v.reason, created_at=v.created_at,
    ) for v, email in rows]
    voted_devices = {v.voter_device_id for v, _ in rows if v.voter_device_id}
    voted_users = {v.voter_user_id for v, _ in rows if v.voter_user_id}
    pass_rows = (await db.execute(
        select(SignalementPass, User.email)
        .outerjoin(User, User.id == SignalementPass.user_id)
        .where(SignalementPass.signalement_id == signalement_id)
        .order_by(SignalementPass.created_at.desc())
    )).all()
    passes = [SignalementPassOut(
        id=p.id, device_id=p.device_id, user_id=p.user_id, voter=email or p.device_id,
        latitude=p.latitude, longitude=p.longitude, distance_m=p.distance_m, counted=p.counted,
        voted=bool((p.device_id and p.device_id in voted_devices) or (p.user_id and p.user_id in voted_users)),
        created_at=p.created_at,
    ) for p, email in pass_rows]
    return SignalementVoteListOut(total=len(items), present_count=sig.present_count or 0,
                                  absent_count=sig.absent_count or 0,
                                  invalid_vote_count=sig.invalid_vote_count or 0,
                                  vote_max_distance_m=cfg.vote_max_distance_m,
                                  ignore_count=sig.ignore_count or 0, uncertain=bool(sig.uncertain),
                                  items=items, passes=passes)


# ── Admin / listing ───────────────────────────────────────────────────────────

@router.get("/", response_model=SignalementListOut, summary="List community reports")
async def list_signalements(
    status:      Optional[SignalementStatus] = Query(None, description="Only reports with this moderation status."),
    type:        Optional[SignalementType]   = Query(None, description="Only reports of this type."),
    reported_by: Optional[str]              = Query(None, description="Only reports by this reporter (exact match)."),
    expired:     Optional[bool]             = Query(None, description=(
        "`true` = only expired reports, `false` = only fresh ones; omitted = both.")),
    skip:  int = Query(0, description="Number of matching reports to skip (offset)."),
    limit: int = Query(50, description="Page size."),
    db: AsyncSession = Depends(get_db),
):
    """
    **Admin console** — every report, newest first, whatever its status. `expired` filters on freshness
    (`community.expiry_hours_by_type`, extended by counted "present" votes): expired reports leave the
    app's lists but stay listed here. Answers `{total, items}`.
    """
    q = select(Signalement)
    if status:      q = q.where(Signalement.status == status)
    if type:        q = q.where(Signalement.type   == type)
    if reported_by: q = q.where(Signalement.reported_by == reported_by)
    cfg = await _community(db)
    now = datetime.utcnow()

    if expired is None:
        total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar()
        items = list((
            await db.execute(q.offset(skip).limit(limit).order_by(Signalement.reported_at.desc()))
        ).scalars().all())
    else:
        # Expiry depends on per-type settings — filtered in Python.
        rows = (await db.execute(q.order_by(Signalement.reported_at.desc()))).scalars().all()
        matching = [s for s in rows if votes.is_fresh(s, cfg, now) != expired]
        total, items = len(matching), matching[skip:skip + limit]
    votes.annotate(items, cfg, now)
    return SignalementListOut(total=total, items=items)


@router.get("/{signalement_id}", response_model=SignalementDetailOut, summary="Get a community report",
            responses=errors(_NOT_FOUND))
async def get_signalement(
    signalement_id: str = _SIG_ID,
    device_id: Optional[str] = Query(None, description=(
        "Anonymous caller's device id, used to fill `my_vote` when no user is signed in.")),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """
    One report, whatever its status, with `my_vote`: the caller's current counted vote (signed-in user,
    else `device_id`), or null.
    """
    sig = await db.get(Signalement, signalement_id)
    if not sig:
        raise HTTPException(404, "Signalement not found")
    votes.annotate([sig], await _community(db))
    user_id = principal.user.id if principal.user is not None else None
    out = SignalementDetailOut.model_validate(sig)
    out.my_vote = await votes.my_vote(db, sig.id, user_id, device_id)  # type: ignore[assignment]
    return out


# ── Admin: moderate (cancel / reject / restore) ──────────────────────────────

@router.patch("/{signalement_id}/moderate", response_model=SignalementOut,
              summary="Moderate a report (cancel, reject or restore)", responses=errors(_NOT_FOUND))
async def moderate_signalement(
    body: SignalementModerationIn,
    request: Request,
    signalement_id: str = _SIG_ID,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """
    **Admin** — `annule` / `rejete` take a report down; `actif` restores it, makes it fresh again (as if
    just confirmed) and clears `uncertain`. Any status can be set from any status. `moderated_by` is the
    signed-in user's e-mail when there is one, `moderated_at` is now and `note` is stored as
    `moderation_note`. Audited as `signalement.moderate`.
    """
    sig = await db.get(Signalement, signalement_id)
    if not sig:
        raise HTTPException(404, "Signalement not found")
    previous = sig.status.value

    now = datetime.utcnow()
    sig.status          = body.status
    sig.moderated_by    = actor_name(principal, body.moderated_by)
    sig.moderated_at    = now
    sig.moderation_note = body.note
    if body.status == SignalementStatus.actif:
        sig.last_confirmed_at = now
        sig.uncertain = False   # an admin vouched for it
    if principal.kind != "user":   # users are audited by the middleware
        audit.record(db, request, "signalement.moderate", principal=principal, object_type="signalement",
                     object_id=sig.id, details={"status": [previous, body.status.value], "note": body.note})
    await db.commit()
    await db.refresh(sig)
    votes.annotate([sig], await _community(db))
    return sig


# ── Admin: delete ─────────────────────────────────────────────────────────────

@router.delete("/{signalement_id}", status_code=204, summary="Delete a community report",
               responses=errors(_NOT_FOUND))
async def delete_signalement(signalement_id: str = _SIG_ID, db: AsyncSession = Depends(get_db)):
    """**Admin** — permanently deletes the report with its votes and pass-bys."""
    sig = await db.get(Signalement, signalement_id)
    if not sig:
        raise HTTPException(404, "Signalement not found")
    await db.delete(sig)
    await db.commit()
