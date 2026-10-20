"""
Two-stage traffic-sign pipeline (detector traffic_sign/traffic_light + sign
classifier) and the video-consistency accept blockers: per-class thresholds,
unknown_sign fallback, not_a_sign rejection, legacy single-stage
compatibility, taxonomy loading, static-object / road-area rules.
"""
import json
from types import SimpleNamespace

import numpy as np
import pytest
from pydantic import ValidationError

from app.services import model_registry, sign_taxonomy, static_check
from app.services.ai_validation import Policy, Thresholds, decide, resolve_thresholds, sign_subtype
from app.services.settings_service import AiValidationSettings, ValidationSettings
from tests.test_ai_validation import CLS_CARD, DET_CARD, _FakeProbs, client, model_dir  # noqa: F401  (fixtures)

SIGNS = Thresholds(detector_accept=0.6, detector_reject=0.25, auto_accept=0.85, auto_reject=0.40,
                   per_class={"speed_limit_50": {"auto_accept": 0.95, "auto_reject": 0.6},
                              "not_a_sign": {"auto_accept": 0.7}},
                   reject_class="not_a_sign")


def policy(mode="assisted", th=SIGNS, rate=0.0) -> Policy:
    return Policy(mode=mode, audit_sample_rate=rate, thresholds={"traffic_sign": th})


def outcome(d):
    return d.status, d.ai_decision, d.rejection_code


# ── decisions ────────────────────────────────────────────────────────────────

def test_per_class_thresholds():
    assert SIGNS.for_class("speed_limit_50") == (0.95, 0.6)
    assert SIGNS.for_class("stop") == (0.85, 0.40)          # inherits the type values
    p = policy()
    # stop: 0.90 ≥ global 0.85 → accept; speed_limit_50: 0.90 < its own 0.95 → review
    assert outcome(decide(p, "traffic_sign", 0.8, 0.90, "confirmed", predicted_class="stop")) == \
        ("validated", "auto_accept", None)
    d = decide(p, "traffic_sign", 0.8, 0.90, "confirmed", predicted_class="speed_limit_50")
    assert outcome(d) == ("pending", "review", None) and "speed_limit_50" in d.reason
    assert decide(policy("automatic"), "traffic_sign", 0.4, 0.97, "confirmed",
                  predicted_class="speed_limit_50").ai_decision == "auto_accept"


def test_classifier_unsure_falls_back_to_unknown_sign():
    # P < per-class auto_reject → unknown_sign subtype + review (not a rejection)
    assert sign_subtype(SIGNS, "speed_limit_50", 0.55) == "unknown_sign"
    assert sign_subtype(SIGNS, "speed_limit_50", 0.65) == "speed_limit_50"
    assert sign_subtype(SIGNS, "stop", 0.39) == "unknown_sign"
    for mode in ("assisted", "automatic"):
        d = decide(policy(mode), "traffic_sign", 0.9, 0.55, "confirmed", predicted_class="speed_limit_50")
        assert outcome(d) == ("pending", "review", None) and "unknown_sign" in d.reason
    # the classifier itself predicting unknown_sign → review
    d = decide(policy(), "traffic_sign", 0.9, 0.99, "confirmed", predicted_class="unknown_sign")
    assert outcome(d) == ("pending", "review", None)
    # 2-class detector without a usable classifier → review
    d = decide(policy(), "traffic_sign", 0.99, None, "confirmed", predicted_class="unknown_sign")
    assert d.ai_decision == "review"


