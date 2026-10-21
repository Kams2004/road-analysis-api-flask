"""
The "is this vehicle parked?" decision (pure). Every rule yields a `Reason`
{code, value, threshold, passed} — `passed` None means "not applicable /
unknown, skipped" (never a failure) — so the validation screen shows exactly
why a track was or was not flagged. The verdict is the first failing rule in
this order:

  too_far                closest_box_area < min_box_area_at_closest (rule 7)
  insufficient_evidence  sightings / track_seconds / mean_confidence (rule 6)
  traffic_queue          oncoming_lane: still but inside the oncoming lane
                         (|lateral offset| < oncoming_lane_max_offset_m on
                         the other side of the road) — a jam, not parked;
                         queue_ego_stop: ≥ 2 sightings in our corridor while
                         our speed < queue_ego_speed_kmh; queue_peers: with
                         the other stationary in-corridor vehicles seen at the
                         same time ≥ queue_min_vehicles (rule 5)
  moving                 target_speed > max_target_speed_kmh + r·ego speed
                         or world_drift > max_world_drift_m + r·ego distance
                         (r = range_error_ratio: a static object's monocular
                         position drifts by |1 − k|·Δ for a range-scale error
                         k) — `motion` tells same_direction / oncoming. Without
                         ego motion (no GPS, no OSD speed) the prototype's
                         image heuristics decide instead (lateral_drift,
                         growth_rate, exit_or_growth) and the result can never
                         be auto-accepted (blocker `fallback_heuristics`,
                         score capped) (rule 2)
  insufficient_evidence  ego_distance < min_ego_distance_m or ego_speed <
                         min_ego_speed_kmh — we did not move enough to tell a
                         parked vehicle from stopped traffic (rule 1; a
                         moving vehicle needs no ego motion to be told apart)
  not_grounded           ground_contact: share of close sightings whose
                         contact-line range agrees with the class-height range
                         < min_ground_contact_ratio (billboard, carrier,
                         reflection) (rule 3)
  off_road               road_distance > max_road_distance_m (OSRM snap of
                         the estimated position; skipped when OSRM is off /
                         unreachable / no GPS position: road_distance_unknown)
                         (rule 4)
  parked                 everything else; subtype `blocking` (in our corridor
                         at the closest sighting), `wrong_side` (on the
                         oncoming lane's side for `drive_side`) or `roadside`
                         (rule 10).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from . import geometry as geo
from .features import TrackFeatures
from .settings import ParkedVehicleSettings

PARKED, MOVING, TRAFFIC_QUEUE, OFF_ROAD = "parked", "moving", "traffic_queue", "off_road"
NOT_GROUNDED, INSUFFICIENT, TOO_FAR = "not_grounded", "insufficient_evidence", "too_far"
BLOCKING, WRONG_SIDE, ROADSIDE = "blocking", "wrong_side", "roadside"
FALLBACK_BLOCKER = "fallback_heuristics"
QUEUE_MIN_SLOW_SIGHTINGS = 2     # one slow sample may be an OSD misread


@dataclass(frozen=True)
class Reason:
    code: str
    value: Any
    threshold: Any
    passed: Optional[bool]       # None = not applicable / unknown (skipped)
    detail: str = ""

    def as_dict(self) -> dict:
        r = lambda x: round(x, 3) if isinstance(x, float) else x  # noqa: E731
        return {"code": self.code, "value": r(self.value), "threshold": r(self.threshold),
                "passed": self.passed, "detail": self.detail}


@dataclass
class Judgement:
    verdict: str
    subtype: Optional[str]
    reasons: list[Reason] = field(default_factory=list)
    blockers: list[tuple[str, str]] = field(default_factory=list)   # accept blockers (ai_validation)
    fallback: bool = False                                          # image heuristics only
    motion: str = "unknown"                                         # stationary | same_direction | oncoming | unknown
    allowed_speed_kmh: Optional[float] = None
    allowed_drift_m: Optional[float] = None

    def failed(self) -> list[str]:
        return [r.code for r in self.reasons if r.passed is False]


def allowed_limits(f: TrackFeatures, s: ParkedVehicleSettings) -> tuple[float, float]:
    """(target speed km/h, world drift m) a static vehicle may show, given the
    monocular range-scale error tolerated (range_error_ratio)."""
    return (s.max_target_speed_kmh + s.range_error_ratio * (f.ego_speed_kmh or 0.0),
            s.max_world_drift_m + s.range_error_ratio * (f.ego_distance_m or 0.0))


def uses_fallback(f: TrackFeatures) -> bool:
    """No ego motion, or neither a world position nor a closing ratio → image heuristics only."""
    return f.ego_mode == "none" or (f.target_speed_kmh is None and f.closing_ratio is None
                                    and f.closing_ratio_height is None)


def closing_checks(f: TrackFeatures, s: ParkedVehicleSettings) -> list[Reason]:
    """Rule 2b: closing ratios within [closing_ratio_min, closing_ratio_max] and
    not a followed vehicle (empty when the closing ratio is off / unknown)."""
    if not s.closing_check:
        return []
    window = [s.closing_ratio_min, s.closing_ratio_max]
    out = []
    # the contact-line ratio decides; the box-growth ratio (class height unknown for
    # trucks / buses, box cut by the frame) only when the contact line is not usable
    primary = f.closing_ratio is not None
    for code, value, what, decides in (
            ("closing_ratio", f.closing_ratio, "contact-line range", True),
            ("box_growth_ratio", f.closing_ratio_height, "apparent height (box growth)", not primary)):
        if value is not None:
            ok = window[0] <= value <= window[1]
            out.append(Reason(code, value, window, ok if decides else None,
                              f"−Δrange / our travel ({what}) over {f.closing_ego_m or 0:.0f} m: "
                              f"parked ≈ 1, ahead with us ≈ 0, oncoming > 1"
                              + ("" if decides else " (informational: the contact-line ratio decides)")))
    out.append(Reason("followed_vehicle", f.followed, False, not f.followed,
                      f"{f.corridor_share:.0%} of sightings in our corridor, box area ×{f.scale_growth:.2f}"))
    return out


def closing_motion(f: TrackFeatures, s: ParkedVehicleSettings) -> str:
    vals = [f.closing_ratio] if f.closing_ratio is not None else \
        [f.closing_ratio_height] if f.closing_ratio_height is not None else []
    if f.followed or any(v < s.closing_ratio_min for v in vals):
        return "same_direction"
    if any(v > s.closing_ratio_max for v in vals):
        return "oncoming"
    return "stationary"


def fallback_passes(f: TrackFeatures, s: ParkedVehicleSettings) -> tuple[bool, bool, bool]:
    """Prototype heuristics: (lateral drift, growth rate, exit-or-growth)."""
    return (abs(f.lateral_drift) >= s.fallback_min_lateral_drift,
            f.growth_rate <= s.fallback_max_growth_rate,
            f.exits_via_side or f.scale_growth >= s.fallback_min_scale_growth)


def is_stationary(f: TrackFeatures, s: ParkedVehicleSettings) -> bool:
    """World-stationary by the stability rule (used for queue peers)."""
    if uses_fallback(f):
        return all(fallback_passes(f, s))
    if any(r.passed is False for r in closing_checks(f, s)):
        return False
    if not s.world_drift_check or f.target_speed_kmh is None:
        return True
    max_v, max_d = allowed_limits(f, s)
    return f.target_speed_kmh <= max_v and (f.world_drift_m or 0.0) <= max_d


def subtype_of(f: TrackFeatures, drive_side: str) -> str:
    """Subtype at the closest sighting (corridor / side computed with the job's camera)."""
    if f.in_corridor_at_closest:
        return BLOCKING
    return WRONG_SIDE if geo.is_wrong_side(f.side_at_closest, drive_side) else ROADSIDE


def judge(f: TrackFeatures, s: ParkedVehicleSettings, *,
          road_distance_m: Optional[float] = None, road_note: str = "", queue_peers: int = 0) -> Judgement:
    """Verdict of one track (see module docstring). `road_distance_m` is the
    OSRM snap distance of the estimated position (None = unknown, the rule is
    skipped and `road_note` says why); `queue_peers` the number of OTHER
    stationary vehicles in our corridor seen at the same time."""
    R: list[Reason] = []
    add = R.append

    # rule 7 — proximity
    near = Reason("closest_box_area", f.closest_area, s.min_box_area_at_closest,
                  f.closest_area >= s.min_box_area_at_closest, "box area / frame area at the closest sighting")
    add(near)
    # rule 6 — evidence
    evidence = [
        Reason("sightings", f.sightings, s.min_sightings, f.sightings >= s.min_sightings),
        Reason("track_seconds", f.duration_s, s.min_track_seconds, f.duration_s >= s.min_track_seconds),
        Reason("mean_confidence", f.mean_confidence, s.min_mean_confidence,
               f.mean_confidence >= s.min_mean_confidence),
    ]
    R.extend(evidence)

    # rule 5 — traffic queue
    queue_stop = Reason("queue_ego_stop", f.queue_stop_sightings, QUEUE_MIN_SLOW_SIGHTINGS,
                        f.queue_stop_sightings < QUEUE_MIN_SLOW_SIGHTINGS,
                        f"sightings ahead in our corridor while our speed < {s.queue_ego_speed_kmh:g} km/h")
    add(queue_stop)
    stationary = is_stationary(f, s)
    in_lane_still = f.in_corridor_at_closest and stationary
    queue_peer = Reason("queue_peers", 1 + queue_peers if in_lane_still else queue_peers, s.queue_min_vehicles,
                        (1 + queue_peers < s.queue_min_vehicles) if in_lane_still else None,
                        "stationary vehicles in our corridor at the same time" if in_lane_still
                        else "not stationary in our corridor")
    add(queue_peer)

    # rule 1 — ego motion
    fallback = uses_fallback(f)
    ego_checks: list[Reason] = []
    if f.ego_mode == "none":
        add(Reason("ego_motion_unknown", None, None, None,
                   "no GPS fix nor OSD speed: image heuristics only"
                   + ("" if s.fallback_parked else " — a still-looking vehicle stays insufficient_evidence")))
    else:
        ego_checks = [
            Reason("ego_distance", f.ego_distance_m, s.min_ego_distance_m,
                   f.ego_distance_m is not None and f.ego_distance_m >= s.min_ego_distance_m,
                   f"ego motion from {f.ego_mode}"),
            Reason("ego_speed", f.ego_speed_kmh, s.min_ego_speed_kmh,
                   f.ego_speed_kmh is not None and f.ego_speed_kmh >= s.min_ego_speed_kmh, "median over the track"),
        ]
        R.extend(ego_checks)

    # rule 2 — world stability (or the image fallback)
    max_v, max_d = allowed_limits(f, s)
    motion = "unknown"
    if not fallback:
        stability = closing_checks(f, s)
        closing_ok = all(r.passed is not False for r in stability)
        world_ok = True
        if f.target_speed_kmh is not None:
            speed_ok = f.target_speed_kmh <= max_v
            drift_ok = (f.world_drift_m or 0.0) <= max_d
            on = s.world_drift_check
            world_ok = (speed_ok and drift_ok) or not on
            stability += [
                Reason("target_speed", f.target_speed_kmh, max_v, speed_ok if on else None,
                       f"{f.positions_used} positions; limit = {s.max_target_speed_kmh:g} + "
                       f"{s.range_error_ratio:g} × ego speed" + ("" if on else " (world_drift_check off)")),
                Reason("world_drift", f.world_drift_m, max_d, drift_ok if on else None,
                       f"limit = {s.max_world_drift_m:g} + {s.range_error_ratio:g} × ego distance"),
            ]
        if not closing_ok:
            motion = closing_motion(f, s)
        elif world_ok:
            motion = "stationary"
        elif f.drift_along_m is not None:
            motion = "same_direction" if f.drift_along_m > 0 else "oncoming"
    else:
        if f.ego_mode != "none":
            add(Reason("world_position_unknown", f.positions_used, None, None,
                       "no usable range / heading for the world positions: image heuristics only"))
        lat_ok, growth_ok, exit_ok = fallback_passes(f, s)
        stability = [
            Reason("lateral_drift", abs(f.lateral_drift), s.fallback_min_lateral_drift, lat_ok,
                   "image heuristic: share of the frame width swept"),
            Reason("growth_rate", f.growth_rate, s.fallback_max_growth_rate, growth_ok,
                   "image heuristic: (box scale growth − 1) / s — faster = oncoming"),
            Reason("exit_or_growth", f.exits_via_side or f.scale_growth >= s.fallback_min_scale_growth,
                   s.fallback_min_scale_growth, exit_ok, "image heuristic: leaves by a side or grows enough"),
        ]
        motion = "stationary" if all((lat_ok, growth_ok, exit_ok)) else ("oncoming" if not growth_ok else "unknown")
    R.extend(stability)

    # rule 5b — still, but inside the oncoming traffic lane: a jam on the other side
    # (a parked car sits at the road edge, farther out)
    lane = None
    if f.lateral_m is not None and s.oncoming_lane_max_offset_m > 0:
        toward = -f.lateral_m if s.drive_side == "right" else f.lateral_m     # metres into the oncoming side
        inside = 0 < toward < s.oncoming_lane_max_offset_m
        lane = Reason("oncoming_lane", round(f.lateral_m, 2), s.oncoming_lane_max_offset_m, not inside,
                      "lateral offset (m, + right): inside the oncoming lane = stopped traffic, not parked")
        add(lane)

    # rule 3 — ground contact
    if f.ground_contact_ratio is None:
        ground = Reason("ground_contact_unknown", None, s.min_ground_contact_ratio, None,
                        f"no close, uncut sighting (≤ {s.ground_contact_max_range_m:g} m)")
    else:
        ground = Reason("ground_contact", f.ground_contact_ratio, s.min_ground_contact_ratio,
                        f.ground_contact_ratio >= s.min_ground_contact_ratio,
                        f"median |r_bottom − r_height| / r = {f.ground_contact_error:.2f}, "
                        f"tolerance {s.ground_contact_tolerance:g}, {f.ground_checked} sightings")
    add(ground)

    # rule 4 — road distance
    if road_distance_m is None:
        road = Reason("road_distance_unknown", None, s.max_road_distance_m, None, road_note or "not checked")
    else:
        road = Reason("road_distance", road_distance_m, s.max_road_distance_m,
                      road_distance_m <= s.max_road_distance_m, "OSRM snap of the estimated position")
    add(road)

    # rule 10 — subtype (informational)
    subtype = subtype_of(f, s.drive_side) if near.passed else None
    add(Reason("in_corridor", f.in_corridor_at_closest, None, None, "at the closest sighting"))
    add(Reason("road_side", f.side_at_closest, s.drive_side, None, f"traffic drives on the {s.drive_side}"))

    if not near.passed:
        verdict = TOO_FAR
    elif any(r.passed is False for r in evidence):
        verdict = INSUFFICIENT
    elif queue_stop.passed is False or queue_peer.passed is False:
        verdict = TRAFFIC_QUEUE
    elif any(r.passed is False for r in stability):
        verdict = MOVING
    elif lane is not None and lane.passed is False:
        verdict = TRAFFIC_QUEUE
    elif any(r.passed is False for r in ego_checks):
        verdict = INSUFFICIENT
    elif fallback and not s.fallback_parked:
        verdict = INSUFFICIENT          # still-looking but our own motion is unknown: never `parked`
    elif ground.passed is False:
        verdict = NOT_GROUNDED
    elif road.passed is False:
        verdict = OFF_ROAD
    else:
        verdict = PARKED
    blockers = []
    if fallback:
        blockers.append((FALLBACK_BLOCKER, "no ego motion / world position: image heuristics only"))
    return Judgement(verdict, subtype, R, blockers, fallback, motion, max_v, max_d)
