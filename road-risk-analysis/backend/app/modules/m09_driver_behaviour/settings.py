"""
Settings section `driver_behaviour` (Module 5 — Driver Behaviour Analytics).

Behaviour risk score of a driver / vehicle over a period (RRAP-09-011, the
documented scoring method):

    points  = Σ  type_weight[event_type] × severity_multiplier[severity]
    score   = 100 × min(1, points / saturation_points)          (0 … 100)

`severity` comes from the source when it sends one (low / medium / high and
common synonyms), else — for speeding with both speed and limit known —
from the excess over the limit (`speeding_medium_excess_pct`,
`speeding_high_excess_pct`), else `unknown` (multiplier 1). Without exposure
data (distance / driving time, RRAP-09-010) the score is an event-based
indicator, not a validated driver-performance assessment.

High-risk behaviour zones (RRAP-09-006): grid cells of `zone_cell_size_m`
with at least `zone_min_events` behaviour events in the period (all types,
or per type when a type is selected) and at least `zone_min_drivers`
distinct drivers/vehicles when identities exist.
"""
from typing import Literal

from pydantic import BaseModel, Field, model_validator

BEHAVIOUR_TYPES = ("SPEEDING", "HARD_BRAKING", "HARD_ACCELERATION", "HARSH_CORNERING")
SEVERITIES = ("low", "medium", "high", "unknown")
METHODOLOGY_VERSION = "dba-1"


class DriverBehaviourSettings(BaseModel):
    type_weights: dict[str, float] = Field(default_factory=lambda: {
        "SPEEDING": 1.0, "HARD_BRAKING": 1.5, "HARD_ACCELERATION": 1.2, "HARSH_CORNERING": 1.5})
    severity_multipliers: dict[str, float] = Field(default_factory=lambda: {
        "low": 0.5, "medium": 1.0, "high": 2.0, "unknown": 1.0})
    saturation_points: float = Field(40.0, gt=0, le=100_000)
    speeding_medium_excess_pct: float = Field(10.0, ge=0, le=500)
    speeding_high_excess_pct: float = Field(25.0, ge=0, le=500)
    high_risk_score: float = Field(60.0, ge=0, le=100)
    zone_cell_size_m: float = Field(250.0, ge=25, le=5000)
    zone_min_events: int = Field(5, ge=1, le=10_000)
    zone_min_drivers: int = Field(1, ge=1, le=1000)
    default_period: Literal["7d", "30d", "90d", "12m", "all"] = "90d"
    timezone: str = "Africa/Douala"          # hour-of-day / weekday patterns are local time

    @model_validator(mode="after")
    def _tz(self):
        from zoneinfo import ZoneInfo
        try:
            ZoneInfo(self.timezone)
        except Exception:
            raise ValueError(f"timezone: unknown time zone '{self.timezone}'")
        return self

    @model_validator(mode="after")
    def _check(self):
        unknown = set(self.type_weights) - set(BEHAVIOUR_TYPES)
        if unknown:
            raise ValueError(f"type_weights: unknown behaviour type(s) {sorted(unknown)}")
        for k, v in self.type_weights.items():
            if not 0 <= v <= 100:
                raise ValueError(f"type_weights.{k} must be between 0 and 100")
        bad = set(self.severity_multipliers) - set(SEVERITIES)
        if bad:
            raise ValueError(f"severity_multipliers: unknown severity {sorted(bad)}")
        for k, v in self.severity_multipliers.items():
            if not 0 <= v <= 100:
                raise ValueError(f"severity_multipliers.{k} must be between 0 and 100")
        if self.speeding_high_excess_pct < self.speeding_medium_excess_pct:
            raise ValueError("speeding_high_excess_pct must not be below speeding_medium_excess_pct")
        return self

    def weight(self, event_type: str) -> float:
        return float(self.type_weights.get(event_type, 1.0))

    def multiplier(self, severity: str) -> float:
        return float(self.severity_multipliers.get(severity, 1.0))
