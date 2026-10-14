"""
Step 3 — the trajectory predictor: RoadAwareVehicleState (Step 2) → a
deterministic, road-aware sequence of future positions.

Pure function, no I/O — takes explicit `now`/`prev_speed_sample` inputs so
it's trivially unit-testable (see tests/test_trajectory.py) and reusable
from both the ping-triggered background task and the on-demand API route
(app/services/trajectory_service.py wires those in).

Deliberately NOT machine learning — see the spec's closing "development
principle": establish a physically-understandable kinematic baseline first.

Design notes
────────────
Road-aware, not GPS-aware: positions are placed by walking distance along
the road geometry Step 2 already resolved (current segment + upcoming
segments), never by interpolating raw lat/lon — that's what keeps curves
correct and prevents a prediction from cutting across a building.

"Segment" here means exactly what it means in Step 2 (see
app/services/osrm/models.py's docstring) — an OSRM route step, not true
OSM junction-to-junction topology. The predictor inherits that
approximation rather than reinventing road topology of its own (per Step 3
spec item 18: extend the OSRM layer's output, don't duplicate it).

REVERSE direction: Step 2 only fetches road geometry AHEAD of the vehicle
(in the forward/bearing sense), so a REVERSE-direction vehicle has only the
remaining behind-portion of its current segment to walk — genuinely less
runway, and the trajectory truncates accordingly. This is an honest
limitation of Step 2's output, not a bug here.
"""
import bisect
from datetime import datetime, timezone
from typing import List, Optional, Sequence, Tuple

from app.core.config import settings
from app.services.osrm import geo
from app.services.osrm.models import (
    MapMatchQuality,
    RoadAwareVehicleState,
    RoadContext,
    RoadDirection,
    TelemetrySnapshot,
)
from app.services.trajectory.models import (
    AccelerationSource,
    ConfidenceLevel,
    Trajectory,
    TrajectoryPoint,
    TrajectoryStatus,
)

LatLon = Tuple[float, float]


# ── Road-geometry path construction ─────────────────────────────────────────

def _slice_from(coords: List[LatLon], cum: List[float], from_m: float) -> Tuple[List[LatLon], List[float]]:
    """Points of a (coords, cum) polyline from distance from_m onward,
    re-based so the first point sits at cumulative distance 0."""
    sliced_coords = [geo.point_at_distance(coords, cum, from_m)]
    sliced_cum = [0.0]
    for c, d in zip(coords, cum):
        if d > from_m:
            sliced_coords.append(c)
            sliced_cum.append(d - from_m)
    return sliced_coords, sliced_cum


def _build_forward_path(road_context: RoadContext) -> Tuple[List[LatLon], List[float], List[Optional[str]]]:
    current_coords: List[LatLon] = [(lat, lon) for lon, lat in road_context.geometry.coordinates]
    if not current_coords:
        current_coords = [(road_context.matched_latitude, road_context.matched_longitude)]
    current_cum = geo.cumulative_distances_m(current_coords)

    coords, cum = _slice_from(current_coords, current_cum, road_context.distance_along_segment_m)
    segment_ids: List[Optional[str]] = [road_context.road_segment_id] * len(coords)

    for seg in road_context.upcoming_segments:
        seg_coords: List[LatLon] = [(lat, lon) for lon, lat in seg.geometry.coordinates]
        if not seg_coords:
            continue
        seg_cum = geo.cumulative_distances_m(seg_coords)
        offset = cum[-1]
        for c, d in zip(seg_coords, seg_cum):
            coords.append(c)
            cum.append(offset + d)
            segment_ids.append(seg.road_segment_id)

    return coords, cum, segment_ids


def _build_backward_path(road_context: RoadContext) -> Tuple[List[LatLon], List[float], List[Optional[str]]]:
    """For a REVERSE-direction vehicle: walk from the current position back
    toward the start of the current segment. Step 2 doesn't fetch geometry
    behind the vehicle beyond the current segment, so this is necessarily
    shorter runway than the forward case — see module docstring."""
    current_coords: List[LatLon] = [(lat, lon) for lon, lat in road_context.geometry.coordinates]
    if not current_coords:
        current_coords = [(road_context.matched_latitude, road_context.matched_longitude)]
    current_cum = geo.cumulative_distances_m(current_coords)
    pos = road_context.distance_along_segment_m

    coords: List[LatLon] = [geo.point_at_distance(current_coords, current_cum, pos)]
    cum: List[float] = [0.0]
    for c, d in zip(reversed(current_coords), reversed(current_cum)):
        if d < pos:
            coords.append(c)
            cum.append(pos - d)

    segment_ids: List[Optional[str]] = [road_context.road_segment_id] * len(coords)
    return coords, cum, segment_ids


def _segment_id_at(cum: List[float], segment_ids: List[Optional[str]], target_m: float) -> Optional[str]:
    if not cum:
        return None
    idx = bisect.bisect_right(cum, target_m) - 1
    idx = max(0, min(idx, len(segment_ids) - 1))
    return segment_ids[idx]


# ── Kinematics ───────────────────────────────────────────────────────────────

