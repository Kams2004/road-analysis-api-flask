"""
Parked / roadside vehicle detection (app.services.detection.parked_vehicle):
geometry, every rule (parked, oncoming, same direction, traffic queue,
off-road, billboard / not grounded, masks, hood, motorcycle disabled, no-GPS
fallback capped, camera profile, wrong_side vs roadside), score
monotonicity, settings validation, stage output and the processor glue.

Tracks are synthetic: a flat-road pinhole camera (the default calibration)
renders the box of a vehicle standing / moving at a known world position
while the recording vehicle drives north at a known speed.
"""
import math
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pytest
from pydantic import ValidationError

from app.services import ai_validation, static_check
from app.services.detection import parked_vehicle as pv
from app.services.detection.parked_vehicle import geometry as geo
from app.services.detection.parked_vehicle import scoring
from app.services.detection.parked_vehicle.settings import CameraCalibration, ParkedVehicleSettings
from app.services.detection.parked_vehicle.severity import severity

W, H = 1280, 720
LAT0, LON0 = 4.05, 9.70
M_PER_DEG = 111_195.0


def _latlon(east: float, north: float) -> tuple[float, float]:
    return LAT0 + north / M_PER_DEG, LON0 + east / (M_PER_DEG * math.cos(math.radians(LAT0)))


def _box(d: float, x: float, cam: CameraCalibration, height_m=1.5, width_m=1.8, lift_m=0.0):
    """Pixel box of a vehicle `d` m ahead, `x` m to the right, its bottom `lift_m` above the road."""
    f = geo.focal(cam)
    aspect = W / H
    bottom = cam.horizon_y + (cam.camera_height_m - lift_m) * f / d
    h = height_m * f / d
    cx = 0.5 + cam.center_offset + x * f / (d * aspect)
    half_w = width_m * f / d / aspect / 2
    return (round((cx - half_w) * W), round((bottom - h) * H), round((cx + half_w) * W), round(bottom * H))


def scenario(target=lambda t: (3.5, 40.0), ego_speed=lambda t: 36.0, duration=3.5, dt=0.2, cls="car",
             height_m=1.5, lift_m=0.0, gps=True, osd_speed=True, conf=0.8, cam=None, fix_every=1):
    """(sightings, ego samples): ego drives north at ego_speed(t) km/h, the
    target is at world (east, north) = target(t) metres."""
    cam = cam or CameraCalibration()
    sightings, ego = [], []
    north, t, i = 0.0, 0.0, 0
    last_fix = None
    while t <= duration + 1e-9:
        e_t, n_t = target(t)
        d, x = n_t - north, e_t
        lat, lon = _latlon(0.0, north)
        if i % fix_every == 0:
            last_fix = (lat, lon)
        glat, glon = (last_fix if gps else (None, None))
        speed = ego_speed(t) if osd_speed else None
        ego.append(pv.EgoSample(t, glat, glon, speed))
        if d > 2.0:
            sightings.append(pv.VehicleSighting(
                i * 5 + 1, _box(d, x, cam, height_m=height_m, lift_m=lift_m), conf, glat, glon,
                t_s=t, cls=cls, speed_kmh=speed, frame_w=W, frame_h=H))
        north += ego_speed(t) / 3.6 * dt
        t = round(t + dt, 6)
        i += 1
    return sightings, ego


def run(s=None, **kw):
    snap = kw.pop("snap", None)
    peers = kw.pop("queue_peers", 0)
    vehicle_id = kw.pop("vehicle_id", None)
    sts, ego = scenario(**kw)
    return pv.evaluate(sts, ego, s or ParkedVehicleSettings(), snap=snap, queue_peers=peers, vehicle_id=vehicle_id)


def reasons(ev):
    return {r.code: r for r in ev.judgement.reasons}


# ── geometry ─────────────────────────────────────────────────────────────────

def test_pinhole_ranges_and_offsets():
    cam = CameraCalibration()
    f = geo.focal(cam)
    assert f == pytest.approx(0.5 / math.tan(math.radians(30)))
    assert geo.range_from_bottom(0.45 + 1.4 * f / 20, cam) == pytest.approx(20)
    assert geo.range_from_bottom(0.44, cam) is None                     # above the horizon
    assert geo.range_from_height(1.5 * f / 25, 1.5, cam) == pytest.approx(25)
    assert geo.range_from_height(0.001, 1.5, cam) is None
    assert geo.lateral_offset(0.5, 30, W / H, cam) == 0
    assert geo.lateral_offset(0.7, 30, W / H, cam) > 0 > geo.lateral_offset(0.3, 30, W / H, cam)
    shifted = CameraCalibration(center_offset=0.2)
    assert geo.lateral_offset(0.7, 30, W / H, shifted) == pytest.approx(0)
    assert geo.ground_contact_error(20, 20) == 0
    assert geo.ground_contact_error(30, 10) == pytest.approx(1.0)
    # a rendered box gives back its range and offset
    b = geo.normalise_box(_box(18, 2.5, cam), W, H)
    r = geo.target_range(b, 1.5, cam)
    assert r == pytest.approx(18, rel=0.03)
    assert geo.lateral_offset(geo.centre(b)[0], r, W / H, cam) == pytest.approx(2.5, rel=0.05)
    # box cut by the bottom of the frame → apparent height is used
    assert geo.is_truncated_bottom((0.2, 0.6, 0.4, 0.99))
    assert geo.target_range((0.2, 0.6, 0.4, 0.99), 1.5, cam) == pytest.approx(1.5 * f / 0.39)


