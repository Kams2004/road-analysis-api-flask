"""
Two-stage pothole pipeline + AI validation mode (RRAP-05-010/011/012/016,
RRAP-06-007, RRAP-13-001/016): decision rules per mode, audit sampling,
threshold resolution, model cards, single-stage fallback, model registry
endpoints and the live quality metrics.
"""
import json
import random
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from app.services import ai_validation as av, model_registry, model_stats
from app.services.ai_validation import Policy, Thresholds, build_policy, decide, resolve_thresholds
from app.services.detection_workflow import initial_status
from app.services.settings_service import SECTIONS, AiValidationSettings, ValidationSettings

POTHOLE = Thresholds(detector_accept=0.80, detector_reject=0.30, auto_accept=0.90, auto_reject=0.20)


def policy(mode="assisted", rate=0.0, agree=True, th=POTHOLE) -> Policy:
    return Policy(mode=mode, audit_sample_rate=rate, require_classifier_agreement=agree,
                  thresholds={"pothole": th})


def outcome(d):
    return d.status, d.action, d.ai_decision, d.rejection_code


# ── settings ─────────────────────────────────────────────────────────────────

def test_settings_defaults_and_validation():
    s = AiValidationSettings()
    assert SECTIONS["ai_validation"] is AiValidationSettings
    assert (s.mode, s.audit_sample_rate, s.require_classifier_agreement, s.type_overrides) == ("assisted", 0.10, True, {})
    with pytest.raises(ValidationError):
        AiValidationSettings(mode="yolo")
    with pytest.raises(ValidationError):
        AiValidationSettings(audit_sample_rate=1.5)
    with pytest.raises(ValidationError):
        AiValidationSettings(type_overrides={"pothole": {"auto_accept": 0.2, "auto_reject": 0.5}})
    with pytest.raises(ValidationError):
        AiValidationSettings(type_overrides={"pothole": {"detector_accept": 0.3, "detector_reject": 0.3}})


# ── manual ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("conf,score,loc,status", [
    (0.99, 0.99, "confirmed", "pending"),           # would be auto-accepted
    (0.10, 0.01, "confirmed", "pending"),           # would be auto-rejected
    (0.10, None, "missing", "location_pending"),
    (0.50, 0.50, "approximate", "location_pending"),
])
def test_manual_never_decides(conf, score, loc, status):
    d = decide(policy("manual", rate=1.0), "pothole", conf, score, loc, draw=0.0)
    assert outcome(d) == (status, "create", "review", None)
    assert not d.audit_sample and d.reason.startswith("manual mode")


def test_manual_records_the_suggestion():
    assert "suggests accept" in decide(policy("manual"), "pothole", 0.95, 0.95, "confirmed").reason
    assert "suggests reject" in decide(policy("manual"), "pothole", 0.10, None, "confirmed").reason


# ── assisted ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("conf,score,loc,expected", [
    # both confident + located → accept
    (0.85, 0.95, "confirmed", ("validated", "auto_accept", "auto_accept", None)),
    # classifier not confident enough → review
    (0.85, 0.80, "confirmed", ("pending", "create", "review", None)),
    # detector below its accept threshold → review even with a confident classifier
    (0.60, 0.99, "confirmed", ("pending", "create", "review", None)),
    # location not confirmed → never accepted
    (0.95, 0.99, "approximate", ("location_pending", "create", "review", None)),
    # classifier says no → reject (verifier code)
    (0.60, 0.10, "confirmed", ("rejected", "auto_reject", "auto_reject", "ai_verifier")),
    (0.60, 0.10, "missing", ("rejected", "auto_reject", "auto_reject", "ai_verifier")),
    # very low detector confidence → reject (low_confidence)
    (0.20, 0.95, "confirmed", ("rejected", "auto_reject", "auto_reject", "low_confidence")),
    # strong detector vs. classifier "no" → disagreement → review
    (0.90, 0.10, "confirmed", ("pending", "create", "review", None)),
])
def test_assisted_rules(conf, score, loc, expected):
    assert outcome(decide(policy(), "pothole", conf, score, loc)) == expected


