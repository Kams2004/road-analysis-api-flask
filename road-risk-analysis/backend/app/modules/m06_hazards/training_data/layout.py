"""
Pure rules of the live training dataset (no DB, no MinIO, no cv2) — see
docs/TRAINING-DATA.md.

Bucket `datasets` layout, per detection type:

    <type>/detector/classes.txt                       class index → name (one per line)
    <type>/detector/images/<frame_key>.jpg            clean full frame (no box drawn)
    <type>/detector/labels/<frame_key>.txt            YOLO: "<class> <cx> <cy> <w> <h>" normalised,
                                                      empty file = reviewed background
    <type>/classifier/<class>/<detection_id>.jpg      padded crop (card crop_pad_ratio), rejected →
                                                      not_<type> (signs: not_a_sign)

frame_key = "<video slug>-<job id[:8]>__z<minute:03d>__t<ms:08d>": the kit's
split-safe stem (ai/training/pothole-dataset), everything before the
second "__" (video + job + minute of video) is the leakage group.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

DECISIONS = ("accepted", "corrected", "rejected")
ACCEPTED_DECISIONS = ("accepted", "corrected")
REVIEW_STATUSES = ("needs_box_review", "box_reviewed")

# Kind of a rejection reason (rejection_reasons.kind) — what a rejection teaches:
#   false_positive  AI mistake → negative sample (crop in the reject class, the
#                   frame is background for the type unless it has accepted boxes)
#   real_redundant  the object is real but redundant (duplicate, already
#                   validated) → positive sample, exactly like an accepted one
#   unusable        not judgeable (blur, cut off, too far …) → no sample: no
#                   crop, no box, no frame created for it (kept in the manifest
#                   with excluded = true for traceability)
REJECTION_KINDS = ("false_positive", "real_redundant", "unusable")
DEFAULT_REJECTION_KIND = "false_positive"


def rejection_kind(kind: Optional[str]) -> str:
    """Normalised kind; unknown / missing → false_positive (the historical behaviour)."""
    return kind if kind in REJECTION_KINDS else DEFAULT_REJECTION_KIND


def sample_role(decision: str, kind: Optional[str] = None) -> str:
    """positive | negative | excluded — the training role of a decision."""
    if decision in ACCEPTED_DECISIONS:
        return "positive"
    k = rejection_kind(kind)
    if k == "real_redundant":
        return "positive"
    if k == "unusable":
        return "excluded"
    return "negative"

# Detector classes when the type's detector has no model card listing them.
DEFAULT_DETECTOR_CLASSES: dict[str, list[str]] = {
    "pothole":        ["pothole"],
    "speed_bump":     ["speed_bump"],
    "traffic_sign":   ["traffic_sign", "traffic_light"],
    "parked_vehicle": ["car", "motorcycle", "bus", "truck"],
}
SIGN_REJECT_CLASS = "not_a_sign"

_SLUG_RE = re.compile(r"[^A-Za-z0-9]+")


def slug(text: Optional[str], maxlen: int = 24) -> str:
    """File-name-safe slug without '__' (reserved as the stem separator)."""
    s = _SLUG_RE.sub("_", (text or "").rsplit("/", 1)[-1]).strip("_")
    s = re.sub(r"_+", "_", s)
    if s.lower().endswith(("_mp4", "_avi", "_mov", "_mkv", "_jpg", "_png")):
        s = s[:-4]
    return s[:maxlen].strip("_") or "video"


def frame_key(source_name: Optional[str], job_id: str, video_time_s: Optional[float],
              frame_number: Optional[int]) -> str:
    """Split-safe stem of a frame (see module docstring). Without a video time
    (single image), the frame number stands for the milliseconds."""
    vid = f"{slug(source_name)}-{job_id.replace('-', '')[:8]}"
    if video_time_s is not None:
        ms = int(round(video_time_s * 1000))
    else:
        ms = int(frame_number or 0)
    minute = ms // 60000 if video_time_s is not None else 0
    return f"{vid}__z{minute:03d}__t{ms:08d}"


def group_key(key: str) -> str:
    """Leakage group of a frame key: '<video>-<job>__z<minute>'."""
    parts = key.split("__")
    return "__".join(parts[:2]) if len(parts) >= 3 else key


# ── classes ──────────────────────────────────────────────────────────────────

def detector_classes(det_type: str, card_classes: Optional[Sequence[str]] = None) -> list[str]:
    """Class list (index = YOLO class id) of a type's detector dataset."""
    if card_classes:
        return [str(c) for c in card_classes]
    return list(DEFAULT_DETECTOR_CLASSES.get(det_type, [det_type]))


