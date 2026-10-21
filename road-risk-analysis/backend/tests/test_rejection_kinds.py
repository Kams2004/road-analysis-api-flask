"""Rejection reason kinds → training data (docs/TRAINING-DATA.md, "Rejection kinds")."""
import asyncio

import pytest

from app.modules.m06_hazards.rejection_seeds import FP, REDUNDANT, UNUSABLE, SEED_REASONS, seed_kind
from app.modules.m06_hazards.training_data import layout as L


# ── classification of the built-in reasons ──────────────────────────────────

EXPECTED = {
    FP: {"not_road_side", "ai_verifier", "ai_off_road", "ai_box_too_large", "water_reflection", "shadow",
         "road_marking", "manhole", "dirt_patch", "ditch", "road_edge", "person_as_sign", "bike_as_stop",
         "pole_as_sign", "street_light", "ai_not_a_sign", "ai_not_grounded", "ai_moving_vehicle",
         "ai_traffic_queue", "crack_or_tyre_marks", "road_patch", "broken_edge", "vegetation", "debris",
         "billboard", "vehicle_sticker", "tree_as_sign", "pothole_or_damage", "marking_or_crossing",
         "not_a_vehicle"},
    REDUNDANT: {"duplicate", "speed_bump_ok"},
    UNUSABLE: {"image_quality", "out_of_frame", "sign_not_visible", "low_confidence"},
}


def test_every_seeded_reason_has_the_expected_kind():
    got: dict = {}
    for _, code, _, _, kind in SEED_REASONS:
        got.setdefault(kind, set()).add(code)
    assert got == EXPECTED
    assert all(k in L.REJECTION_KINDS for *_, k in SEED_REASONS)


def test_seeds_are_unique_per_type_and_new_reasons_present():
    keys = [(t, c) for t, c, *_ in SEED_REASONS]
    assert len(keys) == len(set(keys))
    for key in [("pothole", "crack_or_tyre_marks"), ("pothole", "road_patch"), ("pothole", "broken_edge"),
                ("pothole", "vegetation"), ("pothole", "debris"), ("traffic_sign", "billboard"),
                ("traffic_sign", "vehicle_sticker"), ("traffic_sign", "tree_as_sign"),
                ("speed_bump", "pothole_or_damage"), ("speed_bump", "marking_or_crossing"),
                ("parked_vehicle", "not_a_vehicle"), ("parked_vehicle", "ai_off_road"), ("pothole", "ai_off_road")]:
        assert key in keys
    label = next(lbl for t, c, lbl, *_ in SEED_REASONS if c == "bike_as_stop")
    assert label == "Vehicle / motorbike part"


def test_seed_kind_lookup():
    assert seed_kind("duplicate", "pothole") == REDUNDANT          # an `all` reason
    assert seed_kind("ai_off_road", "parked_vehicle") == FP
    assert seed_kind("sign_not_visible", "traffic_sign") == UNUSABLE
    assert seed_kind("my_custom", "pothole") is None
    assert seed_kind(None) is None


# ── layout decisions per kind ───────────────────────────────────────────────

def test_sample_role_per_kind():
    assert L.sample_role("accepted") == "positive"
    assert L.sample_role("corrected", None) == "positive"
    assert L.sample_role("rejected", FP) == "negative"
    assert L.sample_role("rejected", None) == "negative"           # unknown → historical behaviour
    assert L.sample_role("rejected", "bogus") == "negative"
    assert L.sample_role("rejected", REDUNDANT) == "positive"
    assert L.sample_role("rejected", UNUSABLE) == "excluded"


def test_classifier_class_per_kind():
    assert L.classifier_class("pothole", None, "rejected", kind=FP) == "not_pothole"
    assert L.classifier_class("pothole", None, "rejected", kind=REDUNDANT) == "pothole"
    assert L.classifier_class("traffic_sign", "stop", "rejected", kind=REDUNDANT) == "stop"
    assert L.classifier_class("traffic_sign", "stop", "rejected", kind=FP) == "not_a_sign"


def _v(i, decision, kind=None, excluded=False, idx=0, box=(100, 300, 200, 360)):
    role = "excluded" if excluded else L.sample_role(decision, kind)
    return L.SampleView(i, "active", decision, idx if role == "positive" else None,
                        "pothole" if role == "positive" else None, box, kind=kind, excluded=excluded)


def test_frame_boxes_per_kind():
    samples = [_v("a", "rejected", FP), _v("b", "rejected", REDUNDANT, box=(400, 400, 500, 450)),
               _v("c", "rejected", UNUSABLE, excluded=True), _v("d", "accepted")]
    assert [b["detection_id"] for b in L.frame_boxes(samples, 720, 576)] == ["b", "d"]