def test_assisted_without_classifier_agreement():
    p = policy(agree=False)
    assert decide(p, "pothole", 0.85, 0.50, "confirmed").ai_decision == "auto_accept"
    assert decide(p, "pothole", 0.60, 0.10, "confirmed").ai_decision == "auto_reject"   # still rejects


def test_assisted_without_classifier_is_the_single_stage_rule():
    """No classifier score → detector thresholds only (exactly the old routing)."""
    p = policy()
    assert outcome(decide(p, "pothole", 0.85, None, "confirmed"))[:3] == ("validated", "auto_accept", "auto_accept")
    assert outcome(decide(p, "pothole", 0.50, None, "confirmed"))[:3] == ("pending", "create", "review")
    assert outcome(decide(p, "pothole", 0.29, None, "missing"))[1:] == ("auto_reject", "auto_reject", "low_confidence")


def test_auto_accept_disabled_without_detector_threshold():
    th = Thresholds(detector_accept=None, detector_reject=0.3, auto_accept=0.9, auto_reject=0.2)
    d = decide(policy(th=th), "pothole", 0.99, 0.99, "confirmed")
    assert d.ai_decision == "review" and d.reason == "auto-accept disabled"


# ── automatic ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("conf,score,loc,expected", [
    (0.50, 0.95, "confirmed", ("validated", "auto_accept", "auto_accept", None)),    # no detector band
    (0.90, 0.10, "confirmed", ("rejected", "auto_reject", "auto_reject", "ai_verifier")),  # classifier wins
    (0.50, 0.50, "confirmed", ("pending", "create", "review", None)),                # classifier uncertain
    (0.50, 0.95, "missing", ("location_pending", "create", "review", None)),         # location first
    (0.20, 0.99, "confirmed", ("rejected", "auto_reject", "auto_reject", "low_confidence")),
])
def test_automatic_rules(conf, score, loc, expected):
    assert outcome(decide(policy("automatic"), "pothole", conf, score, loc)) == expected


def test_automatic_without_classifier_uses_detector():
    p = policy("automatic")
    assert decide(p, "pothole", 0.85, None, "confirmed").ai_decision == "auto_accept"
    assert decide(p, "pothole", 0.50, None, "confirmed").ai_decision == "review"


# ── audit sampling ───────────────────────────────────────────────────────────

def test_audit_sample_leaves_decision_pending():
    p = policy(rate=0.10)
    d = decide(p, "pothole", 0.85, 0.95, "confirmed", draw=0.05)
    assert outcome(d) == ("pending", "create", "auto_accept", None) and d.audit_sample
    d = decide(p, "pothole", 0.60, 0.10, "missing", draw=0.05)
    assert outcome(d) == ("location_pending", "create", "auto_reject", None) and d.audit_sample
    assert not decide(p, "pothole", 0.85, 0.95, "confirmed", draw=0.10).audit_sample
    assert not decide(p, "pothole", 0.50, 0.50, "confirmed", draw=0.0).audit_sample      # reviews are not sampled
    assert not decide(p, "pothole", 0.85, 0.95, "confirmed", draw=None).audit_sample


def _sample(seed: int, mode="assisted") -> list[bool]:
    rng = random.Random(seed)
    return [decide(policy(mode, rate=0.10), "pothole", 0.85, 0.95, "confirmed", rng.random()).audit_sample
            for _ in range(2000)]


@pytest.mark.parametrize("mode", ["assisted", "automatic"])
def test_audit_sampling_is_deterministic_with_a_seed(mode):
    a, b = _sample(42, mode), _sample(42, mode)
    assert a == b
    assert 150 <= sum(a) <= 250                 # ≈ 10 % of the automatic decisions
    assert _sample(7, mode) != a


