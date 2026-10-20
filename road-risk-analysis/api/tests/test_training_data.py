"""Live training dataset — pure layout / label rules (app.services.training_data)."""
from types import SimpleNamespace

import pytest

from app.services.training_data import layout as L
from app.services.training_data.export import clean_review_boxes


# ── keys and groups ──────────────────────────────────────────────────────────

def test_frame_key_is_a_split_safe_stem():
    k = L.frame_key("LTTR 458 AV_24_04 (2).mp4", "1a2b3c4d-5e6f-7a8b-9c0d-112233445566", 151.2, 3780)
    assert k == "LTTR_458_AV_24_04_2-1a2b3c4d__z002__t00151200"
    assert L.group_key(k) == "LTTR_458_AV_24_04_2-1a2b3c4d__z002"
    # same minute of the same job → same group; next minute → another one
    assert L.group_key(L.frame_key("LTTR 458 AV_24_04 (2).mp4", "1a2b3c4d-x", 179.9, 1)) == L.group_key(k)
    assert L.group_key(L.frame_key("LTTR 458 AV_24_04 (2).mp4", "1a2b3c4d-x", 180.0, 1)) != L.group_key(k)


def test_frame_key_matches_the_kit_stem_regex():
    import re
    k = L.frame_key("a__b c.mp4", "ffffffff-0000", 5.0, 1)
    assert "__" not in k.split("__z")[0]
    assert re.match(r"^(.+?__[zn]\d+)__t\d+$", k).group(1) == L.group_key(k)


def test_frame_key_without_video_time_uses_the_frame_number():
    assert L.frame_key("photo.jpg", "abcdef12-0", None, 7) == "photo-abcdef12__z000__t00000007"


# ── classes ──────────────────────────────────────────────────────────────────

def test_detector_classes_from_card_or_defaults():
    assert L.detector_classes("pothole") == ["pothole"]
    assert L.detector_classes("traffic_sign") == ["traffic_sign", "traffic_light"]
    assert L.detector_classes("pothole", ["pothole", "crack"]) == ["pothole", "crack"]
    assert L.detector_classes("unknown_type") == ["unknown_type"]


@pytest.mark.parametrize("det_type,subtype,details,classes,expected", [
    ("pothole", None, None, ["pothole"], ("pothole", 0)),
    ("speed_bump", None, None, ["speed_bump"], ("speed_bump", 0)),
    ("traffic_sign", "speed_limit_50", None, ["traffic_sign", "traffic_light"], ("traffic_sign", 0)),
    ("traffic_sign", "traffic_light", None, ["traffic_sign", "traffic_light"], ("traffic_light", 1)),
    ("traffic_sign", "trafficlight", None, ["traffic_sign", "traffic_light"], ("traffic_light", 1)),
    ("traffic_sign", "stop", None, ["yield", "stop"], ("stop", 1)),             # legacy multi-class detector
    ("traffic_sign", "unknown_sign", None, ["yield", "stop"], (None, None)),
    ("parked_vehicle", "roadside", {"vehicle_class": "truck"}, ["car", "motorcycle", "bus", "truck"], ("truck", 3)),
    ("parked_vehicle", "roadside", {}, ["car", "bus"], (None, None)),            # class unknown
])
def test_detector_class_mapping(det_type, subtype, details, classes, expected):
    assert L.detector_class(det_type, subtype, details, classes) == expected


def test_classifier_class_folders():
    assert L.classifier_class("pothole", None, "accepted") == "pothole"
    assert L.classifier_class("pothole", None, "rejected") == "not_pothole"
    assert L.classifier_class("speed_bump", None, "rejected") == "not_speed_bump"
    assert L.classifier_class("traffic_sign", "speed_limit_50", "corrected") == "speed_limit_50"
    assert L.classifier_class("traffic_sign", "stop", "rejected") == "not_a_sign"
    assert L.classifier_class("pothole", None, "rejected", "background") == "background"   # card reject class


# ── boxes ────────────────────────────────────────────────────────────────────

def test_corrected_box_overrides_the_predicted_one():
    assert L.effective_box((10, 20, 110, 70), None) == (10.0, 20.0, 110.0, 70.0)
    assert L.effective_box((10, 20, 110, 70), [300, 260, 200, 200]) == (200.0, 200.0, 300.0, 260.0)
    assert L.effective_box((None, None, None, None), None) is None


def test_yolo_normalisation_and_clipping():
    cx, cy, w, h = L.yolo_box(100, 200, 300, 400, 720, 576)
    assert (round(cx, 6), round(cy, 6), round(w, 6), round(h, 6)) == (0.277778, 0.520833, 0.277778, 0.347222)
    assert L.yolo_box(-50, 500, 100, 700, 720, 576) == pytest.approx((50 / 720, 538 / 576, 100 / 720, 76 / 576))
    assert L.yolo_box(800, 10, 900, 20, 720, 576) is None          # outside the frame
    assert L.yolo_box(1, 1, 2, 2, 0, 0) is None                    # unknown size
    assert L.yolo_line(1, (0.5, 0.25, 0.1, 0.2)) == "1 0.500000 0.250000 0.100000 0.200000"