def test_world_position_and_enu_offsets():
    lat, lon = geo.world_position(LAT0, LON0, 0.0, 100.0, 0.0)        # 100 m north
    assert (lat - LAT0) * M_PER_DEG == pytest.approx(100, rel=1e-3) and lon == pytest.approx(LON0)
    lat, lon = geo.world_position(LAT0, LON0, 0.0, 0.0, 10.0)          # 10 m right of a north heading = east
    assert lon > LON0 and lat == pytest.approx(LAT0, abs=1e-7)
    assert geo.offset_enu(0, 0, 90.0, 10, 0) == pytest.approx((10, 0), abs=1e-9)   # heading east
    assert geo.offset_enu(0, 0, 90.0, 0, 5) == pytest.approx((0, -5), abs=1e-9)    # right of east = south


def test_corridor_side_and_masks():
    cam = CameraCalibration()
    assert geo.in_corridor(0.5, 0.9, cam) and geo.in_corridor(0.85, 0.99, cam)
    assert not geo.in_corridor(0.62, 0.47, cam)                        # narrow near the horizon
    assert geo.side_of_road(0.3, cam) == "left" and geo.side_of_road(0.7, cam) == "right"
    assert geo.side_of_road(0.55, CameraCalibration(center_offset=0.1)) == "left"
    assert geo.is_wrong_side("left", "right") and not geo.is_wrong_side("right", "right")
    assert geo.is_wrong_side("right", "left")
    s = ParkedVehicleSettings()
    # OSD overlay band (top) and own bonnet (bottom 10 %)
    assert geo.is_masked((0.6, 0.05, 0.7, 0.15), s.mask_regions, s.hood_mask_bottom_ratio)
    assert geo.is_masked((0.3, 0.85, 0.7, 1.0), s.mask_regions, s.hood_mask_bottom_ratio)
    assert not geo.is_masked((0.6, 0.5, 0.7, 0.6), s.mask_regions, s.hood_mask_bottom_ratio)
    assert not geo.is_masked((0.3, 0.85, 0.7, 1.0), [], 0.0)


# ── ego motion ───────────────────────────────────────────────────────────────

def test_ego_motion_modes_dedupe_and_jumps():
    sts, ego = scenario(fix_every=5)             # dashcam repeats each fix for 1 s
    m = pv.ego_motion(ego)
    assert m.mode == "gps" and len(m.fixes) == 4
    assert m.distance(0, 3.0) == pytest.approx(30, rel=0.05)
    assert m.heading(1.5) == pytest.approx(0, abs=1)
    # an OCR glitch 5 km away is dropped
    glitch = list(ego)
    glitch[7] = pv.EgoSample(glitch[7].t_s, LAT0 + 0.05, LON0, 36.0)
    assert all(abs(e) < 1 for _, e, _n in pv.ego_motion(glitch).fixes)
    _sts, no_gps = scenario(gps=False)
    m = pv.ego_motion(no_gps)
    assert m.mode == "odometry" and m.distance(0, 3.0) == pytest.approx(30, rel=0.01)
    _sts, nothing = scenario(gps=False, osd_speed=False)
    assert pv.ego_motion(nothing).mode == "none"
    path = pv.EgoPath(keep_s=10)
    for k in range(100):
        path.add(k * 0.5, LAT0, LON0, 30.0, interpolated=(k == 99))
    assert path.samples[0].t_s >= 39.0 and path.samples[-1].lat is None   # pruned; voter fix not used


# ── rules ────────────────────────────────────────────────────────────────────