# ── thresholds resolution ────────────────────────────────────────────────────

def test_threshold_order_override_card_validation():
    rules = ValidationSettings(auto_reject_below=0.3, auto_accept_above=0.85)
    cards = {"pothole": {"detector_accept": 0.7, "detector_reject": 0.25, "auto_accept": 0.9, "auto_reject": 0.15}}
    ai = AiValidationSettings(type_overrides={"pothole": {"auto_accept": 0.95}})
    th = resolve_thresholds(ai, rules, cards, "pothole")
    assert (th.detector_accept, th.detector_reject, th.auto_accept, th.auto_reject) == (0.7, 0.25, 0.95, 0.15)
    assert th.sources == {"detector_accept": "card", "detector_reject": "card",
                          "auto_accept": "override", "auto_reject": "card"}
    # no card → section `validation` for the detector, classifier rules off
    th = resolve_thresholds(AiValidationSettings(), rules, {}, "speed_bump")
    assert (th.detector_accept, th.detector_reject, th.auto_accept, th.auto_reject) == (0.85, 0.3, None, None)
    assert th.sources["detector_reject"] == "validation" and th.sources["auto_accept"] is None


def test_policy_without_cards_matches_legacy_rule():
    rules = ValidationSettings(auto_reject_below=0.3, auto_accept_above=0.9)
    p = build_policy(AiValidationSettings(audit_sample_rate=0), rules, {}, ["pothole"])
    for conf in (0.1, 0.29, 0.3, 0.5, 0.89, 0.9, 0.99):
        for loc in ("confirmed", "approximate", "missing"):
            d = decide(p, "pothole", conf, None, loc)
            assert (d.status, d.action, d.rejection_code) == initial_status(conf, loc, "pothole", rules)
    # unknown type → global validation values
    assert decide(p, "litter", 0.95, None, "confirmed").ai_decision == "auto_accept"


# ── model cards ──────────────────────────────────────────────────────────────

CLS_CARD = {
    "model_type": "classifier", "name": "pothole_cls", "version": "v1", "created_at": "2026-10-14T10:00:00Z",
    "framework": "ultralytics 8.3.40", "base": "yolov8n-cls.pt", "classes": ["not_pothole", "pothole"],
    "imgsz": 224, "datasets": [{"name": "rdd", "source": "https://example.org", "license_note": "CC-BY", "images": 1200}],
    "metrics": {"top1_acc": 0.95}, "thresholds": {"auto_accept": 0.92, "auto_reject": 0.12, "target_precision": 0.97},
    "sha256": "x",
}
DET_CARD = {**CLS_CARD, "model_type": "detector", "name": "pothole_det", "classes": ["pothole"], "imgsz": 640,
            "thresholds": {"auto_accept": 0.6, "auto_reject": 0.2, "target_precision": 0.95}}


def test_parse_card():
    card = model_registry.parse_card(CLS_CARD)
    assert card.label == "pothole_cls:v1" and card.imgsz == 224 and card.thresholds.auto_accept == 0.92
    assert card.datasets[0].images == 1200
    assert model_registry.parse_card({**CLS_CARD, "extra_key": 1}).model_dump()["extra_key"] == 1
    with pytest.raises(ValueError):
        model_registry.parse_card({"name": "x"})                       # model_type / version missing
    with pytest.raises(ValueError):
        model_registry.parse_card({**CLS_CARD, "thresholds": {"auto_accept": 3}})
    with pytest.raises(ValueError):
        model_registry.parse_card([1, 2])


