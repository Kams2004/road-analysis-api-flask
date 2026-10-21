"""
Detection validation workflow (RRAP-06-005/007/008/012/017/018).

    location_pending ──location_fix──▶ pending ──accept/correct──▶ validated | corrected ──resolve──▶ resolved
           │                              │                                 ▲                          │
           └────────reject────────▶ rejected ◀──reject──┘                    └────────unresolve─────────┘
    validated | corrected | rejected | resolved ──reopen──▶ pending | location_pending
    pending | location_pending ──resight (system)──▶ validated | corrected   (re-sighting of a confirmed hazard)

Every transition goes through `_enter`, which writes a `detection_reviews`
history row and an `audit_logs` row (`detection.<action>`), and keeps the
cluster membership in step (docs/CLUSTERS.md): accepting confirms the
detection's cluster, a location fix or class correction re-evaluates it,
a rejection leaves it (a candidate left empty is deleted).
Anything outside the table below is refused with HTTP 409
`invalid_transition`, itself audited as a failure.

The rules (transition table, confidence routing, target statuses) are pure
functions so they are unit-tested without a database.
"""
import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from fastapi import Request
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, actor_name, api_error
from app.modules.m06_hazards.models.detection import (
    ACCEPTED_STATUSES, MANUAL_LOCATION_SOURCES, Detection, LocationSource, LocationStatus, ReviewStatus,
)
from app.modules.m06_hazards.models.detection_review import DetectionReview
from app.modules.m07_ingestion.models.job_track_point import JobTrackPoint
from app.modules.m06_hazards.models.rejection_reason import RejectionReason
from app.modules.m06_hazards.models.validation_label import ValidationLabel
from app.modules.m05_ai_detection import ai_validation
from app.modules.m06_hazards import hazard_service, training_data
from app.core import audit
from app.modules.m03_maps.geocoding import has_address, reverse_geocode_details
from app.modules.m05_ai_detection.osd_parser import _fix_dot_separators, parse_gps_text
from app.core.settings_service import ValidationSettings

LOW_CONFIDENCE_CODE = ai_validation.LOW_CONFIDENCE_CODE

_FINAL = ("validated", "corrected", "rejected", "resolved")

# (from, to) → actions allowed to make that move
TRANSITIONS: dict[tuple[str, str], frozenset[str]] = {
    ("location_pending", "pending"):  frozenset({"location_fix"}),
    ("location_pending", "rejected"): frozenset({"reject", "auto_reject"}),
    ("location_pending", "validated"): frozenset({"resight"}),           # re-sighting, approximate position
    ("pending", "pending"):           frozenset({"location_fix"}),       # location corrected before deciding
    ("pending", "validated"):         frozenset({"accept", "auto_accept", "resight"}),
    ("pending", "corrected"):         frozenset({"accept", "correct", "resight"}),  # accept after a manual location fix
    ("pending", "rejected"):          frozenset({"reject", "auto_reject"}),
    ("validated", "resolved"):        frozenset({"resolve"}),
    ("corrected", "resolved"):        frozenset({"resolve"}),
    ("resolved", "validated"):        frozenset({"unresolve"}),          # hazard reopened
    ("resolved", "corrected"):        frozenset({"unresolve"}),
    **{(f, t): frozenset({"reopen"}) for f in _FINAL for t in ("pending", "location_pending")},
}


def is_allowed(from_status: Optional[str], to_status: str, action: str) -> bool:
    return action in TRANSITIONS.get((from_status or "", to_status), frozenset())


def location_status_for(lat: Optional[float], lon: Optional[float], interpolated: bool) -> tuple[str, str]:
    """(location_status, location_source) of a fresh processor detection."""
    if lat is None or lon is None:
        return LocationStatus.missing.value, LocationSource.none.value
    if interpolated:
        return LocationStatus.approximate.value, LocationSource.interpolated.value
    return LocationStatus.confirmed.value, LocationSource.ocr.value


def thresholds_for(rules: ValidationSettings, det_type: str) -> tuple[float, Optional[float]]:
    """(auto_reject_below, auto_accept_above) of section `validation` for a
    type; per-type overrides win over the global values, None in an override means inherit."""
    return ai_validation.validation_thresholds(rules, det_type)


def initial_status(confidence: float, location_status: str, det_type: str,
                   rules: ValidationSettings) -> tuple[str, str, Optional[str]]:
    """Single-stage confidence routing of RRAP-06-007 — section `validation`
    alone, no classifier, no audit sampling: (review_status, history action,
    rejection_code). The processor uses ai_validation.decide, of which this is
    the assisted-mode special case."""
    d = ai_validation.decide(ai_validation.legacy_policy(rules), det_type, confidence, None, location_status)
    return d.status, d.action, d.rejection_code


