"""Pothole / sign / bump boxes on the dashcam text overlay are ignored (ai_validation.ignore_osd_text_boxes)."""
from types import SimpleNamespace

import pytest

pipeline = pytest.importorskip("app.modules.m07_ingestion.pipeline")
from app.core.settings_service import AiValidationSettings  # noqa: E402
from app.modules.m05_ai_detection import osd_layouts as L  # noqa: E402

W, H = 720, 288


def _ctx(profile, on=True):
    return SimpleNamespace(osd_reader=SimpleNamespace(profile=profile),
                           ai=AiValidationSettings(ignore_osd_text_boxes=on))


def test_box_on_the_total_overlay_text_is_masked():
    total = L.effective_profiles()["total"]
    # 'kpf' (user-defined field, x=500 y=100 on the 0-1000 grid) → centre ≈ (0.53, 0.12)
    assert pipeline._on_osd_text(_ctx(total), (370, 28, 395, 40), W, H)
    # a 50 km/h sign in the scene (centre ≈ 0.76, 0.55) is kept
    assert not pipeline._on_osd_text(_ctx(total), (540, 150, 560, 165), W, H)


def test_besafe_bottom_text_is_masked_but_the_road_is_not():
    besafe = L.effective_profiles()["besafe"]
    assert pipeline._on_osd_text(_ctx(besafe), (40, 262, 160, 278), W, H)       # date at the bottom-left
    assert not pipeline._on_osd_text(_ctx(besafe), (300, 200, 360, 230), W, H)  # pothole on the road


def test_no_mask_without_layout_or_when_switched_off():
    total = L.effective_profiles()["total"]
    box = (370, 28, 395, 40)
    assert not pipeline._on_osd_text(_ctx(None), box, W, H)
    assert not pipeline._on_osd_text(_ctx(L.full_frame_profile()), box, W, H)
    assert not pipeline._on_osd_text(_ctx(total, on=False), box, W, H)