@pytest.fixture
def model_dir(tmp_path, monkeypatch):
    from app.core.config import settings
    det = tmp_path / "model_pothole.pt"
    det.write_bytes(b"detector weights")
    cls = tmp_path / "pothole_cls.pt"
    cls.write_bytes(b"classifier weights")
    det.with_suffix(".json").write_text(json.dumps({**DET_CARD, "sha256": model_registry.file_sha256(det)}))
    cls.with_suffix(".json").write_text(json.dumps(CLS_CARD))
    monkeypatch.setattr(settings, "MODEL_POTHOLE", str(det))
    monkeypatch.setattr(settings, "MODEL_SIGNS", str(tmp_path / "missing_signs.pt"))
    monkeypatch.setattr(settings, "MODEL_SPEEDBUMP", "")
    monkeypatch.setattr(settings, "MODEL_POTHOLE_CLASSIFIER", str(cls))
    monkeypatch.setattr(settings, "MODEL_SIGNS_CLASSIFIER", str(tmp_path / "missing_signs_cls.pt"))
    return tmp_path


def test_load_cards_and_describe(model_dir):
    card, err = model_registry.load_card(model_dir / "pothole_cls.pt")
    assert card.name == "pothole_cls" and err is None
    assert model_registry.load_card(model_dir / "nothing.pt") == (None, None)
    (model_dir / "broken.json").write_text("{not json")
    card, err = model_registry.load_card(model_dir / "broken.pt")
    assert card is None and err

    assert model_registry.card_thresholds() == {"pothole": {
        "detector_accept": 0.6, "detector_reject": 0.2, "auto_accept": 0.92, "auto_reject": 0.12}}
    by_key = {m["key"]: m for m in model_registry.describe_all()}
    assert by_key["pothole"]["present"] and by_key["pothole"]["sha256_matches_card"] is True
    assert by_key["pothole"]["version"].startswith("model_pothole.pt@") and len(by_key["pothole"]["version"]) == 29
    assert by_key["pothole_classifier"]["card"]["version"] == "v1"
    assert by_key["pothole_classifier"]["sha256_matches_card"] is False
    assert not by_key["signs"]["present"] and by_key["speedbump"]["path"] is None


def test_classifier_path_legacy_alias(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "MODEL_POTHOLE_CLASSIFIER", "")
    monkeypatch.setattr(settings, "MODEL_CLASSIFIER", "/legacy/cls.pt")
    assert settings.pothole_classifier_path == "/legacy/cls.pt"
    monkeypatch.setattr(settings, "MODEL_POTHOLE_CLASSIFIER", "/new/cls.pt")
    assert settings.pothole_classifier_path == "/new/cls.pt"


def test_crop_box_padding():
    assert model_registry.crop_box(100, 100, 200, 150, 640, 480, None) == (80, 80, 220, 170)
    assert model_registry.crop_box(5, 5, 630, 470, 640, 480, None) == (0, 0, 640, 480)
    card = model_registry.parse_card({**CLS_CARD, "crop_pad_ratio": 0.1})
    assert model_registry.crop_box(100, 100, 200, 150, 640, 480, card) == (90, 95, 210, 155)
    card = model_registry.parse_card({**CLS_CARD, "crop_pad_px": 0})
    assert model_registry.crop_box(100, 100, 200, 150, 640, 480, card) == (100, 100, 200, 150)


# ── processor: single-stage fallback ─────────────────────────────────────────

@pytest.fixture
def processor():
    from app.workers import processor as proc
    proc._unload_models()
    yield proc
    proc._unload_models()


def test_missing_classifier_keeps_single_stage(processor, monkeypatch, tmp_path):
    from app.core.config import settings
    import numpy as np
    monkeypatch.setattr(settings, "MODEL_POTHOLE_CLASSIFIER", str(tmp_path / "absent.pt"))
    processor._load_verifier("pothole")
    assert "pothole_classifier" not in processor._models
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    assert processor._verify("pothole", frame, 10, 10, 50, 50) is None
    assert processor._verify("traffic_sign", frame, 10, 10, 50, 50) is None