def test_not_a_sign_is_rejected_and_never_a_subtype():
    d = decide(policy(), "traffic_sign", 0.5, 0.8, "confirmed", predicted_class="not_a_sign")
    assert outcome(d) == ("rejected", "auto_reject", "ai_not_a_sign")
    assert sign_subtype(SIGNS, "not_a_sign", 0.8) == "unknown_sign"
    assert sign_subtype(SIGNS, "background", 0.99) == "unknown_sign"
    # unsure "not a sign" → review; strong detector vs classifier "no" (assisted) → disagreement review
    assert decide(policy(), "traffic_sign", 0.5, 0.6, "confirmed", predicted_class="not_a_sign").ai_decision == "review"
    assert decide(policy(), "traffic_sign", 0.9, 0.8, "confirmed", predicted_class="not_a_sign").ai_decision == "review"
    assert decide(policy("automatic"), "traffic_sign", 0.9, 0.8, "confirmed",
                  predicted_class="not_a_sign").rejection_code == "ai_not_a_sign"
    # low detector confidence still wins first
    assert decide(policy(), "traffic_sign", 0.1, 0.99, "confirmed",
                  predicted_class="stop").rejection_code == "low_confidence"


def test_sign_modes_and_audit_like_potholes():
    d = decide(policy("manual"), "traffic_sign", 0.9, 0.99, "confirmed", predicted_class="stop")
    assert outcome(d) == ("pending", "review", None) and "suggests accept" in d.reason
    d = decide(policy(rate=0.5), "traffic_sign", 0.9, 0.99, "confirmed", draw=0.1, predicted_class="stop")
    assert outcome(d) == ("pending", "auto_accept", None) and d.audit_sample
    assert decide(policy(), "traffic_sign", 0.9, 0.99, "approximate",
                  predicted_class="stop").status == "location_pending"


def test_traffic_light_and_legacy_use_detector_rules():
    """No classifier score, no predicted_class → exactly the single-stage rule."""
    p = policy()
    assert decide(p, "traffic_sign", 0.7, None, "confirmed").ai_decision == "auto_accept"
    assert decide(p, "traffic_sign", 0.5, None, "confirmed").ai_decision == "review"
    assert decide(p, "traffic_sign", 0.2, None, "confirmed").rejection_code == "low_confidence"


def test_resolve_per_class_override_card_global():
    cards = {"traffic_sign": {"auto_accept": 0.8, "auto_reject": 0.3, "reject_class": "not_a_sign",
                              "per_class": {"stop": {"auto_accept": 0.9, "auto_reject": 0.5},
                                            "give_way": {"auto_accept": 0.88, "auto_reject": None}}}}
    ai = AiValidationSettings(type_overrides={"traffic_sign": {"per_class": {"stop": {"auto_accept": 0.97}}}})
    th = resolve_thresholds(ai, ValidationSettings(), cards, "traffic_sign")
    assert th.for_class("stop") == (0.97, 0.5) and th.for_class("give_way") == (0.88, 0.3)
    assert th.reject_class == "not_a_sign"
    eff = th.effective_per_class()
    assert eff["stop"]["auto_accept"] == {"value": 0.97, "source": "override"}
    assert eff["stop"]["auto_reject"] == {"value": 0.5, "source": "card"}
    assert eff["give_way"]["auto_reject"] == {"value": 0.3, "source": "global"}
    with pytest.raises(ValidationError):
        AiValidationSettings(type_overrides={"traffic_sign": {"per_class": {"stop": {"auto_accept": 0.2,
                                                                                     "auto_reject": 0.5}}}})


def test_accept_blockers_turn_accept_into_review():
    blk = [("not_static", "seen in 1 frame < 2")]
    d = decide(policy(), "traffic_sign", 0.9, 0.99, "confirmed", predicted_class="stop", accept_blockers=blk)
    assert outcome(d) == ("pending", "review", None) and d.reason.startswith("not_static:")
    # rejections are not affected
    d = decide(policy(), "traffic_sign", 0.5, 0.9, "confirmed", predicted_class="not_a_sign", accept_blockers=blk)
    assert d.ai_decision == "auto_reject"
    assert decide(policy(), "traffic_sign", 0.9, 0.99, "confirmed", predicted_class="stop").ai_decision == "auto_accept"


# ── taxonomy ─────────────────────────────────────────────────────────────────

