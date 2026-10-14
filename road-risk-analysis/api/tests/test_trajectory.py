"""
Trajectory prediction scenarios (Step 3 spec section "EXAMPLE SCENARIOS" /
"TESTING"). Fully deterministic — RoadAwareVehicleState fixtures are built
directly, no OSRM/network involved (that's Step 2's concern, already tested
in tests/test_map_matching.py).
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import settings
from app.services.osrm import geo
from app.services.osrm.models import (
    MapMatchQuality,
    RoadAwareVehicleState,
    RoadContext,
    RoadDirection,
    RoadGeometry,
    TelemetrySnapshot,
    UpcomingSegment,
)
from app.services.trajectory.models import AccelerationSource, ConfidenceLevel, TrajectoryStatus
from app.services.trajectory.predictor import predict_trajectory

NOW = datetime.now(timezone.utc)
KAMBO = (4.0412, 9.8107)  # real coordinate used elsewhere in this project's demo/testing


def _geometry_from(coords):
    return RoadGeometry(coordinates=[(lon, lat) for lat, lon in coords])


def make_state(
    vehicle_id="BUS-001",
    matched=KAMBO,
    bearing=90.0,
    speed_mps=10.0,
    heading=90.0,
    acceleration_mps2=None,
    accuracy_m=8.0,
    ts=None,
    distance_along_segment_m=50.0,
    segment_length_m=300.0,
    direction=None,
    match_quality=MapMatchQuality.GOOD,
    match_confidence=0.9,
    current_coords=None,
    upcoming=None,
    road_context=True,
) -> RoadAwareVehicleState:
    ts = ts or NOW
    telemetry = TelemetrySnapshot(
        vehicle_id=vehicle_id, latitude=matched[0], longitude=matched[1],
        speed_mps=speed_mps, heading=heading, accuracy_m=accuracy_m,
        acceleration_mps2=acceleration_mps2, timestamp=ts,
    )
    if not road_context:
        return RoadAwareVehicleState(
            vehicle_id=vehicle_id, telemetry=telemetry, road_context=None,
            match_quality=match_quality, match_confidence=match_confidence,
            match_error="no usable match", computed_at=ts,
        )

    if current_coords is None:
        start = geo.destination(*matched, (bearing + 180) % 360, distance_along_segment_m)
        end = geo.destination(*matched, bearing, segment_length_m - distance_along_segment_m)
        current_coords = [start, matched, end]

    rc = RoadContext(
        road_segment_id="seg-current",
        road_name="Route de Kambo",
        direction=direction or RoadDirection.FORWARD,
        road_bearing_deg=bearing,
        matched_latitude=matched[0],
        matched_longitude=matched[1],
        raw_to_matched_distance_m=0.0,
        distance_along_segment_m=distance_along_segment_m,
        distance_to_segment_end_m=max(0.0, segment_length_m - distance_along_segment_m),
        segment_length_m=segment_length_m,
        geometry=_geometry_from(current_coords),
        upcoming_segments=upcoming or [],
    )
    return RoadAwareVehicleState(
        vehicle_id=vehicle_id, telemetry=telemetry, road_context=rc,
        match_quality=match_quality, match_confidence=match_confidence, computed_at=ts,
    )


# ── Scenario 1 — straight road, constant speed ──────────────────────────────

def test_straight_road_constant_speed_follows_road():
    state = make_state(speed_mps=10.0, bearing=90.0, distance_along_segment_m=50, segment_length_m=1000)
    traj = predict_trajectory(state, now=NOW, horizons_s=[1, 5, 10])

    assert traj.status == TrajectoryStatus.VALID
    assert len(traj.points) == 3
    for p, t in zip(traj.points, [1, 5, 10]):
        assert p.distance_along_road_m == pytest.approx(10.0 * t, abs=0.01)
    lons = [p.longitude for p in traj.points]
    assert lons == sorted(lons)  # moving east — longitude increases monotonically


# ── Scenario 2 — curved road ─────────────────────────────────────────────────

def test_curved_road_prediction_follows_curve_not_straight_line():
    p0 = KAMBO
    p1 = geo.destination(*p0, 90.0, 100.0)   # 100m east
    p2 = geo.destination(*p1, 0.0, 100.0)    # then 100m due north
    state = make_state(
        matched=p0, distance_along_segment_m=0, segment_length_m=200,
        current_coords=[p0, p1, p2], speed_mps=20.0, bearing=90.0,
    )
    traj = predict_trajectory(state, now=NOW, horizons_s=[6])  # 120m: 20m past the bend

    pt = traj.points[0]
    expected_curved = geo.destination(*p1, 0.0, 20.0)     # correct: 20m north of the bend
    naive_straight = geo.destination(*p0, 90.0, 120.0)    # wrong: kept going east in a straight line

    assert geo.haversine_m(pt.latitude, pt.longitude, *expected_curved) < 1.0
    assert geo.haversine_m(pt.latitude, pt.longitude, *naive_straight) > 15.0


# ── Scenario 3 — acceleration ───────────────────────────────────────────────

def test_acceleration_increases_distance_beyond_constant_speed():
    const = predict_trajectory(
        make_state(speed_mps=10.0, acceleration_mps2=None, segment_length_m=1000), now=NOW, horizons_s=[5]
    ).points[0].distance_along_road_m
    accel = predict_trajectory(
        make_state(speed_mps=10.0, acceleration_mps2=2.0, segment_length_m=1000), now=NOW, horizons_s=[5]
    ).points[0].distance_along_road_m
    assert accel > const


# ── Scenario 4 — deceleration / braking to a stop ───────────────────────────

def test_deceleration_causes_distance_to_plateau_at_stop():
    # 10 m/s, braking at -5 m/s^2 -> stops after 2s having covered 10m
    state = make_state(speed_mps=10.0, acceleration_mps2=-5.0, segment_length_m=1000)
    traj = predict_trajectory(state, now=NOW, horizons_s=[1, 2, 3, 5])
    d1, d2, d3, d5 = [p.distance_along_road_m for p in traj.points]

    assert d1 == pytest.approx(10 * 1 - 0.5 * 5 * 1**2, abs=0.01)  # 7.5m
    assert d2 == pytest.approx(10.0, abs=0.01)   # fully stopped
    assert d3 == pytest.approx(10.0, abs=0.01)   # stays stopped, doesn't reverse
    assert d5 == pytest.approx(10.0, abs=0.01)
    assert traj.points[-1].speed_mps == 0.0


# ── Scenario 5 — segment transition ─────────────────────────────────────────

def test_segment_transition_continues_into_upcoming_segment():
    seg_end = geo.destination(*KAMBO, 90.0, 50.0)
    upcoming_end = geo.destination(*seg_end, 90.0, 200.0)
    upcoming_seg = UpcomingSegment(
        road_segment_id="seg-next", road_name="Avenue de la Paix",
        distance_from_vehicle_m=50.0, length_m=200.0,
        geometry=_geometry_from([seg_end, upcoming_end]),
    )
    state = make_state(
        matched=KAMBO, distance_along_segment_m=0, segment_length_m=50,
        current_coords=[KAMBO, seg_end], speed_mps=15.0, bearing=90.0,
        upcoming=[upcoming_seg],
    )
    traj = predict_trajectory(state, now=NOW, horizons_s=[5])  # 75m: 25m into the next segment

    pt = traj.points[0]
    assert pt.distance_along_road_m == pytest.approx(75.0, abs=0.01)
    assert pt.road_segment_id == "seg-next"
    assert not traj.truncated


# ── Scenario: reverse direction walks backward along the segment ───────────

def test_reverse_direction_walks_backward_along_segment():
    state = make_state(
        matched=KAMBO, distance_along_segment_m=200.0, segment_length_m=300.0,
        speed_mps=10.0, bearing=90.0, direction=RoadDirection.REVERSE,
    )
    traj = predict_trajectory(state, now=NOW, horizons_s=[5])  # 50m back toward segment start

    pt = traj.points[0]
    assert pt.distance_along_road_m == pytest.approx(50.0, abs=0.01)
    assert pt.longitude < KAMBO[1]  # moving west (backward) along an east-bearing road


# ── Missing speed / heading / acceleration ──────────────────────────────────

def test_missing_speed_assumes_stationary_not_fabricated_motion():
    traj = predict_trajectory(make_state(speed_mps=None, segment_length_m=1000), now=NOW, horizons_s=[5])
    assert traj.points[0].distance_along_road_m == 0.0
    assert traj.confidence < 1.0


def test_missing_heading_still_predicts_using_road_direction():
    state = make_state(heading=None, speed_mps=10.0, direction=RoadDirection.FORWARD, segment_length_m=1000)
    traj = predict_trajectory(state, now=NOW, horizons_s=[3])
    assert traj.status in (TrajectoryStatus.VALID, TrajectoryStatus.DEGRADED)
    assert traj.points[0].distance_along_road_m > 0


def test_missing_acceleration_falls_back_to_constant_velocity():
    state = make_state(speed_mps=12.0, acceleration_mps2=None, segment_length_m=1000)
    traj = predict_trajectory(state, now=NOW, horizons_s=[4])
    assert traj.acceleration_source == AccelerationSource.ASSUMED_ZERO
    assert traj.points[0].distance_along_road_m == pytest.approx(48.0, abs=0.01)


def test_derived_acceleration_from_previous_speed_sample():
    prev = (5.0, NOW - timedelta(seconds=2))
    state = make_state(speed_mps=9.0, acceleration_mps2=None, segment_length_m=1000, ts=NOW)
    traj = predict_trajectory(state, prev_speed_sample=prev, now=NOW, horizons_s=[1])
    assert traj.acceleration_source == AccelerationSource.DERIVED
    assert traj.acceleration_mps2 == pytest.approx((9.0 - 5.0) / 2.0, abs=0.01)


# ── GPS noise ────────────────────────────────────────────────────────────────

def test_gps_noise_reduces_confidence_but_still_predicts():
    state = make_state(match_quality=MapMatchQuality.DEGRADED, accuracy_m=40.0, speed_mps=10.0)
    traj = predict_trajectory(state, now=NOW, horizons_s=[3])
    assert traj.status == TrajectoryStatus.DEGRADED
    assert 0 < traj.confidence < 0.8
    assert len(traj.points) > 0


# ── Stale telemetry ──────────────────────────────────────────────────────────

def test_stale_telemetry_freezes_position_and_flags_status():
    old_ts = NOW - timedelta(seconds=settings.TRAJECTORY_STALE_TELEMETRY_S + 5)
    state = make_state(speed_mps=15.0, ts=old_ts, segment_length_m=1000)
    traj = predict_trajectory(state, now=NOW, horizons_s=[1, 5])

    assert traj.status == TrajectoryStatus.STALE
    assert all(p.distance_along_road_m == 0.0 for p in traj.points)
    assert traj.confidence < 0.2


# ── OSRM/road context unavailable ───────────────────────────────────────────

def test_no_road_context_returns_unavailable_not_a_crash():
    state = make_state(road_context=False, match_quality=MapMatchQuality.UNAVAILABLE)
    traj = predict_trajectory(state, now=NOW)  # must not raise
    assert traj.status == TrajectoryStatus.UNAVAILABLE
    assert traj.points == []
    assert traj.confidence == 0.0


# ── Invalid/degenerate road geometry ────────────────────────────────────────

def test_degenerate_single_point_geometry_does_not_crash():
    state = make_state(
        current_coords=[KAMBO], distance_along_segment_m=0.0, segment_length_m=0.0, speed_mps=10.0,
    )
    traj = predict_trajectory(state, now=NOW, horizons_s=[1, 5])  # must not raise
    assert traj.truncated is True
    assert traj.points == []
    assert traj.status == TrajectoryStatus.DEGRADED


# ── Multiple vehicles ────────────────────────────────────────────────────────

def test_multiple_vehicles_independent_trajectories():
    other = geo.destination(*KAMBO, 0.0, 5000.0)
    traj_a = predict_trajectory(
        make_state(vehicle_id="BUS-001", speed_mps=10.0, bearing=90.0, segment_length_m=1000),
        now=NOW, horizons_s=[5],
    )
    traj_b = predict_trajectory(
        make_state(vehicle_id="BUS-002", matched=other, speed_mps=20.0, bearing=180.0, segment_length_m=1000),
        now=NOW, horizons_s=[5],
    )

    assert traj_a.vehicle_id == "BUS-001"
    assert traj_b.vehicle_id == "BUS-002"
    assert traj_a.points[0].distance_along_road_m == pytest.approx(50.0, abs=0.01)
    assert traj_b.points[0].distance_along_road_m == pytest.approx(100.0, abs=0.01)


# ── Prediction horizon ───────────────────────────────────────────────────────

def test_prediction_horizon_uses_configured_intervals():
    state = make_state(speed_mps=10.0, segment_length_m=1000)
    traj = predict_trajectory(state, now=NOW, horizons_s=[2, 4, 6])
    assert [p.t_s for p in traj.points] == [2, 4, 6]
    assert traj.prediction_horizon_s == 6


def test_default_horizons_match_settings():
    state = make_state(speed_mps=5.0, segment_length_m=1000)
    traj = predict_trajectory(state, now=NOW)
    assert [p.t_s for p in traj.points] == sorted(settings.TRAJECTORY_HORIZONS_S)


# ── Prediction confidence ────────────────────────────────────────────────────

def test_confidence_higher_for_good_fresh_match_than_degraded_old_one():
    good = predict_trajectory(
        make_state(match_quality=MapMatchQuality.GOOD, accuracy_m=5.0, ts=NOW), now=NOW,
    )
    bad = predict_trajectory(
        make_state(
            match_quality=MapMatchQuality.DEGRADED, accuracy_m=60.0,
            ts=NOW - timedelta(seconds=8),
        ),
        now=NOW,
    )
    assert good.confidence > bad.confidence
    assert good.confidence_level == ConfidenceLevel.HIGH
    assert bad.confidence_level in (ConfidenceLevel.MEDIUM, ConfidenceLevel.LOW)


def test_speed_is_clamped_to_vehicle_type_limit():
    state = make_state(speed_mps=200.0, segment_length_m=100_000)  # absurd sensor-error speed
    traj = predict_trajectory(state, vehicle_type="private", now=NOW, horizons_s=[1])
    assert traj.initial_speed_mps <= settings.TRAJECTORY_MAX_SPEED_MPS_BY_TYPE["private"]