def test_unloadable_classifier_is_ignored(processor, monkeypatch, tmp_path):
    from app.core.config import settings
    stub = tmp_path / "stub_cls.pt"
    stub.write_bytes(b"not a checkpoint")
    monkeypatch.setattr(settings, "MODEL_POTHOLE_CLASSIFIER", str(stub))
    processor._load_verifier("pothole")
    assert "pothole_classifier" not in processor._models
    assert "pothole_classifier" not in processor._model_versions


class _FakeProbs:
    def __init__(self, values):
        import torch
        self.data = torch.tensor(values)


class _FakeClassifier:
    task = "classify"
    names = {0: "not_pothole", 1: "pothole"}

    def __init__(self):
        self.calls = []

    def __call__(self, crop, imgsz=None, verbose=False):
        self.calls.append((crop.shape, imgsz))
        from types import SimpleNamespace
        return [SimpleNamespace(probs=_FakeProbs([0.07, 0.93]))]


def test_verify_scores_padded_crop_with_card_imgsz(processor):
    import numpy as np
    fake = _FakeClassifier()
    processor._models["pothole_classifier"] = fake
    processor._model_cards["pothole_classifier"] = model_registry.parse_card(CLS_CARD)
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    assert processor._verify("pothole", frame, 100, 300, 200, 350) == 0.93
    assert fake.calls == [((90, 140, 3), 224)]     # 20 px padding each side, imgsz from the card
    assert processor._positive_index(fake, "pothole") == 1


def test_job_policy_uses_loaded_cards(processor):
    processor._model_cards["pothole"] = model_registry.parse_card(DET_CARD)
    processor._model_cards["pothole_classifier"] = model_registry.parse_card(CLS_CARD)
    p = processor._policy_for_job(AiValidationSettings(), ValidationSettings())
    th = p.for_type("pothole")
    assert (th.detector_accept, th.detector_reject, th.auto_accept, th.auto_reject) == (0.6, 0.2, 0.92, 0.12)
    assert p.for_type("traffic_sign").detector_reject == 0.30          # no card → validation
    assert p.audit_sample_rate == 0.10 and p.mode == "assisted"


# ── live quality metrics ─────────────────────────────────────────────────────

NOW = datetime(2026, 10, 15, 12, 0, 0)


def _row(i, ai, status, by="alice", audit=False, days=1, **kw):
    return model_stats.Row(id=f"d{i}", created_at=NOW - timedelta(days=days), ai_decision=ai, audit_sample=audit,
                           review_status=status, reviewed_by=by, type=kw.pop("type", "pothole"),
                           predicted_type="pothole", model_name="pothole", model_version="m.pt@1",
                           verifier_model=kw.pop("verifier_model", "c.pt@2"), **kw)


def test_stats_compute():
    rows = [
        _row(1, "auto_accept", "validated", by="system"),
        _row(2, "auto_accept", "pending", by=None, audit=True),                  # audit, undecided
        _row(3, "auto_accept", "validated", audit=True),                         # audit, agreed
        _row(4, "auto_accept", "rejected", audit=True),                          # audit, disagreed
        _row(5, "auto_accept", "corrected", audit=True, type="speed_bump"),      # reclassified → disagree
        _row(6, "auto_reject", "rejected", audit=True),                          # agreed
        _row(7, "auto_reject", "validated"),                                      # reopened, overturned
        _row(8, "auto_accept", "validated"),                                      # reopened, confirmed
        _row(9, "review", "pending", by=None),
        _row(10, "review", "validated", days=20),
        _row(11, None, "validated"),                                              # pre-feature row, ignored
        _row(12, "auto_reject", "rejected", by="system", days=20, verifier_model=None),
    ]
    out = model_stats.compute(rows, {"d7", "d8"}, NOW)
    w7, w30 = out["windows"]
    assert (w7["days"], w30["days"]) == (7, 30)
    assert (w7["total"], w7["auto_accept"], w7["auto_reject"], w7["review"], w7["audit_samples"]) == (9, 6, 2, 1, 5)
    assert w7["workload_pct"] == round(100 * 6 / 9, 1)
    assert w7["audit"] == {"decided": 4, "agreed": 2, "agreement_rate": 0.5}
    assert w7["auto_accept_precision"] == round(1 / 3, 4) and w7["auto_accept_audited"] == 3
    assert w7["auto_reject_precision"] == 1.0
    assert w7["reopened"] == {"count": 2, "decided": 2, "agreed": 1, "agreement_rate": 0.5}
    assert w30["total"] == 11 and len(w30["by_model"]) == 2
    assert w30["by_model"][0]["verifier_model"] == "c.pt@2" and w30["by_model"][0]["total"] == 10
    empty = model_stats.compute([], set(), NOW)["windows"][0]
    assert empty["workload_pct"] is None and empty["audit"]["agreement_rate"] is None