def test_parked_roadside_car_is_parked():
    ev = run()
    assert ev.verdict == pv.PARKED and ev.subtype == pv.ROADSIDE and ev.store and ev.rejection_code is None
    r = reasons(ev)
    assert r["target_speed"].passed and r["world_drift"].passed and r["ground_contact"].passed
    assert r["ego_distance"].passed and r["road_distance_unknown"].passed is None
    assert ev.features.target_speed_kmh < 3 and ev.judgement.motion == "stationary"
    assert ev.severity == "low" and ev.vehicle_class == "car"
    # estimated position ≈ 40 m north, 3.5 m east of the start
    lat, lon = _latlon(3.5, 40)
    assert ev.features.target_lat == pytest.approx(lat, abs=2 / M_PER_DEG)
    assert ev.features.target_lon == pytest.approx(lon, abs=2 / M_PER_DEG)
    assert not ev.blockers and ev.score >= 0.7


def test_wrong_side_vs_roadside_and_blocking():
    left = run(target=lambda t: (-3.5, 40.0))
    assert left.verdict == pv.PARKED and left.subtype == pv.WRONG_SIDE and left.severity == "medium"
    lhd = run(ParkedVehicleSettings(drive_side="left"), target=lambda t: (-3.5, 40.0))
    assert lhd.subtype == pv.ROADSIDE
    assert run(ParkedVehicleSettings(drive_side="left")).subtype == pv.WRONG_SIDE
    block = run(target=lambda t: (0.3, 40.0))
    assert block.verdict == pv.PARKED and block.subtype == pv.BLOCKING and block.severity == "high"
    truck = run(target=lambda t: (0.3, 40.0), cls="truck", height_m=3.2)
    assert truck.verdict == pv.PARKED and truck.severity == "critical"


def test_oncoming_vehicle_is_moving():
    ev = run(target=lambda t: (-3.0, 60.0 - 12.0 * t))       # 43 km/h towards us
    assert ev.verdict == pv.MOVING and ev.judgement.motion == "oncoming" and not ev.store
    assert reasons(ev)["target_speed"].passed is False


def test_same_direction_vehicle_is_moving():
    ev = run(target=lambda t: (0.0, 10.0 + 10.0 * t))        # drives 10 m ahead of us at our speed
    assert ev.verdict == pv.MOVING and ev.judgement.motion == "same_direction"
    slow = run(target=lambda t: (3.0, 30.0 + 5.0 * t), duration=5)   # we overtake a slow car (18 km/h)
    assert slow.verdict == pv.MOVING


def test_traffic_queue_when_we_stop_behind_it():
    def speed(t):
        return 36.0 if t < 2.0 else 2.0
    ev = run(target=lambda t: (0.0, 30.0), ego_speed=speed, duration=5.0)
    assert ev.verdict == pv.TRAFFIC_QUEUE and reasons(ev)["queue_ego_stop"].passed is False
    assert not ev.store
    stored = run(ParkedVehicleSettings(store_rejected_for_review=True), target=lambda t: (0.0, 30.0),
                 ego_speed=speed, duration=5.0)
    assert stored.store and stored.rejection_code == "ai_traffic_queue"


def test_queue_of_stationary_vehicles_in_our_lane():
    ev = run(target=lambda t: (0.3, 40.0), queue_peers=1)
    assert ev.verdict == pv.TRAFFIC_QUEUE and reasons(ev)["queue_peers"].value == 2
    # a peer only counts when it is stationary, in our corridor and seen at the same time
    f = ev.features
    peers = [pv.PeerSummary(f.t_start, f.t_end, True), pv.PeerSummary(f.t_start, f.t_end, False),
             pv.PeerSummary(f.t_end + 5, f.t_end + 9, True)]
    assert pv.count_queue_peers(f, peers) == 1
    # roadside cars are never a queue
    assert run(queue_peers=3).verdict == pv.PARKED


def test_off_road_from_road_snap_and_unknown_road():
    calls = []

    def snap(lat, lon):
        calls.append((lat, lon))
        return 30.0
    ev = run(snap=snap)
    assert ev.verdict == pv.OFF_ROAD and reasons(ev)["road_distance"].value == 30.0 and calls
    assert not ev.store
    assert run(ParkedVehicleSettings(store_rejected_for_review=True), snap=snap).rejection_code == "ai_off_road"
    assert run(snap=lambda lat, lon: 4.0).verdict == pv.PARKED

    def down(lat, lon):
        raise ConnectionError("osrm down")
    ev = run(snap=down)
    r = reasons(ev)["road_distance_unknown"]
    assert ev.verdict == pv.PARKED and r.passed is None and "unavailable" in r.detail
    # disabled → the snapper is not even called
    calls.clear()
    assert run(ParkedVehicleSettings(road_check=False), snap=snap).verdict == pv.PARKED and not calls
    # moving tracks never query OSRM
    run(target=lambda t: (-3.0, 60.0 - 12.0 * t), snap=snap)
    assert not calls


