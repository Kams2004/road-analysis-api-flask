"""
Step 4 (conflict/collision-risk) scenarios. Fully deterministic — Trajectory
fixtures are built directly, no OSRM/network/Redis involved (those are
Steps 2/3's concern, already tested elsewhere; the CPA math and risk
classification here are pure functions of two Trajectory objects).
"""
from datetime import datetime, timezone

import pytest

from app.services.collision import detector
from app.services.collision.models import ConflictRiskLevel, ConflictStatus
from app.services.osrm import geo
from app.services.trajectory.models import (
    AccelerationSource,
    ConfidenceLevel,
    Trajectory,
    TrajectoryPoint,
    TrajectoryStatus,
)

NOW = datetime.now(timezone.utc)
KAMBO = (4.0412, 9.8107)  # real coordinate used elsewhere in this project's demo/testing
HORIZONS = [1, 2, 3, 5, 8, 10, 15]


def pt(t, latlon, speed=10.0, seg="seg-1"):
    lat, lon = latlon
    return TrajectoryPoint(t_s=t, latitude=lat, longitude=lon, distance_along_road_m=speed * t, speed_mps=speed, road_segment_id=seg)


def make_trajectory(vehicle_id, points, status=TrajectoryStatus.VALID, confidence=0.9, road_segment_id="seg-1"):
    return Trajectory(
        vehicle_id=vehicle_id,
        generated_at=NOW,
        status=status,
        confidence=confidence,
        confidence_level=ConfidenceLevel.HIGH if confidence >= 0.7 else (
            ConfidenceLevel.MEDIUM if confidence >= 0.4 else ConfidenceLevel.LOW
        ),
        telemetry_timestamp=NOW,
        telemetry_age_s=0.0,
        initial_speed_mps=10.0,
        acceleration_mps2=0.0,
        acceleration_source=AccelerationSource.ASSUMED_ZERO,
        road_segment_id=road_segment_id,
        road_direction="FORWARD",
        prediction_horizon_s=15.0,
        truncated=False,
        points=points,
    )


# ── CPA math (unit-level, pinned against hand-computed physics) ────────────

def test_cpa_in_interval_matches_simple_closing_physics():
    # A moving east at 10 m/s; B, 100m ahead, moving west (toward A) at 10 m/s.
    # Closing speed 20 m/s over a 100m gap -> meet at t=5s. Over the [0,1]
    # interval tested here they're still closing throughout, so the nearest
    # approach within the interval is at its very end (t_star=1s), with a
    # gap of 100 - 20*1 = 80m remaining.
    a1 = KAMBO
    a2 = geo.destination(*KAMBO, 90.0, 10.0)
    b_far = geo.destination(*KAMBO, 90.0, 100.0)
    b1 = b_far
    b2 = geo.destination(*b_far, 270.0, 10.0)

    t_star, distance, _point, closing_speed = detector._cpa_in_interval(a1, a2, b1, b2, 1.0, KAMBO[0])

    assert t_star == pytest.approx(1.0, abs=0.01)
    assert distance == pytest.approx(80.0, abs=1.0)
    assert closing_speed == pytest.approx(20.0, abs=0.5)  # 10 m/s each, head-on


# ── Scenario: head-on collision course ──────────────────────────────────────

def test_head_on_collision_course_is_critical():
    # 60m gap, both closing at 10 m/s -> meet at t=3s, well inside the
    # CRITICAL time-to-closest-approach threshold.
    b_start = geo.destination(*KAMBO, 90.0, 60.0)
    points_a = [pt(t, geo.destination(*KAMBO, 90.0, 10 * t)) for t in HORIZONS]
    points_b = [pt(t, geo.destination(*b_start, 270.0, 10 * t)) for t in HORIZONS]

    conflict = detector.assess_conflict("A", make_trajectory("A", points_a), "B", make_trajectory("B", points_b))

    assert conflict is not None
    assert conflict.risk_level == ConflictRiskLevel.CRITICAL
    assert conflict.status == ConflictStatus.ACTIVE
    assert conflict.distance_at_closest_approach_m < 1.0
    assert conflict.time_to_closest_approach_s == pytest.approx(3.0, abs=0.2)
    # both closing head-on at 10 m/s -> ~20 m/s closing speed, ~180° relative heading
    assert conflict.closing_speed_mps == pytest.approx(20.0, abs=1.0)
    assert conflict.relative_heading_deg == pytest.approx(180.0, abs=2.0)
    assert conflict.relative_speed_mps == pytest.approx(0.0, abs=0.01)  # same declared initial speed on both sides


# ── Scenario: vehicles never come close ─────────────────────────────────────

def test_vehicles_far_apart_the_whole_time_is_no_conflict():
    far = geo.destination(*KAMBO, 90.0, 5000.0)
    points_a = [pt(t, geo.destination(*KAMBO, 90.0, 10 * t)) for t in HORIZONS]
    points_b = [pt(t, geo.destination(*far, 90.0, 10 * t)) for t in HORIZONS]

    conflict = detector.assess_conflict("A", make_trajectory("A", points_a), "B", make_trajectory("B", points_b))

    assert conflict.risk_level == ConflictRiskLevel.NONE


