"""
Read side of the live training dataset: per-type summary (web console),
manifest (kit `platform-pull`), zip export and the Label Studio box review
write-back (`platform-push`). See docs/TRAINING-DATA.md.
"""
from __future__ import annotations

import csv
import io
import json
import tempfile
import zipfile
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.modules.m06_hazards.models.training_sample import TrainingFrame, TrainingSample
from app.modules.m06_hazards.training_data import card_info, layout

FILES_PREFIX = "/datasets/files/"

SAMPLE_FIELDS = (
    "id", "detection_type", "decision", "state", "decided_by", "reviewer", "reviewed_at", "reason_code",
    "corrections", "class_name", "class_index", "classifier_class", "bbox_x1", "bbox_y1", "bbox_x2", "bbox_y2",
    "box_corrected", "frame_key", "group_key", "job_id", "source_name", "frame_number", "video_time_s",
    "recorded_at", "latitude", "longitude", "confidence", "model_version", "model_card_version", "crop_key",
    "sync_status", "sync_error", "synced_at", "updated_at",
)
FRAME_FIELDS = (
    "frame_key", "group_key", "detection_type", "job_id", "frame_number", "video_time_s", "image_width",
    "image_height", "image_key", "label_key", "n_boxes", "status", "review_status", "box_reviewer",
    "box_reviewed_at", "synced_at", "updated_at",
)


def _jsonable(v: Any) -> Any:
    return v.isoformat() if isinstance(v, datetime) else v


def sample_dict(s: TrainingSample, review_status: Optional[str] = None) -> dict:
    d = {k: _jsonable(getattr(s, k)) for k in SAMPLE_FIELDS}
    d["review_status"] = review_status
    d["crop_url"] = FILES_PREFIX + s.crop_key if s.crop_key else None
    return d


def frame_dict(f: TrainingFrame) -> dict:
    d = {k: _jsonable(getattr(f, k)) for k in FRAME_FIELDS}
    d["image_url"] = FILES_PREFIX + f.image_key if f.image_key else None
    d["label_url"] = FILES_PREFIX + f.label_key if f.label_key else None
    d["labels"] = f.labels or []
    return d


# ── summary ──────────────────────────────────────────────────────────────────

async def summary(db: AsyncSession) -> list[dict]:
    """Per detection type: decisions, detector frames, classifier crops, box
    review state, sync state."""
    out: dict[str, dict] = {}

    def entry(t: str) -> dict:
        return out.setdefault(t, {
            "detection_type": t, "accepted": 0, "corrected": 0, "rejected": 0, "superseded": 0,
            "by_human": 0, "by_system": 0, "classifier_crops": 0, "crops_by_class": {},
            "detector_frames": 0, "positive_frames": 0, "background_frames": 0, "boxes": 0,
            "box_reviewed": 0, "needs_box_review": 0, "frames_without_image": 0,
            "sync_pending": 0, "sync_failed": 0, "last_synced_at": None, "last_decision_at": None})

    rows = (await db.execute(select(
        TrainingSample.detection_type, TrainingSample.state, TrainingSample.decision, TrainingSample.decided_by,
        TrainingSample.sync_status, TrainingSample.classifier_class, TrainingSample.crop_key.is_not(None),
        func.count(), func.max(TrainingSample.synced_at), func.max(TrainingSample.updated_at),
    ).group_by(TrainingSample.detection_type, TrainingSample.state, TrainingSample.decision,
               TrainingSample.decided_by, TrainingSample.sync_status, TrainingSample.classifier_class,
               TrainingSample.crop_key.is_not(None)))).all()
    for t, state, decision, by, sync, cls, has_crop, n, synced, updated in rows:
        e = entry(t)
        if state == "superseded":
            e["superseded"] += n
        else:
            e[decision] = e.get(decision, 0) + n
            e["by_human" if by == "human" else "by_system"] += n
            if has_crop:
                e["classifier_crops"] += n
                e["crops_by_class"][cls] = e["crops_by_class"].get(cls, 0) + n
        if sync == "pending":
            e["sync_pending"] += n
        elif sync == "failed":
            e["sync_failed"] += n
        for k, v in (("last_synced_at", synced), ("last_decision_at", updated)):
            if v is not None and (e[k] is None or v > e[k]):
                e[k] = v
    frows = (await db.execute(select(
        TrainingFrame.detection_type, TrainingFrame.status, TrainingFrame.review_status, TrainingFrame.n_boxes,
        func.count(),
    ).group_by(TrainingFrame.detection_type, TrainingFrame.status, TrainingFrame.review_status,
               TrainingFrame.n_boxes))).all()
    for t, status, review, n_boxes, n in frows:
        positive, boxes = bool(n_boxes), (n_boxes or 0) * n
        e = entry(t)
        if status == "no_image":
            e["frames_without_image"] += n
        if status != "active":
            continue
        e["detector_frames"] += n
        e["positive_frames" if positive else "background_frames"] += n
        e["boxes"] += int(boxes)
        e[review if review in layout.REVIEW_STATUSES else "needs_box_review"] += n
    for e in out.values():
        e["classes"] = card_info(e["detection_type"])[0]
        for k in ("last_synced_at", "last_decision_at"):
            e[k] = _jsonable(e[k])
    return sorted(out.values(), key=lambda e: e["detection_type"])


# ── manifest / zip ───────────────────────────────────────────────────────────

