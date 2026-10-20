"""
/models — model registry (lite) and live AI quality (RRAP-05-010/012/016, RRAP-13-016).

  GET /models/        the model slots of the platform (detectors + crop
                      classifiers) with their weights version and model card,
                      the AI validation mode and the effective thresholds
  GET /models/stats   quality of the automatic decisions measured against the
                      human verdicts (audit samples, reopened decisions),
                      per model version, over 7 / 30 days
  GET /models/signs/taxonomy
                      the VIASafe traffic-sign codes (subtype of traffic_sign
                      detections) with their names and families

Readable with settings:view or detections:view.
"""
import asyncio
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, api_error, get_principal
from app.core.config import settings
from app.core.db.session import get_db
from app.modules.m05_ai_detection import ai_validation, model_registry, model_stats, sign_taxonomy
from app.modules.m05_ai_detection import parked_vehicle
from app.modules.m05_ai_detection.parked_vehicle import ParkedVehicleSettings
from app.core.settings_service import AiValidationSettings, ValidationSettings, get_section

router = APIRouter()

READ_MODULES = ("settings", "detections")


async def model_reader(principal: Principal = Depends(get_principal)) -> Principal:
    """settings:view or detections:view."""
    if not settings.AUTH_ENFORCED:
        return principal
    if principal.kind != "user":
        raise api_error(401, "not_authenticated", "Please sign in.")
    if not any(principal.can(m, "view") for m in READ_MODULES):
        raise api_error(403, "forbidden", "You do not have permission for this action.",
                        module="settings", action="view")
    return principal


model_reader.openapi_access = {"kind": "any_view", "modules": READ_MODULES}  # type: ignore[attr-defined]


# ── schemas ──────────────────────────────────────────────────────────────────

class CardClassThresholdsOut(BaseModel):
    model_config = ConfigDict(extra="allow")
    auto_accept: Optional[float] = Field(None, description="P(class) at/above which the classifier is sure of it.")
    auto_reject: Optional[float] = Field(None, description="P(class) below which the class is `unknown_sign` "
                                                           "(human review).")


class CardThresholdsOut(BaseModel):
    model_config = ConfigDict(extra="allow")
    auto_accept:      Optional[float] = Field(None, description="Score at/above which the model proposes to accept.")
    auto_reject:      Optional[float] = Field(None, description="Score at/below which it proposes to reject.")
    target_precision: Optional[float] = Field(None, description="Precision the trainer calibrated the thresholds for.")
    per_class:        dict[str, CardClassThresholdsOut] = Field(
        {}, description="Multi-class classifier (traffic signs): thresholds per class code on P(predicted class).",
        examples=[{"stop": {"auto_accept": 0.9, "auto_reject": 0.5}}])
    reject_class:     Optional[str] = Field(None, description="Class meaning \"not this type\" (signs: "
                                                              "`not_a_sign`): a confident prediction auto-rejects "
                                                              "with code `ai_not_a_sign`.", examples=["not_a_sign"])


class ModelCardOut(BaseModel):
    """A model card (`<weights>.json`) written by the training project."""
    model_config = ConfigDict(extra="allow", protected_namespaces=())
    model_type: str = Field(description="`detector` or `classifier`.", examples=["classifier"])
    name:       str = Field(examples=["pothole_cls"])
    version:    str = Field(examples=["v1"])
    created_at: Optional[str] = Field(None, description="Training date (ISO 8601).")
    framework:  Optional[str] = Field(None, examples=["ultralytics 8.3.40"])
    base:       Optional[Any] = Field(None, description="Base weights it was fine-tuned from.")
    classes:    list[str] = Field([], description="Classes. Sign detector: `traffic_sign`, `traffic_light` "
                                              "(two-stage); sign classifier: VIASafe sign codes.",
                          examples=[["not_pothole", "pothole"]])
    imgsz:      Optional[int] = Field(None, description="Input size.", examples=[224])
    datasets:   list[dict[str, Any]] = Field([], description="Training datasets: `name`, `source`, "
                                                             "`license_note`, `images`.")
    metrics:    dict[str, Any] = Field({}, description="Validation metrics reported by the trainer (a multi-class "
                                                       "classifier may add `per_class` {code: {precision, recall …}}).")
    thresholds: CardThresholdsOut = Field(default_factory=CardThresholdsOut)
    sha256:     Optional[str] = Field(None, description="SHA-256 of the weights file the card describes.")
    crop_pad_px:    Optional[int] = Field(None, description="Classifier crop padding in pixels.")
    crop_pad_ratio: Optional[float] = Field(None, description="Classifier crop padding as a share of the box size.")
    taxonomy_version: Optional[str] = Field(None, description="Sign classifier: version of the sign taxonomy its "
                                                              "classes come from.", examples=["viasafe-signs-v1"])