def test_frame_status_ignores_unusable_rejections():
    assert L.frame_status([_v("c", "rejected", UNUSABLE, excluded=True)], has_image=True) == "empty"
    assert L.frame_status([_v("a", "rejected", FP)], has_image=True) == "active"          # background
    assert L.frame_status([_v("b", "rejected", REDUNDANT)], has_image=True) == "active"
    assert L.frame_status([_v("a", "rejected", FP), _v("c", "rejected", UNUSABLE, excluded=True)],
                          has_image=True) == "active"


# ── re-filing existing samples (sqlite) ─────────────────────────────────────

def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def session_factory(monkeypatch):
    pytest.importorskip("aiosqlite")
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.core.db.session import Base
    from app.modules.m06_hazards import training_data
    from app.modules.m06_hazards.models.detection import Detection
    from app.modules.m06_hazards.models.rejection_reason import RejectionReason
    from app.modules.m06_hazards.models.training_sample import TrainingFrame, TrainingSample
    from app.modules.m07_ingestion.models.job import Job

    monkeypatch.setattr(training_data, "card_info", lambda t: (L.detector_classes(t), None, None, None))
    engine = create_async_engine("sqlite+aiosqlite://")
    tables = [Base.metadata.tables[t] for t in ("jobs", "detections", "rejection_reasons",
                                                 "training_samples", "training_frames")]

    async def setup():
        async with engine.begin() as conn:
            await conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
            await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    _run(setup())
    yield async_sessionmaker(engine, expire_on_commit=False), (Job, Detection, RejectionReason,
                                                               TrainingSample, TrainingFrame)
    _run(engine.dispose())


def test_resync_refiles_by_kind_and_is_idempotent(session_factory):
    from sqlalchemy import select
    from app.modules.m06_hazards import training_data
    from app.modules.m06_hazards.models.detection import ReviewStatus
    from app.modules.m06_hazards.rejection_seeds import seed_rejection_reasons

    make, (Job, Detection, RejectionReason, TrainingSample, TrainingFrame) = session_factory

    async def scenario():
        loop_out = {}
        async with make() as db:
            await seed_rejection_reasons(db)
            await seed_rejection_reasons(db)          # idempotent
            n = len((await db.execute(select(RejectionReason))).scalars().all())
            assert n == len(SEED_REASONS)
            db.add(Job(id="job-1", filename="v.mp4"))
            for i, (code, t) in enumerate([("duplicate", "pothole"), ("image_quality", "pothole"),
                                            ("shadow", "pothole")]):
                db.add(Detection(id=f"d{i}", job_id="job-1", type=t, confidence=0.6, frame_number=10 + i,
                                 video_time_s=1.0 + i, bbox_x1=100, bbox_y1=300, bbox_x2=200, bbox_y2=360,
                                 frame_width=720, frame_height=576, review_status=ReviewStatus.rejected,
                                 rejection_code=code, reviewed_by="alice"))
            await db.commit()
            # filed the old way: every rejection a negative, no kind
            for i in range(3):
                det = await db.get(Detection, f"d{i}")
                await training_data.record_decision(db, det, actor_type="user")
            await db.commit()
            for s in (await db.execute(select(TrainingSample))).scalars():
                s.rejection_kind, s.excluded = None, False
                s.classifier_class, s.class_name, s.class_index = "not_pothole", None, None
                s.sync_status, s.crop_key = "synced", f"pothole/classifier/not_pothole/{s.id}.jpg"
                if s.id == "d1":            # the old filing had a frame for it too
                    db.add(TrainingFrame(detection_type="pothole", frame_key=s.frame_key,
                                         group_key=L.group_key(s.frame_key), job_id="job-1"))
            await db.flush()
            for f in (await db.execute(select(TrainingFrame))).scalars():
                f.dirty = False
            await db.commit()

            res = await training_data.resync_rejections(db, only_unclassified=True)
            assert res == {"checked": 3, "changed": 3}
            rows = {s.id: s for s in (await db.execute(select(TrainingSample))).scalars()}
            dup, bad, shadow = rows["d0"], rows["d1"], rows["d2"]
            assert (dup.rejection_kind, dup.classifier_class, dup.class_index, dup.excluded) == \
                (REDUNDANT, "pothole", 0, False)
            assert (bad.rejection_kind, bad.excluded) == (UNUSABLE, True)
            assert (shadow.rejection_kind, shadow.classifier_class, shadow.class_index) == (FP, "not_pothole", None)
            dirty = {f.frame_key for f in (await db.execute(select(TrainingFrame))).scalars() if f.dirty}
            assert dup.frame_key in dirty and bad.frame_key in dirty and shadow.frame_key in dirty
            assert dup.sync_status == "pending" and bad.sync_status == "pending"   # crops to move / remove

            again = await training_data.resync_rejections(db)
            assert again == {"checked": 3, "changed": 0}
            assert (await training_data.resync_rejections(db, only_unclassified=True))["checked"] == 0

            # a fresh unusable rejection on a new frame creates no frame
            db.add(Detection(id="d9", job_id="job-1", type="pothole", confidence=0.4, frame_number=99,
                             video_time_s=99.0, bbox_x1=1, bbox_y1=1, bbox_x2=5, bbox_y2=5,
                             review_status=ReviewStatus.rejected, rejection_code="low_confidence",
                             reviewed_by="system"))
            await db.commit()
            await training_data.record_decision(db, await db.get(Detection, "d9"), actor_type="system")
            await db.commit()
            s9 = await db.get(TrainingSample, "d9")
            assert s9.excluded and s9.sync_status == "synced" and s9.crop_key is None
            frames = (await db.execute(select(TrainingFrame).where(TrainingFrame.frame_key == s9.frame_key)
                                       )).scalars().all()
            assert frames == []
        return loop_out

    _run(scenario())