def detector_class(det_type: str, subtype: Optional[str], ai_details: Optional[dict],
                   classes: Sequence[str]) -> tuple[Optional[str], Optional[int]]:
    """(class name, index) of an accepted box in the type's detector dataset;
    (None, None) when the object has no class there (e.g. a sign code of the
    legacy multi-class model that is not in its class list)."""
    names = [str(c) for c in classes]

    def hit(name: Optional[str]) -> tuple[Optional[str], Optional[int]]:
        return (name, names.index(name)) if name is not None and name in names else (None, None)

    if det_type == "traffic_sign":
        if "traffic_sign" in names:                       # two-stage detector: sign / light
            light = (subtype or "").replace("_", "").lower() == "trafficlight"   # legacy model: "trafficlight"
            return hit("traffic_light" if light else "traffic_sign")
        return hit(subtype)                               # legacy multi-class detector
    if det_type == "parked_vehicle":
        return hit((ai_details or {}).get("vehicle_class"))
    if len(names) == 1:
        return names[0], 0
    return hit(det_type) if det_type in names else hit(subtype)


def reject_class(det_type: str, card_reject_class: Optional[str] = None) -> str:
    if card_reject_class:
        return card_reject_class
    return SIGN_REJECT_CLASS if det_type == "traffic_sign" else f"not_{det_type}"


def classifier_class(det_type: str, subtype: Optional[str], decision: str,
                     card_reject_class: Optional[str] = None, kind: Optional[str] = None) -> str:
    """Folder of the crop in <type>/classifier/: the accepted class (sign code
    for traffic signs) — also for a real_redundant rejection — or the type's
    reject class (false_positive; unusable ones keep it but get no crop)."""
    if sample_role(decision, kind) != "positive":
        return reject_class(det_type, card_reject_class)
    if det_type == "traffic_sign" and subtype:
        return safe_name(subtype)
    return safe_name(det_type)


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_") or "unknown"


# ── boxes ────────────────────────────────────────────────────────────────────

def effective_box(bbox: Sequence[Optional[float]], corrected: Optional[Sequence[float]]) -> Optional[tuple]:
    """The corrected box when a validator moved it, else the predicted one."""
    if corrected and len(corrected) == 4 and all(v is not None for v in corrected):
        x1, y1, x2, y2 = (float(v) for v in corrected)
    elif bbox and all(v is not None for v in bbox):
        x1, y1, x2, y2 = (float(v) for v in bbox)          # type: ignore[arg-type]
    else:
        return None
    return min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)


def yolo_box(x1: float, y1: float, x2: float, y2: float, width: int, height: int
             ) -> Optional[tuple[float, float, float, float]]:
    """Pixel box → YOLO (cx, cy, w, h) in [0, 1], clipped to the image; None
    when nothing of it is inside."""
    if not width or not height:
        return None
    x1, x2 = max(0.0, min(x1, x2)), min(float(width), max(x1, x2))
    y1, y2 = max(0.0, min(y1, y2)), min(float(height), max(y1, y2))
    if x2 - x1 <= 0 or y2 - y1 <= 0:
        return None
    return ((x1 + x2) / 2 / width, (y1 + y2) / 2 / height, (x2 - x1) / width, (y2 - y1) / height)


def yolo_line(class_index: int, box: Sequence[float]) -> str:
    return f"{int(class_index)} " + " ".join(f"{v:.6f}" for v in box)


def label_text(boxes: Iterable[dict]) -> str:
    """YOLO label file content; '' = background image."""
    return "".join(yolo_line(b["class_index"], (b["cx"], b["cy"], b["w"], b["h"])) + "\n" for b in boxes)