def test_billboard_car_is_not_grounded():
    ev = run(target=lambda t: (4.0, 40.0), lift_m=4.0)       # car picture 4 m up on a billboard
    assert ev.verdict == pv.NOT_GROUNDED and reasons(ev)["ground_contact"].passed is False
    s = ParkedVehicleSettings(store_rejected_for_review=True)
    assert run(s, target=lambda t: (4.0, 40.0), lift_m=4.0).rejection_code == "ai_not_grounded"


def test_evidence_proximity_and_ego_gate():
    short = run(duration=0.6, target=lambda t: (3.5, 15.0))
    assert short.verdict == pv.INSUFFICIENT and reasons(short)["sightings"].passed is False
    weak = run(conf=0.4)
    assert weak.verdict == pv.INSUFFICIENT and reasons(weak)["mean_confidence"].passed is False
    far = run(target=lambda t: (8.0, 160.0))
    assert far.verdict == pv.TOO_FAR and far.subtype is None and not far.store
    parked_us = run(ego_speed=lambda t: 0.0, target=lambda t: (3.5, 10.0))   # we are stopped: cannot tell
    assert parked_us.verdict == pv.INSUFFICIENT and reasons(parked_us)["ego_distance"].passed is False
    slow = run(ego_speed=lambda t: 10.0, duration=12.0, target=lambda t: (3.5, 38.0))
    assert slow.verdict == pv.INSUFFICIENT and reasons(slow)["ego_speed"].passed is False
    for verdict in (short, far, parked_us):
        assert not verdict.store
    # never stored even when keeping rejections for review
    assert not run(ParkedVehicleSettings(store_rejected_for_review=True), duration=0.6,
                   target=lambda t: (3.5, 15.0)).store


def _noisy(seed, **kw):
    """1 Hz GPS fixes with ~2.5 m noise, ±4 px box jitter."""
    import random
    rnd = random.Random(seed)
    sts, ego = scenario(fix_every=5, **kw)
    for x in sts:
        x.box = tuple(v + rnd.randint(-4, 4) for v in x.box)
    noisy, cache = [], {}
    for e in ego:
        if e.lat is not None and (e.lat, e.lon) not in cache:
            cache[(e.lat, e.lon)] = (e.lat + rnd.gauss(0, 2.5) / M_PER_DEG, e.lon + rnd.gauss(0, 2.5) / M_PER_DEG)
        noisy.append(pv.EgoSample(e.t_s, *cache.get((e.lat, e.lon), (None, None)), e.speed_kmh))
    return pv.evaluate(sts, noisy, ParkedVehicleSettings())


def test_robust_to_gps_noise_and_box_jitter():
    parked = [_noisy(i).verdict for i in range(30)]
    assert parked.count(pv.PARKED) >= 27
    oncoming = [_noisy(i, target=lambda t: (-3.0, 60.0 - 12.0 * t)).verdict for i in range(15)]
    assert set(oncoming) == {pv.MOVING}
    slow = [_noisy(i, target=lambda t: (3.0, 30.0 + 5.0 * t), duration=5).verdict for i in range(15)]
    assert slow.count(pv.MOVING) >= 13


def test_odometry_without_gps():
    ev = run(gps=False)
    assert ev.verdict == pv.PARKED and ev.features.ego_mode == "odometry" and not ev.judgement.fallback
    assert ev.features.target_lat is None and reasons(ev)["road_distance_unknown"].passed is None


def test_no_gps_no_speed_fallback_is_capped_and_blocked():
    kw = dict(gps=False, osd_speed=False, target=lambda t: (5.0, 25.0), ego_speed=lambda t: 10.8, duration=5.0)
    ev = run(**kw)
    assert ev.judgement.fallback and ev.verdict == pv.PARKED
    assert reasons(ev)["ego_motion_unknown"].passed is None
    assert ev.score <= 0.6 and ev.blockers[0][0] == "fallback_heuristics"
    assert run(ParkedVehicleSettings(fallback_score_cap=0.3), **kw).score <= 0.3
    # the blocker vetoes an auto-accept whatever the thresholds
    policy = ai_validation.Policy(mode="automatic", thresholds={"parked_vehicle": ai_validation.Thresholds(
        detector_accept=0.1, auto_accept=0.2)})
    d = ai_validation.decide(policy, "parked_vehicle", 0.9, ev.score, "confirmed", accept_blockers=ev.blockers)
    assert d.ai_decision == "review" and d.reason.startswith("fallback_heuristics")
    # oncoming by the image heuristic: box grows too fast
    fast = run(gps=False, osd_speed=False, target=lambda t: (-3.0, 60.0 - 12.0 * t))
    assert fast.verdict == pv.MOVING and reasons(fast)["growth_rate"].passed is False


