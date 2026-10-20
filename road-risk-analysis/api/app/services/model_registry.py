"""
Model registry (lite) — RRAP-05-010/011, RRAP-13-016.

The weights the worker loads live in the model directory (`/models`, see
MODEL_* in app.core.config). Next to each weights file `X.pt` the training
project (model-training/pothole/) writes a JSON *model card* `X.json`:

    {"model_type": "detector" | "classifier", "name": "pothole_det", "version": "v1",
     "created_at": ISO, "framework": "ultralytics 8.x", "base": "...",
     "classes": [...]            # classifier: ["not_pothole", "pothole"]
     "imgsz": 640, "datasets": [{"name", "source", "license_note", "images"}],
     "metrics": {...}, "thresholds": {"auto_accept", "auto_reject", "target_precision"},
     "sha256": "..."}

The traffic-sign classifier card (model-training/traffic-signs/) also has
`thresholds.per_class` {sign code: {auto_accept, auto_reject}} on P(predicted
class), `crop_pad_ratio` and `taxonomy_version`; its `classes` are VIASafe
sign codes (see app.services.sign_taxonomy). The sign detector card of the
two-stage pipeline has classes ["traffic_sign", "traffic_light"]; any other
sign detector (the legacy 78/43-class model) keeps the single-stage pipeline.
The `vehicles` slot is a stock COCO-pretrained YOLOv8 (card written by hand:
no fine-tuning, classes used, licence note); its second stage is the rule
set of app.services.detection.parked_vehicle, not a classifier.

This module only reads files (no ultralytics import), so the API process can
describe the models the worker uses. A card is optional: without one a model
still loads, its version is `file@sha256[:12]` and the decision thresholds
fall back to settings section `validation` (see app.services.ai_validation).
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import settings

logger = logging.getLogger(__name__)

# Default padding around the box when cropping for the classifier — the same
# PAD the processor uses for the stored crops the classifier dataset is built
# from. A card may override it with `crop_pad_px` or `crop_pad_ratio`.
DEFAULT_CROP_PAD_PX = 20
DEFAULT_CLASSIFIER_IMGSZ = 224


class CardClassThresholds(BaseModel):
    model_config = ConfigDict(extra="allow")
    auto_accept: Optional[float] = Field(None, ge=0, le=1)
    auto_reject: Optional[float] = Field(None, ge=0, le=1)


class CardThresholds(BaseModel):
    model_config = ConfigDict(extra="allow")
    auto_accept:      Optional[float] = Field(None, ge=0, le=1)
    auto_reject:      Optional[float] = Field(None, ge=0, le=1)
    target_precision: Optional[float] = Field(None, ge=0, le=1)
    per_class:        dict[str, CardClassThresholds] = {}   # multi-class classifier (traffic signs)
    reject_class:     Optional[str] = None                  # class meaning "not this type" (signs: not_a_sign)


class CardDataset(BaseModel):
    model_config = ConfigDict(extra="allow")
    name:         Optional[str] = None
    source:       Optional[str] = None
    license_note: Optional[str] = None
    images:       Optional[int] = None


class ModelCard(BaseModel):
    """A model card as written by the training project (unknown keys kept)."""
    model_config = ConfigDict(extra="allow", protected_namespaces=())
    model_type: str
    name:       str
    version:    str
    created_at: Optional[str] = None
    framework:  Optional[str] = None
    base:       Optional[Any] = None
    classes:    list[str] = []
    imgsz:      Optional[int] = None
    datasets:   list[CardDataset] = []
    metrics:    dict[str, Any] = {}
    thresholds: CardThresholds = CardThresholds()
    sha256:     Optional[str] = None
    crop_pad_px:    Optional[int] = Field(None, ge=0)
    crop_pad_ratio: Optional[float] = Field(None, ge=0, le=2)
    taxonomy_version: Optional[str] = None

    @property
    def label(self) -> str:
        return f"{self.name}:{self.version}"


def card_path(weights: Path) -> Path:
    return weights.with_suffix(".json")


def parse_card(raw: Any) -> ModelCard:
    """Validate a card's JSON content (raises ValueError when unusable)."""
    if not isinstance(raw, dict):
        raise ValueError("model card must be a JSON object")
    try:
        return ModelCard.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"invalid model card: {exc.errors()[0]['loc']} {exc.errors()[0]['msg']}") from exc