def _speed_limit_mps(vehicle_type: Optional[str]) -> float:
    if vehicle_type and vehicle_type in settings.TRAJECTORY_MAX_SPEED_MPS_BY_TYPE:
        return settings.TRAJECTORY_MAX_SPEED_MPS_BY_TYPE[vehicle_type]
    return settings.TRAJECTORY_MAX_SPEED_MPS_DEFAULT


def _kinematics(v0: float, a: float, elapsed: float, v_max: float) -> Tuple[float, float]:
    """Piecewise-integrated (distance_m, speed_mps) at time `elapsed` under
    constant acceleration `a`, starting from speed `v0` — clamped so speed
    never goes negative (braking to a stop doesn't imply reversing) or above
    `v_max` (a runaway sensor-error accel doesn't imply an ever-increasing
    speed)."""
    v0 = max(0.0, min(v0, v_max))
    if elapsed <= 0:
        return 0.0, v0

    if a < 0:
        t_stop = (-v0 / a) if a != 0 else float("inf")
        if elapsed >= t_stop:
            distance = v0 * t_stop + 0.5 * a * t_stop * t_stop
            return max(0.0, distance), 0.0
        distance = v0 * elapsed + 0.5 * a * elapsed * elapsed
        return max(0.0, distance), max(0.0, v0 + a * elapsed)

    if a > 0:
        t_cap = (v_max - v0) / a
        if t_cap >= 0 and elapsed >= t_cap:
            distance_to_cap = v0 * t_cap + 0.5 * a * t_cap * t_cap
            distance = distance_to_cap + v_max * (elapsed - t_cap)
            return distance, v_max
        distance = v0 * elapsed + 0.5 * a * elapsed * elapsed
        return distance, min(v0 + a * elapsed, v_max)

    return v0 * elapsed, v0


def _resolve_acceleration(
    telemetry: TelemetrySnapshot,
    prev_speed_sample: Optional[Tuple[float, datetime]],
    now: datetime,
) -> Tuple[float, AccelerationSource]:
    if telemetry.acceleration_mps2 is not None:
        a = max(-settings.TRAJECTORY_MAX_DECEL_MPS2, min(settings.TRAJECTORY_MAX_ACCEL_MPS2, telemetry.acceleration_mps2))
        return a, AccelerationSource.DEVICE

    if prev_speed_sample is not None and telemetry.speed_mps is not None:
        prev_speed, prev_ts = prev_speed_sample
        prev_ts = prev_ts if prev_ts.tzinfo else prev_ts.replace(tzinfo=timezone.utc)
        ts = telemetry.timestamp if telemetry.timestamp.tzinfo else telemetry.timestamp.replace(tzinfo=timezone.utc)
        dt = (ts - prev_ts).total_seconds()
        sample_age = (now - ts).total_seconds()
        if dt >= settings.TRAJECTORY_ACCEL_SAMPLE_MIN_DT_S and sample_age <= settings.TRAJECTORY_ACCEL_SAMPLE_MAX_AGE_S:
            a = (telemetry.speed_mps - prev_speed) / dt
            a = max(-settings.TRAJECTORY_MAX_DECEL_MPS2, min(settings.TRAJECTORY_MAX_ACCEL_MPS2, a))
            return a, AccelerationSource.DERIVED

    return 0.0, AccelerationSource.ASSUMED_ZERO


# ── Confidence ───────────────────────────────────────────────────────────────

_QUALITY_FACTOR = {
    MapMatchQuality.GOOD: 1.0,
    MapMatchQuality.DEGRADED: 0.6,
}


def _confidence(
    match_quality: MapMatchQuality,
    age_s: float,
    accuracy_m: Optional[float],
    speed_available: bool,
    heading_available: bool,
    accel_source: AccelerationSource,
    truncated: bool,
    direction_assumed: bool,
) -> float:
    score = _QUALITY_FACTOR.get(match_quality, 0.0)

    if age_s <= settings.TRAJECTORY_FRESH_TELEMETRY_S:
        score *= 1.0
    elif age_s <= settings.TRAJECTORY_STALE_TELEMETRY_S:
        span = settings.TRAJECTORY_STALE_TELEMETRY_S - settings.TRAJECTORY_FRESH_TELEMETRY_S
        frac = (age_s - settings.TRAJECTORY_FRESH_TELEMETRY_S) / span if span > 0 else 1.0
        score *= max(0.2, 1.0 - 0.8 * frac)
    else:
        score *= 0.05

    if accuracy_m is None:
        score *= 0.85
    elif accuracy_m <= 10:
        score *= 1.0
    elif accuracy_m <= 50:
        score *= max(0.5, 1.0 - 0.5 * (accuracy_m - 10) / 40)
    else:
        score *= 0.3

    if not speed_available:
        score *= 0.7
    if not heading_available:
        score *= 0.85
    if direction_assumed:
        score *= 0.8

    score *= {
        AccelerationSource.DEVICE: 1.0,
        AccelerationSource.DERIVED: 0.92,
        AccelerationSource.ASSUMED_ZERO: 0.88,
    }[accel_source]

    if truncated:
        score *= 0.75

    return max(0.0, min(1.0, score))