class ModelOut(BaseModel):
    """One model slot of the platform."""
    key:            str = Field(description="Slot: `pothole`, `signs`, `speedbump`, `pothole_classifier`, "
                                            "`signs_classifier`, `vehicles` (COCO YOLOv8 of the parked-vehicle "
                                            "pipeline).",
                                examples=["pothole_classifier"])
    role:           str = Field(description="`detector` or `classifier` (second stage).", examples=["classifier"])
    detection_type: str = Field(description="Detection type it produces / verifies.", examples=["pothole"])
    path:           Optional[str] = Field(None, description="Configured weights path.")
    file:           Optional[str] = Field(None, description="Weights file name.")
    present:        bool = Field(description="The weights file exists in the model directory.")
    version:        Optional[str] = Field(None, description="`file@sha256[:12]` — stored on detections as "
                                                            "`model_version` / `verifier_model`.")
    sha256:         Optional[str] = Field(None, description="Full SHA-256 of the weights.")
    sha256_matches_card: Optional[bool] = Field(None, description="The card's `sha256` matches the weights "
                                                                  "(`null` = no card or no hash in it).")
    card:           Optional[ModelCardOut] = Field(None, description="Model card, when `<weights>.json` exists.")
    card_error:     Optional[str] = Field(None, description="Why the card next to the weights was ignored.")
    pipeline:       Optional[str] = Field(None, description="`signs` slot only: `two_stage` (detector card classes "
                                                            "traffic_sign/traffic_light + sign classifier present) "
                                                            "or `single_stage` (legacy multi-class sign detector, "
                                                            "or no classifier); `null` when the detector has no "
                                                            "card (the worker decides from the weights).")


class ThresholdValueOut(BaseModel):
    value:  Optional[float] = Field(None, description="Effective value (`null` = not set → the rule is off).")
    source: Optional[str] = Field(None, description="`override` (ai_validation), `card`, `validation`; per "
                                                    "class also `global` (inherits the type value).")


class ClassThresholdsOut(BaseModel):
    auto_accept: ThresholdValueOut
    auto_reject: ThresholdValueOut


class EffectiveThresholdsOut(BaseModel):
    """Thresholds the processor applies to a type."""
    detector_accept: ThresholdValueOut
    detector_reject: ThresholdValueOut
    auto_accept:     ThresholdValueOut
    auto_reject:     ThresholdValueOut
    card:            dict[str, Optional[float]] = Field({}, description="What the model cards propose "
                                                                        "(pre-fill of the settings form).")
    two_stage:       bool = Field(description="A crop classifier is installed for this type (signs: and the "
                                          "detector is the 2-class traffic_sign/traffic_light model).")
    per_class:       dict[str, ClassThresholdsOut] = Field(
        {}, description="traffic_sign: effective thresholds per sign code (override `ai_validation.type_overrides"
                        ".traffic_sign.per_class`, else the classifier card's `thresholds.per_class`, else the "
                        "type value — source `global`).")
    reject_class:    Optional[str] = Field(None, description="Classifier class that auto-rejects (signs: `not_a_sign`).")


class ModelsOut(BaseModel):
    """Model registry view."""
    model_config = ConfigDict(protected_namespaces=())
    models:        list[ModelOut]
    ai_validation: AiValidationSettings = Field(description="Current settings section `ai_validation`.")
    thresholds:    dict[str, EffectiveThresholdsOut] = Field(description="Effective thresholds per detection type.")


