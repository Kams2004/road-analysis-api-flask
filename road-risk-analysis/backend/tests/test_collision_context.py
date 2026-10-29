"""
Collision prevention — layer 2 (Context Risk Index) and the layer-1 reliability rules, as pure
functions: factors and reasons, combination, threshold widening and the one-step boost, the
stopped-vehicle (required deceleration) rule, the geometric same-road rule, speed / heading
smoothing, the local speed-limit inference, candidate capping and the calibration report.
No Redis, OSRM or database here (tests/test_collision_pipeline.py runs the whole chain).
"""
from datetime import date, datetime, timedelta, timezone

import pytest

from app.modules.m03_maps.osrm import geo
from app.modules.m08_historical.grid import grid_cell_center, grid_cell_id
from app.modules.m10_collision.collision import context as ctx
from app.modules.m10_collision.collision import detector, thresholds
from app.modules.m10_collision.collision.context_settings import ContextSettings
from app.modules.m10_collision.collision.models import ConflictRiskLevel, ConflictStatus, VehicleConflict
from app.modules.m10_collision.collision_service import closing_speed_mps, orient, select_candidates
from app.modules.m10_collision.trajectory.smoothing import smooth_heading, smooth_motion, smooth_speed

NOW = datetime(2026, 10, 29, 10, 0, tzinfo=timezone.utc)      # 11:00 in Douala — daytime
DOUALA = (4.0511, 9.7679)
CELL = 150.0
CFG = ContextSettings()


@pytest.fixture(autouse=True)
def default_thresholds():
    thresholds.set_current(None)
    yield
    thresholds.set_current(None)


def cell_at(point, **kw) -> ctx.PlaceCell:
    return ctx.PlaceCell(cell_id=grid_cell_id(*point, CELL), **kw)


# ── factors & reasons ────────────────────────────────────────────────────────

def test_no_context_is_zero():
    res = ctx.compute_context(None, DOUALA, [ctx.Party("A", 10.0, 5.0), ctx.Party("B", 10.0, 5.0)], NOW,
                              "Africa/Douala", 4.0, CFG)
    assert res.index == 0.0 and res.reasons == []


def test_history_factor_names_the_dominant_exception():
    cell = cell_at(DOUALA, history=0.8, history_events=20, history_counts={"HARD_BRAKING": 14, "SPEEDING": 6},
                   history_dominant="HARD_BRAKING")
    res = ctx.compute_context(cell, DOUALA, [], NOW, "Africa/Douala", 4.0, CFG)
    assert res.index == pytest.approx(0.35 * 0.8, abs=1e-3)
    (r,) = res.reasons
    assert r.code == "HISTORY_HARSH_BRAKING" and r.factor == "history"
    assert r.params["dominant_events"] == 14 and "14 in 12 months" in r.label


def test_hazards_count_only_within_radius_with_exact_distance():
    near = geo.destination(*DOUALA, 90.0, 60.0)
    far = geo.destination(*DOUALA, 90.0, 140.0)
    cell = cell_at(DOUALA, hazards=[("pothole", *near), ("speed_bump", *far), ("traffic_sign", *DOUALA)])
    res = ctx.compute_context(cell, DOUALA, [], NOW, "UTC", 4.0, CFG)
    codes = [r.code for r in res.reasons]
    assert codes == ["POTHOLE_NEARBY"]                         # 140 m bump and the sign do not count
    assert res.factors["hazards"] == pytest.approx(0.5)        # 1 / hazard_saturation 2
    assert res.reasons[0].params["nearest_m"] == 60


