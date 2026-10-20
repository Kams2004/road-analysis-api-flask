import csv
import io
import json
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_
from datetime import datetime, timedelta
from typing import Annotated, Optional

from app.api.openapi import err, errors, legacy

from app.db.session import get_db, AsyncSessionLocal
from app.models.detection import ACCEPTED_STATUSES, Detection, LocationStatus, ReviewStatus
from app.models.detection_review import DetectionReview
from app.models.hazard import Hazard, HazardStatus
from app.models.job_track_point import JobTrackPoint
from app.models.rejection_reason import RejectionReason
from app.models.validation_label import ValidationLabel
from app.schemas.schemas import (
    DetectionOut, DetectionListOut, ReviewIn, LocationCorrectIn,
    ValidationLabelOut, NearbyQueryIn, AlongRouteQueryIn,
    ClusterQueryIn, ClusteredDetectionsOut, ResolveClusterIn,
    AcceptIn, CorrectIn, RejectIn, ReopenIn, LocationFixIn,
    DetectionDetailOut, DetectionReviewOut, HazardOut, HazardRefOut,
    QueueItemOut, QueueListOut, TrajectoryOut, TrackPointOut,
)
from app.services import audit, detection_workflow as wf, hazard_service
from app.services.training_data import sync as training_sync
from app.services.hazards.lifecycle import service as hazard_lifecycle
from app.services.mobile_config import mobile_radius, route_corridor
from app.services.spatial import query_nearby, query_along_route, cluster_detections

from app.api.deps import Principal, api_error, get_principal

router = APIRouter()

MAX_LIMIT = 200
SORTS = {
    "created_at":  Detection.created_at,
    "captured_at": Detection.captured_at,
    "confidence":  Detection.confidence,
    "verifier_score": Detection.verifier_score,
    "type":        Detection.type,
}
# captured_at is the OSD timestamp; rows without one fall back to upload time.
_WHEN = func.coalesce(Detection.captured_at, Detection.created_at)


def _statuses(raw: Optional[str]) -> list[ReviewStatus]:
    try:
        return [ReviewStatus(s.strip()) for s in raw.split(",") if s.strip()] if raw else []
    except ValueError:
        raise api_error(422, "invalid_status", f"Unknown review_status in '{raw}'.",
                        allowed=[s.value for s in ReviewStatus])


def _end_of(d: Optional[datetime]) -> Optional[datetime]:
    """A bare date as `date_to` means the whole day."""
    if d is not None and (d.hour, d.minute, d.second, d.microsecond) == (0, 0, 0, 0):
        return d + timedelta(days=1)
    return d


def _order(q, sort: str, columns: dict, tiebreak):
    desc = sort.startswith("-")
    col = columns.get(sort.lstrip("-"))
    if col is None:
        raise api_error(422, "invalid_sort", f"Unknown sort '{sort}'.", allowed=sorted(columns))
    return q.order_by(col.desc().nullslast() if desc else col.asc().nullsfirst(), tiebreak.desc())


class DetectionFilters:
    """Query parameters shared by the list and the validation queue."""

    def __init__(
        self,
        type:            Optional[str] = Query(None, description="Detection class, e.g. `pothole`, `traffic_sign`, `speed_bump`.", examples=["pothole"]),
        subtype:         Optional[str] = Query(None, description="Detection subtype (e.g. the traffic-sign class)."),
        job_id:          Optional[str] = Query(None, description="Only detections produced by this job (UUID)."),
        hazard_id:       Optional[str] = Query(None, description="Only detections that belong to this hazard (UUID)."),
        country:         Optional[str] = Query(None, description="Country name (case-insensitive exact match).", examples=["Cameroon"]),
        city:            Optional[str] = Query(None, description="City name (case-insensitive exact match).", examples=["Douala"]),
        town:            Optional[str] = Query(None, description="Reverse-geocoded town (case-insensitive exact match).", examples=["Douala"]),
        locality:        Optional[str] = Query(None, description="Reverse-geocoded quartier / neighbourhood (case-insensitive exact match).", examples=["Akwa"]),
        location_status: Optional[str] = Query(None, description="Comma list of location statuses: `confirmed`, `approximate`, `missing`.", examples=["approximate,missing"]),
        date_from:       Optional[datetime] = Query(None, description="Capture time ≥ this instant (UTC; falls back to upload time when the OSD timestamp is unknown)."),
        date_to:         Optional[datetime] = Query(None, description="Capture time < this instant (UTC). A bare date (midnight) includes that whole day."),
        min_confidence:  Optional[float] = Query(None, ge=0, le=1, description="Minimum model confidence (0–1)."),
        max_confidence:  Optional[float] = Query(None, ge=0, le=1, description="Maximum model confidence (0–1)."),
        search:          Optional[str] = Query(None, description="Detection id prefix, or text contained in the place (street, quartier, arrondissement, town, location name) or raw OSD GPS text."),
        ai_decision:     Optional[str] = Query(None, description="Comma list of AI decisions: `auto_accept`, `auto_reject`, `review` (see *AI models*).", examples=["auto_accept,auto_reject"]),
        audit_sample:    Optional[bool] = Query(None, description="`true` = only automatic decisions drawn for a human audit (*AI decisions to audit*); `false` = exclude them."),
        sort:            str = Query("-created_at", description="Sort field, `-` prefix = descending: `created_at`, `captured_at`, `confidence`, `verifier_score`, `type`.", examples=["-confidence"]),
        skip:            int = Query(0, ge=0, description="Number of items to skip (offset)."),
        limit:           int = Query(50, ge=1, le=MAX_LIMIT, description=f"Page size (max {MAX_LIMIT})."),
    ):
        self.__dict__.update(locals())
        del self.__dict__["self"]

    def apply(self, q):
        if self.type:            q = q.where(Detection.type == self.type)
        if self.subtype:         q = q.where(Detection.subtype == self.subtype)
        if self.job_id:          q = q.where(Detection.job_id == self.job_id)
        if self.hazard_id:       q = q.where(Detection.hazard_id == self.hazard_id)
        if self.country:         q = q.where(Detection.country.ilike(self.country))
        if self.city:            q = q.where(Detection.city.ilike(self.city))
        if self.town:            q = q.where(Detection.town.ilike(self.town))
        if self.locality:        q = q.where(Detection.locality.ilike(self.locality))
        if self.location_status: q = q.where(Detection.location_status.in_(self.location_status.split(",")))
        if self.date_from:       q = q.where(_WHEN >= self.date_from)
        if self.date_to:         q = q.where(_WHEN < _end_of(self.date_to))
        if self.ai_decision:     q = q.where(Detection.ai_decision.in_([v.strip() for v in self.ai_decision.split(",") if v.strip()]))
        if self.audit_sample is not None:
            q = q.where(Detection.audit_sample.is_(True) if self.audit_sample else Detection.audit_sample.isnot(True))
        if self.min_confidence is not None: q = q.where(Detection.confidence >= self.min_confidence)
        if self.max_confidence is not None: q = q.where(Detection.confidence <= self.max_confidence)
        if self.search:
            s = self.search.strip()
            q = q.where(or_(Detection.id.ilike(f"{s}%"), Detection.location_name.ilike(f"%{s}%"),
                            Detection.raw_gps_text.ilike(f"%{s}%"),
                            *(getattr(Detection, c).ilike(f"%{s}%")
                              for c in ("display_location", "road", "locality", "district", "town"))))
        return q