# ── Scenario: safe following distance (same road, same direction) ─────────

def test_safe_following_distance_not_flagged_high_risk():
    gap = 80.0  # above the MODERATE threshold, below LOW's cutoff
    b_start = geo.destination(*KAMBO, 90.0, gap)
    points_a = [pt(t, geo.destination(*KAMBO, 90.0, 10 * t)) for t in HORIZONS]
    points_b = [pt(t, geo.destination(*b_start, 90.0, 10 * t)) for t in HORIZONS]  # same speed & direction -> constant gap

    conflict = detector.assess_conflict("A", make_trajectory("A", points_a), "B", make_trajectory("B", points_b))

    assert conflict.risk_level in (ConflictRiskLevel.LOW, ConflictRiskLevel.NONE)
    assert conflict.distance_at_closest_approach_m == pytest.approx(gap, abs=1.0)


# ── Missing/unavailable trajectory handled gracefully ───────────────────────

def test_unavailable_trajectory_does_not_crash():
    traj_a = make_trajectory("A", [pt(1, KAMBO)])
    traj_b = make_trajectory("B", [], status=TrajectoryStatus.UNAVAILABLE)

    conflict = detector.assess_conflict("A", traj_a, "B", traj_b)

    assert conflict.status == ConflictStatus.UNAVAILABLE
    assert conflict.risk_level == ConflictRiskLevel.NONE


def test_self_comparison_returns_none():
    traj = make_trajectory("A", [pt(1, KAMBO)])
    assert detector.assess_conflict("A", traj, "A", traj) is None


def test_no_shared_horizon_is_unavailable():
    traj_a = make_trajectory("A", [pt(1, KAMBO)])
    traj_b = make_trajectory("B", [pt(2, geo.destination(*KAMBO, 0.0, 50.0))])  # no overlapping t_s

    conflict = detector.assess_conflict("A", traj_a, "B", traj_b)

    assert conflict.status == ConflictStatus.UNAVAILABLE
    assert conflict.reason is not None


# ── Low confidence suppresses an otherwise-reportable conflict ─────────────

def test_low_confidence_suppresses_report_despite_proximity():
    # Both essentially stationary at the same point — geometrically this
    # would be CRITICAL, but one side's trajectory confidence is far too low
    # to trust.
    points_a = [pt(t, geo.destination(*KAMBO, 90.0, 1 * t)) for t in [1, 2, 3]]
    points_b = [pt(t, geo.destination(*KAMBO, 90.0, 1 * t)) for t in [1, 2, 3]]

    conflict = detector.assess_conflict(
        "A", make_trajectory("A", points_a, confidence=0.05),
        "B", make_trajectory("B", points_b, confidence=0.9),
    )

    assert conflict.status == ConflictStatus.UNAVAILABLE
    assert conflict.risk_level == ConflictRiskLevel.NONE


# ── Segment overlap annotation ──────────────────────────────────────────────

def test_segment_overlap_flag_set_when_paths_share_a_segment():
    b_start = geo.destination(*KAMBO, 90.0, 60.0)
    points_a = [pt(t, geo.destination(*KAMBO, 90.0, 10 * t), seg="seg-shared") for t in [1, 2, 3]]
    points_b = [pt(t, geo.destination(*b_start, 270.0, 10 * t), seg="seg-shared") for t in [1, 2, 3]]

    conflict = detector.assess_conflict(
        "A", make_trajectory("A", points_a, road_segment_id="seg-shared"),
        "B", make_trajectory("B", points_b, road_segment_id="seg-shared"),
    )

    assert conflict.risk_level != ConflictRiskLevel.NONE
    assert conflict.same_current_segment is True
    assert conflict.segment_overlap_ahead is True


def test_different_segments_not_flagged_as_overlapping():
    b_start = geo.destination(*KAMBO, 90.0, 60.0)
    points_a = [pt(t, geo.destination(*KAMBO, 90.0, 10 * t), seg="seg-a") for t in [1, 2, 3]]
    points_b = [pt(t, geo.destination(*b_start, 270.0, 10 * t), seg="seg-b") for t in [1, 2, 3]]

    conflict = detector.assess_conflict(
        "A", make_trajectory("A", points_a, road_segment_id="seg-a"),
        "B", make_trajectory("B", points_b, road_segment_id="seg-b"),
    )

    assert conflict.same_current_segment is False
    assert conflict.segment_overlap_ahead is False


# ── Degraded/stale trajectories still get assessed, just flagged ───────────

def test_degraded_trajectory_marks_conflict_degraded_not_unavailable():
    b_start = geo.destination(*KAMBO, 90.0, 60.0)
    points_a = [pt(t, geo.destination(*KAMBO, 90.0, 10 * t)) for t in HORIZONS]
    points_b = [pt(t, geo.destination(*b_start, 270.0, 10 * t)) for t in HORIZONS]

    conflict = detector.assess_conflict(
        "A", make_trajectory("A", points_a, status=TrajectoryStatus.DEGRADED),
        "B", make_trajectory("B", points_b),
    )

    assert conflict.risk_level == ConflictRiskLevel.CRITICAL
    assert conflict.status == ConflictStatus.DEGRADED
