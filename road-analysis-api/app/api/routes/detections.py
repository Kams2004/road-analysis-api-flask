from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from datetime import datetime
from typing import Optional, List

from app.db.session import get_db
from app.models.detection import Detection, ReviewStatus
from app.models.validation_label import ValidationLabel
from app.schemas.schemas import (
    DetectionOut, DetectionListOut, ReviewIn, LocationCorrectIn,
    ValidationLabelOut, NearbyQueryIn, AlongRouteQueryIn,
)
from app.services.osd_parser import parse_gps_text, _fix_dot_separators
from app.services.geocoding import reverse_geocode
from app.services.spatial import query_nearby, query_along_route

router = APIRouter()


@router.get("/", response_model=DetectionListOut)
async def list_detections(
    job_id:        Optional[str] = None,
    type:          Optional[str] = None,
    subtype:       Optional[str] = None,
    review_status: Optional[ReviewStatus] = None,
    skip:  int = 0,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
):
    q = select(Detection)
    if job_id:        q = q.where(Detection.job_id == job_id)
    if type:          q = q.where(Detection.type == type)
    if subtype:       q = q.where(Detection.subtype == subtype)
    if review_status: q = q.where(Detection.review_status == review_status)

    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar()
    items = (await db.execute(q.offset(skip).limit(limit).order_by(Detection.created_at.desc()))).scalars().all()
    return DetectionListOut(total=total, items=items)


@router.get("/pending", response_model=DetectionListOut)
async def list_pending(
    skip: int = 0, limit: int = 50,
    db: AsyncSession = Depends(get_db),
):
    """All detections awaiting human review."""
    q = select(Detection).where(Detection.review_status == ReviewStatus.pending)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar()
    items = (await db.execute(q.offset(skip).limit(limit).order_by(Detection.created_at.asc()))).scalars().all()
    return DetectionListOut(total=total, items=items)


@router.post("/nearby", response_model=DetectionListOut)
async def nearby_detections(
    body: NearbyQueryIn,
    db: AsyncSession = Depends(get_db),
):
    """
    **Mobile — circle query.**

    Returns all *validated* detections with GPS coordinates that lie within
    `radius_m` metres of the supplied position.

    Uses a SQL bounding-box pre-filter followed by the exact Haversine
    formula — all computation happens inside PostgreSQL, so only matching
    rows are transferred.

    Typical use: call when the device location updates to show nearby hazards
    on the home/navigation map without loading the entire detections table.
    """
    ids = await query_nearby(db, body.latitude, body.longitude, body.radius_m)
    if not ids:
        return DetectionListOut(total=0, items=[])
    items = (
        await db.execute(select(Detection).where(Detection.id.in_(ids)))
    ).scalars().all()
    return DetectionListOut(total=len(items), items=list(items))


@router.post("/along-route", response_model=DetectionListOut)
async def detections_along_route(
    body: AlongRouteQueryIn,
    db: AsyncSession = Depends(get_db),
):
    """
    **Mobile — route corridor query.**

    Returns all *validated* detections with GPS coordinates that lie within
    `corridor_m` metres of the polyline defined by `waypoints`.

    Algorithm (two phases, no PostGIS required):
    1. A SQL bounding-box query with corridor padding reduces the candidate
       set to only rows near the route envelope.
    2. Exact point-to-segment Haversine distance is applied in Python to the
       surviving rows to eliminate false positives from the rectangular box.

    `waypoints` should be the full OSRM route geometry (decoded coordinates)
    so that every bend and turn is considered.  Sending only the origin +
    destination would miss detections on curved roads.

    Typical use: call once after the route is computed in the mobile app to
    overlay hazards on the planned path and compute the safety score.
    """
    if len(body.waypoints) < 2:
        raise HTTPException(422, "At least 2 waypoints are required to define a route segment.")

    pairs = [(wp.latitude, wp.longitude) for wp in body.waypoints]
    ids   = await query_along_route(db, pairs, body.corridor_m)

    if not ids:
        return DetectionListOut(total=0, items=[])
    items = (
        await db.execute(select(Detection).where(Detection.id.in_(ids)))
    ).scalars().all()
    return DetectionListOut(total=len(items), items=list(items))


@router.get("/{detection_id}", response_model=DetectionOut)
async def get_detection(detection_id: str, db: AsyncSession = Depends(get_db)):
    det = await db.get(Detection, detection_id)
    if not det:
        raise HTTPException(404, "Detection not found")
    return det