async def _page(db: AsyncSession, q, f: DetectionFilters) -> tuple[int, list[Detection]]:
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0
    rows = (await db.execute(_order(q, f.sort, SORTS, Detection.id).offset(f.skip).limit(f.limit))).scalars().all()
    return total, list(rows)


async def _serialize(db: AsyncSession, dets: list[Detection], model=DetectionOut) -> list:
    """Detections + hazard_code / rejection_label looked up in two queries."""
    hazard_ids = {d.hazard_id for d in dets if d.hazard_id}
    codes = dict((await db.execute(select(Hazard.id, Hazard.code).where(Hazard.id.in_(hazard_ids)))).all()) \
        if hazard_ids else {}
    reason_codes = {d.rejection_code for d in dets if d.rejection_code}
    labels: dict[tuple[str, str], str] = {}
    if reason_codes:
        for r in (await db.execute(select(RejectionReason).where(RejectionReason.code.in_(reason_codes)))).scalars():
            labels[(r.detection_type, r.code)] = r.label
    out = []
    for d in dets:
        item = model.model_validate(d)
        item.hazard_code = codes.get(d.hazard_id)
        if d.rejection_code:
            item.rejection_label = labels.get((d.type, d.rejection_code)) or labels.get(("all", d.rejection_code))
        out.append(item)
    return out


async def _get(db: AsyncSession, detection_id: str) -> Detection:
    det = await db.get(Detection, detection_id)
    if not det:
        raise HTTPException(404, "Detection not found")
    return det


# ── OpenAPI documentation helpers ─────────────────────────────────────────────

_STATUS_LIST = [s.value for s in ReviewStatus]
E_NOT_FOUND = legacy(404, "Detection not found")
E_BAD_STATUS = err(422, "invalid_status", "Unknown review_status in 'pending,foo'.", allowed=_STATUS_LIST)
E_BAD_SORT = err(422, "invalid_sort", "Unknown sort 'foo'.", allowed=sorted(SORTS))


def _e_transition(action: str, frm: str, to: str) -> object:
    return err(409, "invalid_transition", f"Cannot {action} a detection in status '{frm}'.",
               **{"from": frm, "to": to})


E_BAD_BBOX = err(422, "invalid_bbox", "The corrected box is empty or outside the frame.")
E_BAD_LOCATION = err(422, "invalid_location", "latitude and longitude are required for a map pin.",
                     summary="invalid_location — missing/out-of-range coordinates, unknown track point or method")
E_BAD_GPS_TEXT = err(422, "invalid_gps_text",
                     "GPS text could not be parsed. Expected NMEA format: DDMM.MMMM,N,DDDMM.MMMM,E "
                     "(received: '0515.42N01013')")
DetectionId = Annotated[str, Path(description="Detection id (UUID).",
                                   examples=["3f2b7c1e-9a4d-4e0b-8c51-2f6d7a9e1b30"])]


@router.get("/", response_model=DetectionListOut, summary="List detections",
            responses=errors(E_BAD_STATUS, E_BAD_SORT))
async def list_detections(
    review_status: Optional[str] = Query(
        None, description="Comma list of review statuses: " + ", ".join(f"`{s}`" for s in _STATUS_LIST) + ".",
        examples=["validated,corrected"]),
    f: DetectionFilters = Depends(),
    db: AsyncSession = Depends(get_db),
):
    """
    Every detection, whatever its review status, with filters, sorting (`-` prefix = descending) and
    pagination. Each item carries `hazard_code` (the hazard it belongs to) and, for rejected
    detections, `rejection_label` resolved from the rejection-reason catalogue.
    """
    q = f.apply(select(Detection))
    statuses = _statuses(review_status)
    if statuses:
        q = q.where(Detection.review_status.in_(statuses))
    total, rows = await _page(db, q, f)
    return DetectionListOut(total=total, items=await _serialize(db, rows))