def test_camera_profile_override():
    cam = CameraCalibration(horizon_y=0.52, camera_height_m=2.2)       # a truck-mounted camera
    s = ParkedVehicleSettings(camera_profiles={"LTD123AB": {"horizon_y": 0.52, "camera_height_m": 2.2}})
    assert s.camera_for("LTD123AB") == cam and s.camera_for("OTHER") == s.camera and s.camera_for(None) == s.camera
    ok = run(s, cam=cam, vehicle_id="LTD123AB")
    assert ok.verdict == pv.PARKED and ok.details()["camera_profile"] == "LTD123AB"
    default = run(s, cam=cam, vehicle_id="OTHER")
    assert default.features.ground_contact_error > ok.features.ground_contact_error
    assert default.verdict != pv.PARKED or default.score < ok.score


def test_motorcycle_disabled_by_default():
    s = ParkedVehicleSettings()
    assert "motorcycle" not in s.vehicle_classes
    assert ParkedVehicleSettings(vehicle_classes=["car", "motorcycle"]).vehicle_classes == ["car", "motorcycle"]


# ── score & severity ─────────────────────────────────────────────────────────

def test_score_is_monotonic():
    s = ParkedVehicleSettings()
    base = run()
    f = base.features

    def sc(**changes):
        g = pv.TrackFeatures(**{**f.__dict__, **changes})
        return scoring.score(g, s, pv.judge(g, s))[0]
    ref = sc()
    assert sc(mean_confidence=f.mean_confidence + 0.1) >= ref
    assert sc(sightings=f.sightings + 10) >= ref and sc(duration_s=f.duration_s + 3) >= ref
    assert sc(target_speed_kmh=0.0) >= ref >= sc(target_speed_kmh=f.target_speed_kmh + 3)
    assert sc(ego_distance_m=200.0) >= ref
    assert sc(ground_contact_ratio=1.0) >= sc(ground_contact_ratio=0.6)
    assert sc(closest_area=f.closest_area * 2) >= ref
    j = pv.judge(f, s)
    assert scoring.score(f, s, j, 1.0)[0] >= scoring.score(f, s, j, None)[0] >= scoring.score(f, s, j, 11.0)[0]
    assert 0 <= ref <= 1 and scoring.score(f, s, j)[0] == scoring.score(f, s, j)[0]     # deterministic
    # weights apply
    only_det = ParkedVehicleSettings(score_weights={"detector": 1.0})
    assert scoring.score(f, only_det, j)[0] == pytest.approx(
        (f.mean_confidence - 0.35) / 0.65, abs=1e-3)


def test_severity_levels_and_heavy_bump():
    s = ParkedVehicleSettings()
    assert severity("roadside", "car", s) == "low" and severity("roadside", "bus", s) == "medium"
    assert severity("blocking", "truck", s) == "critical" and severity(None, "car", s) is None
    s2 = ParkedVehicleSettings(heavy_vehicle_bump=0, severity_by_subtype={"roadside": "medium"})
    assert severity("roadside", "truck", s2) == "medium"


# ── settings ─────────────────────────────────────────────────────────────────

def test_settings_defaults_and_validation():
    s = ParkedVehicleSettings()
    assert (s.min_ego_distance_m, s.min_ego_speed_kmh, s.max_target_speed_kmh, s.max_world_drift_m) == (30, 15, 3, 8)
    assert (s.ground_contact_tolerance, s.max_road_distance_m, s.queue_ego_speed_kmh, s.queue_min_vehicles) == \
        (0.4, 12, 5, 2)
    assert (s.min_sightings, s.min_track_seconds, s.min_mean_confidence, s.detector_confidence) == (5, 2.5, 0.5, 0.35)
    assert (s.min_box_area_at_closest, s.hood_mask_bottom_ratio, s.fallback_score_cap) == (0.008, 0.10, 0.6)
    assert s.vehicle_classes == ["car", "bus", "truck"] and s.drive_side == "right"
    assert s.class_heights_m == {"car": 1.5, "bus": 3.0, "truck": 3.2, "motorcycle": 1.2}
    c = s.camera
    assert (c.horizon_y, c.center_offset, c.camera_height_m, c.vertical_fov_deg) == (0.45, 0.0, 1.4, 60)
    assert (c.corridor_halfwidth_at_horizon, c.corridor_halfwidth_at_bottom) == (0.08, 0.42)
    assert not s.store_rejected_for_review
    for bad in ({"vehicle_classes": ["car", "tractor"]}, {"vehicle_classes": []},
                {"mask_regions": [[0.5, 0.1, 0.2, 0.3]]}, {"mask_regions": [[0, 0, 1]]},
                {"score_weights": {"vibes": 1.0}}, {"score_weights": {"detector": 0.0}},
                {"auto_accept_score": 0.3, "auto_reject_score": 0.5}, {"drive_side": "middle"},
                {"camera": {"corridor_halfwidth_at_horizon": 0.3, "corridor_halfwidth_at_bottom": 0.1}},
                {"severity_by_subtype": {"blocking": "deadly"}}, {"review_verdicts": ["too_far"]},
                {"class_heights_m": {"car": 1.5}}, {"camera_profiles": {"X": {"horizon_y": 2}}},
                {"min_sightings": 1}):
        with pytest.raises(ValidationError):
            ParkedVehicleSettings(**bad)