def _confidence_level(score: float) -> ConfidenceLevel:
    if score >= 0.7:
        return ConfidenceLevel.HIGH
    if score >= 0.4:
        return ConfidenceLevel.MEDIUM
    return ConfidenceLevel.LOW


# ── Entrypoint ───────────────────────────────────────────────────────────────

def predict_trajectory(
    state: RoadAwareVehicleState,
    vehicle_type: Optional[str] = None,
    prev_speed_sample: Optional[Tuple[float, datetime]] = None,
    now: Optional[datetime] = None,
    horizons_s: Optional[Sequence[float]] = None,
) -> Trajectory:
    now = now or datetime.now(timezone.utc)
    horizons = sorted(horizons_s if horizons_s is not None else settings.TRAJECTORY_HORIZONS_S)
    telemetry = state.telemetry
    ts = telemetry.timestamp if telemetry.timestamp.tzinfo else telemetry.timestamp.replace(tzinfo=timezone.utc)
    age_s = max(0.0, (now - ts).total_seconds())

    base = dict(
        vehicle_id=state.vehicle_id,
        generated_at=now,
        telemetry_timestamp=ts,
        telemetry_age_s=age_s,
        prediction_horizon_s=max(horizons) if horizons else 0.0,
    )

    if state.road_context is None or state.match_quality in (
        MapMatchQuality.FAILED, MapMatchQuality.UNAVAILABLE,
    ):
        # No usable road position to walk along at all — a straight-line GPS
        # projection would violate "do not predict off-road" (spec item 17),
        # so there's simply no trajectory rather than a fabricated one.
        return Trajectory(
            **base,
            status=TrajectoryStatus.UNAVAILABLE,
            confidence=0.0,
            confidence_level=ConfidenceLevel.LOW,
            reason=state.match_error or f"no usable road context (match_quality={state.match_quality.value})",
            initial_speed_mps=telemetry.speed_mps or 0.0,
            acceleration_mps2=0.0,
            acceleration_source=AccelerationSource.ASSUMED_ZERO,
            points=[],
        )

    road_context = state.road_context
    v_max = _speed_limit_mps(vehicle_type)
    a, accel_source = _resolve_acceleration(telemetry, prev_speed_sample, now)
    v0 = max(0.0, min(telemetry.speed_mps if telemetry.speed_mps is not None else 0.0, v_max))

    direction_assumed = road_context.direction == RoadDirection.UNKNOWN
    if road_context.direction == RoadDirection.REVERSE:
        path_coords, path_cum, path_segments = _build_backward_path(road_context)
    else:
        path_coords, path_cum, path_segments = _build_forward_path(road_context)
    path_total = path_cum[-1] if path_cum else 0.0

    is_stale = age_s > settings.TRAJECTORY_STALE_TELEMETRY_S

    points: List[TrajectoryPoint] = []
    truncated = False
    for t in horizons:
        if is_stale:
            # Don't extrapolate motion through a gap this large — freeze at
            # the last known position rather than guess (spec item 14: "must
            # NOT pretend the vehicle is still at its [old] position" cuts
            # both ways — we also must not pretend we know where it went).
            distance, speed_at_t = 0.0, 0.0
        else:
            # The vehicle has been moving since the telemetry timestamp, not
            # just since "now" — a fresher, more accurate projection than
            # measuring elapsed time from the request instead of the sample.
            elapsed = age_s + t
            distance, speed_at_t = _kinematics(v0, a, elapsed, v_max)

        if distance > path_total + 1e-6:
            truncated = True
            break

        lat, lon = geo.point_at_distance(path_coords, path_cum, distance)
        points.append(
            TrajectoryPoint(
                t_s=t,
                latitude=lat,
                longitude=lon,
                distance_along_road_m=distance,
                speed_mps=speed_at_t,
                road_segment_id=_segment_id_at(path_cum, path_segments, distance),
            )
        )

    confidence = _confidence(
        state.match_quality, age_s, telemetry.accuracy_m,
        telemetry.speed_mps is not None, telemetry.heading is not None,
        accel_source, truncated, direction_assumed,
    )

    if is_stale:
        status = TrajectoryStatus.STALE
    elif (
        state.match_quality == MapMatchQuality.DEGRADED
        or age_s > settings.TRAJECTORY_FRESH_TELEMETRY_S
        or truncated
        or confidence < 0.5
    ):
        status = TrajectoryStatus.DEGRADED
    else:
        status = TrajectoryStatus.VALID

    reason = None
    if is_stale:
        reason = f"telemetry is {age_s:.0f}s old — motion not extrapolated"
    elif truncated:
        reason = "known road geometry ended before the full prediction horizon"

    return Trajectory(
        **base,
        status=status,
        confidence=confidence,
        confidence_level=_confidence_level(confidence),
        reason=reason,
        initial_speed_mps=v0,
        acceleration_mps2=a,
        acceleration_source=accel_source,
        road_segment_id=road_context.road_segment_id,
        road_direction=road_context.direction.value,
        road_type=road_context.road_type,
        truncated=truncated,
        points=points,
    )
