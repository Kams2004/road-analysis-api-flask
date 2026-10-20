"""
Step 4 core: pairwise conflict/risk assessment between two vehicles'
predicted trajectories (Step 3 output). Pure, no I/O — takes two Trajectory
objects plus settings-driven thresholds, returns a VehicleConflict or None.
Testable in isolation the same way Steps 2/3's core logic is; nothing here
calls OSRM, Redis, or Postgres (see app/services/collision_service.py for
the orchestration that feeds this from live state).

Closest point of approach (CPA)
────────────────────────────────
Each trajectory is a handful of discrete (t, position) samples at the
shared TRAJECTORY_HORIZONS_S offsets (fewer if one side truncated — see
Step 3's map_matching/predictor). Snapping "time/distance of closest
approach" to whichever discrete sample happens to be smallest would be
coarse — two vehicles converging *between* two samples could have their
true closest moment missed entirely, and their distance at that moment
underestimated as "not a conflict" when it actually is one.

Instead, for each interval between a pair of samples both trajectories
share, this treats each vehicle's motion over that short interval as
locally linear (constant velocity — a fair approximation over a few
seconds) and solves the closed-form time of minimum relative distance
between two moving points — standard closest-point-of-approach kinematics,
the same idea used for ship/aircraft conflict detection. The best
(smallest-distance) interval across the whole shared horizon is reported.

Genuine-conflict gates (added after false red alerts for vehicles moving
away / on another road / passing in the opposite lane)
─────────────────────────────────────────────────────────────────────────
Proximity alone is not a conflict. A pair is only rated when ALL hold:
  1. the CPA lies strictly in the future (within max_horizon_s) and the
     vehicles approach it at ≥ min_closing_speed_mps — a pair whose
     separation is not decreasing is `separating` → NONE;
  2. their predicted road paths are connected (same road name / shared
     segment / paths within road_link_margin_m, i.e. same road or a junction)
     when both are reliably map-matched — otherwise `unrelated` → NONE
     (MONITOR at most if the CPA is within unrelated_monitor_distance_m);
  3. an opposite-direction pair on the same road is not simply passing in its
     own lane: lateral separation (raw GPS vs centreline, trusted only with
     good GPS accuracy) ≥ lateral_margin_m → NONE;
then severity needs BOTH distance-at-CPA and TTC within a level's limits.
"""
import logging
import math
from typing import List, Optional, Set, Tuple

from app.services.collision import thresholds
from app.services.collision.models import ConflictRiskLevel, ConflictStatus, VehicleConflict
from app.services.osrm import geo
from app.services.trajectory.models import Trajectory, TrajectoryStatus

logger = logging.getLogger(__name__)

LatLon = Tuple[float, float]

_UNUSABLE_STATUSES = (TrajectoryStatus.UNAVAILABLE,)
_DEGRADING_STATUSES = (TrajectoryStatus.DEGRADED, TrajectoryStatus.STALE)

_SEVERITY_RANK = {
    ConflictRiskLevel.NONE: 0,
    ConflictRiskLevel.LOW: 1,
    ConflictRiskLevel.MODERATE: 2,
    ConflictRiskLevel.HIGH: 3,
    ConflictRiskLevel.CRITICAL: 4,
}
_TRUSTED_MATCH = (None, "GOOD", "DEGRADED")   # None = trajectory predates the field
_FUTURE_EPS_S = 0.05


def _cpa_in_interval(
    a1: LatLon, a2: LatLon, b1: LatLon, b2: LatLon, dt: float, ref_lat: float,
) -> Tuple[float, float, LatLon, float]:
    """Locally-linear closest point of approach within [0, dt] seconds of the
    interval's start. Returns (t_offset_s, distance_m, approx_conflict_point,
    closing_speed_mps) — the last is the magnitude of the relative velocity
    over this interval, a free byproduct of the same calculation (see
    VehicleConflict.closing_speed_mps)."""
    ax1, ay1 = geo.local_xy_m(*a1, ref_lat)
    ax2, ay2 = geo.local_xy_m(*a2, ref_lat)
    bx1, by1 = geo.local_xy_m(*b1, ref_lat)
    bx2, by2 = geo.local_xy_m(*b2, ref_lat)

    rel_x0, rel_y0 = ax1 - bx1, ay1 - by1
    vel_ax, vel_ay = (ax2 - ax1) / dt, (ay2 - ay1) / dt
    vel_bx, vel_by = (bx2 - bx1) / dt, (by2 - by1) / dt
    rel_vx, rel_vy = vel_ax - vel_bx, vel_ay - vel_by

    rel_speed2 = rel_vx * rel_vx + rel_vy * rel_vy
    closing_speed = rel_speed2 ** 0.5
    if rel_speed2 < 1e-6:
        # Near-matching velocity over this interval — relative distance is
        # ~constant, so "closest" is just the start of the interval.
        t_star = 0.0
    else:
        t_star = -(rel_x0 * rel_vx + rel_y0 * rel_vy) / rel_speed2
        t_star = max(0.0, min(dt, t_star))

    cx = rel_x0 + t_star * rel_vx
    cy = rel_y0 + t_star * rel_vy
    distance = (cx * cx + cy * cy) ** 0.5

    frac = t_star / dt if dt > 0 else 0.0
    point = (a1[0] + (a2[0] - a1[0]) * frac, a1[1] + (a2[1] - a1[1]) * frac)
    return t_star, distance, point, closing_speed


