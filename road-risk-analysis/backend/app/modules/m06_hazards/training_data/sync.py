"""
Writes the live training dataset to the MinIO `datasets` bucket (layout in
app.modules.m06_hazards.training_data.layout, docs/TRAINING-DATA.md).

The unit of work is a dirty training_frames row: its detector image + YOLO
label are (re)written — or removed when no active decision is left — and
every sample of the frame whose sync is pending gets its classifier crop
(re)written / removed. Objects are deterministic (same key for the same
frame / detection), so re-running is harmless. Failures leave the frame
dirty with sync_error; the beat task `sync_training_data` retries them.

Callers: the API after a decision (kick(), background), the worker at the
end of a job, the beat task.
"""
from __future__ import annotations

import asyncio
import io
import logging
from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.modules.m06_hazards.models.detection import Detection
from app.modules.m06_hazards.models.training_sample import TrainingFrame, TrainingSample
from app.modules.m06_hazards.training_data import card_info, layout

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 20          # a frame failing this often is left alone (sync_error says why)


# ── MinIO helpers (blocking — run in a thread) ───────────────────────────────

def _client():
    from app.core.storage.minio_service import get_minio
    client = get_minio()
    if not client.bucket_exists(settings.MINIO_BUCKET_DATASETS):
        client.make_bucket(settings.MINIO_BUCKET_DATASETS)
    return client


def _put(key: str, data: bytes, content_type: str) -> None:
    _client().put_object(settings.MINIO_BUCKET_DATASETS, key, io.BytesIO(data), length=len(data),
                         content_type=content_type)


def _remove(key: Optional[str]) -> None:
    if key:
        _client().remove_object(settings.MINIO_BUCKET_DATASETS, key)


def _copy_from_detections(src: str, dst: str) -> None:
    from minio.commonconfig import CopySource
    _client().copy_object(settings.MINIO_BUCKET_DATASETS, dst, CopySource(settings.MINIO_BUCKET_DETECTIONS, src))


def _get_detections_object(src: str) -> bytes:
    resp = _client().get_object(settings.MINIO_BUCKET_DETECTIONS, src)
    try:
        return resp.read()
    finally:
        resp.close()
        resp.release_conn()


def _crop_jpeg(frame_jpeg: bytes, box: tuple, pad_ratio: Optional[float], pad_px: Optional[int]) -> Optional[bytes]:
    import cv2
    import numpy as np
    img = cv2.imdecode(np.frombuffer(frame_jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    h, w = img.shape[:2]
    x1, y1, x2, y2 = layout.crop_box(*box, w, h, pad_ratio=pad_ratio, pad_px=pad_px)
    crop = img[y1:y2, x1:x2]
    if crop.size == 0:
        return None
    ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 95])
    return buf.tobytes() if ok else None


def _image_size(frame_jpeg: bytes) -> tuple[Optional[int], Optional[int]]:
    import cv2
    import numpy as np
    img = cv2.imdecode(np.frombuffer(frame_jpeg, np.uint8), cv2.IMREAD_REDUCED_GRAYSCALE_2)
    if img is None:
        return None, None
    return img.shape[1] * 2, img.shape[0] * 2


# ── one frame ────────────────────────────────────────────────────────────────

def _view(s: TrainingSample) -> layout.SampleView:
    box = (s.bbox_x1, s.bbox_y1, s.bbox_x2, s.bbox_y2)
    return layout.SampleView(s.id, s.state, s.decision, s.class_index, s.class_name,
                             box if all(v is not None for v in box) else None)


def _sync_frame_objects(fr: TrainingFrame, samples: list[TrainingSample], dets: dict[str, Detection]) -> dict:
    """Blocking part of a frame sync; returns the new frame state."""
    t = fr.detection_type
    views = [_view(s) for s in samples]
    status = layout.frame_status(views, bool(fr.source_object))
    out: dict = {"status": status, "labels": None, "n_boxes": 0, "image_key": None, "label_key": None,
                 "crops": {}}
    frame_jpeg: Optional[bytes] = None
    if status == "active":
        img_key, lbl_key = layout.image_key(t, fr.frame_key), layout.label_key(t, fr.frame_key)
        _copy_from_detections(fr.source_object, img_key)   # type: ignore[arg-type]
        if not (fr.image_width and fr.image_height):
            frame_jpeg = _get_detections_object(fr.source_object)   # type: ignore[arg-type]
            out["size"] = _image_size(frame_jpeg)
            w, h = out["size"]
        else:
            w, h = fr.image_width, fr.image_height
        boxes = layout.frame_boxes(views, w, h, fr.reviewed_boxes if fr.review_status == "box_reviewed" else None)
        _put(lbl_key, layout.label_text(boxes).encode(), "text/plain")
        classes, *_ = card_info(t)
        _put(layout.classes_key(t), ("\n".join(classes) + "\n").encode(), "text/plain")
        out.update(labels=boxes, n_boxes=len(boxes), image_key=img_key, label_key=lbl_key)
    else:
        for k in (fr.image_key, fr.label_key, layout.image_key(t, fr.frame_key), layout.label_key(t, fr.frame_key)):
            _remove(k)

    # classifier crops of the samples whose sync is pending
    _, _, pad_ratio, pad_px = card_info(t)
    for s in samples:
        if s.sync_status == "synced":
            continue
        want = (layout.crop_key(t, s.classifier_class, s.id)
                if s.state == "active" and s.detection_type == t else None)
        if want is None:
            _remove(s.crop_key)
            out["crops"][s.id] = None
            continue
        box = (s.bbox_x1, s.bbox_y1, s.bbox_x2, s.bbox_y2)
        data = None
        if fr.source_object and all(v is not None for v in box):
            if frame_jpeg is None:
                frame_jpeg = _get_detections_object(fr.source_object)
            data = _crop_jpeg(frame_jpeg, box, pad_ratio, pad_px)   # type: ignore[arg-type]
        if data is not None:
            _put(want, data, "image/jpeg")
        else:                                            # legacy detection: its stored crop (20 px padding)
            det = dets.get(s.id)
            src = layout.object_name(det.crop_url if det else None, settings.MINIO_BUCKET_DETECTIONS)
            if not src:
                out["crops"][s.id] = None
                continue
            _copy_from_detections(src, want)
        if s.crop_key and s.crop_key != want:
            _remove(s.crop_key)
        out["crops"][s.id] = want
    return out