def test_reports_map_to_kinds_and_weights():
    p = geo.destination(*DOUALA, 0.0, 200.0)
    cell = cell_at(DOUALA, reports=[("accident", "ACCIDENT_REPORTED", *p), ("travaux", "ROADWORK_REPORTED", *p),
                                    ("accident", "ACCIDENT_REPORTED", *geo.destination(*DOUALA, 0.0, 400.0))])
    res = ctx.compute_context(cell, DOUALA, [], NOW, "UTC", 4.0, CFG)
    assert [r.code for r in res.reasons] == ["ACCIDENT_REPORTED", "ROADWORK_REPORTED"]   # 400 m one ignored
    assert res.factors["reports"] == 1.0                       # 1.0 + 0.8, capped


def test_vehicle_speeding_needs_more_than_tolerance():
    cell = cell_at(DOUALA, local_limit_mps=50 / 3.6, limit_samples=10)
    slow = ctx.compute_context(cell, DOUALA, [ctx.Party("A", 54 / 3.6)], NOW, "UTC", 4.0, CFG)   # +8 %
    assert slow.factors["vehicle_speeding"] == 0.0
    fast = ctx.compute_context(cell, DOUALA, [ctx.Party("A", 54 / 3.6), ctx.Party("B", 70 / 3.6)], NOW, "UTC", 4.0, CFG)
    (r,) = [r for r in fast.reasons if r.code == "VEHICLE_SPEEDING"]
    assert r.params["vehicle_id"] == "B" and r.params["excess_pct"] == 40
    assert fast.factors["vehicle_speeding"] == pytest.approx(1.0)      # 40 % = tolerance 10 + full 30


def test_no_driver_factor_and_a_stored_driver_weight_is_ignored():
    # anonymous: a party is its current trip only — no plate, no driver, no behaviour score
    assert "driver" not in ctx.Party.__dataclass_fields__ and not [f for f in ctx.Party.__dataclass_fields__
                                                                   if "driver" in f or "plate" in f]
    res = ctx.compute_context(None, DOUALA, [ctx.Party("A", 10.0), ctx.Party("B", 10.0)], NOW, "UTC", 4.0, CFG)
    assert "driver" not in res.factors and res.index == 0.0 and res.reasons == []
    # settings saved before (web console sent every factor) still load
    old = ContextSettings(weights={"history": 0.4, "driver": 0.25}, driver_window_days=90)
    assert "driver" not in old.weights and old.weights["history"] == 0.4
    merged = thresholds.CollisionSettings.merge_patch(
        {"context": {"weights": {"driver": 0.25}}}, {"context": {"weights": {"night": 0.2}}})
    assert "driver" not in thresholds.CollisionSettings(**merged).context.weights


@pytest.mark.parametrize("utc_hour,night", [(17, False), (18, True), (4, True), (5, False), (12, False)])
def test_night_uses_local_time(utc_hour, night):
    # Africa/Douala = UTC+1: 18:00 UTC is 19:00 local (night), 05:00 UTC is 06:00 (day)
    now = NOW.replace(hour=utc_hour)
    res = ctx.compute_context(None, DOUALA, [], now, "Africa/Douala", 4.0, CFG)
    assert (res.factors["night"] == 1.0) is night


def test_poor_gps_and_stale_data():
    parties = [ctx.Party("A", 10.0, accuracy_m=35.0, telemetry_age_s=1.0),
               ctx.Party("B", 10.0, accuracy_m=None, telemetry_age_s=7.0)]       # None = not reported
    res = ctx.compute_context(None, DOUALA, parties, NOW, "UTC", 4.0, CFG)
    codes = {r.code: r for r in res.reasons}
    assert codes["POOR_GPS"].params == {"vehicle_id": "A", "accuracy_m": 35}
    assert codes["STALE_DATA"].params["vehicle_id"] == "B"
    assert res.index == pytest.approx(0.2)