@router.get("/queue", response_model=QueueListOut, summary="Get the validation queue",
            responses=errors(E_BAD_STATUS, E_BAD_SORT))
async def validation_queue(
    status: str = Query("pending,location_pending",
                        description="Comma list of review statuses to include (default: the two awaiting a "
                                    "human decision)."),
    f: DetectionFilters = Depends(),
    db: AsyncSession = Depends(get_db),
):
    """
    The validation queue (RRAP-06-005): detections awaiting a decision, with the shared filters,
    sorting and pagination. Each item carries `cluster`: the active hazard it would join if accepted
    now (same type, within the clustering radius of its area), or `null` when it would open a new
    hazard or its position is unknown.
    """
    q = f.apply(select(Detection)).where(Detection.review_status.in_(_statuses(status)))
    total, rows = await _page(db, q, f)
    items = await _serialize(db, rows, QueueItemOut)

    types = {d.type for d in rows if d.latitude is not None}
    if types:
        ctx = await hazard_service.load_context(db)
        hazards = (await db.execute(select(Hazard).where(
            Hazard.status == HazardStatus.active.value, Hazard.type.in_(types)
        ))).scalars().all()
        for det, item in zip(rows, items):
            if det.latitude is None or det.longitude is None:
                continue
            h = hazard_service.pick_hazard(
                hazards, det.latitude, det.longitude, det.type, det.subtype,
                ctx.radius_for(det.latitude, det.longitude, det.type), ctx.distinct_subtype_types)
            if h is not None:
                item.cluster = HazardRefOut(hazard_id=h.id, hazard_code=h.code, detection_count=h.detection_count)
    return QueueListOut(total=total, items=items)


@router.get("/pending", response_model=DetectionListOut, summary="List detections awaiting review (legacy)",
            deprecated=True)
async def list_pending(
    skip: int = Query(0, description="Number of items to skip (offset)."),
    limit: int = Query(50, ge=1, le=MAX_LIMIT, description=f"Page size (max {MAX_LIMIT})."),
    db: AsyncSession = Depends(get_db),
):
    """
    Detections in status `pending` or `location_pending`, oldest first.

    **Deprecated** — use `GET /detections/queue`, which adds filters, sorting and the hazard each
    item would join.
    """
    q = select(Detection).where(Detection.review_status.in_([ReviewStatus.pending, ReviewStatus.location_pending]))
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar()
    items = (await db.execute(q.offset(skip).limit(limit).order_by(Detection.created_at.asc()))).scalars().all()
    return DetectionListOut(total=total, items=await _serialize(db, list(items)))


# ── Rejected dataset (RRAP-06-017) ───────────────────────────────────────────

REJECTED_SORTS = {**SORTS, "reviewed_at": Detection.reviewed_at}


def _rejected_query(type, reason_code, rejection_source, reviewer, date_from, date_to):
    q = select(Detection).where(Detection.review_status == ReviewStatus.rejected)
    if type:             q = q.where(Detection.type == type)
    if reason_code:      q = q.where(Detection.rejection_code == reason_code)
    if rejection_source: q = q.where(Detection.rejection_source == rejection_source)
    if reviewer:         q = q.where(Detection.reviewed_by.ilike(f"%{reviewer}%"))
    when = func.coalesce(Detection.reviewed_at, Detection.created_at)
    if date_from:        q = q.where(when >= date_from)
    if date_to:          q = q.where(when < _end_of(date_to))
    return q


def _rq_type():
    return Query(None, description="Detection class, e.g. `pothole`.", examples=["pothole"])


def _rq_reason():
    return Query(None, description="Rejection reason code (see `GET /rejection-reasons/`).",
                 examples=["low_confidence"])


def _rq_source():
    return Query(None, description="Who rejected: `human` (validator) or `auto` (confidence rule).",
                 examples=["auto"])


def _rq_reviewer():
    return Query(None, description="Reviewer name contains this text (case-insensitive).")


def _rq_from():
    return Query(None, description="Rejected (reviewed) at ≥ this instant (UTC; upload time when never reviewed).")


def _rq_to():
    return Query(None, description="Rejected (reviewed) at < this instant (UTC). A bare date includes that whole day.")


@router.get("/rejected", response_model=DetectionListOut, summary="List rejected detections",
            responses=errors(err(422, "invalid_sort", "Unknown sort 'foo'.", allowed=sorted(REJECTED_SORTS))))
async def list_rejected(
    type:             Optional[str] = _rq_type(),
    reason_code:      Optional[str] = _rq_reason(),
    rejection_source: Optional[str] = _rq_source(),
    reviewer:         Optional[str] = _rq_reviewer(),
    date_from:        Optional[datetime] = _rq_from(),
    date_to:          Optional[datetime] = _rq_to(),
    sort:  str = Query("-reviewed_at", description="Sort field, `-` prefix = descending: `reviewed_at`, "
                                                   "`created_at`, `captured_at`, `confidence`, `type`."),
    skip:  int = Query(0, ge=0, description="Number of items to skip (offset)."),
    limit: int = Query(50, ge=1, le=MAX_LIMIT, description=f"Page size (max {MAX_LIMIT})."),
    db: AsyncSession = Depends(get_db),
):
    """
    The rejected dataset (RRAP-06-017): detections in status `rejected`, by human validators or by the
    automatic low-confidence rule (`rejection_code` `low_confidence`, `rejection_source` `auto`). Used to
    review false positives and as negative training data.
    """
    q = _rejected_query(type, reason_code, rejection_source, reviewer, date_from, date_to)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0
    rows = (await db.execute(_order(q, sort, REJECTED_SORTS, Detection.id).offset(skip).limit(limit))).scalars().all()
    return DetectionListOut(total=total, items=await _serialize(db, list(rows)))