def test_settings_section_registered_and_merge_patch():
    from app.services.settings_service import SECTIONS
    assert SECTIONS["parked_vehicle"] is ParkedVehicleSettings
    cur = ParkedVehicleSettings().model_dump()
    merged = ParkedVehicleSettings.merge_patch(cur, {"camera": {"horizon_y": 0.5}, "min_sightings": 7,
                                                     "score_weights": {"proximity": 0.2}})
    s = ParkedVehicleSettings(**merged)
    assert s.camera.horizon_y == 0.5 and s.camera.camera_height_m == 1.4 and s.min_sightings == 7
    assert s.score_weights["proximity"] == 0.2 and s.score_weights["detector"] == 0.25


# ── stage output ─────────────────────────────────────────────────────────────

def test_stage_details_and_storage_decisions():
    ev = run(snap=lambda lat, lon: 3.0)
    d = ev.details()
    assert d["pipeline"] == "parked_vehicle" and d["verdict"] == "parked" and d["subtype"] == "roadside"
    assert d["severity"] == "low" and d["vehicle_class"] == "car" and d["score"] == ev.score
    assert d["road_distance_m"] == 3.0 and d["target_position"]["latitude"]
    assert set(d["score_components"]) == {"detector", "evidence", "target_speed", "ego_motion", "ground_contact",
                                          "road_distance", "proximity"}
    codes = [r["code"] for r in d["reasons"]]
    assert codes[:4] == ["closest_box_area", "sightings", "track_seconds", "mean_confidence"]
    assert all({"code", "value", "threshold", "passed", "detail"} <= set(r) for r in d["reasons"])
    assert d["features"]["sightings"] == ev.features.sightings
    import json
    json.dumps(d)                                                       # JSON-able
    # moving: stored only when listed in review_verdicts
    s = ParkedVehicleSettings(store_rejected_for_review=True, review_verdicts=["moving"])
    mv = run(s, target=lambda t: (-3.0, 60.0 - 12.0 * t))
    assert mv.store and mv.rejection_code == "ai_moving_vehicle" and mv.reason_text().startswith("moving")
    assert not run(ParkedVehicleSettings(store_rejected_for_review=True),
                   target=lambda t: (-3.0, 60.0 - 12.0 * t)).store


def test_policy_thresholds_and_rule_reject():
    s = ParkedVehicleSettings()
    cards = pv.merge_policy_cards({"pothole": {"auto_accept": 0.9}}, s)
    assert cards["parked_vehicle"] == {"auto_accept": 0.8} and cards["pothole"] == {"auto_accept": 0.9}
    # a card value wins over the settings default; ai_validation override wins over both
    assert pv.merge_policy_cards({"parked_vehicle": {"auto_accept": 0.7}}, s)["parked_vehicle"]["auto_accept"] == 0.7
    from app.services.settings_service import AiValidationSettings, ValidationSettings
    ai = AiValidationSettings(type_overrides={"parked_vehicle": {"auto_accept": 0.95}})
    th = ai_validation.resolve_thresholds(ai, ValidationSettings(), cards, "parked_vehicle")
    assert th.auto_accept == 0.95 and th.sources["auto_accept"] == "override"
    p = ai_validation.Policy(mode="assisted")
    d = ai_validation.rule_reject(p, "ai_off_road", "off_road: road_distance 30 vs 12", "confirmed", 0.5)
    assert (d.status, d.ai_decision, d.rejection_code) == ("rejected", "auto_reject", "ai_off_road")
    d = ai_validation.rule_reject(ai_validation.Policy(mode="manual"), "ai_off_road", "x", "confirmed")
    assert d.status == "pending" and d.ai_decision == "review" and "suggests reject" in d.reason
    d = ai_validation.rule_reject(ai_validation.Policy(audit_sample_rate=0.5), "ai_off_road", "x", "missing", 0.1)
    assert d.audit_sample and d.status == "location_pending"
    assert pv.rules_version(s).startswith("parked_vehicle_rules:v1@")
    assert pv.rules_version(s) != pv.rules_version(ParkedVehicleSettings(min_sightings=6))


# ── tracking ─────────────────────────────────────────────────────────────────