def test_humanize():
    assert sign_taxonomy.humanize("speed_limit_50") == "Speed limit 50 km/h"
    assert sign_taxonomy.humanize("speed_limit_50", "fr") == "Limitation 50 km/h"
    assert sign_taxonomy.humanize("no_heavy_goods") == "No heavy goods"
    assert sign_taxonomy.guess_family("no_heavy_goods") == "prohibition"


def test_taxonomy_from_yaml_file(tmp_path):
    (tmp_path / "taxonomy.yaml").write_text(
        "version: viasafe-v2\n"
        "families:\n  - {code: prohibition, name_en: Prohibition, name_fr: Interdiction}\n"
        "classes:\n"
        "  - {code: stop, name_en: STOP, name_fr: Arrêt, family: priority}\n"
        "  - {code: speed_limit_50, family: speed_limit}\n"
        "  - {code: no_heavy_goods}\n"
        "  - {code: not_a_sign, family: other}\n", encoding="utf-8")
    tax = sign_taxonomy.build(str(tmp_path / "model_signs_classifier.pt"))
    by = {c["code"]: c for c in tax["classes"]}
    assert tax["source"] == "file" and tax["taxonomy_version"] == "viasafe-v2"
    assert by["stop"]["name_fr"] == "Arrêt" and by["speed_limit_50"]["name_fr"] == "Limitation 50 km/h"
    assert by["no_heavy_goods"] == {"code": "no_heavy_goods", "name_en": "No heavy goods",
                                    "name_fr": "No heavy goods", "family": "prohibition"}
    assert "not_a_sign" not in by and {"traffic_light", "unknown_sign"} <= set(by)
    assert {f["code"] for f in tax["families"]} >= {"prohibition", "priority", "speed_limit", "signals", "other"}


def test_taxonomy_mapping_shape_card_and_builtin_fallback(tmp_path):
    (tmp_path / "taxonomy.yaml").write_text("taxonomy_version: t1\ncodes:\n  give_way: {name_fr: Cédez}\n")
    tax = sign_taxonomy.build(str(tmp_path / "cls.pt"))
    assert tax["classes"][0]["code"] == "give_way" and tax["classes"][0]["name_fr"] == "Cédez"
    # broken file → card classes
    (tmp_path / "taxonomy.yaml").write_text("classes: [")
    card = model_registry.parse_card({**CLS_CARD, "name": "signs_cls", "classes": ["stop", "not_a_sign", "x_y"],
                                      "taxonomy_version": "t9", "thresholds": {"reject_class": "not_a_sign"}})
    tax = sign_taxonomy.build(str(tmp_path / "cls.pt"), card)
    assert tax["source"] == "card" and tax["taxonomy_version"] == "t9"
    assert [c["code"] for c in tax["classes"]] == ["stop", "x_y", "traffic_light", "unknown_sign"]
    # nothing installed → built-in list
    tax = sign_taxonomy.build(str(tmp_path / "nowhere" / "cls.pt"))
    assert tax["source"] == "builtin" and len(tax["classes"]) > 50


# ── registry / processor ─────────────────────────────────────────────────────

SIGN_CLS_CARD = {**CLS_CARD, "name": "signs_cls", "classes": ["not_a_sign", "speed_limit_30", "speed_limit_50", "stop"],
                 "crop_pad_ratio": 0.1, "taxonomy_version": "viasafe-v1",
                 "thresholds": {"auto_accept": 0.85, "auto_reject": 0.4, "reject_class": "not_a_sign",
                                "per_class": {"stop": {"auto_accept": 0.9, "auto_reject": 0.5}}}}