def reopen_target(location_status: Optional[str]) -> str:
    return (ReviewStatus.pending.value if location_status == LocationStatus.confirmed.value
            else ReviewStatus.location_pending.value)


def accept_target(location_source: Optional[str]) -> str:
    """Accepting a detection whose position a human fixed counts as a correction."""
    return ReviewStatus.corrected.value if location_source in MANUAL_LOCATION_SOURCES else ReviewStatus.validated.value


# ── actors ────────────────────────────────────────────────────────────────────

@dataclass
class Actor:
    type: str                              # user | system
    name: str
    principal: Optional[Principal] = None
    request: Optional[Request] = None


SYSTEM = Actor("system", "system")


def user_actor(principal: Principal, request: Optional[Request] = None, fallback: Optional[str] = None) -> Actor:
    return Actor("user", actor_name(principal, fallback) or principal.label or "anonymous", principal, request)


# ── transitions ──────────────────────────────────────────────────────────────

def _status(det: Detection) -> Optional[str]:
    rs = det.review_status
    return rs.value if isinstance(rs, ReviewStatus) else rs


async def check(det: Detection, to_status: str, action: str, actor: Actor) -> None:
    """Raise 409 invalid_transition (and audit the attempt) unless allowed."""
    frm = _status(det)
    if is_allowed(frm, to_status, action):
        return
    details = {"from": frm, "to": to_status}
    if actor.type == "system":
        raise RuntimeError(f"invalid system transition {frm} → {to_status} ({action})")
    await audit.record_now(actor.request, f"detection.{action}", principal=actor.principal, outcome="failure",
                           object_type="detection", object_id=det.id, details=details)
    raise api_error(409, "invalid_transition",
                    f"Cannot {action.replace('_', ' ')} a detection in status '{frm}'.", **details)


_ACCEPTED = {s.value for s in ACCEPTED_STATUSES}
_UNREVIEWED = {ReviewStatus.pending.value, ReviewStatus.location_pending.value}


async def _enter(db: AsyncSession, det: Detection, to_status: str, action: str, actor: Actor, *,
                 note: Optional[str] = None, details: Optional[dict[str, Any]] = None,
                 ctx: Optional[hazard_service.ClusterContext] = None, resight_others: bool = True,
                 cluster_changed: bool = False) -> None:
    """Apply an already-checked transition: status, history, audit, cluster."""
    frm = _status(det)
    det.review_status = ReviewStatus(to_status)
    _history(db, det, frm, to_status, action, actor, note, details)
    # training dataset (docs/TRAINING-DATA.md): sample upsert / supersede, frame marked dirty
    await training_data.record_decision(db, det, actor_type=actor.type)
    await _sync_cluster(db, det, frm, to_status, action, actor, ctx, resight_others, cluster_changed)


async def _sync_cluster(db: AsyncSession, det: Detection, frm: Optional[str], to_status: str, action: str,
                        actor: Actor, ctx, resight_others: bool, cluster_changed: bool) -> None:
    """Cluster membership after a transition (cluster-first, docs/CLUSTERS.md)."""
    from app.modules.m06_hazards.models.hazard import Hazard, HazardStatus
    if action in ("resight", "resolve", "unresolve"):
        return                                     # membership unchanged (the caller handles the hazard)
    hazard = await db.get(Hazard, det.hazard_id) if det.hazard_id else None
    if to_status == ReviewStatus.rejected.value:
        if hazard is not None and hazard.status != HazardStatus.dismissed.value:
            await hazard_service.detach(db, det)   # a candidate left empty is deleted
        return
    if action == "reopen":
        # back to the queue: stays in (or re-joins) its cluster, never auto-decided again
        if hazard is None or frm == ReviewStatus.rejected.value:
            await hazard_service.attach(db, det, ctx, auto=False)
        else:
            await hazard_service.recompute(db, hazard)
        return
    if action == "location_fix" or cluster_changed:
        await hazard_service.reattach(db, det, ctx)
        return
    if to_status in _ACCEPTED and frm in _UNREVIEWED:
        if hazard is None:
            hazard = await hazard_service.attach(db, det, ctx)
            if hazard is not None and resight_others:
                await hazard_service.resight_unreviewed(db, hazard, skip={det.id})
            return
        await hazard_service.recompute(db, hazard)
        from app.modules.m06_hazards.lifecycle import service as lifecycle_service
        if hazard.status == HazardStatus.resolved.value:
            await lifecycle_service.on_history_member(db, hazard, det)
            return
        confirmed_now = await hazard_service.confirm(db, hazard, det, actor)
        if hazard.status == HazardStatus.active.value:
            await lifecycle_service.on_attached(db, hazard, det, created=False)
            if confirmed_now and resight_others:
                await hazard_service.resight_unreviewed(db, hazard, skip={det.id})


