"""
Hazard Track (RRAP-06-001…004, 06-009): hazards are persisted clusters of
accepted detections, maintained by app.modules.m06_hazards.hazard_service. Resolving a
hazard resolves its accepted detections (with history); reopening restores
them to their accepted status.

Lifecycle (app.modules.m06_hazards.lifecycle): driver votes, the event
timeline and recurring spots; automatic resolution (video passes, votes,
expiry) is done by the lifecycle itself.
"""
from datetime import datetime, timedelta
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, api_error, get_principal
from app.api.openapi import err, errors, legacy
from app.core.db.session import get_db
from app.modules.m06_hazards.models.detection import Detection, ReviewStatus
from app.modules.m06_hazards.models.hazard import Hazard, HazardStatus, ResolutionSource
from app.modules.m06_hazards.schemas import (
    HazardDetailOut, HazardListOut, HazardMemberOut, HazardOut, HazardResolveIn, ReopenIn,
)
from app.core import audit
from app.modules.m06_hazards import detection_workflow as wf, hazard_service
from app.modules.m06_hazards.lifecycle import events as hazard_events, recurring, service as lifecycle
from app.modules.m06_hazards.lifecycle import votes as hazard_votes

router = APIRouter()

SORTS = {
    "last_detected_at":  Hazard.last_detected_at,
    "last_seen_at":      Hazard.last_seen_at,
    "expires_at":        Hazard.expires_at,
    "first_detected_at": Hazard.first_detected_at,
    "detection_count":   Hazard.detection_count,
    "avg_confidence":    Hazard.avg_confidence,
    "code":              Hazard.number,
}

PLACE_COLUMNS = ("display_location", "road", "locality", "district", "town")

HazardId = Annotated[str, Path(description="Hazard id (UUID).", examples=["b8e4a0d2-5c3f-4f7e-9a61-0d2c4e8f1a77"])]
E_NOT_FOUND = legacy(404, "Hazard not found")


class HazardSummaryOut(BaseModel):
    """Hazard counts for the dashboard."""
    total:     int = Field(description="Number of hazards, all statuses.", examples=[42])
    by_status: dict[str, int] = Field(description="Count per status (`active`, `resolved`; every status present).",
                                      examples=[{"active": 30, "resolved": 12}])
    by_type:   dict[str, dict[str, int]] = Field(
        description="Count per detection type, then per status (only types that have hazards).",
        examples=[{"pothole": {"active": 25, "resolved": 10}, "speed_bump": {"active": 5, "resolved": 2}}])


class HazardEventOut(BaseModel):
    """One entry of a hazard's lifecycle timeline."""
    id:           str = Field(description="Event id (UUID).")
    hazard_id:    str = Field(description="Hazard id.")
    kind:         str = Field(description="`seen`, `not_seen`, `vote_still_there`, `vote_not_there`, `expired`, "
                                          "`resolved` or `reopened`.", examples=["not_seen"])
    source:       Optional[str] = Field(description="Where the evidence came from: `video` (processed video), "
                                                    "`review` (accepted detection), `vote`, `expiry`, `sighting`, "
                                                    "or the resolution source (`manual`, `video_absence`, "
                                                    "`votes`, `expiry`).", examples=["video"])
    job_id:       Optional[str] = Field(description="Processing job of a video sighting / pass.")
    detection_id: Optional[str] = Field(description="Detection of a sighting.")
    user_id:      Optional[str] = Field(description="Voter or console user.")
    details:      Optional[dict] = Field(description="Kind-specific details, e.g. a pass's `distance_m`, "
                                                     "`speed_kmh`, `heading_deg`.",
                                         examples=[{"distance_m": 8.4, "speed_kmh": 32.0, "heading_deg": 87.5,
                                                    "reason": "valid"}])
    recorded_at:  Optional[datetime] = Field(description="When the evidence happened (video recording / vote "
                                                         "time, UTC).")
    created_at:   datetime = Field(description="When the event was recorded (UTC).")

    class Config:
        from_attributes = True


class HazardEventListOut(BaseModel):
    """A page of a hazard's timeline, newest first."""
    total: int = Field(description="Number of matching events.", examples=[7])
    items: list[HazardEventOut] = Field(description="Events of this page, newest first.")