async def sync_frame(db: AsyncSession, fr: TrainingFrame) -> bool:
    """Write one frame's objects; commit. False when it failed (kept dirty)."""
    samples = (await db.execute(select(TrainingSample).where(
        TrainingSample.detection_type == fr.detection_type, TrainingSample.frame_key == fr.frame_key,
    ))).scalars().all()
    ids = [s.id for s in samples]
    dets = {d.id: d for d in (await db.execute(select(Detection).where(Detection.id.in_(ids)))).scalars()} \
        if ids else {}
    # (a sample moved to another type / frame is handled by its new frame: the
    # old crop key differs from the wanted one and is removed there)
    try:
        res = await asyncio.to_thread(_sync_frame_objects, fr, list(samples), dets)
    except Exception as exc:
        fr.sync_attempts = (fr.sync_attempts or 0) + 1
        fr.sync_error = f"{type(exc).__name__}: {exc}"[:500]
        for s in samples:
            if s.sync_status != "synced":
                s.sync_status, s.sync_error = "failed", fr.sync_error
                s.sync_attempts = (s.sync_attempts or 0) + 1
        await db.commit()
        logger.warning(f"training data: sync of {fr.detection_type}/{fr.frame_key} failed: {fr.sync_error}")
        return False
    now = datetime.utcnow()
    if res.get("size") and res["size"][0]:
        fr.image_width, fr.image_height = res["size"]
    fr.status, fr.labels, fr.n_boxes = res["status"], res["labels"], res["n_boxes"]
    fr.image_key, fr.label_key = res["image_key"], res["label_key"]
    fr.dirty, fr.sync_error, fr.synced_at = False, None, now
    for s in samples:
        if s.id in res["crops"]:
            s.crop_key = res["crops"][s.id]
        if s.sync_status != "synced":
            s.sync_status, s.sync_error, s.synced_at = "synced", None, now
    await db.commit()
    return True


async def sync_dirty(db: AsyncSession, *, job_id: Optional[str] = None, frame_ids: Optional[Iterable[int]] = None,
                     limit: int = 500) -> dict:
    """Sync the dirty frames (optionally of one job / these ids), oldest first.
    Rows are claimed with SKIP LOCKED so concurrent syncs never write the same
    frame twice at once."""
    q = select(TrainingFrame).where(TrainingFrame.dirty.is_(True),
                                    or_(TrainingFrame.sync_attempts.is_(None),
                                        TrainingFrame.sync_attempts < MAX_ATTEMPTS))
    if job_id:
        q = q.where(TrainingFrame.job_id == job_id)
    if frame_ids is not None:
        q = q.where(TrainingFrame.id.in_(list(frame_ids)))
    ids = (await db.execute(q.order_by(TrainingFrame.updated_at).limit(limit).with_only_columns(TrainingFrame.id)
                            )).scalars().all()
    await db.commit()
    done = failed = 0
    for fid in ids:
        fr = (await db.execute(select(TrainingFrame).where(TrainingFrame.id == fid, TrainingFrame.dirty.is_(True))
                               .with_for_update(skip_locked=True))).scalar_one_or_none()
        if fr is None:
            await db.commit()
            continue
        if await sync_frame(db, fr):
            done += 1
        else:
            failed += 1
    if ids:
        logger.info(f"training data: {done} frame(s) synced, {failed} failed")
    return {"frames": len(ids), "synced": done, "failed": failed}


# ── fire-and-forget after an API decision ─────────────────────────────────────

_tasks: set = set()


def kick(delay_s: float = 0.5) -> None:
    """Schedule a sync of the dirty frames on the running event loop, with
    its own session, after the caller's commit. Never raises."""
    async def run():
        await asyncio.sleep(delay_s)
        from app.core.db.session import AsyncSessionLocal
        try:
            async with AsyncSessionLocal() as db:
                await sync_dirty(db, limit=100)
        except Exception as exc:
            logger.warning(f"training data: background sync failed ({exc}) — the beat task will retry")
    try:
        task = asyncio.get_running_loop().create_task(run())
    except RuntimeError:
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