def _history(db: AsyncSession, det: Detection, frm: Optional[str], to: str, action: str, actor: Actor,
             note: Optional[str], details: Optional[dict[str, Any]]) -> None:
    db.add(DetectionReview(detection_id=det.id, from_status=frm, to_status=to, action=action,
                           actor_type=actor.type, actor=actor.name, note=note, details=details))
    entry = {"from": frm, "to": to, **({"note": note} if note else {}), **(details or {})}
    if actor.type == "system":
        audit.record_system(db, f"detection.{action}", object_type="detection", object_id=det.id, details=entry)
    else:
        audit.record(db, actor.request, f"detection.{action}", principal=actor.principal,
                     actor_label=actor.name, object_type="detection", object_id=det.id, details=entry)


def record_creation(db: AsyncSession, det: Detection, action: str, details: Optional[dict] = None,
                    audited: bool = False) -> None:
    """History row for a detection the processor just created (status set by
    ai_validation.decide). Automatic decisions — and, with `audited`, AI
    suggestions left for a human audit — are also written to the audit trail."""
    status = _status(det)
    db.add(DetectionReview(detection_id=det.id, from_status=None, to_status=status, action=action,
                           actor_type="system", actor="system", details=details))
    if action != "create" or audited:
        event = f"detection.{action}" if action != "create" else "detection.ai_audit_sample"
        audit.record_system(db, event, object_type="detection", object_id=det.id,
                            details={"from": None, "to": status, **(details or {})})


# ── operations (routes and the processor call these, then commit) ─────────────

async def _upsert_label(db: AsyncSession, det: Detection, label: str, severity_score: int, labeled_by: str) -> None:
    """Training label for the classifier dataset (one row per detection)."""
    row = (await db.execute(
        select(ValidationLabel).where(ValidationLabel.detection_id == det.id)
    )).scalar_one_or_none()
    if row is None:
        db.add(ValidationLabel(detection_id=det.id, detection_type=det.type, label=label,
                               severity_score=severity_score, model_confidence=det.confidence,
                               crop_url=det.crop_url, labeled_by=labeled_by))
    else:
        row.detection_type, row.label, row.severity_score = det.type, label, severity_score
        row.labeled_by, row.labeled_at = labeled_by, datetime.utcnow()


def _stamp(det: Detection, actor: Actor, note: Optional[str]) -> None:
    det.reviewed_by = actor.name
    det.reviewed_at = datetime.utcnow()
    det.review_note = note


def clean_bbox(det: Detection, bbox: Optional[list[float]]) -> Optional[list[float]]:
    """Validate a corrected box [x1, y1, x2, y2] (frame pixels), clipped to the
    frame when its size is known; None when no box is given."""
    if bbox is None:
        return None
    if len(bbox) != 4:
        raise api_error(422, "invalid_bbox", "bbox must be [x1, y1, x2, y2] in frame pixels.")
    x1, y1, x2, y2 = (float(v) for v in bbox)
    x1, x2 = min(x1, x2), max(x1, x2)
    y1, y2 = min(y1, y2), max(y1, y2)
    if det.frame_width and det.frame_height:
        x1, x2 = max(0.0, x1), min(float(det.frame_width), x2)
        y1, y2 = max(0.0, y1), min(float(det.frame_height), y2)
    if x2 - x1 < 2 or y2 - y1 < 2:
        raise api_error(422, "invalid_bbox", "The corrected box is empty or outside the frame.")
    return [round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)]


def _set_bbox(det: Detection, bbox: Optional[list[float]]) -> Optional[dict]:
    """Store a corrected box; history details {previous, new} or None."""
    box = clean_bbox(det, bbox)
    if box is None:
        return None
    previous = det.corrected_bbox or [det.bbox_x1, det.bbox_y1, det.bbox_x2, det.bbox_y2]
    det.corrected_bbox = box
    return {"previous": previous, "new": box}