class HazardVoteIn(BaseModel):
    """A driver's vote on a hazard shown in the app."""
    vote:      Literal["still_there", "not_there"] = Field(description="`still_there` or `not_there`.",
                                                           examples=["not_there"])
    latitude:  float = Field(ge=-90, le=90, description="Voter's current WGS84 latitude.", examples=[4.0514])
    longitude: float = Field(ge=-180, le=180, description="Voter's current WGS84 longitude.", examples=[9.7081])
    device_id: Optional[str] = Field(None, max_length=128, description="Device / vehicle id sent by the app "
                                                                        "(kept for review).")


class HazardVoteOut(BaseModel):
    """Acknowledgement only — drivers never see vote counts."""
    message: str = Field(description="Thanks message.", examples=["Thanks, your vote has been recorded."])


class RecurringSpotOut(BaseModel):
    """A place where hazards of one type keep coming back."""
    type:              str = Field(description="Hazard type.", examples=["parked_vehicle"])
    latitude:          float = Field(description="Centroid latitude of the spot's hazards.", examples=[4.0512])
    longitude:         float = Field(description="Centroid longitude.", examples=[9.7085])
    occurrences:       int = Field(description="Hazards opened at this spot in the window.", examples=[5])
    active:            bool = Field(description="One of them is active now.")
    last_occurrence_at: Optional[datetime] = Field(description="Latest sighting of any of them (UTC).")
    location_name:     Optional[str] = Field(description="Place name of the latest hazard.")
    hazard_ids:        list[str] = Field(description="Hazards of the spot, newest first.")
    hazard_codes:      list[str] = Field(description="Their codes, same order.", examples=[["HZ-000120", "HZ-000097"]])


class RecurringSpotListOut(BaseModel):
    """Recurring spots, most occurrences first."""
    window_days:     int = Field(description="Look-back window applied.", examples=[30])
    min_occurrences: int = Field(description="Minimum occurrences applied.", examples=[3])
    total:           int = Field(description="Number of spots.", examples=[2])
    items:           list[RecurringSpotOut] = Field(description="The spots.")


@router.get("/", response_model=HazardListOut, summary="List hazards",
            responses=errors(err(422, "invalid_sort", "Unknown sort 'foo'.", allowed=sorted(SORTS))))
