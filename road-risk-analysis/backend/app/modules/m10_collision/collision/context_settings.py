"""
Settings of the Context Risk Index (`collision.context`, nested in the
`collision` section — app/modules/m10_collision/collision/thresholds.py).

Physics decides whether two vehicles are in conflict; the context only
modulates how early / how loudly (docs/COLLISION-PREVENTION.md):

    C = min(1, Σ weight[f] × factor[f])              factor[f] ∈ [0, 1]
    every distance / TTC threshold × (1 + max_widening × C)
    C ≥ boost_threshold → level + max_boost_steps (never above CRITICAL,
                          never from SAFE: no physical conflict, no alert)

Factors (each 0 … 1):
  history            weighted, recency-faded fleet exceptions of the place (HARD_BRAKING and
                     HARSH_CORNERING count double: `history_type_weights`), / history_saturation
  habitual_speeding  recency-faded SPEEDING events of the place / speeding_saturation
  vehicle_speeding   a party drives > speeding_tolerance_pct above the local limit (median
                     `speed_limit_mps` of the SPEEDING events of the place / neighbours);
                     1 at speeding_tolerance_pct + speeding_full_excess_pct
  hazards            validated hazards (`hazard_types`) within hazard_radius_m / hazard_saturation
  reports            visible community reports within report_radius_m, Σ report_kind_weights
  driver             behaviour score / 100 of the riskiest party when ≥ driver_behaviour.high_risk_score
  night              local time (driver_behaviour.timezone) in [night_start_hour, night_end_hour)
  poor_gps           a party's GPS accuracy > poor_gps_accuracy_m
  stale_data         a party's telemetry older than collision.fresh_after_s
"""
from typing import Dict, List

from pydantic import BaseModel, Field, model_validator

FACTORS = ("history", "habitual_speeding", "vehicle_speeding", "hazards", "reports", "driver",
           "night", "poor_gps", "stale_data")

# Community report type (app.modules.m11_community.models.signalement.SignalementType) → reason kind.
DEFAULT_REPORT_KINDS: Dict[str, str] = {
    "accident": "ACCIDENT_REPORTED",
    "travaux": "ROADWORK_REPORTED",
    "route_fermee": "ROADWORK_REPORTED",
    "voie_bloquee": "ROADWORK_REPORTED",
    "inondation": "FLOODING_REPORTED",
    "embouteillage": "CONGESTION_REPORTED",
    "obstacle": "HAZARD_REPORTED",
    "danger": "HAZARD_REPORTED",
    "route_glissante": "HAZARD_REPORTED",
    "eboulement": "HAZARD_REPORTED",
    "vehicule_panne": "HAZARD_REPORTED",
    "animaux": "HAZARD_REPORTED",
    "vehicule_dangereux": "HAZARD_REPORTED",
}
REPORT_REASON_KINDS = ("ACCIDENT_REPORTED", "ROADWORK_REPORTED", "FLOODING_REPORTED", "CONGESTION_REPORTED",
                       "HAZARD_REPORTED")


def _default_weights() -> Dict[str, float]:
    return {"history": 0.35, "habitual_speeding": 0.15, "vehicle_speeding": 0.30, "hazards": 0.15,
            "reports": 0.30, "driver": 0.25, "night": 0.10, "poor_gps": 0.10, "stale_data": 0.10}


class ContextSettings(BaseModel):
    enabled:             bool  = True
    max_widening:        float = Field(0.5, ge=0, le=1)       # thresholds × (1 + max_widening × C)
    boost_threshold:     float = Field(0.7, gt=0, le=1)       # C at / above this → level + max_boost_steps
    max_boost_steps:     int   = Field(1, ge=0, le=1)
    min_reason_contribution: float = Field(0.02, ge=0, le=1)  # factors contributing less are not listed
    weights:             Dict[str, float] = Field(default_factory=_default_weights)

    # place: exception history (precomputed every refresh_interval_s)
    history_window_days:    int   = Field(365, ge=7, le=3650)
    history_half_life_days: float = Field(90.0, gt=0, le=3650)
    history_saturation:     float = Field(20.0, gt=0, le=10_000)
    history_type_weights:   Dict[str, float] = Field(default_factory=lambda: {
        "ACCIDENT": 5.0, "HARD_BRAKING": 3.0, "HARSH_CORNERING": 3.0, "HARD_ACCELERATION": 1.2, "SPEEDING": 1.0})
    # place: habitual speeding and the local limit
    speeding_saturation:     float = Field(10.0, gt=0, le=10_000)
    speed_limit_min_events:  int   = Field(3, ge=1, le=1000)   # fewer in the cell → its 8 neighbours too
    speeding_tolerance_pct:  float = Field(10.0, ge=0, le=200)
    speeding_full_excess_pct: float = Field(30.0, gt=0, le=500)
    # place: hazards and community reports
    hazard_types:        List[str] = Field(default_factory=lambda: ["pothole", "speed_bump"])
    hazard_radius_m:     float = Field(100.0, ge=10, le=1000)
    hazard_saturation:   float = Field(2.0, gt=0, le=100)
    report_radius_m:     float = Field(300.0, ge=10, le=3000)
    report_kinds:        Dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_REPORT_KINDS))
    report_kind_weights: Dict[str, float] = Field(default_factory=lambda: {
        "ACCIDENT_REPORTED": 1.0, "ROADWORK_REPORTED": 0.8, "FLOODING_REPORTED": 0.8,
        "CONGESTION_REPORTED": 0.6, "HAZARD_REPORTED": 0.6})
    # parties
    driver_window_days:  int   = Field(90, ge=1, le=3650)
    # conditions
    night_start_hour:    int   = Field(19, ge=0, le=23)
    night_end_hour:      int   = Field(6, ge=0, le=23)
    poor_gps_accuracy_m: float = Field(20.0, gt=0, le=500)
    # precomputation (beat task app.workers.collision_tasks.refresh_collision_context)
    refresh_interval_s:  int   = Field(300, ge=60, le=86_400)
    max_cells:           int   = Field(200_000, ge=100, le=5_000_000)

    @model_validator(mode="after")
    def _check(self):
        unknown = set(self.weights) - set(FACTORS)
        if unknown:
            raise ValueError(f"context.weights: unknown factor(s) {sorted(unknown)}")
        for k, v in self.weights.items():
            if not isinstance(v, (int, float)) or not 0 <= v <= 1:
                raise ValueError(f"context.weights.{k} must be between 0 and 1")
        self.weights = {f: float(self.weights.get(f, _default_weights()[f])) for f in FACTORS}
        for k, v in self.history_type_weights.items():
            if not 0 <= v <= 100:
                raise ValueError(f"context.history_type_weights.{k} must be between 0 and 100")
        bad = set(self.report_kinds.values()) - set(REPORT_REASON_KINDS)
        if bad:
            raise ValueError(f"context.report_kinds: unknown kind(s) {sorted(bad)}")
        for k, v in self.report_kind_weights.items():
            if k not in REPORT_REASON_KINDS or not 0 <= v <= 1:
                raise ValueError(f"context.report_kind_weights.{k}: unknown kind or weight outside 0…1")
        if self.night_start_hour == self.night_end_hour:
            raise ValueError("context: night_start_hour and night_end_hour must differ")
        return self

    def weight(self, factor: str) -> float:
        return self.weights.get(factor, 0.0)