EXPORT_FIELDS = [
    "id", "job_id", "type", "subtype", "predicted_type", "confidence", "model_name", "model_version",
    "bbox_x1", "bbox_y1", "bbox_x2", "bbox_y2", "crop_url", "image_url", "rejection_code", "rejection_label",
    "rejection_source", "reviewed_by", "reviewed_at", "note", "captured_at", "latitude", "longitude",
]


def _export_row(d: Detection, labels: dict) -> dict:
    row = {k: getattr(d, k, None) for k in EXPORT_FIELDS}
    row["note"] = d.review_note
    row["rejection_label"] = labels.get((d.type, d.rejection_code)) or labels.get(("all", d.rejection_code))
    for k in ("reviewed_at", "captured_at"):
        row[k] = row[k].isoformat() if row[k] else None
    return row


class RejectedExportRow(BaseModel):
    """One row of the rejected-dataset export (JSON format). The CSV format has the same columns,
    with `bbox_x1`, `bbox_y1`, `bbox_x2`, `bbox_y2` instead of `bbox`."""
    id:               str             = Field(description="Detection id (UUID).")
    job_id:           str             = Field(description="Job that produced the detection.")
    type:             str             = Field(description="Detection class.", examples=["pothole"])
    subtype:          Optional[str]   = Field(description="Detection subtype.")
    predicted_type:   Optional[str]   = Field(description="Class originally predicted by the model.")
    confidence:       float           = Field(description="Model confidence (0–1).", examples=[0.31])
    model_name:       Optional[str]   = Field(description="Model that produced the detection.")
    model_version:    Optional[str]   = Field(description="Model weights version (`file@sha256[:12]`).")
    crop_url:         Optional[str]   = Field(description="Clean crop of the detected object.")
    image_url:        Optional[str]   = Field(description="Annotated frame.")
    rejection_code:   Optional[str]   = Field(description="Rejection reason code.", examples=["low_confidence"])
    rejection_label:  Optional[str]   = Field(description="Label of the rejection reason.")
    rejection_source: Optional[str]   = Field(description="`human` or `auto`.")
    reviewed_by:      Optional[str]   = Field(description="Reviewer (or `system`).")
    reviewed_at:      Optional[str]   = Field(description="Review time, ISO 8601 UTC.")
    note:             Optional[str]   = Field(description="Reviewer's note.")
    captured_at:      Optional[str]   = Field(description="Capture time (OSD), ISO 8601 UTC.")
    latitude:         Optional[float] = Field(description="WGS84 latitude.")
    longitude:        Optional[float] = Field(description="WGS84 longitude.")
    bbox:             list[Optional[float]] = Field(description="Bounding box `[x1, y1, x2, y2]` in frame pixels.")