async def list_hazards(
    status:    Optional[str] = Query(None, pattern="^(active|resolved)$", description="`active` or `resolved`."),
    type:      Optional[str] = Query(None, description="Detection type, e.g. `pothole`.", examples=["pothole"]),
    subtype:   Optional[str] = Query(None, description="Detection subtype."),
    country:   Optional[str] = Query(None, description="Country (case-insensitive exact match)."),
    city:      Optional[str] = Query(None, description="City (case-insensitive exact match).", examples=["Douala"]),
    town:      Optional[str] = Query(None, description="Reverse-geocoded town (case-insensitive exact match).",
                                     examples=["Douala"]),
    locality:  Optional[str] = Query(None, description="Reverse-geocoded quartier / neighbourhood (case-insensitive "
                                                       "exact match).", examples=["Akwa"]),
    date_from: Optional[datetime] = Query(None, description="Hazards last detected at or after this instant (UTC)."),
    date_to:   Optional[datetime] = Query(None, description="Hazards first detected before this instant (UTC); a bare "
                                                            "date includes that whole day."),
    search:    Optional[str] = Query(None, description="Text contained in the hazard code or its place (street, "
                                                       "quartier, arrondissement, town, location name).",
                                     examples=["Akwa"]),
    resolution_source: Optional[str] = Query(None, pattern="^(manual|video_absence|votes|expiry)$",
                                             description="Resolved hazards resolved this way: `manual`, "
                                                         "`video_absence`, `votes`, `expiry`."),
    recurring_spot: Optional[bool] = Query(None, description="Only hazards flagged (true) / not flagged (false) "
                                                             "as recurring spots."),
    sort:      str = Query("-last_detected_at", description="Sort field, `-` prefix = descending: "
                                                            "`last_detected_at`, `first_detected_at`, "
                                                            "`last_seen_at`, `expires_at`, "
                                                            "`detection_count`, `avg_confidence`, `code`."),
    skip:      int = Query(0, ge=0, description="Number of items to skip (offset)."),
    limit:     int = Query(50, ge=1, le=200, description="Page size (max 200)."),
    db: AsyncSession = Depends(get_db),
):
    """
    Hazards — de-duplicated physical road hazards built from accepted detections — with filters,
    sorting and pagination. A date range matches a hazard whose detection span
    (`first_detected_at`…`last_detected_at`) overlaps it. Each item carries its lifecycle state
    (`last_seen_at`, `expires_at`, `absence_count`, `resolution_source`, `recurring_spot`).
    """
    q = select(Hazard)
    if status:    q = q.where(Hazard.status == status)
    if type:      q = q.where(Hazard.type == type)
    if subtype:   q = q.where(Hazard.subtype == subtype)
    if country:   q = q.where(Hazard.country.ilike(country))
    if city:      q = q.where(Hazard.city.ilike(city))
    if town:      q = q.where(Hazard.town.ilike(town))
    if locality:  q = q.where(Hazard.locality.ilike(locality))
    # A hazard matches a date range when its detection span overlaps it.
    if date_from: q = q.where(Hazard.last_detected_at >= date_from)
    if date_to:
        if (date_to.hour, date_to.minute, date_to.second) == (0, 0, 0):
            date_to += timedelta(days=1)
        q = q.where(Hazard.first_detected_at < date_to)
    if resolution_source: q = q.where(Hazard.resolution_source == resolution_source)
    if recurring_spot is not None: q = q.where(Hazard.recurring_spot.is_(recurring_spot))
    if search:
        s = search.strip()
        q = q.where(or_(Hazard.code.ilike(f"%{s}%"), Hazard.location_name.ilike(f"%{s}%"),
                        *(getattr(Hazard, c).ilike(f"%{s}%") for c in PLACE_COLUMNS)))

    col = SORTS.get(sort.lstrip("-"))
    if col is None:
        raise api_error(422, "invalid_sort", f"Unknown sort '{sort}'.", allowed=sorted(SORTS))
    order = col.desc().nullslast() if sort.startswith("-") else col.asc().nullsfirst()

    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0
    rows = (await db.execute(q.order_by(order, Hazard.number.desc()).offset(skip).limit(limit))).scalars().all()
    return HazardListOut(total=total, items=await _with_covers(db, [HazardOut.model_validate(h) for h in rows]))


async def _with_covers(db: AsyncSession, items: list) -> list:
    """Best evidence of each hazard: crop / frame of its most confident member (one query)."""
    ids = [h.id for h in items]
    if not ids:
        return items
    rows = (await db.execute(
        select(Detection.hazard_id, Detection.crop_url, Detection.image_url)
        .where(Detection.hazard_id.in_(ids), or_(Detection.crop_url.isnot(None), Detection.image_url.isnot(None)))
        .order_by(Detection.hazard_id, Detection.confidence.desc())
        .distinct(Detection.hazard_id)
    )).all()
    covers = {hid: (crop, img) for hid, crop, img in rows}
    for h in items:
        if h.id in covers:
            h.cover_crop_url, h.cover_image_url = covers[h.id]
    return items


@router.get("/summary", response_model=HazardSummaryOut, summary="Count hazards by status and type")
async def hazards_summary(db: AsyncSession = Depends(get_db)):
    """Hazard counts by status and by type, for the dashboard."""
    rows = (await db.execute(
        select(Hazard.type, Hazard.status, func.count()).group_by(Hazard.type, Hazard.status)
    )).all()
    by_status = {s.value: 0 for s in HazardStatus}
    by_type: dict[str, dict[str, int]] = {}
    for det_type, status, n in rows:
        by_status[status] = by_status.get(status, 0) + n
        by_type.setdefault(det_type, {s.value: 0 for s in HazardStatus})[status] = n
    return {"total": sum(by_status.values()), "by_status": by_status, "by_type": by_type}