def test_two_stage_detector_detection():
    assert model_registry.is_two_stage_sign_detector(["traffic_sign", "traffic_light"])
    assert model_registry.is_two_stage_sign_detector(["traffic_sign"])
    assert not model_registry.is_two_stage_sign_detector(["traffic_light", "speed_limit_30", "stop"])
    cards = model_registry.thresholds_from_cards({"signs_classifier": model_registry.parse_card(SIGN_CLS_CARD)})
    assert cards["traffic_sign"]["per_class"] == {"stop": {"auto_accept": 0.9, "auto_reject": 0.5}}
    assert cards["traffic_sign"]["reject_class"] == "not_a_sign"


@pytest.fixture
def processor():
    from app.workers import processor as proc
    proc._unload_models()
    yield proc
    proc._unload_models()


class _FakeSignClassifier:
    task = "classify"
    names = {0: "not_a_sign", 1: "speed_limit_30", 2: "speed_limit_50", 3: "stop"}

    def __init__(self, probs):
        self.probs, self.calls = probs, []

    def __call__(self, crop, imgsz=None, verbose=False):
        self.calls.append((crop.shape, imgsz))
        return [SimpleNamespace(probs=_FakeProbs(self.probs))]


def _ctx(processor, ai=None):
    p = processor._policy_for_job(ai or AiValidationSettings(), ValidationSettings())
    return SimpleNamespace(policy=p, ai=ai or AiValidationSettings())


def test_sign_stage_two_stage(processor):
    processor._models["signs_classifier"] = fake = _FakeSignClassifier([0.30, 0.05, 0.60, 0.05])
    processor._model_cards["signs_classifier"] = model_registry.parse_card(SIGN_CLS_CARD)
    ctx = _ctx(processor)
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    out = processor._sign_stage(ctx, "traffic_sign", frame, (100, 100, 200, 150))
    assert out["predicted_class"] == "speed_limit_50" and out["verifier_score"] == 0.6
    assert out["subtype"] == "speed_limit_50"
    assert [c["code"] for c in out["class_candidates"]] == ["speed_limit_50", "speed_limit_30", "stop"]
    assert fake.calls == [((60, 120, 3), 224)]          # crop_pad_ratio 0.1 of the box
    # not_a_sign wins → never stored as subtype
    fake.probs = [0.9, 0.05, 0.03, 0.02]
    out = processor._sign_stage(ctx, "traffic_sign", frame, (100, 100, 200, 150))
    assert out["subtype"] == "unknown_sign" and out["predicted_class"] == "not_a_sign"
    assert all(c["code"] != "not_a_sign" for c in out["class_candidates"])
    # unsure on stop (< its 0.5 auto_reject) → unknown_sign
    fake.probs = [0.2, 0.2, 0.15, 0.45]
    assert processor._sign_stage(ctx, "traffic_sign", frame, (100, 100, 200, 150))["subtype"] == "unknown_sign"
    # traffic lights never go through the classifier
    assert processor._sign_stage(ctx, "traffic_light", frame, (1, 1, 5, 5)) == {"subtype": "traffic_light"}


def test_sign_stage_without_classifier(processor):
    ctx = _ctx(processor)
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    out = processor._sign_stage(ctx, "traffic_sign", frame, (100, 100, 200, 150))
    assert out == {"subtype": "unknown_sign", "predicted_class": "unknown_sign"}


def test_legacy_single_stage_signs(processor, monkeypatch, tmp_path):
    """The old multi-class sign detector: no classifier loaded, subtype = detector class."""
    from app.core.config import settings
    stub = tmp_path / "model_signs_classifier.pt"
    stub.write_bytes(b"would-be classifier")
    monkeypatch.setattr(settings, "MODEL_SIGNS_CLASSIFIER", str(stub))
    processor._models["signs"] = SimpleNamespace(names={0: "traffic_light", 1: "speed_limit_30", 2: "stop"})
    processor._load_verifier("signs")
    assert "signs_classifier" not in processor._models and not processor._sign_two_stage["on"]
    ctx = _ctx(processor)
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    assert processor._sign_stage(ctx, "speed_limit_30", frame, (1, 1, 50, 50)) == {"subtype": "speed_limit_30"}
    assert processor._sign_stage(ctx, "traffic_light", frame, (1, 1, 50, 50)) == {"subtype": "traffic_light"}
    # a 2-class detector with an unloadable classifier → single stage too (stub weights)
    processor._models["signs"] = SimpleNamespace(names={0: "traffic_sign", 1: "traffic_light"})
    processor._load_verifier("signs")
    assert "signs_classifier" not in processor._models


