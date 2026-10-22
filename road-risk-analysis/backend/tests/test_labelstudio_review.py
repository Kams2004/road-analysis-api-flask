"""Box review in the server's Label Studio (docs/TRAINING-DATA.md, "Box review in Label Studio")."""
from types import SimpleNamespace

import pytest

from app.modules.m06_hazards.training_data import export as td_export
from app.modules.m06_hazards.training_data import labelstudio as ls


def test_image_signature_is_bound_to_type_and_frame():
    sig = ls.image_signature("pothole", "clip-abcd1234__z001__t00061000")
    assert len(sig) == 32
    assert ls.check_signature("pothole", "clip-abcd1234__z001__t00061000", sig)
    assert not ls.check_signature("traffic_sign", "clip-abcd1234__z001__t00061000", sig)
    assert not ls.check_signature("pothole", "clip-abcd1234__z001__t00061001", sig)
    assert not ls.check_signature("pothole", "clip-abcd1234__z001__t00061000", "")


def test_image_url_points_at_the_public_api():
    url = ls.image_url("http://1.2.3.4:18080", "pothole", "k")
    assert url.startswith("http://1.2.3.4:18080/labelstudio/image/pothole/k.jpg?s=")


def test_yolo_ls_round_trip():
    b = {"cx": 0.5, "cy": 0.6, "w": 0.2, "h": 0.1}
    v = ls.yolo_to_ls(b)
    assert v == {"x": 40.0, "y": 55.0, "width": 20.0, "height": 10.0, "rotation": 0}
    back = ls.ls_result_boxes([{"type": "rectanglelabels", "value": {**v, "rectanglelabels": ["pothole"]}}])
    assert back[0]["class_name"] == "pothole"
    assert back[0]["cx"] == pytest.approx(0.5) and back[0]["cy"] == pytest.approx(0.6)
    assert back[0]["w"] == pytest.approx(0.2) and back[0]["h"] == pytest.approx(0.1)


def test_result_ignores_non_boxes_and_empty_annotation_is_background():
    res = [{"type": "choices", "value": {"choices": ["degraded"]}},
           {"type": "rectanglelabels", "value": {"x": 1, "y": 1, "width": 1, "height": 1, "rectanglelabels": []}}]
    assert ls.ls_result_boxes(res) == []
    assert td_export.clean_review_boxes(ls.ls_result_boxes([]), ["pothole"]) == []


def test_latest_annotation_skips_cancelled():
    task = {"annotations": [
        {"id": 1, "updated_at": "2026-10-22T10:00:00Z", "was_cancelled": False},
        {"id": 2, "updated_at": "2026-10-22T11:00:00Z", "was_cancelled": True},
        {"id": 3, "updated_at": "2026-10-22T09:00:00Z", "was_cancelled": False}]}
    assert ls.latest_annotation(task)["id"] == 1
    assert ls.annotation_key(ls.latest_annotation(task)) == "1:2026-10-22T10:00:00Z"
    assert ls.latest_annotation({"annotations": [{"id": 4, "was_cancelled": True}]}) is None


def test_label_config_has_every_class():
    xml = ls.label_config(["traffic_sign", "traffic_light"])
    assert '<Label value="traffic_sign"' in xml and '<Label value="traffic_light"' in xml
    assert 'value="$image"' in xml and 'value="$info"' in xml


def test_task_carries_frame_key_and_current_boxes_as_prediction():
    cfg = ls.Config(url="http://labelstudio:8080", public_url="http://h:18085", api_url="http://h:18080", token="t")
    fr = SimpleNamespace(detection_type="pothole", frame_key="v-1__z000__t00001000", image_width=720,
                         image_height=576, video_time_s=1.0, n_boxes=1,
                         labels=[{"class_index": 0, "class_name": "pothole", "cx": 0.5, "cy": 0.5, "w": 0.2, "h": 0.2}])
    s = SimpleNamespace(decision="rejected", reason_code="shadow", decided_by="system")
    t = ls.task_for(cfg, fr, ["pothole"], [s])
    assert t["data"]["frame_key"] == fr.frame_key
    assert t["data"]["image"].startswith("http://h:18080/labelstudio/image/pothole/")
    assert "rejected (shadow) by system" in t["data"]["info"]
    r = t["predictions"][0]["result"][0]
    assert r["value"]["rectanglelabels"] == ["pothole"] and r["original_width"] == 720