@router.get("/recurring-spots", response_model=RecurringSpotListOut, summary="List recurring hazard spots",
            responses=errors(err(422, "invalid_bbox", "bbox must be min_lat,min_lon,max_lat,max_lon.")))
async def recurring_spots(
    type:            Optional[str] = Query(None, description="Hazard type; default = every type whose "
                                                             "`hazard_lifecycle` policy has `recurring_enabled`.",
                                       examples=["parked_vehicle"]),
    bbox:            Optional[str] = Query(None, description="`min_lat,min_lon,max_lat,max_lon` (WGS84).",
                                       examples=["4.00,9.65,4.10,9.80"]),
    window_days:     Optional[int] = Query(None, ge=1, le=3650, description="Look-back window in days (default "
                                                                            "`hazard_lifecycle.recurring_window_days`)."),
    min_occurrences: Optional[int] = Query(None, ge=2, le=1000, description="Minimum hazards per spot (default "
                                                                            "`hazard_lifecycle.recurring_min_occurrences`)."),
    db: AsyncSession = Depends(get_db),
):
    """
    Places where hazards of the same type keep coming back — e.g. a spot where vehicles park every
    day — for route risk analysis (Module 4). Hazards **opened** in the window (any status) are grouped
    into spots (greedy clustering by `hazard_lifecycle.recurring_radius_m`, else each hazard's cluster
    radius); spots with at least `min_occurrences` hazards are listed, most occurrences first.
    New hazards opened at such a spot are flagged `recurring_spot` with `recurrence_count`.
    """
    box = None
    if bbox:
        try:
            box = tuple(float(x) for x in bbox.split(","))
            if len(box) != 4 or box[0] > box[2] or box[1] > box[3]:
                raise ValueError
        except ValueError:
            raise api_error(422, "invalid_bbox", "bbox must be min_lat,min_lon,max_lat,max_lon.")
    cfg = await lifecycle.load_settings(db)
    spots = await recurring.list_spots(db, cfg, det_type=type, bbox=box, window_days=window_days,  # type: ignore[arg-type]
                                       min_occurrences=min_occurrences)
    items = []
    for sp in spots:
        hz = sorted(sp.hazards, key=lambda h: h.created_at or datetime.min, reverse=True)
        seen = [h.last_seen_at or h.last_detected_at for h in hz if (h.last_seen_at or h.last_detected_at)]
        items.append(RecurringSpotOut(
            type=sp.type, latitude=round(sp.latitude, 6), longitude=round(sp.longitude, 6),
            occurrences=sp.occurrences, active=any(h.status == HazardStatus.active.value for h in hz),
            last_occurrence_at=max(seen) if seen else None, location_name=hz[0].location_name,
            hazard_ids=[h.id for h in hz], hazard_codes=[h.code for h in hz]))
    return RecurringSpotListOut(window_days=window_days or cfg.recurring_window_days,
                                min_occurrences=min_occurrences or cfg.recurring_min_occurrences,
                                total=len(items), items=items)


class PlaceLocalityOut(BaseModel):
    name:  str = Field(description="Quartier / neighbourhood.", examples=["Akwa"])
    count: int = Field(description="Hazards in it.", examples=[7])


class PlaceTownOut(BaseModel):
    """A town with its hazards per quartier (filter facets of the Hazard Track)."""
    town:       str = Field(description="Town.", examples=["Douala"])
    count:      int = Field(description="Hazards in the town.", examples=[25])
    localities: list[PlaceLocalityOut] = Field(description="Quartiers, most hazards first.")


class GeocodeBackfillOut(BaseModel):
    """State of the structured-address back-fill (one run per API process)."""
    running:         bool = Field(description="A run is in progress.")
    started:         Optional[bool] = Field(None, description="This call started a run (POST only).")
    pending:         int = Field(description="Located detections never geocoded.", examples=[120])
    started_at:      Optional[datetime] = Field(None, description="Start of the last run (UTC).")
    finished_at:     Optional[datetime] = Field(None, description="End of the last run (UTC).")
    limit:           int = Field(0, description="Lookups allowed in the last run.")
    processed:       int = Field(0, description="Detections looked up in the last run.")
    updated:         int = Field(0, description="Detections that got an address.")
    failed:          int = Field(0, description="Lookups without a result.")
    hazards_updated: int = Field(0, description="Hazards re-aggregated with their members' address.")
    stopped_reason:  Optional[str] = Field(None, description="`done`, `limit`, `failures` (geocoder unreachable), "
                                                             "`disabled` or `error`.")


