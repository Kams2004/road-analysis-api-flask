"""
Step 4 (conflict/collision-risk) scenarios. Fully deterministic — Trajectory
fixtures are built directly, no OSRM/network/Redis involved (those are
Steps 2/3's concern, already tested elsewhere; the CPA math and risk
classification here are pure functions of two Trajectory objects).
"""
from datetime import datetime, timezone

import pytest

from app.modules.m10_collision.collision import detector
from app.modules.m10_collision.collision.models import ConflictRiskLevel, ConflictStatus
from app.modules.m03_maps.osrm import geo
from app.modules.m10_collision.trajectory.models import (
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


def make_trajectory(vehicle_id, points, status=TrajectoryStatus.VALID, confidence=0.9, road_segment_id="seg-1", **extra):
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
        **extra,
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
    b_start = geo.destination(*KAMBO, 90.0, 40.0)
    points_a = [pt(t, geo.destination(*KAMBO, 90.0, 10 * t)) for t in [1, 2, 3]]
    points_b = [pt(t, geo.destination(*b_start, 270.0, 10 * t)) for t in [1, 2, 3]]

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


# ── Road Intelligence Broker: dampen confirmed-unrelated-road false
# positives (no elevation data anywhere in this system — an overpass and
# the road beneath it are otherwise indistinguishable from a head-on) —
# without ever dampening two vehicles that genuinely share a segment, which
# specifically includes meeting head-on on a single-track road carrying
# both directions at once. ───────────────────────────────────────────────

def test_different_segments_on_the_same_line_are_still_connected():
    # Segment ids differ (OSRM step ids are window-dependent), but the
    # predicted paths lie on the same line — same road, full severity.
    b_start = geo.destination(*KAMBO, 90.0, 60.0)
    points_a = [pt(t, geo.destination(*KAMBO, 90.0, 10 * t), seg="seg-a") for t in HORIZONS]
    points_b = [pt(t, geo.destination(*b_start, 270.0, 10 * t), seg="seg-b") for t in HORIZONS]

    conflict = detector.assess_conflict(
        "A", make_trajectory("A", points_a, confidence=0.9, road_segment_id="seg-a"),
        "B", make_trajectory("B", points_b, confidence=0.9, road_segment_id="seg-b"),
    )

    assert conflict.road_relation == "connected"
    assert conflict.risk_level == ConflictRiskLevel.CRITICAL


def test_different_segments_low_confidence_does_not_cap_severity():
    # Same physically-unrelated-roads geometry, but confidence is too low to
    # trust the "different segment" reading (could be a noisy/degraded
    # match, not a genuinely different road) — must fall through unchanged
    # rather than risk silencing a real conflict on unreliable data.
    b_start = geo.destination(*KAMBO, 90.0, 60.0)
    points_a = [pt(t, geo.destination(*KAMBO, 90.0, 10 * t), seg="seg-a") for t in HORIZONS]
    points_b = [pt(t, geo.destination(*b_start, 270.0, 10 * t), seg="seg-b") for t in HORIZONS]

    conflict = detector.assess_conflict(
        "A", make_trajectory("A", points_a, confidence=0.3, road_segment_id="seg-a"),
        "B", make_trajectory("B", points_b, confidence=0.9, road_segment_id="seg-b"),
    )

    assert conflict.risk_level == ConflictRiskLevel.CRITICAL  # uncapped


def test_single_track_road_head_on_meeting_stays_critical():
    # The case this fix must NOT touch: two vehicles on the SAME segment,
    # travelling head-on towards each other — the normal situation on a
    # single-track road that carries both directions, not a false positive.
    # same_current_segment=True must keep this at full, uncapped severity.
    b_start = geo.destination(*KAMBO, 90.0, 60.0)
    points_a = [pt(t, geo.destination(*KAMBO, 90.0, 10 * t), seg="seg-shared") for t in HORIZONS]
    points_b = [pt(t, geo.destination(*b_start, 270.0, 10 * t), seg="seg-shared") for t in HORIZONS]

    conflict = detector.assess_conflict(
        "A", make_trajectory("A", points_a, confidence=0.9, road_segment_id="seg-shared"),
        "B", make_trajectory("B", points_b, confidence=0.9, road_segment_id="seg-shared"),
    )

    assert conflict.same_current_segment is True
    assert conflict.risk_level == ConflictRiskLevel.CRITICAL  # untouched by the dampening logic


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


# ── Genuine-conflict gates (false red alerts for vehicles moving away, on
# another road, or passing in the opposite lane) ──────────────────────────

def _line(start, bearing, speed, seg, ts=HORIZONS):
    return [pt(t, geo.destination(*start, bearing, speed * t), speed=speed, seg=seg) for t in ts]


def test_diverging_vehicles_never_alert():
    # 10 m apart and driving away from each other on the same road: the
    # old detector clamped the CPA to the first horizon sample (t=1 s) and
    # the TTC band made it CRITICAL.
    b_start = geo.destination(*KAMBO, 90.0, 10.0)
    a = make_trajectory("A", _line(KAMBO, 270.0, 10, "s"), road_name="Rue A", match_quality="GOOD")
    b = make_trajectory("B", _line(b_start, 90.0, 10, "s"), road_name="Rue A", match_quality="GOOD")

    conflict = detector.assess_conflict("A", a, "B", b)

    assert conflict.risk_level == ConflictRiskLevel.NONE
    assert conflict.separating is True
    assert conflict.clear_immediately is True


def test_parallel_different_roads_do_not_alert():
    # Head-on geometry but on two parallel roads 25 m apart, both well matched.
    b_road = geo.destination(*KAMBO, 0.0, 25.0)
    b_start = geo.destination(*b_road, 90.0, 60.0)
    a = make_trajectory("A", _line(KAMBO, 90.0, 10, "seg-a"), road_segment_id="seg-a",
                        road_name="Rue A", match_quality="GOOD")
    b = make_trajectory("B", _line(b_start, 270.0, 10, "seg-b"), road_segment_id="seg-b",
                        road_name="Rue B", match_quality="GOOD")

    conflict = detector.assess_conflict("A", a, "B", b)

    assert conflict.road_relation == "unrelated"
    assert conflict.risk_level == ConflictRiskLevel.NONE


def test_opposite_direction_passing_with_lateral_offset_does_not_alert():
    # Same road, opposite directions, each keeping 1.8 m to its own right
    # (3.6 m apart) with good GPS accuracy — normal oncoming traffic.
    b_start = geo.destination(*KAMBO, 90.0, 60.0)
    a = make_trajectory("A", _line(KAMBO, 90.0, 10, "s"), road_segment_id="s", road_name="Rue A",
                        match_quality="GOOD", lateral_offset_m=1.8, gps_accuracy_m=4.0)
    b = make_trajectory("B", _line(b_start, 270.0, 10, "s"), road_segment_id="s", road_name="Rue A",
                        match_quality="GOOD", lateral_offset_m=1.8, gps_accuracy_m=4.0)

    conflict = detector.assess_conflict("A", a, "B", b)

    assert conflict.encounter == "head_on"
    assert conflict.lateral_separation_m == pytest.approx(3.6, abs=0.01)
    assert conflict.risk_level == ConflictRiskLevel.NONE


def test_passing_offset_untrusted_with_poor_gps_accuracy():
    b_start = geo.destination(*KAMBO, 90.0, 60.0)
    a = make_trajectory("A", _line(KAMBO, 90.0, 10, "s"), road_segment_id="s",
                        lateral_offset_m=1.8, gps_accuracy_m=30.0)
    b = make_trajectory("B", _line(b_start, 270.0, 10, "s"), road_segment_id="s",
                        lateral_offset_m=1.8, gps_accuracy_m=4.0)

    conflict = detector.assess_conflict("A", a, "B", b)

    assert conflict.lateral_separation_m is None
    assert conflict.risk_level == ConflictRiskLevel.CRITICAL


def test_head_on_same_lane_is_critical():
    # Same road, opposite directions, both on the same side (A 1.5 m right
    # of its travel, B 1.5 m LEFT of its own = same lane) → 0 m apart.
    b_start = geo.destination(*KAMBO, 90.0, 60.0)
    a = make_trajectory("A", _line(KAMBO, 90.0, 10, "s"), road_segment_id="s", road_name="Rue A",
                        match_quality="GOOD", lateral_offset_m=1.5, gps_accuracy_m=4.0)
    b = make_trajectory("B", _line(b_start, 270.0, 10, "s"), road_segment_id="s", road_name="Rue A",
                        match_quality="GOOD", lateral_offset_m=-1.5, gps_accuracy_m=4.0)

    conflict = detector.assess_conflict("A", a, "B", b)

    assert conflict.lateral_separation_m == pytest.approx(0.0, abs=0.01)
    assert conflict.risk_level == ConflictRiskLevel.CRITICAL
    assert conflict.time_to_closest_approach_s == pytest.approx(3.0, abs=0.2)


def test_crossing_at_intersection_alerts():
    # A heading east, B heading north, both 30 m from the junction at 10 m/s.
    junction = geo.destination(*KAMBO, 90.0, 30.0)
    b_start = geo.destination(*junction, 180.0, 30.0)
    a = make_trajectory("A", _line(KAMBO, 90.0, 10, "seg-a"), road_segment_id="seg-a",
                        road_name="Rue A", match_quality="GOOD")
    b = make_trajectory("B", _line(b_start, 0.0, 10, "seg-b"), road_segment_id="seg-b",
                        road_name="Rue B", match_quality="GOOD")

    conflict = detector.assess_conflict("A", a, "B", b)

    assert conflict.encounter == "crossing"
    assert conflict.road_relation == "connected"
    assert conflict.risk_level == ConflictRiskLevel.CRITICAL
    assert conflict.time_to_closest_approach_s == pytest.approx(3.0, abs=0.2)


def test_following_and_closing_alerts():
    # A follows B 20 m behind, 6 m/s faster → gap closes in ~3.3 s.
    b_start = geo.destination(*KAMBO, 90.0, 20.0)
    a = make_trajectory("A", _line(KAMBO, 90.0, 14, "s"), road_name="Rue A", match_quality="GOOD")
    b = make_trajectory("B", _line(b_start, 90.0, 8, "s"), road_name="Rue A", match_quality="GOOD")

    conflict = detector.assess_conflict("A", a, "B", b)

    assert conflict.encounter == "same_direction"
    assert conflict.separating is False
    assert conflict.closing_speed_mps == pytest.approx(6.0, abs=0.3)
    assert conflict.risk_level == ConflictRiskLevel.CRITICAL


def test_following_not_closing_does_not_alert():
    # Leader faster than follower — the gap grows.
    b_start = geo.destination(*KAMBO, 90.0, 20.0)
    a = make_trajectory("A", _line(KAMBO, 90.0, 8, "s"), road_name="Rue A", match_quality="GOOD")
    b = make_trajectory("B", _line(b_start, 90.0, 12, "s"), road_name="Rue A", match_quality="GOOD")

    conflict = detector.assess_conflict("A", a, "B", b)

    assert conflict.separating is True
    assert conflict.risk_level == ConflictRiskLevel.NONE


def test_critical_needs_both_short_ttc_and_small_cpa():
    # Head-on but 130 m apart at 10 m/s each: CPA 0 m but at t=6.5 s →
    # TTC too long for CRITICAL (4 s) → HIGH_WARNING band (≤30 m, ≤8 s).
    b_start = geo.destination(*KAMBO, 90.0, 130.0)
    a = make_trajectory("A", _line(KAMBO, 90.0, 10, "s"))
    b = make_trajectory("B", _line(b_start, 270.0, 10, "s"))

    conflict = detector.assess_conflict("A", a, "B", b)

    assert conflict.risk_level == ConflictRiskLevel.HIGH


def test_new_settings_keys_have_defaults_and_validate():
    from app.modules.m10_collision.collision import thresholds
    cfg = thresholds.CollisionSettings()
    for key in ("min_closing_speed_mps", "max_horizon_s", "road_link_margin_m", "unrelated_monitor_distance_m",
                "lateral_margin_m", "lateral_max_accuracy_m", "deescalate_after_s", "resolve_after_s"):
        assert getattr(cfg, key) is not None
    # legacy distance_ttc_s is folded into a single TTC limit per level
    assert cfg.levels["CRITICAL"].distance_ttc_s is None
    assert cfg.levels["CRITICAL"].max_ttc_s == 4.0
