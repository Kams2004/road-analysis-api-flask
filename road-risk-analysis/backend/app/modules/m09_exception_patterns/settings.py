"""
Settings section `driver_behaviour` (Module 9 — Exception patterns; the section key is kept so
stored settings stay valid — fields of the former driver scoring are ignored when read).

`severity_multipliers` weight an exception by its normalised severity in the place risk (Module 8
current risk of a zone / route, collision context history) — a property of the event, not of
anyone. Normalised severity of an exception: the source's level when it sends one (low / medium / high
and common synonyms), else — for speeding with both speed and limit known — the excess over the
limit (`speeding_medium_excess_pct`, `speeding_high_excess_pct`), else `unknown`.

Exception zones: grid cells of `zone_cell_size_m` with at least `zone_min_events` exceptions in
the period (of the selected types). Hour-of-day / weekday patterns use `timezone` (also the
night factor of the collision context).
"""
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.modules.m08_historical.domain import HistoricalEventType

EXCEPTION_TYPES = tuple(t.value for t in HistoricalEventType)
SEVERITIES = ("low", "medium", "high", "unknown")
METHODOLOGY_VERSION = "exp-1"


class ExceptionPatternSettings(BaseModel):
    severity_multipliers: dict[str, float] = Field(default_factory=lambda: {
        "low": 0.5, "medium": 1.0, "high": 2.0, "unknown": 1.0})
    speeding_medium_excess_pct: float = Field(10.0, ge=0, le=500)
    speeding_high_excess_pct: float = Field(25.0, ge=0, le=500)
    zone_cell_size_m: float = Field(250.0, ge=25, le=5000)
    zone_min_events: int = Field(5, ge=1, le=10_000)
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
        bad = set(self.severity_multipliers) - set(SEVERITIES)
        if bad:
            raise ValueError(f"severity_multipliers: unknown severity {sorted(bad)}")
        for k, v in self.severity_multipliers.items():
            if not 0 <= v <= 100:
                raise ValueError(f"severity_multipliers.{k} must be between 0 and 100")
        if self.speeding_high_excess_pct < self.speeding_medium_excess_pct:
            raise ValueError("speeding_high_excess_pct must not be below speeding_medium_excess_pct")
        return self

    def multiplier(self, severity: str) -> float:
        return float(self.severity_multipliers.get(severity, 1.0))