@router.get("/places", response_model=list[PlaceTownOut], summary="List the towns and quartiers of hazards")
async def hazard_places(
    status: Optional[str] = Query(None, pattern="^(active|resolved)$", description="`active` or `resolved`."),
    type:   Optional[str] = Query(None, description="Detection type."),
    limit:  int = Query(50, ge=1, le=500, description="Maximum number of towns."),
    db: AsyncSession = Depends(get_db),
):
    """
    Reverse-geocoded towns that have hazards, most hazards first, each with its quartiers — the
    choices of the Hazard Track's place filter (`town` / `locality` of `GET /hazards/`).
    """
    q = select(Hazard.town, Hazard.locality, func.count()).where(Hazard.town.isnot(None))
    if status: q = q.where(Hazard.status == status)
    if type:   q = q.where(Hazard.type == type)
    rows = (await db.execute(q.group_by(Hazard.town, Hazard.locality))).all()
    towns: dict[str, dict] = {}
    for town, loc, n in rows:
        t = towns.setdefault(town, {"town": town, "count": 0, "localities": []})
        t["count"] += n
        if loc:
            t["localities"].append({"name": loc, "count": n})
    out = sorted(towns.values(), key=lambda t: (-t["count"], t["town"]))[:limit]
    for t in out:
        t["localities"].sort(key=lambda x: (-x["count"], x["name"]))
    return out


async def _backfill_out(db: AsyncSession, started: Optional[bool] = None) -> GeocodeBackfillOut:
    from app.modules.m03_maps import geocoding_backfill
    return GeocodeBackfillOut(**geocoding_backfill.state.public(), started=started,
                              pending=await geocoding_backfill.pending_count(db))


@router.get("/geocode/backfill", response_model=GeocodeBackfillOut, summary="Get the address back-fill state")
async def geocode_backfill_status(db: AsyncSession = Depends(get_db)):
    """Progress of the structured-address back-fill and the number of detections still lacking one."""
    return await _backfill_out(db)


@router.post("/geocode/backfill", response_model=GeocodeBackfillOut, status_code=202,
             summary="Reverse-geocode detections and hazards lacking an address")
async def geocode_backfill(
    request: Request,
    limit: int = Query(200, ge=1, le=5000, description="Maximum Nominatim lookups of this run (≤ 1 per second)."),
    db: AsyncSession = Depends(get_db), principal: Principal = Depends(get_principal),
):
    """
    Starts, in the background, the reverse geocoding (street, quartier, arrondissement, town,
    region) of located detections stored before structured addresses existed — hazard members
    first — then re-aggregates their hazards. Idempotent (only detections never geocoded), throttled
    to one OpenStreetMap request per second, stops after 5 failed lookups in a row; one run at a time
    (`started=false` when one is already running). Audited as `hazard.geocode_backfill`.
    """
    from app.modules.m03_maps import geocoding_backfill
    started = geocoding_backfill.start_background(limit)
    if started:
        await audit.record_now(request, "hazard.geocode_backfill", principal=principal,
                               object_type="hazard", details={"limit": limit})
    return await _backfill_out(db, started)


async def _get(db: AsyncSession, hazard_id: str) -> Hazard:
    hazard = await db.get(Hazard, hazard_id)
    if hazard is None:
        raise HTTPException(404, "Hazard not found")
    return hazard


async def _detail(db: AsyncSession, hazard: Hazard) -> HazardDetailOut:
    out = HazardDetailOut.model_validate(hazard)
    members = await hazard_service.members_of(db, hazard.id)
    out.detections = [HazardMemberOut.model_validate(d) for d in members]
    best = max((d for d in members if d.crop_url or d.image_url), key=lambda d: d.confidence, default=None)
    if best is not None:
        out.cover_crop_url, out.cover_image_url = best.crop_url, best.image_url
    return out