async def accept(db: AsyncSession, det: Detection, actor: Actor, *, label: Optional[str] = None,
                 severity_score: Optional[int] = None, note: Optional[str] = None,
                 bbox: Optional[list[float]] = None, resight_others: bool = True) -> None:
    """Accept as predicted; with `bbox` (box moved by the validator) the
    detection becomes `corrected`."""
    target = ReviewStatus.corrected.value if bbox is not None else accept_target(det.location_source)
    await check(det, target, "accept", actor)
    box_change = _set_bbox(det, bbox)
    _stamp(det, actor, note)
    await _upsert_label(db, det, label or det.subtype or det.type,
                        severity_score if severity_score is not None else 1, actor.name)
    details = {"label": label, "severity_score": severity_score} if label or severity_score is not None else {}
    if box_change:
        details["bbox"] = box_change
    await _enter(db, det, target, "accept", actor, note=note, details=details or None,
                 resight_others=resight_others)


async def correct(db: AsyncSession, det: Detection, actor: Actor, *, type: str, subtype: Optional[str],
                  label: Optional[str] = None, severity_score: Optional[int] = None,
                  note: Optional[str] = None, bbox: Optional[list[float]] = None) -> None:
    await check(det, ReviewStatus.corrected.value, "correct", actor)
    previous = {"type": det.type, "subtype": det.subtype}
    box_change = _set_bbox(det, bbox)
    if det.predicted_type is None:  # rows created before Module 6
        det.predicted_type, det.predicted_subtype = det.type, det.subtype
    det.type, det.subtype = type, subtype
    _stamp(det, actor, note)
    await _upsert_label(db, det, label or subtype or type,
                        severity_score if severity_score is not None else 1, actor.name)
    await _enter(db, det, ReviewStatus.corrected.value, "correct", actor, note=note,
                 details={"previous": previous, "new": {"type": type, "subtype": subtype},
                          **({"bbox": box_change} if box_change else {})},
                 cluster_changed=previous != {"type": type, "subtype": subtype})


async def find_reason(db: AsyncSession, det_type: str, code: str) -> Optional[RejectionReason]:
    """Reason `code` for this type, or the generic ("all") one."""
    rows = (await db.execute(select(RejectionReason).where(
        RejectionReason.code == code,
        or_(RejectionReason.detection_type == det_type, RejectionReason.detection_type == "all"),
    ))).scalars().all()
    return next((r for r in rows if r.detection_type == det_type), rows[0] if rows else None)


async def reject(db: AsyncSession, det: Detection, actor: Actor, *, reason_code: Optional[str],
                 note: Optional[str] = None, require_reason: bool = True) -> None:
    await check(det, ReviewStatus.rejected.value, "reject", actor)
    if require_reason or reason_code:
        if not reason_code or await find_reason(db, det.type, reason_code) is None:
            raise api_error(422, "invalid_reason", f"Unknown rejection reason '{reason_code}' for {det.type}.")
    det.rejection_code = reason_code
    det.rejection_source = "human"
    _stamp(det, actor, note)
    await _enter(db, det, ReviewStatus.rejected.value, "reject", actor, note=note,
                 details={"reason_code": reason_code})


async def auto_reject(db: AsyncSession, det: Detection, reason_code: str, *, note: Optional[str] = None) -> None:
    """System rejection (e.g. `not_seen_again` when a candidate is dismissed)."""
    await check(det, ReviewStatus.rejected.value, "auto_reject", SYSTEM)
    det.rejection_code, det.rejection_source = reason_code, "auto"
    _stamp(det, SYSTEM, note)
    await _enter(db, det, ReviewStatus.rejected.value, "auto_reject", SYSTEM, note=note,
                 details={"reason_code": reason_code})


async def resight(db: AsyncSession, det: Detection, hazard) -> None:
    """An unreviewed detection that joined a CONFIRMED hazard: decided by the
    system (`validated`, or `corrected` after a manual location fix), note
    "matches confirmed hazard HZ-…" (policy auto_confirm_resightings; the
    caller records the sighting)."""
    target = accept_target(det.location_source)
    await check(det, target, "resight", SYSTEM)
    note = f"matches confirmed hazard {hazard.code}"
    _stamp(det, SYSTEM, note)
    await _enter(db, det, target, "resight", SYSTEM, note=note,
                 details={"hazard_id": hazard.id, "hazard_code": hazard.code})


async def reopen(db: AsyncSession, det: Detection, actor: Actor, *, reason: str) -> None:
    """Reverse a finalized decision (permission validations:delete)."""
    target = reopen_target(det.location_status)
    await check(det, target, "reopen", actor)
    previous = {"reviewed_by": det.reviewed_by, "rejection_code": det.rejection_code,
                "hazard_id": det.hazard_id, **({"corrected_bbox": det.corrected_bbox} if det.corrected_bbox else {})}
    det.reviewed_by = det.reviewed_at = det.review_note = None
    det.corrected_bbox = None
    det.rejection_code = det.rejection_source = None
    det.resolved_at = None
    await _enter(db, det, target, "reopen", actor, note=reason, details={"previous": previous})