def test_combination_is_capped_and_sorted():
    p = geo.destination(*DOUALA, 0.0, 50.0)
    cell = cell_at(DOUALA, history=1.0, history_events=40, history_counts={"ACCIDENT": 5}, history_dominant="ACCIDENT",
                   speeding=1.0, speeding_events=30, local_limit_mps=10.0,
                   hazards=[("pothole", *p), ("pothole", *p)], reports=[("accident", "ACCIDENT_REPORTED", *p)])
    res = ctx.compute_context(cell, DOUALA, [ctx.Party("A", 20.0, 40.0, 9.0)], NOW.replace(hour=20),
                              "UTC", 4.0, CFG)
    assert res.index == 1.0
    contributions = [r.contribution for r in res.reasons]
    assert contributions == sorted(contributions, reverse=True)
    assert res.reasons[0].code == "HISTORY_ACCIDENTS"


def test_disabled_context_is_inert():
    cfg = ContextSettings(enabled=False)
    cell = cell_at(DOUALA, history=1.0, history_events=5, history_dominant="ACCIDENT")
    assert ctx.compute_context(cell, DOUALA, [], NOW, "UTC", 4.0, cfg).index == 0.0
    assert ctx.widening(1.0, cfg) == 1.0 and ctx.boost_steps(1.0, cfg) == 0


# ── widening & boost ─────────────────────────────────────────────────────────

def test_widening_is_proportional_up_to_half():
    assert ctx.widening(0.0, CFG) == 1.0
    assert ctx.widening(0.5, CFG) == pytest.approx(1.25)
    assert ctx.widening(1.0, CFG) == pytest.approx(1.5)


@pytest.mark.parametrize("physical,widened,index,expected", [
    (0, 0, 1.0, (0, 0)),          # SAFE stays SAFE — context never creates an alert
    (2, 2, 0.69, (2, 0)),         # below the boost threshold
    (2, 2, 0.70, (3, 1)),         # WARNING → HIGH_WARNING
    (2, 3, 0.75, (4, 1)),         # widened to HIGH_WARNING, boosted to CRITICAL
    (4, 4, 1.0, (4, 0)),          # CRITICAL is the ceiling
    (3, 1, 0.0, (3, 0)),          # never lowered
])
def test_raise_level(physical, widened, index, expected):
    assert ctx.raise_level(physical, widened, index, CFG) == expected


def _conflict(distance, ttc, heading=180.0, risk=None, **kw):
    risk = risk or detector._classify(distance, ttc, heading)
    return VehicleConflict(vehicle_a_id="A", vehicle_b_id="B", risk_level=risk, physical_risk_level=risk,
                           status=ConflictStatus.ACTIVE, confidence=0.9, distance_at_closest_approach_m=distance,
                           time_to_closest_approach_s=ttc, relative_heading_deg=heading, **kw)


def test_apply_context_boosts_warning_to_high_warning():
    c = _conflict(0.5, 12.0)
    assert c.risk_level == ConflictRiskLevel.MODERATE
    out = detector.apply_context(c, ctx.ContextResult(index=0.8, reasons=[]))
    assert out.risk_level == ConflictRiskLevel.HIGH and out.physical_risk_level == ConflictRiskLevel.MODERATE
    assert out.context_level_boost == 1 and out.context_index == 0.8


def test_apply_context_widening_alone():
    # 70 m at the CPA: beyond WARNING's 60 m → MONITOR; × 1.25 (C = 0.5) → 75 m → WARNING, no boost
    c = _conflict(70.0, 10.0)
    assert c.risk_level == ConflictRiskLevel.LOW
    out = detector.apply_context(c, ctx.ContextResult(index=0.5))
    assert out.risk_level == ConflictRiskLevel.MODERATE and out.context_level_boost == 0


def test_apply_context_never_creates_a_conflict():
    c = _conflict(150.0, 10.0)                      # beyond MONITOR (120 m) → NONE
    assert c.risk_level == ConflictRiskLevel.NONE
    out = detector.apply_context(c, ctx.ContextResult(index=1.0))
    assert out.risk_level == ConflictRiskLevel.NONE and out.context_level_boost == 0