def test_crop_padding_matches_the_registry():
    from app.services import model_registry
    card = SimpleNamespace(crop_pad_ratio=0.15, crop_pad_px=None)
    assert L.crop_box(100, 200, 200, 260, 720, 576, pad_ratio=0.15) == \
        model_registry.crop_box(100, 200, 200, 260, 720, 576, card)
    assert L.crop_box(100, 200, 200, 260, 720, 576) == (80, 180, 220, 280)          # default 20 px
    assert L.crop_box(5, 5, 700, 570, 720, 576, pad_px=30) == (0, 0, 720, 576)     # clipped


def _s(i, decision="accepted", state="active", box=(100, 300, 200, 360), idx=0):
    return L.SampleView(i, state, decision, idx if decision != "rejected" else None,
                        "pothole" if decision != "rejected" else None, box)


def test_multi_detection_frame_has_every_accepted_box():
    boxes = L.frame_boxes([_s("b"), _s("a", "corrected", box=(400, 400, 500, 450)), _s("c", "rejected")], 720, 576)
    assert [b["detection_id"] for b in boxes] == ["a", "b"]
    assert L.label_text(boxes).count("\n") == 2


def test_rejected_only_frame_is_background():
    samples = [_s("a", "rejected"), _s("b", "rejected")]
    assert L.frame_boxes(samples, 720, 576) == []
    assert L.label_text([]) == ""
    assert L.frame_status(samples, has_image=True) == "active"


def test_reopen_supersedes_the_sample_and_empties_the_frame():
    samples = [_s("a", state="superseded")]
    assert L.frame_boxes(samples, 720, 576) == []
    assert L.frame_status(samples, has_image=True) == "empty"
    assert L.frame_status([_s("a"), _s("b", state="superseded")], has_image=True) == "active"
    assert L.frame_status([_s("a")], has_image=False) == "no_image"


def test_reviewed_boxes_win_over_the_decisions():
    reviewed = [{"class_index": 0, "class_name": "pothole", "cx": 0.5, "cy": 0.8, "w": 0.2, "h": 0.1}]
    assert L.frame_boxes([_s("a")], 720, 576, reviewed) == reviewed
    assert L.frame_boxes([_s("a")], 720, 576, []) == []           # reviewed background


@pytest.mark.parametrize("status,previous,expected", [
    ("validated", None, "accepted"), ("corrected", None, "corrected"), ("rejected", "accepted", "rejected"),
    ("resolved", "corrected", "corrected"), ("resolved", None, "accepted"),
    ("pending", "accepted", None), ("location_pending", "rejected", None),
])
def test_decision_for_status(status, previous, expected):
    assert L.decision_for(status, previous) == expected


def test_corrections_of():
    det = SimpleNamespace(corrected_bbox=[1, 2, 3, 4], predicted_type="pothole", predicted_subtype=None,
                          type="speed_bump", subtype=None, location_source="map_pin")
    assert L.corrections_of(det, ["map_pin"]) == ["box", "class", "location"]
    det = SimpleNamespace(corrected_bbox=None, predicted_type="pothole", predicted_subtype=None,
                          type="pothole", subtype=None, location_source="ocr")
    assert L.corrections_of(det, ["map_pin"]) == []


def test_object_keys_and_names():
    assert L.image_key("pothole", "k") == "pothole/detector/images/k.jpg"
    assert L.label_key("pothole", "k") == "pothole/detector/labels/k.txt"
    assert L.crop_key("traffic_sign", "speed limit/50", "id1") == "traffic_sign/classifier/speed_limit_50/id1.jpg"
    assert L.object_name("minio:9000/detections/j/frames/f1.jpg", "detections") == "j/frames/f1.jpg"
    assert L.object_name("j/frames/f1.jpg", "detections") == "j/frames/f1.jpg"
    assert L.object_name(None, "detections") is None


def test_review_boxes_validation():
    out = clean_review_boxes([{"class_name": "pothole", "cx": 0.95, "cy": 0.5, "w": 0.2, "h": 0.1}], ["pothole"])
    assert out[0]["class_index"] == 0 and out[0]["w"] == pytest.approx(0.15) and out[0]["cx"] == pytest.approx(0.925)
    with pytest.raises(ValueError):
        clean_review_boxes([{"class_name": "crack", "cx": 0.5, "cy": 0.5, "w": 0.1, "h": 0.1}], ["pothole"])
    with pytest.raises(ValueError):
        clean_review_boxes([{"class_index": 3, "cx": 0.5, "cy": 0.5, "w": 0.1, "h": 0.1}], ["pothole"])
