"""
Hazard Track (RRAP-06-001…004, 06-009): hazards are persisted clusters of
accepted detections, maintained by app.modules.m06_hazards.hazard_service. Resolving a
hazard resolves its accepted detections (with history); reopening restores
them to their accepted status.

Lifecycle (app.modules.m06_hazards.lifecycle): driver votes, the event
timeline and recurring spots; automatic resolution (video passes, votes,
expiry) is done by the lifecycle itself.

Cluster-first (docs/CLUSTERS.md): candidate clusters (`candidate`,
`dismissed`) share the table but are NOT hazards — every endpoint here
filters them out (PUBLIC_STATUSES; their ids answer 404) except the
validation endpoints /hazards/review-queue and /hazards/{id}/accept|reject.
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
from app.modules.m06_hazards.models.hazard import PUBLIC_STATUSES, Hazard, HazardStatus, ResolutionSource
from app.modules.m06_hazards.schemas import (
    HazardDetailOut, HazardListOut, HazardMemberOut, HazardOut, HazardResolveIn, QueueItemOut, ReopenIn,
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
                                          "`resolved`, `reopened`, `confirmed` (the candidate cluster became this "
                                          "hazard; details.previous_code = its CL- code, details.by) or `merged` "
                                          "(a duplicate hazard was merged into it; details.merged_code).",
                              examples=["not_seen"])
    source:       Optional[str] = Field(description="Where the evidence came from: `video` (processed video), "
                                                    "`review` (accepted detection), `vote`, `expiry`, `sighting`, "
                                                    "or the resolution source (`manual`, `video_absence`, "
                                                    "`votes`, `expiry`).", examples=["video"])
    job_id:       Optional[str] = Field(description="Processing job of a video sighting / pass.")
    detection_id: Optional[str] = Field(description="Detection of a sighting.")
    user_id:      Optional[str] = Field(description="Voter or console user.")
    details:      Optional[dict] = Field(description="Kind-specific details, e.g. a pass's `distance_m`, "
                                                     "`speed_kmh`, `heading_deg`, `pass_number` / `threshold` "
                                                     "(\"not seen in pass 1/3\") and `job_name`.",
                                         examples=[{"distance_m": 8.4, "speed_kmh": 32.0, "heading_deg": 87.5,
                                                    "reason": "valid", "pass_number": 1, "threshold": 3,
                                                    "job_id": "7d0c5b9e-2f1a-4c3d-8e6b-1a2b3c4d5e6f",
                                                    "job_name": "drive_0412.mp4"}])
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
    has_pending: Optional[bool] = Query(None, description="Only hazards that have (true) / have no (false) member "
                                                          "detections still awaiting a decision."),
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
    q = select(Hazard).where(Hazard.status.in_(PUBLIC_STATUSES))       # never candidate clusters
    if status:    q = q.where(Hazard.status == status)
    if has_pending is not None:
        pend = select(Detection.id).where(Detection.hazard_id == Hazard.id,
                                          Detection.review_status.in_(hazard_service.UNREVIEWED_STATUSES)).exists()
        q = q.where(pend if has_pending else ~pend)
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


def _counts_into(item, counts: dict) -> None:
    item.member_counts = counts
    item.pending_count = counts.get("pending", 0) + counts.get("location_pending", 0)
    item.confirmed = item.status in PUBLIC_STATUSES


async def _with_covers(db: AsyncSession, items: list) -> list:
    """Best evidence of each hazard: crop / frame of its most confident
    non-rejected member (one query), plus its member counts by review status."""
    ids = [h.id for h in items]
    if not ids:
        return items
    counts = await hazard_service.member_counts(db, ids)
    for h in items:
        _counts_into(h, counts.get(h.id, {}))
    rows = (await db.execute(
        select(Detection.hazard_id, Detection.crop_url, Detection.image_url)
        .where(Detection.hazard_id.in_(ids), Detection.review_status != ReviewStatus.rejected,
               or_(Detection.crop_url.isnot(None), Detection.image_url.isnot(None)))
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
        select(Hazard.type, Hazard.status, func.count()).where(Hazard.status.in_(PUBLIC_STATUSES))
        .group_by(Hazard.type, Hazard.status)
    )).all()
    by_status = {s: 0 for s in PUBLIC_STATUSES}
    by_type: dict[str, dict[str, int]] = {}
    for det_type, status, n in rows:
        by_status[status] = by_status.get(status, 0) + n
        by_type.setdefault(det_type, {s: 0 for s in PUBLIC_STATUSES})[status] = n
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
    q = select(Hazard.town, Hazard.locality, func.count()).where(Hazard.town.isnot(None),
                                                                 Hazard.status.in_(PUBLIC_STATUSES))
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


async def _get(db: AsyncSession, hazard_id: str, *, candidates: bool = False) -> Hazard:
    """A hazard; candidate / dismissed clusters are not hazards (404) unless
    `candidates` (validation endpoints)."""
    hazard = await db.get(Hazard, hazard_id)
    if hazard is None or (hazard.status not in PUBLIC_STATUSES and not candidates):
        raise HTTPException(404, "Hazard not found")
    return hazard


async def _detail(db: AsyncSession, hazard: Hazard) -> HazardDetailOut:
    out = HazardDetailOut.model_validate(hazard)
    members = await hazard_service.members_of(db, hazard.id)
    out.detections = [HazardMemberOut.model_validate(d) for d in members]
    best = max((d for d in members if d.crop_url or d.image_url), key=lambda d: d.confidence, default=None)
    if best is not None:
        out.cover_crop_url, out.cover_image_url = best.crop_url, best.image_url
    _counts_into(out, (await hazard_service.member_counts(db, [hazard.id]))[hazard.id])
    return out


# ── grouped review of clusters (validation screen) ──────────────────────────

class ReviewClusterOut(BaseModel):
    """A cluster with detections awaiting a decision."""
    hazard:     HazardOut = Field(description="The cluster: a candidate (`CL-…`, `confirmed=false`) or a confirmed "
                                              "hazard (`HZ-…`) that received new unreviewed detections.")
    detections: list[QueueItemOut] = Field(description="Its unreviewed detections, highest confidence first.")


class ReviewClusterListOut(BaseModel):
    """The validation queue grouped by cluster."""
    total:       int = Field(description="Matching clusters with unreviewed detections.", examples=[12])
    candidates:  int = Field(description="Of which candidate clusters (not hazards yet).", examples=[9])
    unclustered: int = Field(description="Unreviewed detections without a cluster (no position yet).", examples=[3])
    items:       list[ReviewClusterOut] = Field(description="The clusters of this page.")


class ClusterAcceptIn(BaseModel):
    """Body of `POST /hazards/{id}/accept`."""
    note:           Optional[str] = Field(None, max_length=1000, description="Review note stored on each detection.")
    label:          Optional[str] = Field(None, description="Training label (default: subtype or type).")
    severity_score: Optional[int] = Field(None, ge=0, le=3, description="Severity 0–3 (default 1).")


class ClusterRejectIn(BaseModel):
    """Body of `POST /hazards/{id}/reject`."""
    reason_code: str = Field(description="Rejection reason (catalogue of the hazard's type or `all`).",
                             examples=["shadow"])
    note:        Optional[str] = Field(None, max_length=1000, description="Review note.")


class ClusterDecisionOut(BaseModel):
    """Outcome of a cluster decision."""
    hazard_id:      str = Field(description="Cluster id (unchanged when a candidate is confirmed).")
    hazard_code:    str = Field(description="Code after the decision: the new `HZ-…` code of a confirmed candidate.",
                                examples=["HZ-000045"])
    previous_code:  str = Field(description="Code before the decision (`CL-…` for a candidate).",
                                examples=["CL-000123"])
    hazard_status:  Optional[str] = Field(description="Status after the decision; `deleted` when a rejected "
                                                      "candidate was left empty and removed.", examples=["active"])
    accepted:       int = Field(0, description="Detections accepted by the user.")
    auto_validated: int = Field(0, description="Other members auto-validated as re-sightings of the confirmed "
                                               "hazard (approximate positions).")
    rejected:       int = Field(0, description="Detections rejected.")
    skipped:        int = Field(0, description="Unreviewed members left untouched (e.g. no confirmed position).")
    hazard:         Optional[HazardDetailOut] = Field(None, description="The hazard after the decision (confirmed "
                                                                       "hazards only).")


_REVIEW_SORTS = ("-pending_count", "pending_count", "-last_detected_at", "last_detected_at",
                 "-max_confidence", "max_confidence", "code", "-code")
_REVIEWABLE = (HazardStatus.candidate.value, HazardStatus.active.value, HazardStatus.resolved.value)


@router.get("/review-queue", response_model=ReviewClusterListOut, summary="Get the validation queue grouped by cluster",
            responses=errors(err(422, "invalid_sort", "Unknown sort 'foo'.", allowed=list(_REVIEW_SORTS))))
async def review_queue(
    type:    Optional[str] = Query(None, description="Detection type.", examples=["pothole"]),
    subtype: Optional[str] = Query(None, description="Detection subtype."),
    job_id:  Optional[str] = Query(None, description="Only clusters with an unreviewed detection of this job."),
    search:  Optional[str] = Query(None, description="Text in the cluster code or its place."),
    sort:    str = Query("-pending_count", description="`-pending_count`, `-last_detected_at`, `-max_confidence`, "
                                                       "`code` (`-` = descending)."),
    skip:    int = Query(0, ge=0, description="Number of clusters to skip."),
    limit:   int = Query(20, ge=1, le=100, description="Page size (max 100)."),
    members: int = Query(12, ge=1, le=100, description="Unreviewed detections returned per cluster."),
    db: AsyncSession = Depends(get_db),
):
    """
    **Validation screen** — review by cluster, not by duplicate (docs/CLUSTERS.md). Every located
    detection joins a cluster when it is created; this lists the clusters that have detections
    awaiting a decision: candidate clusters (`status=candidate`, code `CL-…`: validated by nobody yet,
    NOT hazards — they appear nowhere else) and confirmed hazards that received unreviewed
    detections (only when `hazard_lifecycle` auto-validation of re-sightings is off for the type, or
    after a reopen). Decide a whole cluster with `POST /hazards/{id}/accept` / `reject`, or each
    detection individually (`GET /detections/queue?hazard_id=…`). `unclustered` counts unreviewed
    detections without a position (they join a cluster once their position is fixed).
    """
    from app.modules.m06_hazards.routes.detections import attach_cluster_refs, _serialize
    unreviewed = hazard_service.UNREVIEWED_STATUSES
    member_cond = [Detection.hazard_id == Hazard.id, Detection.review_status.in_(unreviewed)]
    if job_id:
        member_cond.append(Detection.job_id == job_id)
    pending_n = (select(func.count(Detection.id)).where(Detection.hazard_id == Hazard.id,
                                                        Detection.review_status.in_(unreviewed))
                 .correlate(Hazard).scalar_subquery())
    q = select(Hazard).where(Hazard.status.in_(_REVIEWABLE), select(Detection.id).where(*member_cond).exists())
    if type:    q = q.where(Hazard.type == type)
    if subtype: q = q.where(Hazard.subtype == subtype)
    if search:
        s_ = search.strip()
        q = q.where(or_(Hazard.code.ilike(f"%{s_}%"), Hazard.location_name.ilike(f"%{s_}%"),
                        *(getattr(Hazard, c).ilike(f"%{s_}%") for c in PLACE_COLUMNS)))
    if sort not in _REVIEW_SORTS:
        raise api_error(422, "invalid_sort", f"Unknown sort '{sort}'.", allowed=list(_REVIEW_SORTS))
    col = {"pending_count": pending_n, "last_detected_at": Hazard.last_detected_at,
           "max_confidence": Hazard.max_confidence, "code": Hazard.number}[sort.lstrip("-")]
    order = col.desc().nullslast() if sort.startswith("-") else col.asc().nullsfirst()

    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0
    candidates = (await db.execute(select(func.count()).select_from(
        q.where(Hazard.status == HazardStatus.candidate.value).subquery()))).scalar() or 0
    uq = select(func.count(Detection.id)).where(Detection.hazard_id.is_(None), Detection.review_status.in_(unreviewed))
    if type:    uq = uq.where(Detection.type == type)
    if subtype: uq = uq.where(Detection.subtype == subtype)
    if job_id:  uq = uq.where(Detection.job_id == job_id)
    unclustered = (await db.execute(uq)).scalar() or 0

    rows = (await db.execute(q.order_by(order, Hazard.created_at).offset(skip).limit(limit))).scalars().all()
    outs = await _with_covers(db, [HazardOut.model_validate(h) for h in rows])
    items = []
    for h, out in zip(rows, outs):
        dets = (await db.execute(select(Detection).where(
            Detection.hazard_id == h.id, Detection.review_status.in_(unreviewed))
            .order_by(Detection.confidence.desc(), Detection.id).limit(members))).scalars().all()
        det_items = await _serialize(db, list(dets), QueueItemOut)
        await attach_cluster_refs(db, list(dets), det_items)
        items.append(ReviewClusterOut(hazard=out, detections=det_items))
    return ReviewClusterListOut(total=total, candidates=candidates, unclustered=unclustered, items=items)


E_NOTHING = err(409, "nothing_to_review", "This cluster has no detection awaiting a decision.")
E_CLUSTER_STATE = err(409, "invalid_transition", "This cluster was dismissed.")


async def _cluster_for_review(db: AsyncSession, hazard_id: str, request: Request, principal: Principal,
                              action: str) -> Hazard:
    hazard = await _get(db, hazard_id, candidates=True)
    if hazard.status not in _REVIEWABLE:
        await audit.record_now(request, f"hazard.{action}", principal=principal, outcome="failure",
                               object_type="hazard", object_id=hazard.id, details={"status": hazard.status})
        raise api_error(409, "invalid_transition", f"This cluster is {hazard.status}.", status=hazard.status)
    return hazard


async def _decision_out(db: AsyncSession, hazard_id: str, previous_code: str, **counts) -> ClusterDecisionOut:
    await db.commit()
    from app.modules.m06_hazards.training_data import sync as training_sync
    training_sync.kick()
    hazard = await db.get(Hazard, hazard_id)
    if hazard is not None:
        await db.refresh(hazard)
    public = hazard is not None and hazard.status in PUBLIC_STATUSES
    return ClusterDecisionOut(
        hazard_id=hazard_id, hazard_code=hazard.code if hazard is not None else previous_code,
        previous_code=previous_code, hazard_status=hazard.status if hazard is not None else "deleted",
        hazard=await _detail(db, hazard) if public else None, **counts)


@router.post("/{hazard_id}/accept", response_model=ClusterDecisionOut, summary="Accept a whole cluster",
             responses=errors(E_NOT_FOUND, E_NOTHING, E_CLUSTER_STATE))
async def accept_cluster(hazard_id: HazardId, body: ClusterAcceptIn, request: Request,
                         db: AsyncSession = Depends(get_db), principal: Principal = Depends(get_principal)):
    """
    **Validation screen** — accept every detection of the cluster that awaits a decision with a
    confirmed position (`pending` → `validated`, or `corrected` after a manual position fix), each
    through the normal workflow (history row, `detection.accept` audit, training sample). The first
    acceptance confirms a candidate: it becomes a hazard with a new `HZ-…` code (`confirmed` timeline
    event) and appears on the hazard screens; the members left (approximate positions) are then
    auto-validated as re-sightings when the type's policy allows it. Corrections (class, box,
    position) stay per detection. Audited as `hazard.accept_cluster`.
    """
    actor = wf.user_actor(principal, request)
    hazard = await _cluster_for_review(db, hazard_id, request, principal, "accept_cluster")
    previous_code = hazard.code
    members = await hazard_service.members_of(db, hazard.id)
    pending = sorted((m for m in members if m.review_status == ReviewStatus.pending),
                     key=lambda m: -(m.confidence or 0))
    waiting = [m for m in members if m.review_status == ReviewStatus.location_pending]
    if not pending:
        raise api_error(409, "nothing_to_review",
                        "This cluster has no detection awaiting a decision with a confirmed position."
                        if waiting else "This cluster has no detection awaiting a decision.",
                        location_pending=len(waiting))
    for m in pending:
        await wf.accept(db, m, actor, label=body.label, severity_score=body.severity_score, note=body.note,
                        resight_others=False)
    hazard = await db.get(Hazard, hazard_id)
    auto = await hazard_service.resight_unreviewed(db, hazard) if hazard is not None else 0
    skipped = len(waiting) - auto
    audit.record(db, request, "hazard.accept_cluster", principal=principal, actor_label=actor.name,
                 object_type="hazard", object_id=hazard_id,
                 details={"code": hazard.code if hazard else previous_code, "previous_code": previous_code,
                          "accepted": len(pending), "auto_validated": auto, "skipped": skipped, "note": body.note})
    return await _decision_out(db, hazard_id, previous_code, accepted=len(pending), auto_validated=auto,
                               skipped=skipped)


@router.post("/{hazard_id}/reject", response_model=ClusterDecisionOut, summary="Reject a whole cluster",
             responses=errors(E_NOT_FOUND, E_NOTHING, E_CLUSTER_STATE,
                              err(422, "invalid_reason", "Unknown rejection reason 'blurry' for pothole.")))
async def reject_cluster(hazard_id: HazardId, body: ClusterRejectIn, request: Request,
                         db: AsyncSession = Depends(get_db), principal: Principal = Depends(get_principal)):
    """
    **Validation screen** — reject every detection of the cluster that awaits a decision (`pending`
    or `location_pending` → `rejected`, `rejection_source=human`) with the same reason (catalogue of
    the type or `all`; its kind — false positive / real but redundant / unusable — decides what the
    training data learns), each through the normal workflow (history, `detection.reject` audit,
    training sample). Rejected detections leave the cluster: a candidate left empty is deleted
    (`hazard_status=deleted`); a confirmed hazard keeps its accepted members. Audited as
    `hazard.reject_cluster`.
    """
    actor = wf.user_actor(principal, request)
    hazard = await _cluster_for_review(db, hazard_id, request, principal, "reject_cluster")
    previous_code = hazard.code
    if await wf.find_reason(db, hazard.type, body.reason_code) is None:
        raise api_error(422, "invalid_reason", f"Unknown rejection reason '{body.reason_code}' for {hazard.type}.")
    members = [m for m in await hazard_service.members_of(db, hazard.id)
               if m.review_status in hazard_service.UNREVIEWED_STATUSES]
    if not members:
        raise api_error(409, "nothing_to_review", "This cluster has no detection awaiting a decision.")
    htype = hazard.type
    for m in members:
        await wf.reject(db, m, actor, reason_code=body.reason_code, note=body.note)
    audit.record(db, request, "hazard.reject_cluster", principal=principal, actor_label=actor.name,
                 object_type="hazard", object_id=hazard_id,
                 details={"code": previous_code, "type": htype, "rejected": len(members),
                          "reason_code": body.reason_code, "note": body.note})
    return await _decision_out(db, hazard_id, previous_code, rejected=len(members))


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
    if hazard is None or hazard.status not in PUBLIC_STATUSES:
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