# ── static consistency ───────────────────────────────────────────────────────

def _track(boxes, positions=None, label="traffic_sign"):
    tr = static_check.Tracker()
    t = None
    for i, box in enumerate(boxes):
        lat, lon = positions[i] if positions else (None, None)
        t = tr.observe(static_check.Sighting(i * 5 + 1, box, 0.8, lat, lon), label, 5, lambda: object())
    assert len(tr.open) == 1
    return t


def test_tracker_matching_and_expiry():
    tr = static_check.Tracker()
    a = tr.observe(static_check.Sighting(1, (100, 100, 140, 140), 0.5), "stop", 5, lambda: "a")
    b = tr.observe(static_check.Sighting(6, (102, 101, 146, 145), 0.9), "stop", 5, lambda: "b")
    c = tr.observe(static_check.Sighting(6, (400, 100, 440, 140), 0.7), "stop", 5, lambda: "c")
    d = tr.observe(static_check.Sighting(6, (102, 101, 146, 145), 0.7), "traffic_light", 5, lambda: "d")
    assert a is b and len(a.sightings) == 2 and a.best == "b"          # best-confidence payload
    assert c is not a and d is not a and len(tr.open) == 3
    assert tr.expire(11, 5) == [] and len(tr.expire(17, 5)) == 3       # > 2 sampled frames unseen
    assert tr.flush() == []


def test_seen_once_is_not_static():
    t = _track([(100, 100, 140, 140)])
    assert static_check.assess("traffic_sign", t, 480, min_frames=2, max_drift_m=8)[0][0] == "not_static"
    assert static_check.assess("traffic_sign", t, 480, min_frames=2, max_drift_m=8, video=False) == []
    t = _track([(100, 100, 140, 140), (98, 96, 144, 146)])
    assert static_check.assess("traffic_sign", t, 480, min_frames=2, max_drift_m=8) == []


def _north(lat0, metres):
    return lat0 + metres / 111_195.0


def test_object_moving_with_traffic_is_not_static():
    lat0, lon = 4.05, 9.7
    # vehicle drives 20 m north; a sticker on the truck ahead keeps the same box → moves with us
    pos = [(_north(lat0, 0), lon), (_north(lat0, 10), lon), (_north(lat0, 20), lon)]
    sticker = _track([(300, 200, 340, 240)] * 3, pos)
    blk = static_check.assess("traffic_sign", sticker, 480, min_frames=2, max_drift_m=8)
    assert blk and blk[0][0] == "not_static" and "drifted" in blk[0][1]
    # a real sign 35 m ahead grows as we approach (range 35 → 25 → 15 m): stays put
    def box(r):
        h = 0.75 * 1.2 / r * 480
        return (300, 200, 300 + h, 200 + h)
    sign = _track([box(35), box(25), box(15)], pos)
    assert static_check.assess("traffic_sign", sign, 480, min_frames=2, max_drift_m=8) == []
    # vehicle stopped (same fix) → drift rule not applicable
    still = _track([(300, 200, 340, 240)] * 3, [(lat0, lon)] * 3)
    assert static_check.assess("traffic_sign", still, 480, min_frames=2, max_drift_m=8) == []