def test_apply_context_does_not_rescale_the_stopped_vehicle_rule():
    c = _conflict(0.0, 3.0, heading=0.0, risk=ConflictRiskLevel.MODERATE, required_decel_mps2=3.5)
    assert detector.apply_context(c, ctx.ContextResult(index=0.6)).risk_level == ConflictRiskLevel.MODERATE
    assert detector.apply_context(c, ctx.ContextResult(index=0.9)).risk_level == ConflictRiskLevel.HIGH


# ── stopped vehicles ─────────────────────────────────────────────────────────

def test_required_deceleration():
    assert detector.required_deceleration(14.0, 40.0, 3.0) == pytest.approx(196 / 74)
    assert detector.required_deceleration(0.0, 5.0, 2.0) == 0.0
    assert detector.required_deceleration(5.0, 1.5, 2.0) == float("inf")


@pytest.mark.parametrize("decel,level", [(1.0, ConflictRiskLevel.NONE), (3.0, ConflictRiskLevel.MODERATE),
                                         (5.0, ConflictRiskLevel.HIGH), (8.0, ConflictRiskLevel.CRITICAL)])
def test_queue_risk_scale(decel, level):
    assert detector.queue_risk(decel) == level


# ── same road is geometric ───────────────────────────────────────────────────

def test_same_name_far_apart_is_not_same_road():
    assert detector.road_relation(40.0, 0.0, False, True, True) == ("unrelated", False)


def test_same_name_close_and_aligned_is_same_road():
    assert detector.road_relation(3.0, 2.0, False, True, True) == ("same_road", False)


def test_shared_segment_but_crossing_headings_is_only_connected():
    assert detector.road_relation(0.0, 90.0, True, False, True) == ("connected", False)


def test_divided_carriageway_is_detected():
    assert detector.road_relation(12.0, 178.0, False, True, True) == ("same_road", True)
    assert detector.road_relation(6.0, 175.0, False, False, True) == ("connected", True)
    assert detector.road_relation(0.0, 180.0, True, True, True) == ("same_road", False)   # single carriageway


def test_untrusted_match_far_apart_is_unknown():
    assert detector.road_relation(40.0, 180.0, False, False, False) == ("unknown", False)


# ── smoothing ────────────────────────────────────────────────────────────────

def _samples(values):
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [(t0 + timedelta(seconds=3 * i), s, h) for i, (s, h) in enumerate(values)]


def test_speed_spike_is_rejected_but_braking_kept():
    assert smooth_speed([10.0, 25.0, 11.0]) == 11.0           # spike → median
    assert smooth_speed([20.0, 12.0, 4.0]) == 4.0             # consistent braking → newest
    assert smooth_speed([10.0]) == 10.0


def test_heading_jitter_vs_turn():
    assert smooth_heading([90.0, 130.0, 92.0]) == pytest.approx(103.6, abs=1.0)   # weighted circular mean
    assert smooth_heading([0.0, 45.0, 90.0]) == 90.0          # consistent turn → newest
    assert smooth_heading([350.0, 10.0, 355.0]) % 360 == pytest.approx(359.2, abs=1.0)


def test_stopped_vehicle_keeps_last_moving_heading():
    speed, heading = smooth_motion(_samples([(8.0, 90.0), (3.0, 92.0), (0.2, 250.0)]))
    assert speed == 0.2 and heading == 92.0


def test_out_of_order_and_old_samples():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    samples = [(t0 + timedelta(seconds=6), 12.0, 90.0), (t0, 30.0, 10.0),          # 30 s old one is ignored
               (t0 + timedelta(seconds=3), 11.0, 88.0), (t0 - timedelta(seconds=30), 99.0, 0.0)]
    speed, heading = smooth_motion(samples)
    assert speed == 12.0                                         # 30, 11, 12 → median 12
    assert heading is not None


# ── local speed limit ────────────────────────────────────────────────────────