@router.get("/{hazard_id}", response_model=HazardDetailOut, summary="Get a hazard", responses=errors(E_NOT_FOUND))
async def get_hazard(hazard_id: HazardId, db: AsyncSession = Depends(get_db)):
    """A hazard with its member detections (newest capture first), whatever their review status."""
    return await _detail(db, await _get(db, hazard_id))


E_VOTE = (
    E_NOT_FOUND,
    err(401, "not_authenticated", "Please sign in to vote."),
    err(409, "hazard_resolved", "This hazard is no longer active.", summary="Hazard resolved or expired"),
    err(409, "votes_disabled", "Votes are not enabled for this kind of hazard.", summary="Votes disabled"),
    err(422, "too_far", "You are too far from this hazard to vote.", max_distance_m=300),
    err(429, "cooldown", "You already voted on this hazard recently.", retry_after_minutes=60),
)
_VOTE_ERRORS = {
    "hazard_resolved": (409, "This hazard is no longer active."),
    "votes_disabled":  (409, "Votes are not enabled for this kind of hazard."),
    "too_far":         (422, "You are too far from this hazard to vote."),
    "cooldown":        (429, "You already voted on this hazard recently."),
}


@router.post("/{hazard_id}/votes", response_model=HazardVoteOut, status_code=201,
             summary="Vote on a hazard (still there / not there)", responses=errors(*E_VOTE))
async def vote_hazard(hazard_id: HazardId, body: HazardVoteIn, request: Request,
                      db: AsyncSession = Depends(get_db), principal: Principal = Depends(get_principal)):
    """
    **Mobile — driver vote** on an AI hazard shown in the app (detections carry `hazard_id`):
    `still_there` or `not_there`. The driver must be **signed in** (API key + user Bearer token) and
    within `hazard_lifecycle.vote_max_distance_m` metres of the hazard; one counted vote per user per
    hazard per `hazard_lifecycle.vote_cooldown_minutes`.

    Effects: `still_there` is a `vote_still_there` event and, when the type's policy has
    `still_there_extends` (parked vehicles), a sighting — `last_seen_at` = now, expiry extended.
    `not_there` is a `vote_not_there` event; when the distinct "not there" voters since the last
    sighting reach the policy's `not_there_to_resolve`, the hazard is resolved (`resolution_source`
    `votes`). Votes refused for distance or cooldown are stored (not counted) for review; every vote is
    audited (`hazard.vote`). The answer never reveals any count.
    """
    if principal.user is None:
        raise api_error(401, "not_authenticated", "Please sign in to vote.")
    hazard = await db.get(Hazard, hazard_id, with_for_update=True)
    if hazard is None:
        raise HTTPException(404, "Hazard not found")
    cfg = await lifecycle.load_settings(db)
    result = await hazard_votes.cast_vote(db, hazard, body.vote, body.latitude, body.longitude, cfg,
                                          principal=principal, device_id=body.device_id, request=request)
    await db.commit()
    reason = result["reason"]
    if reason is not None:
        status, message = _VOTE_ERRORS[reason]
        extra = {"max_distance_m": cfg.vote_max_distance_m} if reason == "too_far" else \
            {"retry_after_minutes": cfg.vote_cooldown_minutes} if reason == "cooldown" else {}
        raise api_error(status, reason, message, **extra)
    return HazardVoteOut(message=hazard_votes.THANKS)


@router.get("/{hazard_id}/events", response_model=HazardEventListOut, summary="Get a hazard's lifecycle timeline",
            responses=errors(E_NOT_FOUND))
async def hazard_timeline(
    hazard_id: HazardId,
    kind:  Optional[list[str]] = Query(None, description="Only these kinds (repeatable): `seen`, `not_seen`, "
                                                         "`vote_still_there`, `vote_not_there`, `expired`, "
                                                         "`resolved`, `reopened`."),
    skip:  int = Query(0, ge=0, description="Number of items to skip (offset)."),
    limit: int = Query(200, ge=1, le=1000, description="Page size (max 1000)."),
    db: AsyncSession = Depends(get_db),
):
    """
    **Console** — the lifecycle timeline of a hazard, newest first: sightings (`seen`, from accepted
    detections or later videos), valid video passes without it (`not_seen`, with distance, speed and
    heading), counted driver votes, expiry, resolutions (with their source) and reopenings.
    `recorded_at` is when the evidence happened, `created_at` when it was recorded.
    """
    await _get(db, hazard_id)
    total, rows = await hazard_events.timeline(db, hazard_id, kinds=kind, skip=skip, limit=limit)
    return HazardEventListOut(total=total, items=[HazardEventOut.model_validate(r) for r in rows])