def test_pothole_road_area_and_ground_range():
    up = _track([(300, 100, 340, 140), (300, 102, 342, 142)], label="pothole")
    blk = static_check.assess("pothole", up, 480, min_frames=2, max_drift_m=8, min_y_ratio=0.45)
    assert [c for c, _ in blk] == ["off_road_area"]
    low = _track([(300, 380, 340, 420), (300, 385, 342, 425)], label="pothole")
    assert static_check.assess("pothole", low, 480, min_frames=2, max_drift_m=8, min_y_ratio=0.45) == []
    assert static_check.estimate_range("pothole", (0, 0, 10, 480), 480) == pytest.approx(1.4 * 1.2 / 0.6)
    assert static_check.estimate_range("pothole", (0, 0, 10, 100), 480) is None      # above the horizon


def test_consistency_settings_defaults():
    s = AiValidationSettings()
    assert (s.min_frames_for_auto_accept, s.max_static_drift_m, s.pothole_min_y_ratio) == (2, 8.0, 0.45)
    with pytest.raises(ValidationError):
        AiValidationSettings(min_frames_for_auto_accept=0)
    with pytest.raises(ValidationError):
        AiValidationSettings(pothole_min_y_ratio=1.5)


# ── endpoints ────────────────────────────────────────────────────────────────

@pytest.fixture
def sign_models(model_dir, monkeypatch):
    from app.core.config import settings
    det = model_dir / "model_signs.pt"
    det.write_bytes(b"sign detector")
    det.with_suffix(".json").write_text(json.dumps({**DET_CARD, "name": "signs_det",
                                                    "classes": ["traffic_sign", "traffic_light"]}))
    cls = model_dir / "model_signs_classifier.pt"
    cls.write_bytes(b"sign classifier")
    cls.with_suffix(".json").write_text(json.dumps(SIGN_CLS_CARD))
    (model_dir / "taxonomy.yaml").write_text("version: viasafe-v1\nclasses:\n  - {code: stop, name_fr: Arrêt}\n")
    monkeypatch.setattr(settings, "MODEL_SIGNS", str(det))
    monkeypatch.setattr(settings, "MODEL_SIGNS_CLASSIFIER", str(cls))
    return model_dir


async def test_models_endpoint_signs(sign_models, client):
    body = (await client.get("/models/")).json()
    by_key = {m["key"]: m for m in body["models"]}
    assert by_key["signs"]["pipeline"] == "two_stage" and by_key["pothole"]["pipeline"] is None
    assert by_key["signs_classifier"]["card"]["thresholds"]["per_class"]["stop"]["auto_accept"] == 0.9
    assert by_key["signs_classifier"]["card"]["taxonomy_version"] == "viasafe-v1"
    th = body["thresholds"]["traffic_sign"]
    assert th["two_stage"] is True and th["reject_class"] == "not_a_sign"
    assert th["auto_accept"] == {"value": 0.85, "source": "card"}
    assert th["per_class"]["stop"]["auto_reject"] == {"value": 0.5, "source": "card"}


async def test_taxonomy_endpoint(sign_models, client):
    r = await client.get("/models/signs/taxonomy")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "file" and body["taxonomy_version"] == "viasafe-v1"
    assert [c["code"] for c in body["classes"]] == ["stop", "traffic_light", "unknown_sign"]
    assert body["classes"][0]["name_fr"] == "Arrêt"


async def test_legacy_signs_pipeline_reported(model_dir, client, monkeypatch):
    from app.core.config import settings
    det = model_dir / "model_signs.pt"
    det.write_bytes(b"legacy")
    det.with_suffix(".json").write_text(json.dumps({**DET_CARD, "classes": ["traffic_light", "stop", "speed_limit_30"]}))
    cls = model_dir / "model_signs_classifier.pt"
    cls.write_bytes(b"x")
    monkeypatch.setattr(settings, "MODEL_SIGNS", str(det))
    monkeypatch.setattr(settings, "MODEL_SIGNS_CLASSIFIER", str(cls))
    body = (await client.get("/models/")).json()
    assert {m["key"]: m for m in body["models"]}["signs"]["pipeline"] == "single_stage"
    assert body["thresholds"]["traffic_sign"]["two_stage"] is False