class TaxonomyFamilyOut(BaseModel):
    code:    str = Field(examples=["prohibition"])
    name_en: str = Field(examples=["Prohibition"])
    name_fr: str = Field(examples=["Interdiction"])


class TaxonomyClassOut(BaseModel):
    code:    str = Field(description="Sign code stored as the detection `subtype`.", examples=["speed_limit_50"])
    name_en: str = Field(examples=["Speed limit 50 km/h"])
    name_fr: str = Field(examples=["Limitation 50 km/h"])
    family:  str = Field(description="Family code.", examples=["speed_limit"])


class TaxonomyOut(BaseModel):
    """The traffic-sign taxonomy."""
    taxonomy_version: Optional[str] = Field(None, examples=["viasafe-signs-v1"])
    source:   str = Field(description="`file` (taxonomy.yaml next to the sign classifier), `card` (the classifier "
                                      "card's classes) or `builtin`.", examples=["file"])
    families: list[TaxonomyFamilyOut]
    classes:  list[TaxonomyClassOut]


class StatsBlockOut(BaseModel):
    total:         int = Field(description="Detections with an AI decision.", examples=[240])
    auto_accept:   int = Field(examples=[120])
    auto_reject:   int = Field(examples=[60])
    review:        int = Field(examples=[60])
    audit_samples: int = Field(description="Automatic decisions drawn for a human audit.", examples=[18])
    workload_pct:  Optional[float] = Field(None, description="Share needing a human (review + audit samples), %.",
                                           examples=[32.5])
    audit:         dict[str, Any] = Field(description="Human verdicts on audit samples: `decided`, `agreed`, "
                                                      "`agreement_rate` (0–1).")
    auto_accept_precision: Optional[float] = Field(None, description="Audited auto-accepts confirmed (0–1).")
    auto_accept_audited:   int = Field(0, description="Audited auto-accepts with a human verdict.")
    auto_reject_precision: Optional[float] = Field(None, description="Audited auto-rejects confirmed (0–1).")
    auto_reject_audited:   int = Field(0, description="Audited auto-rejects with a human verdict.")
    reopened:      dict[str, Any] = Field(description="Automatic decisions reopened by a human: `count`, "
                                                      "`decided`, `agreed`, `agreement_rate`.")


class ModelStatsOut(StatsBlockOut):
    model_config = ConfigDict(protected_namespaces=())
    model_name:            Optional[str] = Field(None, description="Detector slot (`pothole`, `signs`, `speedbump`, `vehicles`).",
                                                 examples=["pothole"])
    model_version:         Optional[str] = Field(None, examples=["model_pothole.pt@3fa94c1be2d0"])
    model_card_version:    Optional[str] = Field(None, examples=["pothole_det:v1"])
    verifier_model:        Optional[str] = Field(None, description="Crop classifier version (pothole or sign "
                                                                       "classifier); `null` = single stage.",
                                                 examples=["model_pothole_classifier.pt@91ab0e44c7d2"])
    verifier_card_version: Optional[str] = Field(None, examples=["pothole_cls:v1"])


class StatsWindowOut(StatsBlockOut):
    days:     int = Field(examples=[7])
    since:    datetime
    by_model: list[ModelStatsOut]


class StatsOut(BaseModel):
    generated_at: datetime
    windows:      list[StatsWindowOut]


# ── endpoints ────────────────────────────────────────────────────────────────

def _effective(ai: AiValidationSettings, rules: ValidationSettings, models: list[dict],
               pv: Optional[ParkedVehicleSettings] = None) -> dict[str, Any]:
    # parked_vehicle: the rule score's thresholds (settings `parked_vehicle`) act as its verifier card
    cards = parked_vehicle.merge_policy_cards(model_registry.card_thresholds(), pv or ParkedVehicleSettings())
    two_stage = {m["detection_type"] for m in models if m["role"] == "classifier" and m["present"]}
    signs = next((m for m in models if m["key"] == "signs"), None)
    if signs is not None and signs.get("pipeline") == "single_stage":
        two_stage.discard("traffic_sign")
    out = {}
    for det_type in dict.fromkeys(t for t, _ in model_registry.SLOTS.values()):
        th = ai_validation.resolve_thresholds(ai, rules, cards, det_type)
        card = cards.get(det_type, {})
        out[det_type] = {
            **{f: {"value": getattr(th, f), "source": th.sources.get(f)} for f in ai_validation.FIELDS},
            "card": {k: v for k, v in card.items() if k in ai_validation.FIELDS},
            "two_stage": det_type in two_stage,
            "per_class": th.effective_per_class(), "reject_class": th.reject_class,
        }
    return out