@router.get(
    "/rejected/export", summary="Export the rejected dataset",
    responses={200: {
        "description": "File download (`Content-Disposition: attachment; "
                       "filename=\"rejected-detections-YYYYMMDD-HHMM.csv|json\"`).",
        "model": list[RejectedExportRow],
        "content": {"text/csv": {
            "schema": {"type": "string"},
            "example": ",".join(EXPORT_FIELDS) + "\n"
                       "3f2b7c1e-…,9a1c…,pothole,,pothole,0.31,yolov8-pothole,best.pt@1a2b3c4d5e6f,"
                       "128.0,240.5,310.2,402.0,/media/crops/3f2b.jpg,/media/frames/3f2b.jpg,low_confidence,"
                       "Low confidence,auto,system,2026-03-02T10:15:00,,2026-03-02T09:58:12,4.0512,9.7085\n",
        }},
    }},
)
async def export_rejected(
    request: Request,
    format:           str = Query("csv", pattern="^(csv|json)$",
                                  description="`csv` (default, header row + one line per detection) or `json` "
                                              "(array of objects)."),
    type:             Optional[str] = _rq_type(),
    reason_code:      Optional[str] = _rq_reason(),
    rejection_source: Optional[str] = _rq_source(),
    reviewer:         Optional[str] = _rq_reviewer(),
    date_from:        Optional[datetime] = _rq_from(),
    date_to:          Optional[datetime] = _rq_to(),
    principal: Principal = Depends(get_principal),
):
    """
    Streaming download of the whole rejected dataset (false-positive training data), with the same
    filters as `GET /detections/rejected`, ordered by review time. No pagination — the file is
    streamed. `format=csv` answers `text/csv`, `format=json` answers `application/json` (array of
    `RejectedExportRow`). Audited as `detection.rejected_export`.
    """
    q = _rejected_query(type, reason_code, rejection_source, reviewer, date_from, date_to) \
        .order_by(Detection.reviewed_at.asc().nullsfirst(), Detection.id)
    await audit.record_now(request, "detection.rejected_export", principal=principal,
                           details={"format": format, "type": type, "reason_code": reason_code})

    async def rows():
        # Own session: the request-scoped one is closed before streaming ends.
        async with AsyncSessionLocal() as db:
            labels = {(r.detection_type, r.code): r.label
                      for r in (await db.execute(select(RejectionReason))).scalars()}
            offset = 0
            while True:
                batch = (await db.execute(q.offset(offset).limit(500))).scalars().all()
                if not batch:
                    return
                for d in batch:
                    yield _export_row(d, labels)
                offset += len(batch)

    if format == "json":
        async def body():
            yield "["
            first = True
            async for r in rows():
                r["bbox"] = [r.pop(k) for k in ("bbox_x1", "bbox_y1", "bbox_x2", "bbox_y2")]
                yield ("" if first else ",") + json.dumps(r)
                first = False
            yield "]"
        media, ext = "application/json", "json"
    else:
        async def body():
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=EXPORT_FIELDS)
            writer.writeheader()
            async for r in rows():
                writer.writerow(r)
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate()
            yield buf.getvalue()
        media, ext = "text/csv", "csv"
    name = f"rejected-detections-{datetime.utcnow():%Y%m%d-%H%M}.{ext}"
    return StreamingResponse(body(), media_type=media,
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ── Mobile / map queries — accepted + confirmed only (RRAP-06-019) ───────────

@router.post("/nearby", response_model=DetectionListOut, summary="Find accepted detections around a position")
async def nearby_detections(
    body: NearbyQueryIn,
    db: AsyncSession = Depends(get_db),
):
    """
    **Mobile — circle query.** Returns every *accepted* (`validated`/`corrected`) detection with a
    confirmed location within `radius_m` metres of the given position (exact Haversine distance).
    Pending, rejected and resolved detections are never returned (RRAP-06-019), nor detections of a
    hazard that has expired (`expires_at` passed) or whose type/subtype policy hides it from drivers
    (`hazard_lifecycle` settings, `show_to_drivers`). Each item carries `alert_priority`.

    `radius_m` defaults to — and is capped at — the `mobile.visibility_radius_m` platform setting.
    Not paginated: `total` equals the number of items. Typical use: refresh the hazards shown on the
    app's map when the device moves.
    """
    # Bounding-box pre-filter then exact Haversine, all inside PostgreSQL.
    radius = await mobile_radius(db, body.radius_m)   # default & cap: mobile.visibility_radius_m
    ids = await query_nearby(db, body.latitude, body.longitude, radius)
    if not ids:
        return DetectionListOut(total=0, items=[])
    items = (
        await db.execute(select(Detection).where(Detection.id.in_(ids)))
    ).scalars().all()
    # Hazard lifecycle: resolved / expired hazards and types hidden from drivers are dropped.
    items = await hazard_lifecycle.filter_for_drivers(db, list(items))
    return DetectionListOut(total=len(items), items=await _serialize(db, items))


@router.post("/along-route", response_model=DetectionListOut, summary="Find accepted detections along a route",
             responses=errors(legacy(422, "At least 2 waypoints are required to define a route segment.")))
async def detections_along_route(
    body: AlongRouteQueryIn,
    db: AsyncSession = Depends(get_db),
):
    """
    **Mobile — route corridor query.** Returns every *accepted* (`validated`/`corrected`) detection
    with a confirmed location within `corridor_m` metres (each side) of the polyline `waypoints`
    (exact point-to-segment distance). `corridor_m` defaults to the `mobile.route_corridor_m` setting.
    Detections of an expired hazard or of a type/subtype hidden from drivers (`hazard_lifecycle`
    policies) are left out; each item carries `alert_priority`.

    Send the full decoded route geometry (e.g. the OSRM polyline), not just origin and destination,
    or detections on curved roads will be missed. Not paginated: `total` equals the number of items.
    Typical use: overlay hazards on the planned route and compute its safety score.
    """
    # Two phases, no PostGIS: SQL bounding box padded by the corridor, then exact
    # point-to-segment Haversine in Python on the surviving rows.
    if len(body.waypoints) < 2:
        raise HTTPException(422, "At least 2 waypoints are required to define a route segment.")

    pairs = [(wp.latitude, wp.longitude) for wp in body.waypoints]
    ids   = await query_along_route(db, pairs, await route_corridor(db, body.corridor_m))

    if not ids:
        return DetectionListOut(total=0, items=[])
    items = (
        await db.execute(select(Detection).where(Detection.id.in_(ids)))
    ).scalars().all()
    # Hazard lifecycle: resolved / expired hazards and types hidden from drivers are dropped.
    items = await hazard_lifecycle.filter_for_drivers(db, list(items))
    return DetectionListOut(total=len(items), items=await _serialize(db, items))


@router.post("/clusters", response_model=ClusteredDetectionsOut, summary="Cluster accepted detections")
async def get_clusters(
    body: ClusterQueryIn,
    db: AsyncSession = Depends(get_db),
):
    """
    Groups *accepted* (`validated`/`corrected`) detections with a confirmed location into spatial
    clusters, computed on the fly with the global Cluster Configuration (`GET /clustering/config`):
    each cluster uses the radius resolved at its seed detection for its type (zone → urban /
    out-of-agglomeration profile → per-type value). Optional filters by type, subtype and job. Resolved detections are excluded (RRAP-06-019) —
    resolved hazards are listed by `GET /hazards/?status=resolved`. For the persisted, de-duplicated
    hazards use the *Hazards* endpoints.
    """
    ctx = await hazard_service.load_context(db)

    q = select(Detection).where(
        Detection.review_status.in_(ACCEPTED_STATUSES),
        Detection.location_status == LocationStatus.confirmed.value,
        Detection.latitude.isnot(None),
        Detection.longitude.isnot(None),
    )
    if body.type:    q = q.where(Detection.type    == body.type)
    if body.subtype: q = q.where(Detection.subtype == body.subtype)
    if body.job_id:  q = q.where(Detection.job_id  == body.job_id)

    detections = (await db.execute(q)).scalars().all()
    det_map = {d.id: d for d in detections}

    points = [{"id": d.id, "latitude": d.latitude, "longitude": d.longitude, "type": d.type} for d in detections]
    raw_clusters = cluster_detections(points, lambda p: ctx.radius_for(p["latitude"], p["longitude"], p["type"]))
    radius = max((c["radius_m"] for c in raw_clusters),
                 default=ctx.resolve(None, None, body.type).radius_m)

    clusters = []
    for c in raw_clusters:
        resolved_count = sum(
            1 for did in c["detection_ids"]
            if det_map.get(did) and det_map[did].review_status == ReviewStatus.resolved
        )
        clusters.append({
            **c,
            "resolved_count": resolved_count,
            "is_resolved":    resolved_count == c["count"],
            "detection_coords": [
                [det_map[did].latitude, det_map[did].longitude]
                for did in c["detection_ids"]
                if det_map.get(did) and det_map[did].latitude is not None
            ],
        })

    active   = sum(1 for c in clusters if not c["is_resolved"])
    resolved = sum(1 for c in clusters if c["is_resolved"])

    return ClusteredDetectionsOut(
        radius_m=radius,
        total_detections=len(detections),
        total_clusters=len(clusters),
        active_clusters=active,
        resolved_clusters=resolved,
        clusters=clusters,
    )


@router.post("/clusters/resolve", response_model=DetectionListOut, summary="Resolve a detection cluster (legacy)",
             deprecated=True,
             responses=errors(legacy(404, "No detections found for the given IDs"),
                              legacy(422, "detection_ids must not be empty")))
async def resolve_cluster(
    body: ResolveClusterIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """
    **Deprecated** — use `POST /hazards/{hazard_id}/resolve`.

    Called when a vehicle drives through a cluster zone again. If `new_detection_count` is lower than
    the number of `detection_ids` found, every accepted member goes through the `resolve` transition
    (`validated`/`corrected` → `resolved`, audited `detection.resolve`) and a hazard left with no
    accepted member becomes `resolved`. Otherwise nothing changes (the hazard persists). Returns the
    member detections in their current state (unknown ids are ignored).
    """
    if not body.detection_ids:
        raise HTTPException(422, "detection_ids must not be empty")

    rows = (await db.execute(
        select(Detection).where(Detection.id.in_(body.detection_ids))
    )).scalars().all()

    if not rows:
        raise HTTPException(404, "No detections found for the given IDs")

    original_count = len(rows)
    if body.new_detection_count >= original_count:
        # Hazard persists — nothing to resolve
        return DetectionListOut(total=len(rows), items=await _serialize(db, list(rows)))

    actor = wf.user_actor(principal, request, body.resolved_by)
    now = datetime.utcnow()
    for det in rows:
        if det.review_status in ACCEPTED_STATUSES:
            await wf.resolve(db, det, actor, note="cluster resolution")
    for hid in {d.hazard_id for d in rows if d.hazard_id}:
        hazard = await db.get(Hazard, hid)
        still_open = (await db.execute(select(func.count()).where(
            Detection.hazard_id == hid, Detection.review_status.in_(ACCEPTED_STATUSES)))).scalar()
        if hazard is not None and hazard.status == HazardStatus.active.value and not still_open:
            hazard.status, hazard.resolved_at, hazard.resolved_by = HazardStatus.resolved.value, now, actor.name
            hazard.resolution_source = "manual"
    await db.commit()
    for det in rows:
        await db.refresh(det)

    return DetectionListOut(total=len(rows), items=await _serialize(db, list(rows)))


@router.get("/labels/dataset", response_model=list[ValidationLabelOut], summary="List validation labels")
async def list_labels(
    detection_type: Optional[str] = Query(None, description="`pothole`, `traffic_sign` or `speed_bump`.",
                                          examples=["pothole"]),
    label:          Optional[str] = Query(None, description="Exact label (class name chosen by the validator)."),
    skip: int = Query(0, description="Number of items to skip (offset)."),
    limit: int = Query(100, description="Maximum number of items."),
    db: AsyncSession = Depends(get_db),
):
    """
    The classifier training labels written when detections are accepted or corrected (one per
    detection), newest first. Returns a plain array (no `total`). Versioned snapshots of these labels
    are built by `POST /datasets/export`.
    """
    q = select(ValidationLabel)
    if detection_type: q = q.where(ValidationLabel.detection_type == detection_type)
    if label:          q = q.where(ValidationLabel.label == label)
    rows = (await db.execute(q.offset(skip).limit(limit).order_by(ValidationLabel.labeled_at.desc()))).scalars().all()
    return rows


# ── Single detection ─────────────────────────────────────────────────────────

async def _detail(db: AsyncSession, det: Detection) -> DetectionDetailOut:
    out = (await _serialize(db, [det], DetectionDetailOut))[0]
    history = (await db.execute(
        select(DetectionReview).where(DetectionReview.detection_id == det.id)
        .order_by(DetectionReview.created_at.desc())
    )).scalars().all()
    out.history = [DetectionReviewOut.model_validate(h) for h in history]
    if det.hazard_id:
        hazard = await db.get(Hazard, det.hazard_id)
        out.hazard = HazardOut.model_validate(hazard) if hazard else None
    return out


async def _commit_detail(db: AsyncSession, det: Detection) -> DetectionDetailOut:
    await db.commit()
    training_sync.kick()            # write the training-data objects of the decision (background)
    await db.refresh(det)
    return await _detail(db, det)


@router.get("/{detection_id}", response_model=DetectionDetailOut, summary="Get a detection",
            responses=errors(E_NOT_FOUND))
async def get_detection(detection_id: DetectionId, db: AsyncSession = Depends(get_db)):
    """A detection with its review history (newest first) and the hazard it belongs to, if any."""
    return await _detail(db, await _get(db, detection_id))


@router.post("/{detection_id}/accept", response_model=DetectionDetailOut, summary="Accept a detection",
             responses=errors(E_NOT_FOUND, _e_transition("accept", "location_pending", "validated"), E_BAD_BBOX))
async def accept_detection(detection_id: DetectionId, body: AcceptIn, request: Request,
                           db: AsyncSession = Depends(get_db), principal: Principal = Depends(get_principal)):
    """
    Accept the detection as predicted: `pending` → `validated`, or `pending` → `corrected` when its
    position was fixed by hand (map pin, trajectory point, typed OSD text). A detection still in
    `location_pending` must first get a position (`POST /detections/{detection_id}/location`).

    With `bbox` (the predicted box is wrong) the corrected box is stored in `corrected_bbox` and the
    detection becomes `corrected`.

    Side effects: the training label is written (default label = subtype or type, default severity 1),
    the training sample is recorded (see the *Datasets* tag), the detection joins the nearest active
    hazard of its type within the clustering radius or opens a new one, a history row and the audit
    entry `detection.accept` are written.
    """
    det = await _get(db, detection_id)
    await wf.accept(db, det, wf.user_actor(principal, request), label=body.label,
                    severity_score=body.severity_score, note=body.note, bbox=body.bbox)
    return await _commit_detail(db, det)


@router.post("/{detection_id}/correct", response_model=DetectionDetailOut, summary="Correct a detection's class",
             responses=errors(E_NOT_FOUND, _e_transition("correct", "validated", "corrected"), E_BAD_BBOX))
async def correct_detection(detection_id: DetectionId, body: CorrectIn, request: Request,
                            db: AsyncSession = Depends(get_db), principal: Principal = Depends(get_principal)):
    """
    Accept the detection with a different class: `pending` → `corrected`. `type`/`subtype` take the
    new values while `predicted_type`/`predicted_subtype` keep the model output; `bbox` optionally
    corrects the box too.

    Side effects: training label written (default label = new subtype or type, default severity 1),
    hazard attach (as for accept), history row and audit entry `detection.correct` with the previous
    and new class.
    """
    det = await _get(db, detection_id)
    await wf.correct(db, det, wf.user_actor(principal, request), type=body.type, subtype=body.subtype,
                     label=body.label, severity_score=body.severity_score, note=body.note, bbox=body.bbox)
    return await _commit_detail(db, det)


@router.post("/{detection_id}/reject", response_model=DetectionDetailOut, summary="Reject a detection",
             responses=errors(E_NOT_FOUND, _e_transition("reject", "validated", "rejected"),
                              err(422, "invalid_reason", "Unknown rejection reason 'blurry' for pothole.")))
async def reject_detection(detection_id: DetectionId, body: RejectIn, request: Request,
                           db: AsyncSession = Depends(get_db), principal: Principal = Depends(get_principal)):
    """
    Mark the detection as not a hazard: `pending` or `location_pending` → `rejected`
    (`rejection_source` = `human`). `reason_code` must exist in the rejection-reason catalogue for the
    detection's type or for `all` (`GET /rejection-reasons/?detection_type=…`).

    Side effects: history row and audit entry `detection.reject`; the detection joins the rejected
    dataset (`GET /detections/rejected`).
    """
    det = await _get(db, detection_id)
    await wf.reject(db, det, wf.user_actor(principal, request), reason_code=body.reason_code, note=body.note)
    return await _commit_detail(db, det)


@router.post("/{detection_id}/reopen", response_model=DetectionDetailOut, summary="Reopen a reviewed detection",
             responses=errors(E_NOT_FOUND, _e_transition("reopen", "pending", "pending")))
async def reopen_detection(detection_id: DetectionId, body: ReopenIn, request: Request,
                           db: AsyncSession = Depends(get_db), principal: Principal = Depends(get_principal)):
    """
    Reverse a final decision: `validated`, `corrected`, `rejected` or `resolved` → `pending` (or
    `location_pending` when the position is not confirmed). Reviewer, note, rejection reason and
    resolution are cleared; an accepted detection leaves its hazard (which is recomputed, or deleted
    when empty). Requires the *delete* right of validations. Audited as `detection.reopen` with the
    mandatory `reason`.
    """
    det = await _get(db, detection_id)
    await wf.reopen(db, det, wf.user_actor(principal, request), reason=body.reason)
    return await _commit_detail(db, det)


@router.post("/{detection_id}/location", response_model=DetectionDetailOut, summary="Confirm a detection's position",
             responses=errors(E_NOT_FOUND, _e_transition("location fix", "validated", "pending"),
                              E_BAD_LOCATION, E_BAD_GPS_TEXT))
async def fix_location(detection_id: DetectionId, body: LocationFixIn, request: Request,
                       db: AsyncSession = Depends(get_db), principal: Principal = Depends(get_principal)):
    """
    Set and confirm the position (RRAP-06-012/013/014): `location_pending` → `pending`, or
    `pending` → `pending` to correct it before deciding. `method`:

    * `map_pin` — `latitude` + `longitude` required;
    * `trajectory_point` — `track_point_id` of the same job's vehicle track
      (`GET /detections/{detection_id}/trajectory`);
    * `ocr_text` — `raw_gps_text` typed from the frame's OSD (NMEA, e.g. `0515.4260,N,01013.5383,E,028KM/H`);
    * `accept_proposed` — accept the interpolated position (only when `location_status` is `approximate`).

    The location becomes `confirmed`; the place name, city and country are reverse-geocoded. A
    later accept then gives `corrected` (except for `accept_proposed`). Audited as
    `detection.location_fix`.
    """
    det = await _get(db, detection_id)
    await wf.fix_location(db, det, wf.user_actor(principal, request), method=body.method,
                          latitude=body.latitude, longitude=body.longitude, track_point_id=body.track_point_id,
                          raw_gps_text=body.raw_gps_text, note=body.note)
    return await _commit_detail(db, det)


@router.get("/{detection_id}/trajectory", response_model=TrajectoryOut, summary="Get the vehicle track around a detection",
            responses=errors(E_NOT_FOUND))
async def detection_trajectory(detection_id: DetectionId,
                               window_s: float = Query(60, gt=0, le=3600,
                                                       description="Half-width of the time window around the "
                                                                   "detection, in seconds of video."),
                               db: AsyncSession = Depends(get_db)):
    """
    The vehicle track of the detection's job (valid OSD GPS fixes) within ±`window_s` seconds of the
    detection's video time — or the whole track when that time is unknown — ordered by frame, at most
    5000 points. Used to pick a `trajectory_point` when confirming a position.
    """
    det = await _get(db, detection_id)
    q = select(JobTrackPoint).where(JobTrackPoint.job_id == det.job_id)
    if det.video_time_s is not None:
        q = q.where(JobTrackPoint.video_time_s.between(det.video_time_s - window_s, det.video_time_s + window_s))
    points = (await db.execute(q.order_by(JobTrackPoint.frame_number).limit(5000))).scalars().all()
    return TrajectoryOut(detection_time_s=det.video_time_s,
                         points=[TrackPointOut.model_validate(p) for p in points])


@router.get("/{detection_id}/label", response_model=ValidationLabelOut, summary="Get a detection's training label",
            responses=errors(legacy(404, "No label found for this detection")))
async def get_label(detection_id: DetectionId, db: AsyncSession = Depends(get_db)):
    """The classifier training label written when the detection was accepted or corrected."""
    row = (await db.execute(
        select(ValidationLabel).where(ValidationLabel.detection_id == detection_id)
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "No label found for this detection")
    return row


# ── Legacy endpoints — thin wrappers over the workflow, same rules ────────────

@router.patch("/{detection_id}/location", response_model=DetectionOut, summary="Correct the OSD GPS text (legacy)",
              deprecated=True,
              responses=errors(E_NOT_FOUND, _e_transition("location fix", "validated", "pending"),
                               E_BAD_GPS_TEXT,
                               err(422, "invalid_location", "raw_gps_text is required.")))
async def correct_location(
    detection_id: DetectionId,
    body: LocationCorrectIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """
    **Deprecated** — use `POST /detections/{detection_id}/location` with `method` = `ocr_text`.

    The validator types the GPS text read on the frame's OSD (NMEA, e.g.
    `0515.4260,N,01013.5383,E,028KM/H`); it is parsed into WGS84 coordinates and the location becomes
    `confirmed` (`location_pending`/`pending` → `pending`). Same rules and audit as the new endpoint;
    answers the detection without history.
    """
    det = await _get(db, detection_id)
    await wf.fix_location(db, det, wf.user_actor(principal, request, body.reviewed_by),
                          method="ocr_text", raw_gps_text=body.raw_gps_text)
    await db.commit()
    await db.refresh(det)
    return (await _serialize(db, [det]))[0]


@router.patch("/{detection_id}/review", response_model=DetectionOut, summary="Validate or reject a detection (legacy)",
              deprecated=True,
              responses=errors(
                  E_NOT_FOUND, _e_transition("accept", "rejected", "validated"),
                  legacy(422, "Cannot validate: the location is not confirmed. "
                              "Use POST /{id}/location to confirm the position first.", summary="location not confirmed"),
                  legacy(422, "label is required when validating a detection", summary="label missing"),
                  legacy(422, "severity_score (0-3) is required when validating a pothole", summary="severity missing"),
                  legacy(422, "status must be 'validated' or 'rejected' (use /reopen or /correct otherwise)",
                         summary="unsupported status")))
async def review_detection(
    detection_id: DetectionId,
    body: ReviewIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """
    **Deprecated** — use `POST /detections/{detection_id}/accept` or `/reject`.

    `status` = `validated` accepts the detection (location must be confirmed, `label` required,
    `severity_score` required for potholes); `status` = `rejected` rejects it — without a reason code,
    for older clients. Same transitions, history and audit as the new endpoints; answers the detection
    without history.
    """
    det = await _get(db, detection_id)
    actor = wf.user_actor(principal, request, body.reviewed_by)

    if body.status == ReviewStatus.validated:
        if det.latitude is None or det.location_status != LocationStatus.confirmed.value:
            raise HTTPException(
                422,
                "Cannot validate: the location is not confirmed. "
                "Use POST /{id}/location to confirm the position first."
            )
        if not body.label:
            raise HTTPException(422, "label is required when validating a detection")
        if det.type == "pothole" and body.severity_score is None:
            raise HTTPException(422, "severity_score (0-3) is required when validating a pothole")
        await wf.accept(db, det, actor, label=body.label, severity_score=body.severity_score, note=body.note)
    elif body.status == ReviewStatus.rejected:
        await wf.reject(db, det, actor, reason_code=None, note=body.note, require_reason=False)
    else:
        raise HTTPException(422, "status must be 'validated' or 'rejected' (use /reopen or /correct otherwise)")

    await db.commit()
    training_sync.kick()
    await db.refresh(det)
    return (await _serialize(db, [det]))[0]