def crop_box(x1: float, y1: float, x2: float, y2: float, width: int, height: int,
             pad_ratio: Optional[float] = None, pad_px: Optional[int] = None) -> tuple[int, int, int, int]:
    """Padded crop clipped to the frame — the same padding as
    app.modules.m05_ai_detection.model_registry.crop_box (card crop_pad_ratio of the box size,
    else crop_pad_px, else 20 px)."""
    x1, y1, x2, y2 = int(round(x1)), int(round(y1)), int(round(x2)), int(round(y2))
    if pad_ratio is not None:
        px, py = int(round(pad_ratio * (x2 - x1))), int(round(pad_ratio * (y2 - y1)))
    else:
        px = py = pad_px if pad_px is not None else 20
    return max(0, x1 - px), max(0, y1 - py), min(width, x2 + px), min(height, y2 + py)


@dataclass
class SampleView:
    """What the frame label needs of a training sample."""
    id: str
    state: str                    # active | superseded
    decision: str                 # accepted | corrected | rejected
    class_index: Optional[int]
    class_name: Optional[str]
    box: Optional[tuple]          # effective pixel box
    kind: Optional[str] = None    # rejection kind (rejected samples)
    excluded: bool = False        # unusable rejection: no training contribution

    @property
    def role(self) -> str:
        return "excluded" if self.excluded else sample_role(self.decision, self.kind)


def frame_boxes(samples: Sequence[SampleView], width: Optional[int], height: Optional[int],
                reviewed: Optional[list[dict]] = None) -> list[dict]:
    """Boxes of a detector frame: the Label Studio reviewed boxes when present,
    else every active positive sample with a class — accepted / corrected and
    real_redundant rejections (false_positive rejections contribute nothing →
    a frame with only those is background; unusable ones are ignored)."""
    if reviewed is not None:
        return [dict(b) for b in reviewed]
    out = []
    for s in sorted(samples, key=lambda s: s.id):
        if s.state != "active" or s.role != "positive" or s.class_index is None or s.box is None:
            continue
        yb = yolo_box(*s.box, width or 0, height or 0)
        if yb is None:
            continue
        out.append({"class_index": s.class_index, "class_name": s.class_name, "cx": yb[0], "cy": yb[1],
                    "w": yb[2], "h": yb[3], "detection_id": s.id})
    return out


def frame_status(samples: Sequence[SampleView], has_image: bool) -> str:
    """active (image + label written) | empty (no active usable decision →
    objects removed; unusable rejections alone never make a frame) | no_image
    (decided, but the detection has no clean frame)."""
    if not any(s.state == "active" and s.role != "excluded" for s in samples):
        return "empty"
    return "active" if has_image else "no_image"


# ── decisions ────────────────────────────────────────────────────────────────

def decision_for(review_status: str, previous: Optional[str] = None) -> Optional[str]:
    """Training decision of a detection status; None = not decided (the
    sample is superseded). `resolved` keeps the accepted decision it had."""
    if review_status == "validated":
        return "accepted"
    if review_status == "corrected":
        return "corrected"
    if review_status == "rejected":
        return "rejected"
    if review_status == "resolved":
        return previous if previous in ACCEPTED_DECISIONS else "accepted"
    return None


def corrections_of(det: Any, manual_sources: Iterable[str]) -> list[str]:
    """What a validator corrected: box, class, location."""
    out = []
    if getattr(det, "corrected_bbox", None):
        out.append("box")
    pt, ps = getattr(det, "predicted_type", None), getattr(det, "predicted_subtype", None)
    if pt is not None and (pt != det.type or (ps or None) != (det.subtype or None)):
        out.append("class")
    if getattr(det, "location_source", None) in set(manual_sources):
        out.append("location")
    return out


# ── object keys ──────────────────────────────────────────────────────────────

def image_key(det_type: str, key: str) -> str:
    return f"{det_type}/detector/images/{key}.jpg"


def label_key(det_type: str, key: str) -> str:
    return f"{det_type}/detector/labels/{key}.txt"


def classes_key(det_type: str) -> str:
    return f"{det_type}/detector/classes.txt"


def crop_key(det_type: str, cls: str, detection_id: str) -> str:
    return f"{det_type}/classifier/{safe_name(cls)}/{detection_id}.jpg"


def object_name(url: Optional[str], bucket: str) -> Optional[str]:
    """Object name inside `bucket` of a stored URL ('{endpoint}/{bucket}/{key}'
    or a bare key)."""
    if not url:
        return None
    marker = f"/{bucket}/"
    if marker in url:
        return url.split(marker, 1)[1]
    return url.lstrip("/") if "://" not in url else None