def _segment_ids(trajectory: Trajectory) -> Set[str]:
    ids = {p.road_segment_id for p in trajectory.points}
    ids.add(trajectory.road_segment_id)
    ids.discard(None)
    return ids


_LEVEL_TO_RISK = {
    "CRITICAL": ConflictRiskLevel.CRITICAL,
    "HIGH_WARNING": ConflictRiskLevel.HIGH,
    "WARNING": ConflictRiskLevel.MODERATE,
    "MONITOR": ConflictRiskLevel.LOW,
}


def _classify(
    distance_m: Optional[float], ttc_s: Optional[float], relative_heading_deg: Optional[float] = None,
) -> ConflictRiskLevel:
    """The most severe level whose distance-at-CPA AND TTC limits both hold
    (runtime per-level thresholds, scaled by the encounter scenario's
    multiplier). A level with no TTC limit accepts any TTC within the
    horizon (the horizon gate is applied before this is called)."""
    if distance_m is None:
        return ConflictRiskLevel.NONE
    cfg = thresholds.current()
    mult = cfg.multiplier(relative_heading_deg)
    for name in thresholds.LEVELS:
        level = cfg.levels[name]
        if distance_m > level.max_distance_m * mult:
            continue
        ttc_limits = [v for v in (level.max_ttc_s, level.distance_ttc_s) if v is not None]
        if ttc_limits and (ttc_s is None or ttc_s > max(ttc_limits) * mult):
            continue
        return _LEVEL_TO_RISK[name]
    return ConflictRiskLevel.NONE


def _unavailable(vehicle_a_id: str, vehicle_b_id: str, reason: str) -> VehicleConflict:
    return VehicleConflict(
        vehicle_a_id=vehicle_a_id,
        vehicle_b_id=vehicle_b_id,
        risk_level=ConflictRiskLevel.NONE,
        status=ConflictStatus.UNAVAILABLE,
        confidence=0.0,
        reason=reason,
    )


def _path(trajectory: Trajectory) -> List[LatLon]:
    pts: List[LatLon] = []
    if trajectory.matched_latitude is not None and trajectory.matched_longitude is not None:
        pts.append((trajectory.matched_latitude, trajectory.matched_longitude))
    pts.extend((p.latitude, p.longitude) for p in sorted(trajectory.points, key=lambda p: p.t_s))
    return pts


def _segments_intersect(p1, p2, q1, q2) -> bool:
    def orient(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    d1, d2 = orient(q1, q2, p1), orient(q1, q2, p2)
    d3, d4 = orient(p1, p2, q1), orient(p1, p2, q2)
    return (d1 * d2 < 0) and (d3 * d4 < 0)


def path_distance_m(path_a: List[LatLon], path_b: List[LatLon]) -> Optional[float]:
    """Minimum distance between two polylines (0 if they cross)."""
    if not path_a or not path_b:
        return None
    ref_lat = path_a[0][0]
    xa = [geo.local_xy_m(*p, ref_lat) for p in path_a]
    xb = [geo.local_xy_m(*p, ref_lat) for p in path_b]

    def pt_seg(p, a, b):
        dx, dy = b[0] - a[0], b[1] - a[1]
        l2 = dx * dx + dy * dy
        t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2))
        return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))

    segs_a = list(zip(xa, xa[1:])) or [(xa[0], xa[0])]
    segs_b = list(zip(xb, xb[1:])) or [(xb[0], xb[0])]
    best = float("inf")
    for a1, a2 in segs_a:
        for b1, b2 in segs_b:
            if _segments_intersect(a1, a2, b1, b2):
                return 0.0
            best = min(best, pt_seg(a1, b1, b2), pt_seg(a2, b1, b2), pt_seg(b1, a1, a2), pt_seg(b2, a1, a2))
    return best


