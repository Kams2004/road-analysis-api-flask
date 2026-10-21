"""
Observation identity, object position estimate, cluster merge rule and the
dedupe-observations clean-up (docs/CLUSTERS.md).

Pure tests run everywhere; the DB test needs a throwaway PostgreSQL
(RUN_DB_TESTS=1, DATABASE_URL=…), as tests/test_clusters.py.
"""
import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.modules.m03_maps.osrm.geo import bearing_deg, destination, haversine_m
from app.modules.m05_ai_detection.parked_vehicle.settings import CameraCalibration, ParkedVehicleSettings
from app.modules.m06_hazards import hazard_service, object_position as op, observations as obs

T0 = datetime(2026, 10, 15, 15, 47, 22, tzinfo=timezone.utc)


def _o(**kw):
    base = dict(id=str(uuid.uuid4()), type="pothole", subtype=None, predicted_type="pothole", predicted_subtype=None,
                vehicle_id="LT458AV", recorded_at=T0, raw_gps_text="0515.3583,N,01013.5471,E,028KM/H",
                bbox_x1=145.0, bbox_y1=450.0, bbox_x2=211.0, bbox_y2=480.0, frame_width=720, frame_height=576,
                created_at=datetime(2026, 10, 20), reviewed_by=None, review_status="pending")
    base.update(kw)
    return SimpleNamespace(**base)


# ── observation identity ─────────────────────────────────────────────────────

def test_same_frame_of_the_same_footage_is_one_observation():
    a = _o()
    assert obs.same_observation(a, _o(recorded_at=T0 + timedelta(seconds=0.8)))             # ±1 s
    assert obs.same_observation(a, _o(bbox_x1=150.0, bbox_x2=215.0))                        # IoU ≥ 0.3
    assert obs.same_observation(a, _o(type="speed_bump", predicted_type="pothole"))          # class corrected
    assert not obs.same_observation(a, _o(recorded_at=T0 + timedelta(seconds=2)))            # another instant
    assert not obs.same_observation(a, _o(vehicle_id="OTHER"))                               # another camera
    assert not obs.same_observation(a, _o(type="speed_bump", predicted_type="speed_bump"))   # another type
    assert not obs.same_observation(a, _o(bbox_x1=500.0, bbox_x2=560.0))                     # another object
    # same frame, another resolution (re-encoded clip): normalised boxes
    assert obs.same_observation(a, _o(bbox_x1=290.0, bbox_y1=900.0, bbox_x2=422.0, bbox_y2=960.0,
                                      frame_width=1440, frame_height=1152))


def test_signs_need_the_same_subtype():
    stop = _o(type="traffic_sign", predicted_type="traffic_sign", subtype="stop", predicted_subtype="stop")
    assert obs.same_observation(stop, _o(**{**stop.__dict__, "id": "x"}))
    assert not obs.same_observation(stop, _o(**{**stop.__dict__, "subtype": "no_entry",
                                                "predicted_subtype": "no_entry"}))


def test_without_osd_time_the_gps_reading_is_the_instant():
    a = _o(recorded_at=None, raw_gps_text="0515.3583,N,01013.5471")
    assert obs.gps_key(a.raw_gps_text) == ("0515.3583", "01013.5471")
    assert obs.same_observation(a, _o(recorded_at=None, raw_gps_text="0515.3583,N,01013.5471,E,"))
    assert not obs.same_observation(a, _o(recorded_at=None, raw_gps_text="0515.3601,N,01013.5490,E,"))
    assert not obs.same_observation(a, _o())                       # one with, one without a time
    assert not obs.same_observation(_o(recorded_at=None, raw_gps_text="??"),
                                    _o(recorded_at=None, raw_gps_text="??"))


def test_groups_and_keeper_keep_the_human_decision():
    first = _o(created_at=datetime(2026, 10, 20, 16, 29), reviewed_by="system", review_status="validated")
    second = _o(created_at=datetime(2026, 10, 20, 16, 44), reviewed_by="system", review_status="validated")
    human = _o(created_at=datetime(2026, 10, 21, 8, 35), reviewed_by="admin@viasafe.local",
               review_status="corrected")
    other = _o(bbox_x1=500.0, bbox_x2=560.0)
    gs = obs.groups([human, other, second, first])
    assert len(gs) == 1 and [d.id for d in gs[0]] == [first.id, second.id, human.id]
    assert obs.keeper(gs[0]) is human                              # a person decided: kept
    assert obs.keeper([first, second]) is first                    # else the earliest
    assert obs.keeper([first, second], user_reviewed_ids={second.id}) is second