def _frame_query(det_type: str, review_status: Optional[str], since: Optional[datetime]):
    q = select(TrainingFrame).where(TrainingFrame.detection_type == det_type, TrainingFrame.status == "active")
    if review_status:
        q = q.where(TrainingFrame.review_status == review_status)
    if since:
        q = q.where(TrainingFrame.updated_at >= since)
    return q.order_by(TrainingFrame.frame_key)


async def manifest(db: AsyncSession, det_type: str, *, review_status: Optional[str] = None,
                   since: Optional[datetime] = None, decided_by: Optional[str] = None) -> dict:
    """Detector frames + classifier samples of a type, with file URLs
    (`/datasets/files/<key>`, relative to the API base URL)."""
    frames = (await db.execute(_frame_query(det_type, review_status, since))).scalars().all()
    reviews = {f.frame_key: f.review_status for f in frames}
    sq = select(TrainingSample).where(TrainingSample.detection_type == det_type, TrainingSample.state == "active")
    if decided_by in ("human", "system"):
        sq = sq.where(TrainingSample.decided_by == decided_by)
    if since:
        sq = sq.where(TrainingSample.updated_at >= since)
    samples = (await db.execute(sq.order_by(TrainingSample.frame_key, TrainingSample.id))).scalars().all()
    if decided_by in ("human", "system"):
        keep = {s.frame_key for s in samples}
        frames = [f for f in frames if f.frame_key in keep]
    return {
        "detection_type": det_type, "generated_at": datetime.utcnow().isoformat(),
        "bucket": settings.MINIO_BUCKET_DATASETS, "classes": card_info(det_type)[0],
        "layout": {"detector_image": layout.image_key(det_type, "<frame_key>"),
                   "detector_label": layout.label_key(det_type, "<frame_key>"),
                   "classifier_crop": layout.crop_key(det_type, "<class>", "<detection_id>")},
        "frames": [frame_dict(f) for f in frames],
        "samples": [sample_dict(s, reviews.get(s.frame_key)) for s in samples],
    }


def _csv(rows: list[dict], fields: list[str]) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: json.dumps(v) if isinstance(v, (list, dict)) else v for k, v in r.items()})
    return buf.getvalue().encode()


def build_zip(man: dict) -> tempfile.SpooledTemporaryFile:
    """YOLO detector dataset + classifier folders + manifests of a manifest
    (blocking: reads MinIO). Layout inside the zip = the bucket layout, plus
    <type>/detector/data.yaml, <type>/frames.csv, <type>/samples.csv,
    <type>/manifest.json."""
    from app.core.storage.minio_service import get_minio
    client = get_minio()
    t = man["detection_type"]
    tmp = tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024)

    def get(key: str) -> Optional[bytes]:
        try:
            r = client.get_object(settings.MINIO_BUCKET_DATASETS, key)
            try:
                return r.read()
            finally:
                r.close()
                r.release_conn()
        except Exception:
            return None

    missing = []
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
        for f in man["frames"]:
            img = get(f["image_key"]) if f.get("image_key") else None
            if img is None:
                missing.append(f["image_key"])
                continue
            z.writestr(f["image_key"], img)
            z.writestr(f["label_key"], layout.label_text(f["labels"]))
        for s in man["samples"]:
            if s.get("crop_key"):
                data = get(s["crop_key"])
                if data is None:
                    missing.append(s["crop_key"])
                else:
                    z.writestr(s["crop_key"], data)
        names = "\n".join(f"  {i}: {c}" for i, c in enumerate(man["classes"]))
        z.writestr(layout.classes_key(t), "\n".join(man["classes"]) + "\n")
        z.writestr(f"{t}/detector/data.yaml",
                   f"# {t} detector dataset (VIASafe platform export, not split: split by group_key)\n"
                   f"path: .\ntrain: images\nval: images\nnames:\n{names}\nnc: {len(man['classes'])}\n")
        z.writestr(f"{t}/frames.csv", _csv(man["frames"], list(FRAME_FIELDS) + ["image_url", "label_url"]))
        z.writestr(f"{t}/samples.csv", _csv(man["samples"], list(SAMPLE_FIELDS) + ["review_status"]))
        z.writestr(f"{t}/manifest.json", json.dumps({**man, "missing_objects": missing}, indent=1, default=str))
    tmp.seek(0)
    return tmp


# ── Label Studio review write-back ───────────────────────────────────────────

def clean_review_boxes(boxes: list[dict], classes: list[str]) -> list[dict]:
    """Validate reviewed YOLO boxes [{class_name | class_index, cx, cy, w, h}]
    (normalised) against the type's classes; raises ValueError."""
    out = []
    for b in boxes:
        name, idx = b.get("class_name"), b.get("class_index")
        if name is not None:
            if name not in classes:
                raise ValueError(f"unknown class '{name}' (classes: {classes})")
            idx = classes.index(name)
        elif idx is None or not (0 <= int(idx) < len(classes)):
            raise ValueError(f"class_index out of range (classes: {classes})")
        idx = int(idx)
        cx, cy, w, h = (float(b[k]) for k in ("cx", "cy", "w", "h"))
        x1, y1 = max(0.0, cx - w / 2), max(0.0, cy - h / 2)
        x2, y2 = min(1.0, cx + w / 2), min(1.0, cy + h / 2)
        if x2 - x1 <= 0 or y2 - y1 <= 0:
            raise ValueError("empty box")
        out.append({"class_index": idx, "class_name": classes[idx], "cx": round((x1 + x2) / 2, 6),
                    "cy": round((y1 + y2) / 2, 6), "w": round(x2 - x1, 6), "h": round(y2 - y1, 6),
                    "detection_id": None, "source": "box_review"})
    return out