@router.patch("/{detection_id}/location", response_model=DetectionOut)
async def correct_location(
    detection_id: str,
    body: LocationCorrectIn,
    db: AsyncSession = Depends(get_db),
):
    """
    Validator corrects the GPS text read from the frame image.
    - Accepts raw NMEA text: e.g. '0515.4260,N,01013.5383,E,028KM/H'
    - Transforms to decimal lat/lon and saves both raw and transformed values.
    - Returns 422 if the submitted text cannot be parsed into valid coordinates.
    """
    det = await db.get(Detection, detection_id)
    if not det:
        raise HTTPException(404, "Detection not found")

    # Auto-fix dot separators before parsing
    corrected = _fix_dot_separators(body.raw_gps_text)
    lat, lon, speed = parse_gps_text(corrected)
    if lat is None:
        # Try original as fallback
        lat, lon, speed = parse_gps_text(body.raw_gps_text)
    if lat is None:
        raise HTTPException(
            422,
            f"GPS text could not be parsed. "
            f"Expected NMEA format: DDMM.MMMM,N,DDDMM.MMMM,E  "
            f"(received: {body.raw_gps_text!r})"
        )

    det.raw_gps_text  = corrected  # save the fixed version
    det.latitude      = lat
    det.longitude     = lon
    if speed is not None:
        det.speed_kmh = speed
    det.location_name = reverse_geocode(lat, lon)

    await db.commit()
    await db.refresh(det)
    return det


@router.patch("/{detection_id}/review", response_model=DetectionOut)
async def review_detection(
    detection_id: str,
    body: ReviewIn,
    db: AsyncSession = Depends(get_db),
):
    """
    Validate or reject a detection.
    - Validation is blocked when latitude is None.
    - When validating, `label` is required. `severity` is required for potholes.
    - Writes a ValidationLabel row that feeds the classifier training dataset.
    """
    det = await db.get(Detection, detection_id)
    if not det:
        raise HTTPException(404, "Detection not found")
    if det.review_status != ReviewStatus.pending:
        raise HTTPException(409, f"Detection already reviewed: {det.review_status}")

    if body.status == ReviewStatus.validated:
        if det.latitude is None:
            raise HTTPException(
                422,
                "Cannot validate: GPS coordinates are missing. "
                "Use PATCH /{id}/location to provide the correct GPS text from the frame first."
            )
        if not body.label:
            raise HTTPException(422, "label is required when validating a detection")
        if det.type == "pothole" and body.severity_score is None:
            raise HTTPException(422, "severity_score (0-3) is required when validating a pothole")

        severity_score = body.severity_score if body.severity_score is not None else 0
        existing = (await db.execute(
            select(ValidationLabel).where(ValidationLabel.detection_id == detection_id)
        )).scalar_one_or_none()
        if not existing:
            db.add(ValidationLabel(
                detection_id=detection_id,
                detection_type=det.type,
                label=body.label,
                severity_score=severity_score,
                model_confidence=det.confidence,
                crop_url=det.crop_url,
                labeled_by=body.reviewed_by,
            ))

    det.review_status = body.status
    det.reviewed_by   = body.reviewed_by
    det.reviewed_at   = datetime.utcnow()
    det.review_note   = body.note
    await db.commit()
    await db.refresh(det)
    return det


@router.get("/{detection_id}/label", response_model=ValidationLabelOut)
async def get_label(detection_id: str, db: AsyncSession = Depends(get_db)):
    """Return the validation label for a detection."""
    row = (await db.execute(
        select(ValidationLabel).where(ValidationLabel.detection_id == detection_id)
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "No label found for this detection")
    return row


@router.get("/labels/dataset", response_model=list[ValidationLabelOut])
async def list_labels(
    detection_type: Optional[str] = None,
    label:          Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
):
    """
    Export the classifier training dataset.
    Filter by detection_type (pothole|traffic_sign|speed_bump) and/or label.
    """
    q = select(ValidationLabel)
    if detection_type: q = q.where(ValidationLabel.detection_type == detection_type)
    if label:          q = q.where(ValidationLabel.label == label)
    rows = (await db.execute(q.offset(skip).limit(limit).order_by(ValidationLabel.labeled_at.desc()))).scalars().all()
    return rows


@router.patch("/{detection_id}/review/undo", response_model=DetectionOut)
async def undo_review(
    detection_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Reset a reviewed detection back to pending."""
    det = await db.get(Detection, detection_id)
    if not det:
        raise HTTPException(404, "Detection not found")

    det.review_status = ReviewStatus.pending
    det.reviewed_by   = None
    det.reviewed_at   = None
    det.review_note   = None
    await db.commit()
    await db.refresh(det)
    return det