# ── endpoints ────────────────────────────────────────────────────────────────

class _FakeDB:
    async def get(self, *a, **k):
        return None


@pytest.fixture
async def client(monkeypatch, model_dir):
    from app.api.deps import Principal, get_principal
    from app.db.session import get_db
    from app.main import app

    perms = {"detections": {"view"}}

    async def principal():
        return Principal(kind="user", id="u1", label="tester@example.com", permissions=perms)

    async def db():
        yield _FakeDB()

    app.dependency_overrides[get_principal] = principal
    app.dependency_overrides[get_db] = db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        c.perms = perms
        yield c
    app.dependency_overrides.pop(get_principal, None)
    app.dependency_overrides.pop(get_db, None)


async def test_models_endpoint(client):
    r = await client.get("/models/")
    assert r.status_code == 200, r.text
    body = r.json()
    keys = [m["key"] for m in body["models"]]
    assert keys == ["pothole", "signs", "speedbump", "pothole_classifier", "signs_classifier", "vehicles"]
    cls = body["models"][3]
    assert cls["card"]["name"] == "pothole_cls" and cls["card"]["datasets"][0]["license_note"] == "CC-BY"
    assert body["ai_validation"]["mode"] == "assisted"
    th = body["thresholds"]["pothole"]
    assert th["auto_accept"] == {"value": 0.92, "source": "card"} and th["two_stage"] is True
    assert th["detector_reject"] == {"value": 0.2, "source": "card"}
    assert body["thresholds"]["traffic_sign"]["detector_reject"] == {"value": 0.3, "source": "validation"}
    assert body["thresholds"]["traffic_sign"]["two_stage"] is False


async def test_models_stats_endpoint(client, monkeypatch):
    async def rows(db, since):
        return [_row(1, "auto_accept", "validated", audit=True, days=0)], set()

    monkeypatch.setattr(model_stats, "load_rows", rows)
    r = await client.get("/models/stats")
    assert r.status_code == 200, r.text
    w7 = r.json()["windows"][0]
    assert w7["total"] == 1 and w7["auto_accept_precision"] == 1.0 and w7["by_model"][0]["model_name"] == "pothole"


async def test_models_permission(client, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "AUTH_ENFORCED", True)
    client.perms.clear()
    r = await client.get("/models/")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "forbidden"
    client.perms["settings"] = {"view"}
    assert (await client.get("/models/")).status_code == 200


def test_openapi_documents_ai_models():
    from app.main import app
    app.openapi_schema = None
    schema = app.openapi()
    op = schema["paths"]["/models/"]["get"]
    assert op["tags"] == ["AI models"] and op["x-permission"] == "settings:view | detections:view"
    assert "/models/stats" in schema["paths"]
    props = schema["components"]["schemas"]["DetectionOut"]["properties"]
    assert {"verifier_score", "verifier_model", "ai_decision", "ai_decision_reason", "audit_sample"} <= set(props)
    params = {p["name"] for p in schema["paths"]["/detections/queue"]["get"]["parameters"]}
    assert {"ai_decision", "audit_sample"} <= params