def test_local_limit_is_the_median_of_the_cell():
    cid = grid_cell_id(*DOUALA, CELL)
    out = ctx.infer_local_limits({cid: {13.89: 5, 22.22: 2}}, 3)
    assert out[cid] == (13.89, 7)


def test_local_limit_pools_neighbours_when_sparse():
    cid = grid_cell_id(*DOUALA, CELL)
    nb = ctx.neighbour_ids(cid)
    hist = {cid: {13.89: 1}, nb[0]: {13.89: 1}, nb[1]: {8.33: 1}}
    out = ctx.infer_local_limits(hist, 3)
    assert out[cid] == (13.89, 3)
    assert ctx.infer_local_limits({cid: {13.89: 1}}, 3) == {}


def test_median_from_histogram():
    assert ctx.median_from_histogram({10.0: 1, 20.0: 1}) == 15.0
    assert ctx.median_from_histogram({10.0: 2, 20.0: 1}) == 10.0
    assert ctx.median_from_histogram({}) is None


# ── place cells from synthetic fleet exceptions ──────────────────────────────

def test_harsh_braking_counts_double_and_fades_with_age():
    p = DOUALA
    q = geo.destination(*DOUALA, 90.0, 1000.0)
    rows = [ctx.EventAgg(*p, "HARD_BRAKING", "medium", date(2026, 10, 28), 4),
            ctx.EventAgg(*q, "HARD_ACCELERATION", "medium", date(2026, 10, 28), 4),
            ctx.EventAgg(*q, "HARD_BRAKING", "medium", date(2025, 11, 1), 4)]       # ~1 year old
    hist = ctx.build_history(rows, CFG, lambda s: 1.0, NOW, CELL)
    hp, hq = hist[grid_cell_id(*p, CELL)], hist[grid_cell_id(*q, CELL)]
    assert hp["points"] == pytest.approx(4 * 3.0, rel=0.01)
    assert hq["type_points"]["HARD_ACCELERATION"] == pytest.approx(4 * 1.2, rel=0.01)
    assert hq["type_points"]["HARD_BRAKING"] < 4 * 3.0 * 0.1                # ~4 half-lives old
    cells = ctx.build_place_cells(hist, {}, {}, {}, CFG)
    assert cells[grid_cell_id(*p, CELL)].history_dominant == "HARD_BRAKING"
    assert cells[grid_cell_id(*p, CELL)].history == pytest.approx(12 / 20, abs=0.01)


def test_spread_points_reaches_neighbour_cells():
    cid = grid_cell_id(*DOUALA, CELL)
    lat, lon = grid_cell_center(cid)
    edge = geo.destination(lat, lon, 90.0, 70.0)                              # near the east edge
    spread = ctx.spread_points([("pothole", *edge)], 100.0, CELL)
    assert cid in spread and len(spread) >= 2


def test_max_cells_keeps_the_riskiest():
    hist = {}
    for i in range(5):
        p = geo.destination(*DOUALA, 0.0, 400.0 * i)
        hist.update(ctx.build_history([ctx.EventAgg(*p, "ACCIDENT", "high", date(2026, 10, 1), i + 1)],
                                      CFG, lambda s: 1.0, NOW, CELL))
    cells = ctx.build_place_cells(hist, {}, {}, {}, ContextSettings(max_cells=100))
    assert len(cells) == 5
    top = ctx.build_place_cells(hist, {}, {}, {}, ContextSettings(max_cells=100).model_copy(update={"max_cells": 2}))
    assert sorted(c.history_events for c in top.values()) == [4, 5]


# ── candidates ───────────────────────────────────────────────────────────────

def _live(vid, point, speed, heading, dist):
    return {"vehicle_id": vid, "latitude": point[0], "longitude": point[1], "speed_mps": speed,
            "heading": heading, "distance_m": dist}