@router.post("/{hazard_id}/resolve", response_model=HazardDetailOut, summary="Resolve a hazard",
             responses=errors(E_NOT_FOUND, err(409, "invalid_transition", "This hazard is already resolved.")))
async def resolve_hazard(hazard_id: HazardId, body: HazardResolveIn, request: Request,
                         db: AsyncSession = Depends(get_db), principal: Principal = Depends(get_principal)):
    """
    The hazard was fixed on the road (RRAP-06-003): `active` → `resolved`. Each accepted member
    detection goes `validated`/`corrected` → `resolved` (history row + `detection.resolve` audit);
    `resolved_at`, `resolved_by`, the optional `note` and `resolution_source=manual` are recorded, with a
    `resolved` event in the timeline. Audited as `hazard.resolve`. Resolved hazards no longer appear in
    the mobile queries. Hazards are also resolved automatically by the lifecycle (`video_absence`,
    `votes`, `expiry` — see `hazard_lifecycle` settings).
    """
    hazard = await _get(db, hazard_id)
    actor = wf.user_actor(principal, request)
    if hazard.status != HazardStatus.active.value:
        await audit.record_now(request, "hazard.resolve", principal=principal, outcome="failure",
                               object_type="hazard", object_id=hazard.id, details={"status": hazard.status})
        raise api_error(409, "invalid_transition", "This hazard is already resolved.")
    resolved = await lifecycle.resolve(db, hazard, ResolutionSource.manual.value, actor=actor, note=body.note)
    audit.record(db, request, "hazard.resolve", principal=principal, actor_label=actor.name,
                 object_type="hazard", object_id=hazard.id,
                 details={"code": hazard.code, "detections": resolved, "note": body.note,
                          "source": ResolutionSource.manual.value})
    await db.commit()
    await db.refresh(hazard)
    return await _detail(db, hazard)


@router.post("/{hazard_id}/reopen", response_model=HazardDetailOut, summary="Reopen a resolved hazard",
             responses=errors(E_NOT_FOUND, err(409, "invalid_transition", "This hazard is not resolved.")))
async def reopen_hazard(hazard_id: HazardId, body: ReopenIn, request: Request,
                        db: AsyncSession = Depends(get_db), principal: Principal = Depends(get_principal)):
    """
    The hazard is back: `resolved` → `active`. Each resolved member detection returns to the accepted
    status it had before (`validated` or `corrected`, action `unresolve`); the resolution fields are
    cleared, `last_seen_at` becomes now (a human saw it) and `expires_at` is recomputed. `reason` is
    mandatory. Audited as `hazard.reopen` with the previous resolution; `reopened` timeline event.
    """
    hazard = await _get(db, hazard_id)
    actor = wf.user_actor(principal, request)
    if hazard.status != HazardStatus.resolved.value:
        await audit.record_now(request, "hazard.reopen", principal=principal, outcome="failure",
                               object_type="hazard", object_id=hazard.id, details={"status": hazard.status})
        raise api_error(409, "invalid_transition", "This hazard is not resolved.")
    reopened = sum(1 for d in await hazard_service.members_of(db, hazard.id)
                   if d.review_status == ReviewStatus.resolved)
    previous = await lifecycle.reopen(db, hazard, source=ResolutionSource.manual.value, reason=body.reason,
                                      actor=actor)
    audit.record(db, request, "hazard.reopen", principal=principal, actor_label=actor.name,
                 object_type="hazard", object_id=hazard.id,
                 details={"code": hazard.code, "detections": reopened, "reason": body.reason, "previous": previous})
    await db.commit()
    await db.refresh(hazard)
    return await _detail(db, hazard)
