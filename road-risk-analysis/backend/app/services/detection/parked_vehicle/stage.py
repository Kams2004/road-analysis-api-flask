"""
Glue between the video processor and the pure parked-vehicle code: a
finished vehicle track (+ the ego path around it, the settings and an
optional road snapper) → an `Evaluation` the processor stores (or counts).

Two-phase so the processor can await OSRM in between:
    features, cam = prepare(sightings, ego_samples, settings, vehicle_id)
    distance      = await snap(features.target_lat, features.target_lon)   # if needs_road_check()
    evaluation    = finish(features, settings, road_distance_m=distance, queue_peers=…)
`evaluate()` does both with a synchronous snapper (tests, scripts).

Storage (rule 12): verdict `parked` → stored, routed by the AI validation mode
with verifier_score = score; a verdict listed in `review_verdicts` (default
off_road, not_grounded, traffic_queue; `moving` allowed) is stored as an
automatic rejection (`ai_off_road`, `ai_not_grounded`, `ai_traffic_queue`,
`ai_moving_vehicle`) only when `store_rejected_for_review` is on; anything
else (moving, too_far, insufficient_evidence) is only counted in the job
metrics.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from . import features as feat
from .rules import PARKED, Judgement, is_stationary, judge
from .scoring import score as compute_score
from .settings import CameraCalibration, ParkedVehicleSettings
from .severity import severity as compute_severity

DETECTION_TYPE = "parked_vehicle"
MODEL_KEY = "vehicles"
RULES_VERSION = "parked_vehicle_rules:v1"
REJECTION_CODES = {
    "off_road": "ai_off_road",
    "not_grounded": "ai_not_grounded",
    "traffic_queue": "ai_traffic_queue",
    "moving": "ai_moving_vehicle",
}


@dataclass
class Evaluation:
    verdict: str
    subtype: Optional[str]
    score: float
    severity: Optional[str]
    vehicle_class: str
    features: feat.TrackFeatures
    judgement: Judgement
    components: dict = field(default_factory=dict)
    road_distance_m: Optional[float] = None
    camera_profile: Optional[str] = None
    store: bool = False
    rejection_code: Optional[str] = None

    @property
    def blockers(self) -> list[tuple[str, str]]:
        return self.judgement.blockers

    def reason_text(self) -> str:
        """Short human-readable verdict (ai_decision_reason of an automatic rejection)."""
        failed = [r for r in self.judgement.reasons if r.passed is False]
        if not failed:
            return f"{self.verdict} (score {self.score:.2f})"
        r = failed[0]
        val = f"{r.value:.2f}" if isinstance(r.value, float) else r.value
        thr = f"{r.threshold:.2f}" if isinstance(r.threshold, float) else r.threshold
        return f"{self.verdict}: {r.code} {val} vs {thr}"

    def details(self) -> dict:
        """JSON stored on the detection (`ai_details`) — the validation screen's evidence."""
        f = self.features
        return {
            "pipeline": DETECTION_TYPE, "rules_version": RULES_VERSION,
            "verdict": self.verdict, "subtype": self.subtype, "severity": self.severity,
            "vehicle_class": self.vehicle_class, "score": self.score, "score_components": self.components,
            "motion": self.judgement.motion, "fallback": self.judgement.fallback,
            "camera_profile": self.camera_profile,
            "target_position": ({"latitude": round(f.target_lat, 7), "longitude": round(f.target_lon, 7)}
                                if f.target_lat is not None and f.target_lon is not None else None),
            "road_distance_m": round(self.road_distance_m, 1) if self.road_distance_m is not None else None,
            "reasons": [r.as_dict() for r in self.judgement.reasons],
            "features": f.as_dict(),
        }


def prepare(sightings: Sequence[feat.VehicleSighting], ego_samples: Sequence[feat.EgoSample],
            s: ParkedVehicleSettings, vehicle_id: Optional[str] = None
            ) -> tuple[feat.TrackFeatures, CameraCalibration]:
    """Phase 1: features of the track with the vehicle's camera calibration."""
    cam = s.camera_for(vehicle_id)
    return feat.extract(sightings, feat.ego_motion(ego_samples), s, cam), cam


def needs_road_check(f: feat.TrackFeatures, s: ParkedVehicleSettings) -> bool:
    """The OSRM snap is worth asking: enabled, a GPS-estimated position, and
    every rule before it passed (do not query OSRM for moving / far tracks)."""
    if not s.road_check or f.target_lat is None or f.target_lon is None:
        return False
    return judge(f, s).verdict in (PARKED, "off_road")