def test_closing_speed():
    own = _live("A", DOUALA, 10.0, 90.0, 0)
    ahead = geo.destination(*DOUALA, 90.0, 200.0)
    assert closing_speed_mps(own, _live("B", ahead, 10.0, 270.0, 200)) == pytest.approx(20.0, abs=0.1)
    assert closing_speed_mps(own, _live("B", ahead, 15.0, 90.0, 200)) == pytest.approx(-5.0, abs=0.1)
    assert closing_speed_mps(own, _live("B", ahead, 10.0, None, 200)) is None


def test_candidates_keep_approaching_and_close_ones_capped():
    own = _live("A", DOUALA, 10.0, 90.0, 0)
    east = lambda d: geo.destination(*DOUALA, 90.0, d)      # noqa: E731
    cands = [
        _live("away", east(400), 20.0, 90.0, 400),         # faster, same way → separating, far → cleared
        _live("near", east(50), 20.0, 90.0, 50),           # separating but within MONITOR reach → kept
        _live("oncoming", east(500), 10.0, 270.0, 500),    # approaching → kept
        _live("unknown", east(450), None, None, 450),      # unknown motion → kept (conservative)
    ]
    keep, cleared = select_candidates(own, cands, 180.0, 30)
    assert [c["vehicle_id"] for c in keep] == ["near", "unknown", "oncoming"]
    assert cleared == ["away"]
    keep, _ = select_candidates(own, cands, 180.0, 2)
    assert [c["vehicle_id"] for c in keep] == ["near", "unknown"]


def test_orient_swaps_the_pair():
    c = _conflict(5.0, 2.0, road_type_a="primary", road_type_b=None)
    o = orient(c, "B")
    assert (o.vehicle_a_id, o.vehicle_b_id, o.road_type_a, o.road_type_b) == ("B", "A", None, "primary")
    assert orient(c, "A") is c


# ── calibration ──────────────────────────────────────────────────────────────

def test_calibration_report_on_synthetic_cells():
    index = {f"c{i}": 0.6 + 0.01 * i for i in range(10)}
    index.update({f"q{i}": 0.05 * (i % 4) for i in range(30)})
    outcomes = {f"c{i}": 6 for i in range(10)}
    outcomes.update({"q1": 1, "q2": 2})
    rep = ctx.calibration_report(index, outcomes, 5)
    assert rep["hot_cells"] == 10 and rep["other_cells"] == 30
    assert rep["mean_index_hot"] > rep["mean_index_other"]
    assert rep["auc"] == 1.0 and rep["verdict"] == "ok"
    flat = ctx.calibration_report({k: 0.3 for k in index}, outcomes, 5)
    assert flat["auc"] == 0.5 and flat["verdict"] == "weak"
    assert ctx.calibration_report({}, {}, 5)["verdict"] == "insufficient data"


# ── settings ─────────────────────────────────────────────────────────────────

def test_context_settings_validate():
    with pytest.raises(ValueError):
        ContextSettings(weights={"history": 2.0})
    with pytest.raises(ValueError):
        ContextSettings(weights={"weather": 0.1})
    with pytest.raises(ValueError):
        ContextSettings(report_kinds={"accident": "SOMETHING"})
    cfg = ContextSettings(weights={"history": 0.5})
    assert cfg.weights["history"] == 0.5 and cfg.weights["reports"] == 0.30       # missing ones defaulted
    with pytest.raises(ValueError):
        thresholds.CollisionSettings(queue_comfort_decel_mps2=5.0, queue_high_decel_mps2=4.0)


def test_partial_context_patch_keeps_the_rest():
    current = thresholds.CollisionSettings(context=ContextSettings(max_widening=0.3)).model_dump()
    merged = thresholds.CollisionSettings.merge_patch(current, {"context": {"weights": {"night": 0.0}}})
    cfg = thresholds.CollisionSettings(**merged)
    assert cfg.context.max_widening == 0.3 and cfg.context.weights["night"] == 0.0
    assert cfg.context.weights["history"] == 0.35
