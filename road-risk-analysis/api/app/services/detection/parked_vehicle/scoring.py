"""
Confidence score (0..1) of a parked-vehicle verdict — stored as the
detection's `verifier_score`, so the AI validation mode routes it like a
crop classifier's P(class) (thresholds: ai_validation.type_overrides
["parked_vehicle"], else settings `auto_accept_score` / `auto_reject_score`).

score = Σ wᵢ·cᵢ / Σ wᵢ (weights `score_weights`), each component cᵢ in 0..1
and NON-DECREASING in the evidence it measures — so the score is
deterministic and monotonic: more / better evidence never lowers it.

  detector        (mean conf − detector_confidence) / (1 − detector_confidence)
  evidence        ½·min(1, sightings / 2·min_sightings) + ½·min(1, seconds / 2·min_track_seconds)
  target_speed    1 − target_speed / allowed speed   (fallback: 1 − growth_rate / max)
  ego_motion      min(1, ego distance / 2·min_ego_distance_m)   (fallback: 0)
  ground_contact  share of close sightings that agree              (unknown: ½)
  road_distance   1 − distance / max_road_distance_m              (unknown: ½)
  proximity       min(1, closest box area / 4·min_box_area_at_closest)

Image-only fallback (no ego motion): min(score, fallback_score_cap).
"""
from __future__ import annotations

from typing import Optional

from .features import TrackFeatures
from .rules import Judgement, allowed_limits
from .settings import ParkedVehicleSettings

UNKNOWN = 0.5


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def components(f: TrackFeatures, s: ParkedVehicleSettings, j: Judgement,
               road_distance_m: Optional[float] = None) -> dict[str, float]:
    floor = s.detector_confidence
    c = {
        "detector": _clamp((f.mean_confidence - floor) / (1 - floor)) if floor < 1 else 0.0,
        "evidence": 0.5 * _clamp(f.sightings / (2 * s.min_sightings))
        + 0.5 * _clamp(f.duration_s / (2 * s.min_track_seconds)),
        "proximity": _clamp(f.closest_area / (4 * s.min_box_area_at_closest)) if s.min_box_area_at_closest else 1.0,
        "ground_contact": UNKNOWN if f.ground_contact_ratio is None else _clamp(f.ground_contact_ratio),
        "road_distance": UNKNOWN if road_distance_m is None else _clamp(1 - road_distance_m / s.max_road_distance_m),
    }
    if j.fallback:
        c["target_speed"] = _clamp(1 - f.growth_rate / s.fallback_max_growth_rate) if s.fallback_max_growth_rate else 0.0
        c["ego_motion"] = 0.0
    else:
        max_v, _ = allowed_limits(f, s)
        c["target_speed"] = _clamp(1 - (f.target_speed_kmh or 0.0) / max_v) if max_v > 0 else 0.0
        c["ego_motion"] = (_clamp((f.ego_distance_m or 0.0) / (2 * s.min_ego_distance_m))
                           if s.min_ego_distance_m else 1.0)
    return c


def score(f: TrackFeatures, s: ParkedVehicleSettings, j: Judgement,
          road_distance_m: Optional[float] = None) -> tuple[float, dict[str, float]]:
    """(score, components) — see module docstring."""
    c = components(f, s, j, road_distance_m)
    total_w = sum(s.weight(k) for k in c)
    value = sum(s.weight(k) * v for k, v in c.items()) / total_w if total_w > 0 else 0.0
    if j.fallback:
        value = min(value, s.fallback_score_cap)
    return round(value, 4), {k: round(v, 4) for k, v in c.items()}