@router.get("/", response_model=ModelsOut, summary="List the AI models and their model cards")
async def list_ai_models(db: AsyncSession = Depends(get_db), _: Principal = Depends(model_reader)):
    """
    Every model slot of the platform — the three detectors and the two crop classifiers (second stage of
    the two-stage pipelines: pothole verifier, traffic-sign classifier naming each sign crop with a
    VIASafe sign code) and the `vehicles` slot (stock COCO YOLOv8 of the parked-vehicle pipeline, judged
    by rules; its `parked_vehicle` thresholds apply to the rule score and come from settings section
    `parked_vehicle` unless `ai_validation` overrides them) — with its weights version (`file@sha256[:12]`, as stored on
    detections) and its **model card** (`<weights>.json` written by the training project: version,
    training date, datasets, metrics, proposed thresholds). Also returns the AI validation settings and
    the thresholds the processor applies per detection type, with their source (`override` from
    `ai_validation`, `card`, or the fallback section `validation`). A slot whose file is missing has
    `present: false`; a missing classifier means the single-stage pipeline. For traffic signs,
    `thresholds.traffic_sign.per_class` gives the effective thresholds per sign code (P(predicted
    code) < `auto_reject` → `unknown_sign`, review; ≥ `auto_accept` → the classifier agrees) and
    the `signs` slot's `pipeline` tells whether the two-stage sign pipeline is active (the legacy
    multi-class sign detector stays single-stage).
    """
    models = await asyncio.to_thread(model_registry.describe_all)
    ai: AiValidationSettings = await get_section(db, "ai_validation")  # type: ignore[assignment]
    rules: ValidationSettings = await get_section(db, "validation")  # type: ignore[assignment]
    pv: ParkedVehicleSettings = await get_section(db, "parked_vehicle")  # type: ignore[assignment]
    return {"models": models, "ai_validation": ai,
            "thresholds": await asyncio.to_thread(_effective, ai, rules, models, pv)}


@router.get("/stats", response_model=StatsOut, summary="Get live AI quality metrics")
async def get_model_stats(db: AsyncSession = Depends(get_db), _: Principal = Depends(model_reader)):
    """
    Quality of the AI validation measured against human decisions, over the last 7 and 30 days
    (detection creation time), overall and per model version (detector weights + card, classifier
    weights + card): counts of `auto_accept` / `auto_reject` / `review`, the human **workload**
    (share needing review, incl. audit samples), the human **agreement rate on audit samples** and on
    **reopened** automatic decisions, and the **precision of auto-accepts** (resp. auto-rejects)
    among audited samples. A human verdict agrees with an auto-accept when the detection was accepted
    with its predicted class (traffic signs: also its predicted sign code — a corrected code counts as
    a disagreement), with an auto-reject when it was rejected. Sign detections appear per model
    version like potholes (`model_name` `signs`, `verifier_model` = the sign classifier). Detections created before the
    AI validation mode are ignored.
    """
    return await model_stats.stats(db)


@router.get("/signs/taxonomy", response_model=TaxonomyOut, summary="Get the traffic-sign taxonomy")
async def get_sign_taxonomy(_: Principal = Depends(model_reader)):
    """
    The VIASafe traffic-sign codes a `traffic_sign` detection's `subtype` can take — the full list
    offered when correcting a sign — with English / French names and their family (priority,
    prohibition, speed_limit, mandatory, warning, information, signals, other). Source, first
    available: `taxonomy.yaml` installed next to the sign classifier, the classifier card's classes,
    or a built-in list. Always contains `traffic_light` and `unknown_sign`; never the classifier's
    reject class (`not_a_sign`). Names missing from the source are humanized from the code.
    """
    return await asyncio.to_thread(sign_taxonomy.current)