def test_vehicle_tracker_follows_a_fast_sweeping_box():
    """A parked car passed at speed jumps across the frame between sampled
    frames: motion prediction keeps it one track."""
    boxes = [(700, 300, 760, 340), (760, 300, 830, 345), (840, 298, 930, 352), (950, 296, 1070, 362),
             (1100, 290, 1260, 375)]
    plain, pred = static_check.Tracker(), static_check.Tracker(max_gap=3, match_iou=0.1, predict=True)
    for i, b in enumerate(boxes):
        for tr in (plain, pred):
            tr.observe(static_check.Sighting(i * 5 + 1, b, 0.8), "vehicle", 5, lambda: None)
    assert len(pred.open) == 1 and len(pred.open[0].sightings) == 5
    assert len(plain.open) > 1
    # two cars side by side stay two tracks
    tr = static_check.Tracker(max_gap=3, match_iou=0.1, predict=True)
    for i in range(4):
        tr.observe(static_check.Sighting(i * 5 + 1, (100 + 10 * i, 300, 200 + 10 * i, 360), 0.8), "vehicle", 5,
                   lambda: None)
        tr.observe(static_check.Sighting(i * 5 + 1, (900 - 10 * i, 300, 1000 - 10 * i, 360), 0.8), "vehicle", 5,
                   lambda: None)
    assert len(tr.open) == 2 and all(len(t.sightings) == 4 for t in tr.open)


# ── processor glue ───────────────────────────────────────────────────────────

@pytest.fixture
def processor():
    pytest.importorskip("ultralytics")             # the worker module imports it; pure tests above do not
    pytest.importorskip("cv2")
    from app.workers import processor as proc
    proc._unload_models()
    yield proc
    proc._unload_models()


class _Boxes(list):
    pass


def _result(boxes, names):
    items = [SimpleNamespace(xyxy=[_T(b)], conf=[_T(c)], cls=[_T(k)]) for b, c, k in boxes]
    return [SimpleNamespace(names=names, boxes=items)]


class _T:
    def __init__(self, v):
        self.v = v

    def tolist(self):
        return list(self.v)

    def item(self):
        return self.v


def _ctx(processor, pvs=None, fps=25.0):
    from app.services.settings_service import ValidationSettings
    job = SimpleNamespace(id="job-1")
    return processor._JobContext(job, "/tmp/x.mp4", fps, ValidationSettings(), None, pvs=pvs)


def test_processor_vehicle_filter_masks_and_classes(processor):
    names = {0: "person", 2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}
    assert processor._vehicle_class_ids(SimpleNamespace(names=names), ["car", "bus", "truck"]) == [2, 5, 7]
    assert processor._DETECTORS["vehicles"][0] == "parked_vehicle"
    ctx = _ctx(processor)
    tracker = processor._vehicle_tracker(ctx.pv)
    assert tracker.predict and tracker.max_gap == 3
    stats = processor._Stats()
    frame = np.zeros((H, W, 3), dtype=np.uint8)
    osd = SimpleNamespace(latitude=LAT0, longitude=LON0, speed_kmh=40.0, timestamp=None, vehicle_id=None,
                          gps_interpolated=False)
    res = _result([((600, 330, 700, 400), 0.9, 2),       # car → kept
                   ((300, 330, 340, 400), 0.9, 3),       # motorcycle → disabled by default
                   ((800, 330, 900, 400), 0.2, 7),       # truck below detector_confidence
                   ((600, 20, 700, 120), 0.9, 2),        # OSD overlay band
                   ((400, 600, 900, 720), 0.9, 2)],      # own bonnet
                  names)
    processor._observe_vehicles(ctx, tracker, res, frame, 6, osd, 5, stats)
    assert len(tracker.open) == 1 and tracker.open[0].sightings[0].cls == "car"
    assert stats.vehicle_masked == 2
    s = tracker.open[0].sightings[0]
    assert isinstance(s, pv.VehicleSighting) and s.t_s == pytest.approx(0.2) and s.speed_kmh == 40.0


def test_processor_recorded_at_and_policy(processor):
    ctx = _ctx(processor)
    t0 = datetime(2026, 3, 1, 8, 30, 0)
    ctx.observe_osd(26, SimpleNamespace(timestamp=t0, latitude=None, longitude=None, speed_kmh=None,
                                        gps_interpolated=False))
    blank = SimpleNamespace(timestamp=None)
    rec = ctx.recorded_at(76, blank)                         # 2 s later, OSD time unreadable
    assert rec.tzinfo is not None and rec.replace(tzinfo=None) == datetime(2026, 3, 1, 8, 30, 2)
    assert rec.astimezone(timezone.utc).hour == 7            # Africa/Douala = UTC+1
    assert ctx.recorded_at(26, SimpleNamespace(timestamp=t0)).replace(tzinfo=None) == t0
    assert _ctx(processor).recorded_at(10, blank) is None
    from app.services.settings_service import AiValidationSettings, ValidationSettings
    policy = processor._policy_for_job(AiValidationSettings(), ValidationSettings(),
                                       ParkedVehicleSettings(auto_accept_score=0.75))
    th = policy.for_type("parked_vehicle")
    assert th.auto_accept == 0.75 and th.sources["auto_accept"] == "card"