def finish(f: feat.TrackFeatures, s: ParkedVehicleSettings, *, road_distance_m: Optional[float] = None,
           road_note: str = "", queue_peers: int = 0, vehicle_id: Optional[str] = None) -> Evaluation:
    """Phase 2: verdict, score, severity and the storage decision."""
    if road_distance_m is None and not road_note:
        road_note = ("road check disabled" if not s.road_check else
                     "no GPS-estimated position" if f.target_lat is None else "OSRM not queried")
    j = judge(f, s, road_distance_m=road_distance_m, road_note=road_note, queue_peers=queue_peers)
    value, comps = compute_score(f, s, j, road_distance_m)
    subtype = j.subtype
    ev = Evaluation(
        verdict=j.verdict, subtype=subtype, score=value,
        severity=compute_severity(subtype, f.vehicle_class, s),
        vehicle_class=f.vehicle_class, features=f, judgement=j, components=comps,
        road_distance_m=road_distance_m,
        camera_profile=vehicle_id if vehicle_id and vehicle_id in s.camera_profiles else None,
    )
    if j.verdict == PARKED:
        ev.store = True
    elif s.store_rejected_for_review and j.verdict in s.review_verdicts and j.verdict in REJECTION_CODES:
        ev.store, ev.rejection_code = True, REJECTION_CODES[j.verdict]
    return ev


def evaluate(sightings: Sequence[feat.VehicleSighting], ego_samples: Sequence[feat.EgoSample],
             s: ParkedVehicleSettings, *, vehicle_id: Optional[str] = None,
             snap: Optional[Callable[[float, float], Optional[float]]] = None, queue_peers: int = 0) -> Evaluation:
    """prepare + (optional synchronous road snap) + finish. A snapper that
    raises or returns None makes the road rule `road_distance_unknown`."""
    f, _cam = prepare(sightings, ego_samples, s, vehicle_id)
    distance, note = None, ""
    if snap is not None and needs_road_check(f, s):
        try:
            distance = snap(f.target_lat, f.target_lon)  # type: ignore[arg-type]
            note = "" if distance is not None else "road snap returned no road"
        except Exception as exc:     # OSRM down / timeout — skipped, not a failure
            note = f"road snap unavailable ({type(exc).__name__})"
    return finish(f, s, road_distance_m=distance, road_note=note, queue_peers=queue_peers, vehicle_id=vehicle_id)


# ── queue peers (rule 5, second part) ────────────────────────────────────────

@dataclass(frozen=True)
class PeerSummary:
    t_start: float
    t_end: float
    in_lane_stationary: bool


def peer_summary(f: feat.TrackFeatures, s: ParkedVehicleSettings) -> PeerSummary:
    return PeerSummary(f.t_start, f.t_end, f.in_corridor_at_closest and is_stationary(f, s))


def count_queue_peers(f: feat.TrackFeatures, peers: Sequence[PeerSummary]) -> int:
    """OTHER stationary in-corridor vehicles whose track overlaps this one in time."""
    return sum(1 for p in peers if p.in_lane_stationary and p.t_start <= f.t_end and p.t_end >= f.t_start)


# ── AI validation thresholds & traceability ─────────────────────────────────

def score_thresholds(s: ParkedVehicleSettings) -> dict:
    """Card-like {auto_accept, auto_reject} on verifier_score for ai_validation.build_policy
    (ai_validation.type_overrides["parked_vehicle"] still wins)."""
    return {"auto_accept": s.auto_accept_score, "auto_reject": s.auto_reject_score}


def merge_policy_cards(cards: dict, s: ParkedVehicleSettings) -> dict:
    """Thresholds of the model cards + the parked_vehicle score thresholds where the cards set none."""
    out = {k: dict(v) for k, v in cards.items()}
    entry = out.setdefault(DETECTION_TYPE, {})
    for k, v in score_thresholds(s).items():
        if entry.get(k) is None and v is not None:
            entry[k] = v
    return out


def rules_version(s: ParkedVehicleSettings) -> str:
    """`parked_vehicle_rules:v1@<sha256(settings)[:12]>` — stored as verifier_model, so a
    detection says which rule set and which settings judged it."""
    digest = hashlib.sha256(json.dumps(s.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
    return f"{RULES_VERSION}@{digest[:12]}"
