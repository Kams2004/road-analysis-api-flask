"""Pothole false-positive rules (static_check.pothole_rule_check) and their
settings / decision in the modes (ai_validation.rule_reject)."""
import pytest
from pydantic import ValidationError

from app.services import ai_validation as av
from app.services import static_check as sc
from app.services.settings_service import AiValidationSettings, ValidationSettings

W, H = 720, 576
DEFAULTS = sc.pothole_rule_kwargs(AiValidationSettings())


def check(box, **over):
    return sc.pothole_rule_check(box, W, H, **{**DEFAULTS, **over})


def codes(box, **over):
    return [c for c, _ in check(box, **over)[0]]


def test_settings_defaults_and_validation():
    s = AiValidationSettings()
    assert (s.pothole_off_road_action, s.pothole_max_width_ratio, s.pothole_max_area_ratio,
            s.pothole_reject_edge_touching) == ("reject", 0.40, 0.12, True)
    assert DEFAULTS == {"min_y_ratio": 0.45, "off_road_action": "reject", "max_width_ratio": 0.40,
                        "max_area_ratio": 0.12, "reject_edge_touching": True}
    for bad in ({"pothole_off_road_action": "drop"}, {"pothole_max_width_ratio": 0},
                {"pothole_max_area_ratio": 1.5}):
        with pytest.raises(ValidationError):
            AiValidationSettings(**bad)


def test_small_road_pothole_passes_every_rule():
    rejects, reasons = check((300, 400, 380, 440))          # 11 % wide, centre at 0.73
    assert rejects == []
    assert [r["code"] for r in reasons] == ["pothole_road_area", "pothole_box_width", "pothole_box_area",
                                            "pothole_side_edge"]
    assert all(r["passed"] for r in reasons)


def test_off_road_box_rejected_or_left_to_review():
    tree = (300, 50, 360, 150)                               # centre at 0.17 of the height
    rejects, _ = check(tree)
    assert [c for c, _ in rejects] == [sc.AI_OFF_ROAD]
    assert "above the road-area line 0.45" in rejects[0][1]
    # action "review": no rejection here, assess() still blocks the auto-accept
    assert check(tree, off_road_action="review")[0] == []
    t = sc.Track(label="pothole", sightings=[sc.Sighting(1, tree, 0.5), sc.Sighting(2, tree, 0.6)])
    assert [c for c, _ in sc.assess("pothole", t, H, min_frames=2, max_drift_m=8, min_y_ratio=0.45)] == \
        [sc.OFF_ROAD_AREA]


def test_box_too_wide_or_too_large():
    assert codes((100, 420, 450, 470)) == [sc.AI_BOX_TOO_LARGE]            # 49 % wide, area 4 %
    assert codes((200, 250, 480, 550)) == [sc.AI_BOX_TOO_LARGE]            # 39 % wide but area 20 %
    _, reasons = check((200, 250, 480, 550))
    by = {r["code"]: r for r in reasons}
    assert by["pothole_box_width"]["passed"] and not by["pothole_box_area"]["passed"]
    # ratio 1 = rule off
    assert codes((100, 420, 450, 470), max_width_ratio=1.0) == []


def test_wide_box_touching_the_side_is_roadside():
    box = (0, 380, 200, 470)                                 # 28 % wide, area 4 %, touches the left side
    assert codes(box) == [sc.AI_BOX_TOO_LARGE]
    assert "touches the image side" in check(box)[0][0][1]
    assert codes(box, reject_edge_touching=False) == []
    assert codes((0, 400, 150, 470)) == []                   # touches the side but only 21 % wide
    assert codes((W - 200, 380, W - 3, 470)) == [sc.AI_BOX_TOO_LARGE]   # within 1 % of the right side


def test_both_rules_and_degenerate_frame():
    rejects, _ = check((0, 0, 400, 300))                     # above the line AND huge
    assert [c for c, _ in rejects] == [sc.AI_OFF_ROAD, sc.AI_BOX_TOO_LARGE]
    assert sc.pothole_rule_check((0, 0, 10, 10), 0, 0, **DEFAULTS) == ([], [])
    assert sc.pothole_rule_rejects((300, 400, 380, 440), W, H, **DEFAULTS) == []


def test_box_ratios():
    r = sc.box_ratios((0, 288, 360, 576), W, H)
    assert r == {"width_ratio": 0.5, "area_ratio": 0.25, "centre_y_ratio": 0.75, "touches_side": True}


@pytest.mark.parametrize("mode,status,ai_decision,code", [
    ("assisted", "rejected", "auto_reject", "ai_box_too_large"),
    ("automatic", "rejected", "auto_reject", "ai_box_too_large"),
    ("manual", "pending", "review", None),
])
def test_rule_reject_only_automatic_outside_manual_mode(mode, status, ai_decision, code):
    policy = av.build_policy(AiValidationSettings(mode=mode, audit_sample_rate=0.0), ValidationSettings(), {})
    d = av.rule_reject(policy, "ai_box_too_large", "ai_box_too_large: width 49% > 40%", "confirmed", 0.5)
    assert (d.status, d.ai_decision, d.rejection_code) == (status, ai_decision, code)
    if mode == "manual":
        assert d.reason.startswith("manual mode — AI suggests reject (ai_box_too_large:")


def test_rule_reject_audit_sample_stays_pending():
    policy = av.build_policy(AiValidationSettings(audit_sample_rate=0.5), ValidationSettings(), {})
    d = av.rule_reject(policy, "ai_off_road", "ai_off_road: …", None, 0.1)
    assert (d.status, d.ai_decision, d.audit_sample) == ("location_pending", "auto_reject", True)
