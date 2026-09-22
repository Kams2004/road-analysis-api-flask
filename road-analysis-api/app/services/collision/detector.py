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
"""
import logging
from typing import Optional, Set, Tuple

from app.core.config import settings
from app.services.collision.models import ConflictRiskLevel, ConflictStatus, VehicleConflict
from app.services.osrm import geo
from app.services.trajectory.models import Trajectory, TrajectoryStatus

logger = logging.getLogger(__name__)

LatLon = Tuple[float, float]

_UNUSABLE_STATUSES = (TrajectoryStatus.UNAVAILABLE,)
_DEGRADING_STATUSES = (TrajectoryStatus.DEGRADED, TrajectoryStatus.STALE)


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


def _classify(distance_m: Optional[float], ttc_s: Optional[float]) -> ConflictRiskLevel:
    if distance_m is None:
        return ConflictRiskLevel.NONE
    if distance_m <= settings.COLLISION_CRITICAL_DISTANCE_M and ttc_s is not None and ttc_s <= settings.COLLISION_CRITICAL_TTC_S:
        return ConflictRiskLevel.CRITICAL
    if distance_m <= settings.COLLISION_HIGH_DISTANCE_M and ttc_s is not None and ttc_s <= settings.COLLISION_HIGH_TTC_S:
        return ConflictRiskLevel.HIGH
    if distance_m <= settings.COLLISION_MODERATE_DISTANCE_M:
        return ConflictRiskLevel.MODERATE
    if distance_m <= settings.COLLISION_LOW_DISTANCE_M:
        return ConflictRiskLevel.LOW
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

    points_a = {p.t_s: p for p in trajectory_a.points}
    points_b = {p.t_s: p for p in trajectory_b.points}
    shared_ts = sorted(set(points_a) & set(points_b))

    if not shared_ts:
        return _unavailable(vehicle_a_id, vehicle_b_id, "trajectories share no common predicted time offset")

    ref_lat = points_a[shared_ts[0]].latitude

    relative_speed = abs(trajectory_a.initial_speed_mps - trajectory_b.initial_speed_mps)
    best_heading_a: Optional[float] = None
    best_heading_b: Optional[float] = None

    if len(shared_ts) == 1:
        # Only one shared sample (heavy truncation on one/both sides) — no
        # interval to run CPA over, but a same-moment distance is still
        # useful information rather than nothing at all. No motion vector
        # available from a single point, so no heading/closing-speed here.
        t = shared_ts[0]
        pa, pb = points_a[t], points_b[t]
        best_distance = geo.haversine_m(pa.latitude, pa.longitude, pb.latitude, pb.longitude)
        best_ttc = float(t)
        best_point = ((pa.latitude + pb.latitude) / 2, (pa.longitude + pb.longitude) / 2)
        best_closing_speed = None
    else:
        best_distance = None
        best_ttc = None
        best_point = None
        best_closing_speed = None
        for t1, t2 in zip(shared_ts, shared_ts[1:]):
            pa1, pa2 = points_a[t1], points_a[t2]
            pb1, pb2 = points_b[t1], points_b[t2]
            dt = t2 - t1
            if dt <= 0:
                continue
            t_star, distance, point, closing_speed = _cpa_in_interval(
                (pa1.latitude, pa1.longitude), (pa2.latitude, pa2.longitude),
                (pb1.latitude, pb1.longitude), (pb2.latitude, pb2.longitude),
                dt, ref_lat,
            )
            if best_distance is None or distance < best_distance:
                best_distance = distance
                best_ttc = t1 + t_star
                best_point = point
                best_closing_speed = closing_speed
                best_heading_a = geo.bearing_deg(pa1.latitude, pa1.longitude, pa2.latitude, pa2.longitude)
                best_heading_b = geo.bearing_deg(pb1.latitude, pb1.longitude, pb2.latitude, pb2.longitude)

    if best_distance is None:
        return _unavailable(vehicle_a_id, vehicle_b_id, "no valid interval to compare (degenerate time samples)")

    relative_heading = (
        geo.heading_diff_deg(best_heading_a, best_heading_b)
        if best_heading_a is not None and best_heading_b is not None
        else None
    )

    risk = _classify(best_distance, best_ttc)
    confidence = min(trajectory_a.confidence, trajectory_b.confidence)
    degraded = trajectory_a.status in _DEGRADING_STATUSES or trajectory_b.status in _DEGRADING_STATUSES

    if risk == ConflictRiskLevel.NONE:
        return VehicleConflict(
            vehicle_a_id=vehicle_a_id,
            vehicle_b_id=vehicle_b_id,
            risk_level=risk,
            status=ConflictStatus.DEGRADED if degraded else ConflictStatus.ACTIVE,
            confidence=confidence,
            time_to_closest_approach_s=best_ttc,
            distance_at_closest_approach_m=best_distance,
            conflict_point=best_point,
            relative_speed_mps=relative_speed,
            closing_speed_mps=best_closing_speed,
            relative_heading_deg=relative_heading,
            road_type_a=trajectory_a.road_type,
            road_type_b=trajectory_b.road_type,
        )

    if confidence < settings.COLLISION_MIN_CONFIDENCE_TO_REPORT:
        return _unavailable(
            vehicle_a_id, vehicle_b_id,
            "predicted proximity found, but trajectory confidence too low to report reliably",
        )

    ids_a, ids_b = _segment_ids(trajectory_a), _segment_ids(trajectory_b)

    return VehicleConflict(
        vehicle_a_id=vehicle_a_id,
        vehicle_b_id=vehicle_b_id,
        risk_level=risk,
        status=ConflictStatus.DEGRADED if degraded else ConflictStatus.ACTIVE,
        confidence=confidence,
        time_to_closest_approach_s=best_ttc,
        distance_at_closest_approach_m=best_distance,
        conflict_point=best_point,
        same_current_segment=(
            trajectory_a.road_segment_id is not None
            and trajectory_a.road_segment_id == trajectory_b.road_segment_id
        ),
        segment_overlap_ahead=bool(ids_a & ids_b),
        relative_speed_mps=relative_speed,
        closing_speed_mps=best_closing_speed,
        relative_heading_deg=relative_heading,
        road_type_a=trajectory_a.road_type,
        road_type_b=trajectory_b.road_type,
    )