_card_cache: dict[tuple[str, float, int], tuple[Optional[ModelCard], Optional[str]]] = {}


def load_card(weights: Path) -> tuple[Optional[ModelCard], Optional[str]]:
    """(card, error) for a weights file; (None, None) when it has no card."""
    p = card_path(weights)
    try:
        st = p.stat()
    except OSError:
        return None, None
    key = (str(p), st.st_mtime, st.st_size)
    if key not in _card_cache:
        try:
            _card_cache[key] = (parse_card(json.loads(p.read_text(encoding="utf-8"))), None)
        except (OSError, ValueError) as exc:
            logger.warning(f"Model card {p} ignored: {exc}")
            _card_cache[key] = (None, str(exc))
    return _card_cache[key]


_hash_cache: dict[tuple[str, float, int], str] = {}


def file_sha256(p: Path) -> str:
    """Full SHA-256 of a file, hashed once per file version (path, mtime, size)."""
    st = p.stat()
    key = (str(p), st.st_mtime, st.st_size)
    if key not in _hash_cache:
        h = hashlib.sha256()
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        _hash_cache[key] = h.hexdigest()
    return _hash_cache[key]


def weights_version(p: Path) -> str:
    """Weights file name + first 12 hex chars of its SHA-256 — stored as `model_version`."""
    return f"{p.name}@{file_sha256(p)[:12]}"


# ── the model slots of the platform ─────────────────────────────────────────

# key → (detection type, role); paths come from settings at call time.
SLOTS: dict[str, tuple[str, str]] = {
    "pothole":            ("pothole", "detector"),
    "signs":              ("traffic_sign", "detector"),
    "speedbump":          ("speed_bump", "detector"),
    "pothole_classifier": ("pothole", "classifier"),
    "signs_classifier":   ("traffic_sign", "classifier"),
    "vehicles":           ("parked_vehicle", "detector"),   # COCO YOLOv8 + rules (detection.parked_vehicle)
}
# detection type → classifier slot that verifies it (two-stage pipeline)
VERIFIER_FOR_TYPE: dict[str, str] = {"pothole": "pothole_classifier", "traffic_sign": "signs_classifier"}

# Classes of the two-stage sign detector; any other class set = legacy single stage.
SIGN_DETECTOR_CLASSES = frozenset({"traffic_sign", "traffic_light"})


def is_two_stage_sign_detector(class_names) -> bool:
    """The sign detector emits only traffic_sign / traffic_light (two-stage
    pipeline) — not the legacy model whose classes are the signs themselves."""
    names = {str(n).lower() for n in class_names}
    return "traffic_sign" in names and names <= SIGN_DETECTOR_CLASSES


def slot_path(key: str) -> str:
    return {
        "pothole":            settings.MODEL_POTHOLE,
        "signs":              settings.MODEL_SIGNS,
        "speedbump":          settings.MODEL_SPEEDBUMP,
        "pothole_classifier": settings.pothole_classifier_path,
        "signs_classifier":   settings.MODEL_SIGNS_CLASSIFIER,
        "vehicles":           settings.MODEL_VEHICLES,
    }[key]


def slot_card(key: str) -> Optional[ModelCard]:
    """Card of a slot's weights file, when both exist."""
    path = slot_path(key)
    if not path:
        return None
    p = Path(path)
    return load_card(p)[0] if p.exists() else None


def detector_slot(det_type: str) -> Optional[str]:
    return next((k for k, (t, role) in SLOTS.items() if t == det_type and role == "detector"), None)