async def resolve(db: AsyncSession, det: Detection, actor: Actor, *, note: Optional[str] = None) -> None:
    await check(det, ReviewStatus.resolved.value, "resolve", actor)
    det.resolved_at = datetime.utcnow()
    await _enter(db, det, ReviewStatus.resolved.value, "resolve", actor, note=note)


async def unresolve(db: AsyncSession, det: Detection, actor: Actor, *, note: Optional[str] = None) -> None:
    """Back to the accepted status the detection had before it was resolved."""
    last = (await db.execute(
        select(DetectionReview).where(DetectionReview.detection_id == det.id,
                                      DetectionReview.to_status == ReviewStatus.resolved.value)
        .order_by(DetectionReview.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    target = (last.from_status if last and last.from_status in {s.value for s in ACCEPTED_STATUSES}
              else ReviewStatus.validated.value)
    await check(det, target, "unresolve", actor)
    det.resolved_at = None
    await _enter(db, det, target, "unresolve", actor, note=note)


LOCATION_METHODS = ("map_pin", "trajectory_point", "ocr_text", "accept_proposed")


async def fix_location(db: AsyncSession, det: Detection, actor: Actor, *, method: str,
                       latitude: Optional[float] = None, longitude: Optional[float] = None,
                       track_point_id: Optional[int] = None, raw_gps_text: Optional[str] = None,
                       note: Optional[str] = None) -> None:
    """Confirm the position (RRAP-06-012/013/014); location_pending → pending."""
    await check(det, ReviewStatus.pending.value, "location_fix", actor)
    speed = None
    if method == "map_pin":
        if latitude is None or longitude is None:
            raise api_error(422, "invalid_location", "latitude and longitude are required for a map pin.")
        source = LocationSource.map_pin.value
    elif method == "trajectory_point":
        point = await db.get(JobTrackPoint, track_point_id) if track_point_id is not None else None
        if point is None or point.job_id != det.job_id:
            raise api_error(422, "invalid_location", "Unknown trajectory point for this detection's job.")
        latitude, longitude, speed = point.latitude, point.longitude, point.speed_kmh
        source = LocationSource.trajectory.value
    elif method == "ocr_text":
        if not raw_gps_text:
            raise api_error(422, "invalid_location", "raw_gps_text is required.")
        fixed = _fix_dot_separators(raw_gps_text)
        latitude, longitude, speed = parse_gps_text(fixed)
        if latitude is None:
            latitude, longitude, speed = parse_gps_text(raw_gps_text)
        if latitude is None:
            raise api_error(422, "invalid_gps_text",
                            "GPS text could not be parsed. Expected NMEA format: DDMM.MMMM,N,DDDMM.MMMM,E "
                            f"(received: {raw_gps_text!r})")
        det.raw_gps_text = fixed
        source = LocationSource.ocr_text.value
    elif method == "accept_proposed":
        if det.location_status != LocationStatus.approximate.value or det.latitude is None:
            raise api_error(422, "invalid_location", "This detection has no proposed position to accept.")
        latitude, longitude = det.latitude, det.longitude
        source = det.location_source or LocationSource.interpolated.value
    else:
        raise api_error(422, "invalid_location", f"Unknown method '{method}'.")
    if not (-90.0 <= latitude <= 90.0) or not (-180.0 <= longitude <= 180.0):
        raise api_error(422, "invalid_location", "Coordinates out of range.")

    previous = {"latitude": det.latitude, "longitude": det.longitude,
                "location_status": det.location_status, "location_source": det.location_source}
    det.latitude, det.longitude = latitude, longitude
    if speed is not None:
        det.speed_kmh = speed
    det.location_status = LocationStatus.confirmed.value
    det.location_source = source
    geo = await asyncio.to_thread(reverse_geocode_details, latitude, longitude)
    det.location_name = geo["location_name"] or det.location_name
    det.city, det.country = geo["city"], geo["country"]
    for key in ("road", "locality", "district", "town", "region"):
        setattr(det, key, geo[key])
    det.display_location = geo["display"]
    det.geocoded_at = datetime.utcnow() if has_address(geo) else None
    await _enter(db, det, ReviewStatus.pending.value, "location_fix", actor, note=note, details={
        "method": method, "previous": previous,
        "new": {"latitude": latitude, "longitude": longitude, "location_source": source},
        **({"track_point_id": track_point_id} if method == "trajectory_point" else {}),
    })