# ── object position ──────────────────────────────────────────────────────────

CAM = CameraCalibration()


def test_heading_from_the_gps_track():
    lat, lon = 5.25, 10.22
    fixes = [(t, *destination(lat, lon, 90.0, 10.0 * t)) for t in range(10)]       # driving east, 10 m/s
    assert abs(op.heading_from_fixes(fixes, 5.0) - 90.0) < 0.5
    assert op.heading_from_fixes(fixes, None) is None
    assert op.heading_from_fixes([(t, lat, lon) for t in range(10)], 5.0) is None    # stopped


def test_object_is_ahead_of_the_vehicle_along_the_heading():
    lat, lon = 5.25, 10.22
    near = op.estimate("pothole", (330, 540, 390, 570), 720, 576, lat, lon, 90.0, CAM)
    far = op.estimate("pothole", (350, 300, 370, 310), 720, 576, lat, lon, 90.0, CAM)
    assert near.source == far.source == op.ESTIMATED
    assert near.range_m < far.range_m <= op.MAX_RANGE_M
    for e in (near, far):
        d = haversine_m(lat, lon, e.latitude, e.longitude)
        assert op.MIN_RANGE_M <= d <= op.MAX_RANGE_M + op.MAX_LATERAL_M
        assert abs(((bearing_deg(lat, lon, e.latitude, e.longitude) - 90.0 + 180) % 360) - 180) < 30
    # the same pothole seen from ~far and from ~near on two passes: the estimates
    # are much closer to each other than the two vehicle fixes
    fix_far = (lat, lon)
    obj = (far.latitude, far.longitude)
    fix_near = destination(lat, lon, 90.0, far.range_m - near.range_m)
    again = op.estimate("pothole", (330, 540, 390, 570), 720, 576, *fix_near, 90.0, CAM)
    assert haversine_m(*obj, again.latitude, again.longitude) < 0.3 * haversine_m(*fix_far, *fix_near)


def test_fallbacks_and_pipeline_targets():
    lat, lon = 5.25, 10.22
    assert op.estimate("pothole", (1, 2, 3, 4), 720, 576, lat, lon, None, CAM).source == op.VEHICLE
    assert op.estimate("pothole", (330, 100, 390, 120), 720, 576, lat, lon, 0.0, CAM).source == op.VEHICLE  # above horizon
    assert op.estimate("pothole", (1, 2, 3, 4), 720, 576, None, None, 0.0, CAM).source is None
    t = op.estimate("parked_vehicle", None, None, None, lat, lon, None, CAM, target={"latitude": 5.2501,
                                                                                    "longitude": 10.2203})
    assert (t.latitude, t.longitude, t.source) == (5.2501, 10.2203, op.ESTIMATED)
    sign = op.estimate("traffic_sign", (500, 200, 530, 230), 720, 576, lat, lon, 0.0, CAM)
    assert sign.source == op.ESTIMATED and sign.latitude > lat
    det = SimpleNamespace(latitude=lat, longitude=lon, object_latitude=None, object_longitude=None)
    assert op.position(det) == (lat, lon)
    det.object_latitude, det.object_longitude = 5.2502, 10.2201
    assert op.position(det) == (5.2502, 10.2201)


def test_for_detection_uses_the_vehicle_camera_profile():
    pvs = ParkedVehicleSettings(camera_profiles={"CAM1": {"horizon_y": 0.30}})
    lat, lon = 5.25, 10.22
    fixes = [(t, *destination(lat, lon, 0.0, 8.0 * (t - 10))) for t in range(20)]
    base = dict(type="pothole", bbox_x1=330, bbox_y1=500, bbox_x2=390, bbox_y2=520, corrected_bbox=None,
                frame_width=720, frame_height=576, latitude=lat, longitude=lon, video_time_s=10.0, ai_details=None)
    default = op.for_detection(SimpleNamespace(**base, vehicle_id=None), fixes, pvs)
    profiled = op.for_detection(SimpleNamespace(**base, vehicle_id="CAM1"), fixes, pvs)
    assert default.source == profiled.source == op.ESTIMATED
    assert default.latitude > lat and profiled.range_m < default.range_m          # lower horizon → nearer