async def test_processor_finalize_vehicle_counts_and_stores(processor, monkeypatch):
    stored = []

    async def fake_store(db, ctx, det_id, key, det_type, subtype, conf, frame_num, box, osd, img, crop, ann,
                         score=None, **kw):
        stored.append((det_type, subtype, score, kw))

    monkeypatch.setattr(processor, "_store_detection", fake_store)
    monkeypatch.setattr(processor, "upload_image", lambda data, path: f"s3://{path}")
    monkeypatch.setattr(processor, "_save_crop", lambda *a, **k: None)

    async def no_osrm(ctx, lat, lon):
        return None, "OSRM disabled in tests"
    monkeypatch.setattr(processor, "_road_distance", no_osrm)
    ctx = _ctx(processor, pvs=ParkedVehicleSettings())
    sts, ego = scenario()
    for e in ego:
        ctx.ego.add(e.t_s, e.lat, e.lon, e.speed_kmh)
    track = static_check.Track(label="vehicle", sightings=list(sts))
    best = max(sts, key=lambda x: x.conf)
    osd = SimpleNamespace(latitude=best.lat, longitude=best.lon, vehicle_id=None, speed_kmh=36.0, timestamp=None,
                          gps_interpolated=False)
    track.best = processor._Best(np.zeros((H, W, 3), dtype=np.uint8), best.frame_num, best.box, best.conf, osd)
    stats = processor._Stats()
    geo_seen = {k: [] for k in processor._DETECTORS}
    px_seen = {k: [] for k in processor._DETECTORS}
    await processor._finalize(None, ctx, "vehicles", track, geo_seen, px_seen, stats, True)
    assert stats.vehicle_verdicts == {"parked": 1} and stats.by_model == {"vehicles": 1}
    det_type, subtype, score, kw = stored[0]
    assert det_type == "parked_vehicle" and subtype == "roadside" and 0 < score <= 1
    assert kw["ai_details"]["verdict"] == "parked" and kw["verifier_model"].startswith("parked_vehicle_rules:v1@")
    assert kw["rule_reject"] is None and kw["accept_blockers"] == []
    assert stats.event_details()["vehicle_verdicts"] == {"parked": 1}
    # a moving track is only counted
    sts, _ego = scenario(target=lambda t: (-3.0, 60.0 - 12.0 * t))
    track2 = static_check.Track(label="vehicle", sightings=list(sts))
    track2.best = processor._Best(np.zeros((H, W, 3), dtype=np.uint8), sts[0].frame_num, sts[0].box, 0.8, osd)
    await processor._finalize(None, ctx, "vehicles", track2, geo_seen, px_seen, stats, True)
    assert stats.vehicle_verdicts == {"parked": 1, "moving": 1} and len(stored) == 1


def test_vehicles_slot_and_valid_models():
    from app.services import job_service, model_registry
    assert model_registry.SLOTS["vehicles"] == ("parked_vehicle", "detector")
    assert "vehicles" in job_service.VALID_MODELS and "vehicles" not in job_service.DEFAULT_MODELS
    assert job_service.parse_models(None) == ["pothole", "signs", "speedbump"]
    assert job_service.parse_models("vehicles,pothole") == ["pothole", "vehicles"]


async def test_settings_update_section_roundtrip():
    """PUT /settings/parked_vehicle goes through settings_service.update_section."""
    from app.services import settings_service

    class _DB:
        def __init__(self):
            self.rows = {}

        async def get(self, model, key):
            return self.rows.get(key)

        def add(self, row):
            self.rows[row.section] = row

        async def commit(self):
            pass

    db = _DB()
    before, after = await settings_service.update_section(
        db, "parked_vehicle", {"camera": {"horizon_y": 0.5}, "camera_profiles": {"LTD1": {"center_offset": 0.05}},
                               "store_rejected_for_review": True}, "admin")
    assert before["camera"]["horizon_y"] == 0.45 and after["camera"]["horizon_y"] == 0.5
    s = await settings_service.get_section(db, "parked_vehicle")
    assert s.camera.camera_height_m == 1.4 and s.camera_for("LTD1").center_offset == 0.05
    assert s.store_rejected_for_review
    with pytest.raises(ValidationError):
        await settings_service.update_section(db, "parked_vehicle", {"vehicle_classes": ["boat"]}, "admin")
