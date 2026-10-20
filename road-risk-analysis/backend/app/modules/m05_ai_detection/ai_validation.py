"""
AI validation mode (RRAP-05-010/011/012/016, RRAP-06-007, RRAP-13-001/016).

Single source of the automatic decision taken when the processor creates a
detection. Inputs: the detector confidence, the crop classifier's score
P(class) (`verifier_score`, two-stage pipeline — None when no classifier is
installed for the type) and the location status. Settings section
`ai_validation` picks the mode:

  manual     no automatic decision at all: every detection waits for a human
             (pending / location_pending). The AI suggestion is still stored
             in `ai_decision_reason`.
  assisted   (default) auto-accept only when detector conf ≥ detector_accept
             AND — when `require_classifier_agreement` and a classifier is
             installed — verifier ≥ auto_accept AND the location is confirmed;
             auto-reject when verifier ≤ auto_reject or detector conf <
             detector_reject; a detector/classifier disagreement and
             everything else → human review.
  automatic  same thresholds without the detector "review band": whenever the
             classifier is confident either way it decides (accept still needs
             a confirmed location); only an uncertain classifier — or, without
             a classifier, an uncertain detector — goes to review.

Audit sampling: in assisted/automatic mode a random `audit_sample_rate`
share of the automatic decisions is NOT applied: the detection stays pending
for a human with `audit_sample = true` and the AI decision recorded as a
suggestion — the human verdict on those samples measures the AI's precision
(GET /models/stats).

Thresholds per detection type, first defined wins:
  1. `ai_validation.type_overrides[type]`
  2. the model cards (detector card → detector_accept/reject on confidence,
     classifier card → auto_accept/reject on P(class))
  3. section `validation` (auto_accept_above / auto_reject_below, global or
     per type) — detector thresholds only; this keeps the pre-AI-mode
     behaviour when no model card is installed.

Traffic signs (two-stage: detector traffic_sign/traffic_light + a crop
classifier of VIASafe sign codes, see app.modules.m05_ai_detection.sign_taxonomy): decide()
gets `predicted_class` = the classifier's top code and `verifier_score` =
P(that code). Thresholds per code (`per_class`: override, else the classifier
card's per_class, else the type's auto_accept / auto_reject):
  - code is the card's `reject_class` (`not_a_sign`) or another non-sign
    class with P ≥ auto_accept(code)  → auto-reject, code `ai_not_a_sign`
                                        (sticker, billboard, person, tree …);
  - code `unknown_sign`, or P < auto_reject(code)
                                      → subtype `unknown_sign`, review
                                        (sign_subtype() gives the stored subtype;
                                        a non-sign class is never stored);
  - P ≥ auto_accept(code)             → the classifier agrees (as P(pothole) ≥
                                        auto_accept for potholes);
  - otherwise                         → review (code kept).
The detector rules (detector_reject / detector_accept), the modes, the
location rule and audit sampling are exactly the pothole ones. Traffic
lights (no classifier) and the legacy single-stage sign model call decide()
without a classifier score: detector thresholds only.

Accept blockers (video consistency, computed by the processor — see
app.modules.m05_ai_detection.static_check): `not_static` (object seen in fewer than
`min_frames_for_auto_accept` sampled frames, or its estimated position
drifted more than `max_static_drift_m` while the vehicle moved) and
`off_road_area` (pothole box centre above `pothole_min_y_ratio` of the frame
height). A blocker turns a would-be auto-accept into `review` (reason
starts with the blocker code); rejections are not affected.

Pothole false-positive rules (static_check.pothole_rule_check, settings
ai_validation.pothole_*): a box above the road-area line (with
`pothole_off_road_action` = reject) or too large / a wide box touching the
image side is rejected through rule_reject() — codes `ai_off_road`,
`ai_box_too_large`; manual mode turns them into review hints.

Parked vehicles (app.modules.m05_ai_detection.parked_vehicle): verifier_score is
the rule score (0..1), its auto_accept / auto_reject come from settings
section `parked_vehicle` (auto_accept_score / auto_reject_score) unless
`type_overrides.parked_vehicle` sets them; the image-only fallback adds the
accept blocker `fallback_heuristics`. Verdicts stored for review are
rejected through rule_reject() (rejection codes ai_off_road,
ai_not_grounded, ai_traffic_queue, ai_moving_vehicle).

Pure functions, unit-tested in tests/test_ai_validation.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from app.core.settings_service import AiValidationSettings, ValidationSettings
from app.modules.m05_ai_detection.sign_taxonomy import UNKNOWN_SIGN, is_negative
from app.modules.m05_ai_detection.static_check import NOT_STATIC, OFF_ROAD_AREA  # noqa: F401  (accept-blocker codes)

ACCEPT, REJECT, REVIEW = "auto_accept", "auto_reject", "review"
LOW_CONFIDENCE_CODE = "low_confidence"      # detector confidence below detector_reject
AI_VERIFIER_CODE = "ai_verifier"            # crop classifier says "not this class"
NOT_A_SIGN_CODE = "ai_not_a_sign"           # sign classifier's reject class (not_a_sign) wins

FIELDS = ("detector_accept", "detector_reject", "auto_accept", "auto_reject")
CLASS_FIELDS = ("auto_accept", "auto_reject")


def validation_thresholds(rules: ValidationSettings, det_type: str) -> tuple[float, Optional[float]]:
    """(auto_reject_below, auto_accept_above) of section `validation` for a
    type; per-type overrides win over the global values, None = inherit."""
    reject_below, accept_above = rules.auto_reject_below, rules.auto_accept_above
    override = rules.type_overrides.get(det_type)
    if override is not None:
        if override.auto_reject_below is not None:
            reject_below = override.auto_reject_below
        if override.auto_accept_above is not None:
            accept_above = override.auto_accept_above
    return reject_below, accept_above


@dataclass(frozen=True)
class Thresholds:
    detector_accept: Optional[float] = None   # detector confidence ≥ → may accept (None = never)
    detector_reject: Optional[float] = None   # detector confidence < → reject
    auto_accept:     Optional[float] = None   # verifier P(class) ≥ → accept
    auto_reject:     Optional[float] = None   # verifier P(class) ≤ → reject
    sources: dict = field(default_factory=dict, compare=False)   # field → override | card | validation
    # multi-class classifier (traffic signs): {code: {auto_accept, auto_reject}}, None = inherit the type value
    per_class: dict = field(default_factory=dict, compare=False)
    per_class_sources: dict = field(default_factory=dict, compare=False)   # {code: {field: override | card}}
    reject_class: Optional[str] = field(default=None, compare=False)       # classifier "not this type" class

    def for_class(self, code: Optional[str]) -> tuple[Optional[float], Optional[float]]:
        """(auto_accept, auto_reject) for a predicted class: per-class value, else the type value."""
        pc = self.per_class.get(code or "", {})
        acc, rej = pc.get("auto_accept"), pc.get("auto_reject")
        return (self.auto_accept if acc is None else acc, self.auto_reject if rej is None else rej)

    def effective_per_class(self) -> dict[str, dict[str, dict]]:
        """{code: {field: {value, source}}} — inherited values have source `global`."""
        out = {}
        for code in sorted(self.per_class):
            acc, rej = self.for_class(code)
            src = self.per_class_sources.get(code, {})
            out[code] = {"auto_accept": {"value": acc, "source": src.get("auto_accept") or
                                         ("global" if acc is not None else None)},
                         "auto_reject": {"value": rej, "source": src.get("auto_reject") or
                                         ("global" if rej is not None else None)}}
        return out


def resolve_thresholds(ai: AiValidationSettings, rules: ValidationSettings,
                       cards: dict[str, dict], det_type: str) -> Thresholds:
    """Effective thresholds of a type (see module docstring for the order)."""
    reject_below, accept_above = validation_thresholds(rules, det_type)
    fallback = {"detector_accept": accept_above, "detector_reject": reject_below}
    override = ai.type_overrides.get(det_type)
    card = cards.get(det_type, {})
    values, sources = {}, {}
    for f in FIELDS:
        ov = getattr(override, f) if override is not None else None
        if ov is not None:
            values[f], sources[f] = ov, "override"
        elif card.get(f) is not None:
            values[f], sources[f] = card[f], "card"
        elif fallback.get(f) is not None:
            values[f], sources[f] = fallback[f], "validation"
        else:
            values[f], sources[f] = None, None
    # per-class (traffic signs): override per_class, else card per_class; unset → inherit the type value
    ov_pc = override.per_class if override is not None else {}
    card_pc = card.get("per_class") or {}
    per_class, per_sources = {}, {}
    for code in dict.fromkeys([*card_pc, *ov_pc]):
        vals, srcs = {}, {}
        for f in CLASS_FIELDS:
            ov = getattr(ov_pc[code], f) if code in ov_pc else None
            cv = (card_pc.get(code) or {}).get(f)
            if ov is not None:
                vals[f], srcs[f] = ov, "override"
            elif cv is not None:
                vals[f], srcs[f] = cv, "card"
        if vals:
            per_class[code], per_sources[code] = vals, srcs
    return Thresholds(**values, sources=sources, per_class=per_class, per_class_sources=per_sources,
                      reject_class=card.get("reject_class"))


@dataclass
class Policy:
    """Everything decide() needs, resolved once per job."""
    mode: str = "assisted"
    audit_sample_rate: float = 0.0
    require_classifier_agreement: bool = True
    thresholds: dict[str, Thresholds] = field(default_factory=dict)
    default: Thresholds = field(default_factory=Thresholds)

    def for_type(self, det_type: str) -> Thresholds:
        return self.thresholds.get(det_type, self.default)


def build_policy(ai: AiValidationSettings, rules: ValidationSettings,
                 cards: dict[str, dict], types=()) -> Policy:
    all_types = set(types) | set(cards) | set(ai.type_overrides) | set(rules.type_overrides)
    reject_below, accept_above = rules.auto_reject_below, rules.auto_accept_above
    return Policy(
        mode=ai.mode, audit_sample_rate=ai.audit_sample_rate,
        require_classifier_agreement=ai.require_classifier_agreement,
        thresholds={t: resolve_thresholds(ai, rules, cards, t) for t in all_types},
        default=Thresholds(detector_accept=accept_above, detector_reject=reject_below,
                           sources={"detector_accept": "validation" if accept_above is not None else None,
                                    "detector_reject": "validation"}),
    )


def legacy_policy(rules: ValidationSettings) -> Policy:
    """Section `validation` alone, no audit sampling — the single-stage rule
    of RRAP-06-007 (detection_workflow.initial_status)."""
    return build_policy(AiValidationSettings(mode="assisted", audit_sample_rate=0.0), rules, {})


@dataclass(frozen=True)
class Decision:
    status: str                     # review_status at creation
    action: str                     # history action: create | auto_accept | auto_reject
    rejection_code: Optional[str]
    ai_decision: str                # auto_accept | auto_reject | review
    reason: str                     # short human-readable explanation
    audit_sample: bool = False      # automatic decision left pending for a human audit


def _f(x: float) -> str:
    return f"{x:.2f}"


def _route(mode: str, th: Thresholds, require_agreement: bool, conf: float, located: bool,
           verifier: bool, v_accept: bool, v_reject: bool, cls: str, accept_at: Optional[float],
           reject_why: str, reject_code: str = AI_VERIFIER_CODE) -> tuple[str, str, Optional[str]]:
    """Mode rules once the classifier verdict is known (shared by potholes and signs)."""
    det_high = th.detector_accept is not None and conf >= th.detector_accept

    if mode == "automatic" and verifier:
        if v_reject:
            return REJECT, f"{cls} {reject_why}", reject_code
        if v_accept:
            if not located:
                return REVIEW, f"{cls} ≥ {_f(accept_at)} but location not confirmed", None  # type: ignore[arg-type]
            return ACCEPT, f"{cls} ≥ {_f(accept_at)}", None  # type: ignore[arg-type]
        return REVIEW, f"{cls} uncertain", None

    # assisted (or automatic without a usable classifier: detector thresholds)
    if v_reject:
        if det_high:
            return REVIEW, f"detector {_f(conf)} and {cls} disagree", None
        return REJECT, f"{cls} {reject_why}", reject_code
    if not det_high:
        if th.detector_accept is None:
            return REVIEW, "auto-accept disabled", None
        return REVIEW, f"detector {_f(conf)} uncertain", None
    need_verifier = require_agreement and verifier and accept_at is not None
    if need_verifier and not v_accept:
        return REVIEW, f"detector {_f(conf)} ok, {cls} < {_f(accept_at)}", None  # type: ignore[arg-type]
    if not located:
        return REVIEW, f"detector {_f(conf)} ≥ {_f(th.detector_accept)} but location not confirmed", None  # type: ignore[arg-type]
    why = f"detector {_f(conf)} ≥ {_f(th.detector_accept)}"  # type: ignore[arg-type]
    if need_verifier:
        why += f", {cls} ≥ {_f(accept_at)}"  # type: ignore[arg-type]
    return ACCEPT, why, None


def _propose(mode: str, th: Thresholds, require_agreement: bool, conf: float,
             score: Optional[float], located: bool) -> tuple[str, str, Optional[str]]:
    """(proposal, reason, rejection_code) before mode / audit handling —
    binary verifier: score = P(this type)."""
    if th.detector_reject is not None and conf < th.detector_reject:
        return REJECT, f"detector {_f(conf)} < {_f(th.detector_reject)}", LOW_CONFIDENCE_CODE

    verifier = score is not None and (th.auto_accept is not None or th.auto_reject is not None)
    v_accept = verifier and th.auto_accept is not None and score >= th.auto_accept  # type: ignore[operator]
    v_reject = verifier and th.auto_reject is not None and score <= th.auto_reject  # type: ignore[operator]
    cls = f"classifier {_f(score)}" if score is not None else ""
    reject_why = f"≤ {_f(th.auto_reject)}" if th.auto_reject is not None else ""
    return _route(mode, th, require_agreement, conf, located, verifier, v_accept, v_reject, cls,
                  th.auto_accept, reject_why)


def sign_subtype(th: Thresholds, code: str, score: Optional[float]) -> str:
    """Subtype stored for a classified sign crop: the predicted code, or
    `unknown_sign` when it is a non-sign class (never stored as a subtype) or
    P(code) < auto_reject(code)."""
    if is_negative(code, th.reject_class):
        return UNKNOWN_SIGN
    _acc, rej = th.for_class(code)
    if score is not None and rej is not None and score < rej:
        return UNKNOWN_SIGN
    return code


def _propose_sign(mode: str, th: Thresholds, require_agreement: bool, conf: float, code: str,
                  score: Optional[float], located: bool) -> tuple[str, str, Optional[str]]:
    """Multi-class verifier: score = P(code) (see module docstring)."""
    if th.detector_reject is not None and conf < th.detector_reject:
        return REJECT, f"detector {_f(conf)} < {_f(th.detector_reject)}", LOW_CONFIDENCE_CODE
    if score is None:
        return REVIEW, "sign class unknown (no classifier score)", None
    acc, rej = th.for_class(code)
    cls = f"classifier {code} {_f(score)}"
    if is_negative(code, th.reject_class):
        if acc is not None and score >= acc:
            return _route(mode, th, require_agreement, conf, located, True, False, True, cls, acc,
                          f"≥ {_f(acc)} (not a sign)", NOT_A_SIGN_CODE)
        return REVIEW, f"{cls} uncertain", None
    if code == UNKNOWN_SIGN:
        return REVIEW, f"{cls}: sign not identified", None
    if rej is not None and score < rej:
        return REVIEW, f"{cls} < {_f(rej)} → {UNKNOWN_SIGN}", None
    v_accept = acc is not None and score >= acc
    return _route(mode, th, require_agreement, conf, located, acc is not None, v_accept, False, cls, acc, "")


def decide(policy: Policy, det_type: str, confidence: float, verifier_score: Optional[float],
           location_status: Optional[str], draw: Optional[float] = None,
           predicted_class: Optional[str] = None,
           accept_blockers: Sequence[tuple[str, str]] = ()) -> Decision:
    """
    Automatic decision for a new detection. `draw` is a uniform [0, 1) random
    number: the decision becomes an audit sample when draw < audit_sample_rate
    (pass a seeded random.Random().random() for reproducible sampling, or
    None to disable sampling). `predicted_class` (multi-class classifier —
    traffic signs) switches to the per-class rules; `verifier_score` is then
    P(predicted_class). `accept_blockers` [(code, detail)] veto an
    auto-accept: it becomes `review` with reason "<code>: <detail> …".
    """
    located = location_status == "confirmed"
    waiting = "pending" if located else "location_pending"
    th = policy.for_type(det_type)
    if predicted_class is not None:
        proposal, reason, code = _propose_sign(policy.mode, th, policy.require_classifier_agreement,
                                               confidence, predicted_class, verifier_score, located)
    else:
        proposal, reason, code = _propose(policy.mode, th, policy.require_classifier_agreement,
                                          confidence, verifier_score, located)
    if proposal == ACCEPT and accept_blockers:
        code_b, detail = accept_blockers[0]
        proposal, reason, code = REVIEW, f"{code_b}: {detail} (AI would accept: {reason})", None
    if policy.mode == "manual":
        suggestion = {ACCEPT: "accept", REJECT: "reject", REVIEW: "review"}[proposal]
        return Decision(waiting, "create", None, REVIEW, f"manual mode — AI suggests {suggestion} ({reason})")
    if proposal == REVIEW:
        return Decision(waiting, "create", None, REVIEW, reason)
    if draw is not None and draw < policy.audit_sample_rate:
        return Decision(waiting, "create", None, proposal, reason, audit_sample=True)
    if proposal == ACCEPT:
        return Decision("validated", ACCEPT, None, ACCEPT, reason)
    return Decision("rejected", REJECT, code, REJECT, reason)


def rule_reject(policy: Policy, code: str, reason: str, location_status: Optional[str],
                draw: Optional[float] = None) -> Decision:
    """Automatic rejection decided by a rule set rather than by thresholds
    (parked vehicles stored for review: `ai_off_road`, `ai_not_grounded`,
    `ai_traffic_queue`, `ai_moving_vehicle`; potholes: `ai_off_road`,
    `ai_box_too_large`). The mode and audit sampling
    apply as in decide(): manual → pending with the suggestion; an audit
    sample stays pending with the rejection as a suggestion."""
    waiting = "pending" if location_status == "confirmed" else "location_pending"
    if policy.mode == "manual":
        return Decision(waiting, "create", None, REVIEW, f"manual mode — AI suggests reject ({reason})")
    if draw is not None and draw < policy.audit_sample_rate:
        return Decision(waiting, "create", None, REJECT, reason, audit_sample=True)
    return Decision("rejected", REJECT, code, REJECT, reason)