def test_location_fix_keeps_the_offset_or_takes_the_pin():
    det = SimpleNamespace(latitude=5.0, longitude=10.0, object_latitude=5.0002, object_longitude=10.0,
                          object_position_source=op.ESTIMATED)
    old = (det.latitude, det.longitude)
    det.latitude = 5.001
    op.on_location_fix(det, *old, "trajectory_point")
    assert abs(det.object_latitude - 5.0012) < 1e-9
    op.on_location_fix(det, det.latitude, det.longitude, "map_pin")
    assert det.object_latitude is None and det.object_position_source == op.VEHICLE


# ── merge rule ───────────────────────────────────────────────────────────────

def _h(code, status, north_m, created, type_="pothole", subtype=None, number=1):
    return SimpleNamespace(id=code, code=code, status=status, type=type_, subtype=subtype, number=number,
                           latitude=5.25 + north_m / 111_320.0, longitude=10.22, created_at=created)


def test_merge_pair_uses_the_larger_radius_and_keeps_confirmed_then_oldest():
    d0, d1 = datetime(2026, 10, 1), datetime(2026, 10, 2)
    radius = {"A": 60.0, "B": 30.0, "C": 30.0}
    cand = _h("A", "candidate", 0, d0, number=-1)
    hz = _h("B", "active", 45, d1, number=7)
    keep, dup = hazard_service.merge_pair([cand, hz], lambda h: radius[h.id], ["traffic_sign"])
    assert keep is hz and dup is cand                             # 45 m ≤ max(60, 30); confirmed kept
    older = _h("C", "candidate", 20, datetime(2026, 9, 1), number=-2)
    assert hazard_service.merge_pair([cand, older], lambda h: radius[h.id], [])[0] is older
    far = _h("B", "active", 70, d1)
    assert hazard_service.merge_pair([cand, far], lambda h: radius[h.id], []) is None
    signs = [_h("A", "candidate", 0, d0, "traffic_sign", "stop"), _h("B", "candidate", 5, d1, "traffic_sign", "yield")]
    assert hazard_service.merge_pair(signs, lambda h: radius[h.id], ["traffic_sign"]) is None


# ── database: dedupe-observations ────────────────────────────────────────────

from tests.test_clusters import db  # noqa: E402,F401  (fixture)

db_only = pytest.mark.skipif(os.environ.get("RUN_DB_TESTS") != "1",
                             reason="needs a throwaway PostgreSQL (RUN_DB_TESTS=1)")


@db_only
async def test_dedupe_observations_keeps_one_and_is_idempotent(db):
    from sqlalchemy import select
    from tests.test_clusters import _ctx, _job, _spot
    from app.modules.m06_hazards.models.detection import Detection, ReviewStatus
    spot = _spot()
    when = (datetime.utcnow() - timedelta(days=400)).replace(tzinfo=timezone.utc, microsecond=0)
    jobs = [await _job(db) for _ in range(3)]
    made = []
    for i, job in enumerate(jobs):
        det = Detection(job_id=job.id, type="pothole", confidence=0.8, frame_number=121, latitude=spot[0],
                        longitude=spot[1], location_status="confirmed", location_source="ocr",
                        review_status=ReviewStatus.corrected if i == 2 else ReviewStatus.pending,
                        reviewed_by="reviewer@example.com" if i == 2 else None, recorded_at=when,
                        vehicle_id=f"PYTEST-{spot}", bbox_x1=145, bbox_y1=450, bbox_x2=211, bbox_y2=480,
                        frame_width=720, frame_height=576, raw_gps_text="0515.3583,N,01013.5471,E,",
                        created_at=datetime.utcnow() + timedelta(seconds=i))
        db.add(det)
        await db.flush()
        await hazard_service.attach(db, det, _ctx())
        made.append(det)
    await db.commit()
    # the processor lookup finds the stored observation
    probe = SimpleNamespace(**{c: getattr(made[0], c) for c in (
        "type", "subtype", "predicted_type", "predicted_subtype", "vehicle_id", "recorded_at", "raw_gps_text",
        "bbox_x1", "bbox_y1", "bbox_x2", "bbox_y2", "frame_width", "frame_height")}, id=None)
    assert (await obs.find_existing(db, probe)) is not None

    res = await obs.dedupe(db, commit=db.commit)
    assert res["deleted"] >= 2
    left = (await db.execute(select(Detection).where(Detection.vehicle_id == f"PYTEST-{spot}"))).scalars().all()
    assert [d.id for d in left] == [made[2].id]                    # the human-reviewed one stays
    again = await obs.dedupe(db, commit=db.commit)
    assert again["groups"] == 0 and again["deleted"] == 0