def test_custom_reason_kind_is_used(session_factory):
    from app.modules.m06_hazards import training_data
    make, (_, _, RejectionReason, *_) = session_factory

    async def scenario():
        async with make() as db:
            db.add(RejectionReason(detection_type="pothole", code="puddle", label="Puddle", description="",
                                   is_custom=True))
            db.add(RejectionReason(detection_type="all", code="dup2", label="Dup", description="",
                                   is_custom=True, kind=REDUNDANT))
            await db.commit()
            assert await training_data.reason_kind(db, "puddle", "pothole") == FP     # default
            assert await training_data.reason_kind(db, "dup2", "speed_bump") == REDUNDANT
            assert await training_data.reason_kind(db, "duplicate", "pothole") == REDUNDANT   # seed fallback
            assert await training_data.reason_kind(db, None, "pothole") == FP

    _run(scenario())


def test_sync_writes_positive_crop_and_drops_unusable_files(monkeypatch):
    from types import SimpleNamespace
    from app.modules.m06_hazards.training_data import sync

    puts, removed = {}, []
    monkeypatch.setattr(sync, "_put", lambda k, data, ct: puts.__setitem__(k, data))
    monkeypatch.setattr(sync, "_remove", lambda k: removed.append(k) if k else None)
    monkeypatch.setattr(sync, "_copy_from_detections", lambda src, dst: puts.__setitem__(dst, b"img"))
    monkeypatch.setattr(sync, "_get_detections_object", lambda src: b"jpeg")
    monkeypatch.setattr(sync, "_crop_jpeg", lambda *a: b"crop")
    monkeypatch.setattr(sync, "card_info", lambda t: (["pothole"], None, None, None))

    def sample(i, kind, cls, idx, excluded=False, crop=None):
        return SimpleNamespace(id=i, state="active", decision="rejected", class_index=idx,
                               class_name="pothole" if idx is not None else None, rejection_kind=kind,
                               excluded=excluded, classifier_class=cls, detection_type="pothole",
                               bbox_x1=100, bbox_y1=300, bbox_x2=200, bbox_y2=360, sync_status="pending",
                               crop_key=crop)
    fr = SimpleNamespace(detection_type="pothole", frame_key="v-j__z000__t00001000", source_object="f.jpg",
                         image_width=720, image_height=576, review_status="needs_box_review",
                         reviewed_boxes=None, image_key=None, label_key=None)
    samples = [sample("dup", REDUNDANT, "pothole", 0, crop="pothole/classifier/not_pothole/dup.jpg"),
               sample("bad", UNUSABLE, "not_pothole", None, excluded=True,
                      crop="pothole/classifier/not_pothole/bad.jpg"),
               sample("neg", FP, "not_pothole", None)]
    out = sync._sync_frame_objects(fr, samples, {})
    assert out["status"] == "active" and out["n_boxes"] == 1 and out["labels"][0]["detection_id"] == "dup"
    assert out["crops"] == {"dup": "pothole/classifier/pothole/dup.jpg", "bad": None,
                            "neg": "pothole/classifier/not_pothole/neg.jpg"}
    assert "pothole/classifier/not_pothole/dup.jpg" in removed and "pothole/classifier/not_pothole/bad.jpg" in removed

    # a frame with only an unusable rejection: objects removed, no crop
    removed.clear()
    out = sync._sync_frame_objects(fr, [samples[1]], {})
    assert out["status"] == "empty" and out["crops"] == {"bad": None}