def _lateral_separation(ta: Trajectory, tb: Trajectory, relative_heading: Optional[float]) -> Optional[float]:
    """Lane separation from each vehicle's signed raw-GPS offset to the road
    centreline (+ = right of its own travel direction). Opposite directions:
    keeping right on both sides puts them |offA + offB| apart; same direction:
    |offA − offB|. None when offsets are missing or GPS accuracy is unknown /
    too poor to resolve lanes."""
    cfg = thresholds.current()
    if ta.lateral_offset_m is None or tb.lateral_offset_m is None or relative_heading is None:
        return None
    for acc in (ta.gps_accuracy_m, tb.gps_accuracy_m):
        if acc is None or acc > cfg.lateral_max_accuracy_m:
            return None
    if relative_heading >= 90:
        return abs(ta.lateral_offset_m + tb.lateral_offset_m)
    return abs(ta.lateral_offset_m - tb.lateral_offset_m)


def _norm_name(name: Optional[str]) -> Optional[str]:
    n = (name or "").strip().lower()
    return n or None


def assess_conflict(
    vehicle_a_id: str,
    trajectory_a: Trajectory,
    vehicle_b_id: str,
    trajectory_b: Trajectory,
) -> Optional[VehicleConflict]:
    """The Step 4 entrypoint. Returns None only for the degenerate
    self-comparison case; every other outcome (including "no conflict") is a
    VehicleConflict so callers can see *why* something wasn't flagged."""
    if vehicle_a_id == vehicle_b_id:
        return None

    if (
        not trajectory_a.points
        or not trajectory_b.points
        or trajectory_a.status in _UNUSABLE_STATUSES
        or trajectory_b.status in _UNUSABLE_STATUSES
    ):
        return _unavailable(vehicle_a_id, vehicle_b_id, "one or both vehicles have no usable predicted trajectory")

    points_a = {p.t_s: (p.latitude, p.longitude) for p in trajectory_a.points}
    points_b = {p.t_s: (p.latitude, p.longitude) for p in trajectory_b.points}
    shared_ts = sorted(set(points_a) & set(points_b))

    if not shared_ts:
        return _unavailable(vehicle_a_id, vehicle_b_id, "trajectories share no common predicted time offset")

    cfg = thresholds.current()
    ref_lat = points_a[shared_ts[0]][0]
    relative_speed = abs(trajectory_a.initial_speed_mps - trajectory_b.initial_speed_mps)

    # "Now" sample (t=0): the first horizon is usually 1 s ahead — without
    # it, two vehicles meeting within the next second would already look
    # like they are separating. Linear back-extrapolation of the first
    # interval (same constant-velocity assumption as the CPA math).
    if len(shared_ts) >= 2 and shared_ts[0] > 0:
        t1, t2 = shared_ts[0], shared_ts[1]
        k = t1 / (t2 - t1)
        for pts in (points_a, points_b):
            (la1, lo1), (la2, lo2) = pts[t1], pts[t2]
            pts[0.0] = (la1 - (la2 - la1) * k, lo1 - (lo2 - lo1) * k)
        shared_ts = [0.0] + shared_ts

    def dist_at(t):
        return geo.haversine_m(*points_a[t], *points_b[t])

    best_heading_a: Optional[float] = None
    best_heading_b: Optional[float] = None
    best_closing_speed: Optional[float] = None
    if len(shared_ts) == 1:
        t = shared_ts[0]
        pa, pb = points_a[t], points_b[t]
        best_distance = dist_at(t)
        best_ttc = float(t)
        best_point = ((pa[0] + pb[0]) / 2, (pa[1] + pb[1]) / 2)
    else:
        best_distance = None
        best_ttc = None
        best_point = None
        for t1, t2 in zip(shared_ts, shared_ts[1:]):
            dt = t2 - t1
            if dt <= 0:
                continue
            t_star, distance, point, _rel = _cpa_in_interval(
                points_a[t1], points_a[t2], points_b[t1], points_b[t2], dt, ref_lat,
            )
            if best_distance is None or distance < best_distance - 1e-6:
                best_distance = distance
                best_ttc = t1 + t_star
                best_point = point
                # a (near-)stationary vehicle has no meaningful motion bearing
                best_heading_a = (
                    geo.bearing_deg(*points_a[t1], *points_a[t2])
                    if geo.haversine_m(*points_a[t1], *points_a[t2]) > 0.2 else trajectory_a.travel_bearing_deg
                )
                best_heading_b = (
                    geo.bearing_deg(*points_b[t1], *points_b[t2])
                    if geo.haversine_m(*points_b[t1], *points_b[t2]) > 0.2 else trajectory_b.travel_bearing_deg
                )

    if best_distance is None:
        return _unavailable(vehicle_a_id, vehicle_b_id, "no valid interval to compare (degenerate time samples)")

    # Headings: prefer the road travel bearings at the current position (a
    # stationary vehicle has no motion vector over the CPA interval).
    if trajectory_a.travel_bearing_deg is not None and best_heading_a is None:
        best_heading_a = trajectory_a.travel_bearing_deg
    if trajectory_b.travel_bearing_deg is not None and best_heading_b is None:
        best_heading_b = trajectory_b.travel_bearing_deg
    relative_heading = (
        geo.heading_diff_deg(best_heading_a, best_heading_b)
        if best_heading_a is not None and best_heading_b is not None
        else None
    )
    encounter = thresholds.scenario(relative_heading)

    # ── gate 1: closing, CPA in the future, within the horizon ─────────────
    t0 = shared_ts[0]
    d0 = dist_at(t0)
    in_future = best_ttc is not None and best_ttc > t0 + _FUTURE_EPS_S
    if in_future:
        best_closing_speed = max(0.0, (d0 - best_distance) / (best_ttc - t0))
    else:
        best_closing_speed = 0.0
    separating = (not in_future) or best_closing_speed < cfg.min_closing_speed_mps
    beyond_horizon = best_ttc is not None and best_ttc > cfg.max_horizon_s

    confidence = min(trajectory_a.confidence, trajectory_b.confidence)
    degraded = trajectory_a.status in _DEGRADING_STATUSES or trajectory_b.status in _DEGRADING_STATUSES

    # ── gate 2: road context ───────────────────────────────────────────────
    ids_a, ids_b = _segment_ids(trajectory_a), _segment_ids(trajectory_b)
    same_current_segment = (
        trajectory_a.road_segment_id is not None
        and trajectory_a.road_segment_id == trajectory_b.road_segment_id
    )
    segment_overlap_ahead = bool(ids_a & ids_b)
    name_a, name_b = _norm_name(trajectory_a.road_name), _norm_name(trajectory_b.road_name)
    same_road = same_current_segment or segment_overlap_ahead or (name_a is not None and name_a == name_b)
    gap = path_distance_m(_path(trajectory_a), _path(trajectory_b))
    connected = same_road or (gap is not None and gap <= cfg.road_link_margin_m)
    road_trusted = (
        trajectory_a.match_quality in _TRUSTED_MATCH
        and trajectory_b.match_quality in _TRUSTED_MATCH
        and confidence >= cfg.segment_trust_confidence
    )
    if same_road:
        road_relation = "same_road"
    elif connected:
        road_relation = "connected"
    elif road_trusted:
        road_relation = "unrelated"
    else:
        road_relation = "unknown"

    # ── gate 3: opposite-direction passing in separate lanes ───────────────
    lateral = _lateral_separation(trajectory_a, trajectory_b, relative_heading)
    passing = (
        encounter == "head_on" and connected
        and lateral is not None and lateral >= cfg.lateral_margin_m
    )

    reason = None
    clear_now = False
    if separating:
        risk, reason, clear_now = ConflictRiskLevel.NONE, "separation not decreasing — vehicles are not closing", True
    elif beyond_horizon:
        risk, reason = ConflictRiskLevel.NONE, "closest approach beyond the prediction horizon"
    elif road_relation == "unrelated":
        clear_now = True
        if best_distance <= cfg.unrelated_monitor_distance_m:
            risk, reason = ConflictRiskLevel.LOW, "very close, but on an unconnected road"
        else:
            risk, reason = ConflictRiskLevel.NONE, "different, unconnected roads"
    elif passing:
        risk, reason, clear_now = (
            ConflictRiskLevel.NONE, f"opposite-direction passing, lateral separation {lateral:.1f} m", True,
        )
    else:
        risk = _classify(best_distance, best_ttc, relative_heading)

    common = dict(
        vehicle_a_id=vehicle_a_id,
        vehicle_b_id=vehicle_b_id,
        status=ConflictStatus.DEGRADED if degraded else ConflictStatus.ACTIVE,
        confidence=confidence,
        time_to_closest_approach_s=best_ttc,
        distance_at_closest_approach_m=best_distance,
        conflict_point=best_point,
        same_current_segment=same_current_segment,
        segment_overlap_ahead=segment_overlap_ahead,
        relative_speed_mps=relative_speed,
        closing_speed_mps=best_closing_speed,
        relative_heading_deg=relative_heading,
        encounter=encounter,
        road_relation=road_relation,
        lateral_separation_m=lateral,
        separating=separating,
        road_type_a=trajectory_a.road_type,
        road_type_b=trajectory_b.road_type,
    )

    if risk == ConflictRiskLevel.NONE:
        return VehicleConflict(**common, risk_level=risk, reason=reason, clear_immediately=clear_now)

    if confidence < cfg.min_confidence_to_report:
        return _unavailable(
            vehicle_a_id, vehicle_b_id,
            "predicted proximity found, but trajectory confidence too low to report reliably",
        )

    return VehicleConflict(**common, risk_level=risk, reason=reason, clear_immediately=clear_now)
