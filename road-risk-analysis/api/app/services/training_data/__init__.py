"""
Live training dataset fed by every decision on a detection — docs/TRAINING-DATA.md.

    record_decision(db, det)   in the caller's transaction (detection_workflow._enter,
                               processor._store_detection): upsert the
                               training_samples row, mark its training_frames
                               row(s) dirty. Never touches MinIO.
    sync.sync_dirty(db, ...)   writes the objects of the dirty frames to the
                               `datasets` bucket (after the commit: API
                               background task, end of a job, beat task
                               `sync_training_data` for retries).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.detection import MANUAL_LOCATION_SOURCES, Detection
from app.models.job import Job
from app.models.training_sample import TrainingFrame, TrainingSample
from app.services.training_data import layout

logger = logging.getLogger(__name__)


def _status(det: Detection) -> str:
    rs = det.review_status
    return getattr(rs, "value", rs)


def card_info(det_type: str) -> tuple[list[str], Optional[str], Optional[float], Optional[int]]:
    """(detector classes, classifier reject class, crop_pad_ratio, crop_pad_px)
    from the installed model cards of the type (defaults without cards)."""
    from app.services import model_registry

    det_card = cls_card = None
    try:
        slot = model_registry.detector_slot(det_type)
        det_card = model_registry.slot_card(slot) if slot else None
        vslot = model_registry.VERIFIER_FOR_TYPE.get(det_type)
        cls_card = model_registry.slot_card(vslot) if vslot else None
    except Exception as exc:                            # unreadable model dir → defaults
        logger.warning(f"training data: model cards of {det_type} unreadable ({exc})")
    classes = layout.detector_classes(det_type, det_card.classes if det_card and det_card.classes else None)
    reject = cls_card.thresholds.reject_class if cls_card is not None else None
    if cls_card is not None and reject is None and det_type != "traffic_sign":
        reject = next((c for c in cls_card.classes if c.startswith("not_")), None)
    ratio = cls_card.crop_pad_ratio if cls_card is not None else None
    px = cls_card.crop_pad_px if cls_card is not None else None
    return classes, reject, ratio, px


def _signature(row: Optional[TrainingSample]) -> tuple:
    if row is None:
        return ()
    return (row.state, row.decision, row.detection_type, row.frame_key, row.class_index,
            row.bbox_x1, row.bbox_y1, row.bbox_x2, row.bbox_y2)


async def _frame(db: AsyncSession, det_type: str, key: str) -> Optional[TrainingFrame]:
    return (await db.execute(select(TrainingFrame).where(
        TrainingFrame.detection_type == det_type, TrainingFrame.frame_key == key))).scalar_one_or_none()


async def _touch_frame(db: AsyncSession, det_type: str, key: str, *, det: Optional[Detection] = None,
                       job: Optional[Job] = None, reset_review: bool = False) -> None:
    fr = await _frame(db, det_type, key)
    now = datetime.utcnow()
    if fr is None:
        if det is None:
            return
        fr = TrainingFrame(detection_type=det_type, frame_key=key, group_key=layout.group_key(key),
                           job_id=det.job_id, n_boxes=0, status="active", review_status="needs_box_review",
                           sync_attempts=0, created_at=now)
        db.add(fr)
    if det is not None:
        fr.frame_number, fr.video_time_s = det.frame_number, det.video_time_s
        if det.frame_width and det.frame_height:
            fr.image_width, fr.image_height = det.frame_width, det.frame_height
        src = layout.object_name(det.frame_clean_url, settings.MINIO_BUCKET_DETECTIONS)
        if src:
            fr.source_object = src
    if reset_review and fr.review_status == "box_reviewed":
        # a decision changed after the Label Studio review: back to the decisions' boxes
        fr.review_status, fr.reviewed_boxes = "needs_box_review", None
    fr.dirty, fr.sync_attempts, fr.sync_error, fr.updated_at = True, 0, None, now
    await db.flush()


async def record_decision(db: AsyncSession, det: Detection, *, job: Optional[Job] = None,
                          actor_type: Optional[str] = None) -> None:
    """Keep the training sample of `det` in step with its review status (see
    module docstring). Idempotent; unchanged decisions touch nothing."""
    try:
        async with db.begin_nested():                    # SAVEPOINT: a failure here rolls back only this
            await _record(db, det, job, actor_type)
    except Exception:                                    # never block a validation
        logger.exception(f"training data: could not record detection {det.id}")


async def _record(db: AsyncSession, det: Detection, job: Optional[Job], actor_type: Optional[str]) -> None:
    row = await db.get(TrainingSample, det.id)
    before = _signature(row)
    decision = layout.decision_for(_status(det), row.decision if row else None)
    now = datetime.utcnow()
    if decision is None:                                 # reopened (or never decided)
        if row is None or row.state == "superseded":
            return
        row.state, row.sync_status, row.sync_attempts, row.updated_at = "superseded", "pending", 0, now
        await _touch_frame(db, row.detection_type, row.frame_key, reset_review=True)
        return

    job = job if job is not None and job.id == det.job_id else await db.get(Job, det.job_id)
    source = (job.source_name or job.filename) if job is not None else None
    key = layout.frame_key(source, det.job_id, det.video_time_s, det.frame_number)
    classes, reject, _, _ = card_info(det.type)
    cls_name, cls_idx = (layout.detector_class(det.type, det.subtype, det.ai_details, classes)
                         if decision in layout.ACCEPTED_DECISIONS else (None, None))
    box = layout.effective_box((det.bbox_x1, det.bbox_y1, det.bbox_x2, det.bbox_y2), det.corrected_bbox)
    system = (actor_type == "system") if actor_type else (det.reviewed_by in (None, "system"))

    if row is None:
        row = TrainingSample(id=det.id, created_at=now, sync_attempts=0)
        db.add(row)
    old_frame = (row.detection_type, row.frame_key) if before else None
    row.detection_type, row.frame_key, row.group_key = det.type, key, layout.group_key(key)
    row.job_id, row.source_name = det.job_id, source
    row.decision, row.state = decision, "active"
    row.decided_by = "system" if system else "human"
    row.reviewer, row.reviewed_at = det.reviewed_by, det.reviewed_at
    row.reason_code = det.rejection_code if decision == "rejected" else None
    row.corrections = layout.corrections_of(det, MANUAL_LOCATION_SOURCES) or None
    row.class_name, row.class_index = cls_name, cls_idx
    row.classifier_class = layout.classifier_class(det.type, det.subtype, decision, reject)
    row.bbox_x1, row.bbox_y1, row.bbox_x2, row.bbox_y2 = box if box else (None, None, None, None)
    row.box_corrected = bool(det.corrected_bbox)
    row.frame_number, row.video_time_s, row.recorded_at = det.frame_number, det.video_time_s, det.recorded_at
    row.latitude, row.longitude, row.confidence = det.latitude, det.longitude, det.confidence
    row.model_version, row.model_card_version = det.model_version, det.model_card_version
    after = _signature(row)
    changed = after != before
    if changed or row.sync_status != "synced":
        row.sync_status, row.sync_error, row.updated_at = "pending", None, now
        if changed:
            row.sync_attempts = 0
    await db.flush()
    if old_frame and old_frame != (det.type, key):
        await _touch_frame(db, *old_frame, reset_review=True)
    if changed or row.sync_status == "pending":
        await _touch_frame(db, det.type, key, det=det, job=job, reset_review=bool(before) and changed)