def thresholds_from_cards(cards: dict[str, "ModelCard"]) -> dict[str, dict[str, Any]]:
    """{type: {detector_accept, detector_reject, auto_accept, auto_reject[, per_class]}}
    from cards keyed by slot — the two `detector_*` from the detector card (on
    detector confidence), the others from the verifier card (on the
    classifier's P(class)); `per_class` {code: {auto_accept, auto_reject}} when
    the classifier card has per-class thresholds (traffic signs)."""
    out: dict[str, dict[str, Any]] = {}
    for key, card in cards.items():
        if card is None or key not in SLOTS:
            continue
        det_type, role = SLOTS[key]
        entry = out.setdefault(det_type, {})
        if role == "detector":
            entry["detector_accept"] = card.thresholds.auto_accept
            entry["detector_reject"] = card.thresholds.auto_reject
        else:
            entry["auto_accept"] = card.thresholds.auto_accept
            entry["auto_reject"] = card.thresholds.auto_reject
            if card.thresholds.reject_class:
                entry["reject_class"] = card.thresholds.reject_class
            if card.thresholds.per_class:
                entry["per_class"] = {c: {"auto_accept": t.auto_accept, "auto_reject": t.auto_reject}
                                      for c, t in card.thresholds.per_class.items()}
    return out


def card_thresholds() -> dict[str, dict[str, Any]]:
    """Thresholds the installed cards propose, per detection type (see thresholds_from_cards)."""
    return thresholds_from_cards({key: slot_card(key) for key in SLOTS})


def sign_pipeline() -> Optional[str]:
    """`two_stage` | `single_stage` as the API can tell from the files (the
    worker decides from the loaded weights' class names); None when the sign
    detector has no card listing its classes."""
    det, cls = slot_card("signs"), slot_path("signs_classifier")
    if det is None or not det.classes:
        return None
    if not is_two_stage_sign_detector(det.classes):
        return "single_stage"
    return "two_stage" if cls and Path(cls).exists() else "single_stage"


def describe(key: str) -> dict[str, Any]:
    """Public description of one slot (GET /models/)."""
    det_type, role = SLOTS[key]
    path = slot_path(key)
    p = Path(path) if path else None
    out: dict[str, Any] = {
        "key": key, "role": role, "detection_type": det_type, "path": path or None,
        "file": p.name if p else None, "present": bool(p and p.exists()),
        "version": None, "sha256": None, "card": None, "card_error": None, "sha256_matches_card": None,
        "pipeline": None,
    }
    if key == "signs":
        out["pipeline"] = sign_pipeline()
    if not out["present"]:
        return out
    try:
        sha = file_sha256(p)  # type: ignore[arg-type]
    except OSError as exc:
        out["card_error"] = f"unreadable weights: {exc}"
        return out
    out["sha256"], out["version"] = sha, f"{p.name}@{sha[:12]}"  # type: ignore[union-attr]
    card, error = load_card(p)  # type: ignore[arg-type]
    out["card_error"] = error
    if card is not None:
        out["card"] = card.model_dump(mode="json")
        if card.sha256:
            out["sha256_matches_card"] = card.sha256.lower() == sha
    return out


def describe_all() -> list[dict[str, Any]]:
    return [describe(k) for k in SLOTS]


def crop_box(x1: int, y1: int, x2: int, y2: int, width: int, height: int,
             card: Optional[ModelCard]) -> tuple[int, int, int, int]:
    """Padded crop box for the classifier, clipped to the frame — padding as
    the trainer cut its crops (card `crop_pad_ratio` of the box size or
    `crop_pad_px`, default the processor's 20 px)."""
    if card is not None and card.crop_pad_ratio is not None:
        px = int(round(card.crop_pad_ratio * (x2 - x1)))
        py = int(round(card.crop_pad_ratio * (y2 - y1)))
    else:
        px = py = card.crop_pad_px if card is not None and card.crop_pad_px is not None else DEFAULT_CROP_PAD_PX
    return max(0, x1 - px), max(0, y1 - py), min(width, x2 + px), min(height, y2 + py)
