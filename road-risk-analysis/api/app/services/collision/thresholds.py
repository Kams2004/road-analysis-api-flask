"""
Runtime-editable collision-prevention thresholds (Settings → Collision
prevention, section `collision`).

The detector (Step 4) and the alert engine (Step 5) are pure functions; they
read `current()` — an in-process snapshot — instead of env constants. The
async orchestrators (trajectory/collision/alert services) call `refresh()`
first, which reloads the section from Postgres at most every CACHE_TTL_S
seconds, so an administrator's change reaches every API/worker process
within ~10 s. The env values in app/core/config.py (COLLISION_*, ALERT_*,
TRAJECTORY_*_TELEMETRY_S) are only the defaults.

Per-level semantics (one entry per alert level):
  max_distance_m   distance-at-closest-approach floor: a conflict that close
                   is at least this level …
  distance_ttc_s   … provided its time to closest approach is also within
                   this many seconds (null = no time condition)
  max_ttc_s        TTC band: a conflict whose time to closest approach is at
                   or below this is at least this level (null = no band)
A conflict reaches a level only when BOTH its distance at closest approach
is within max_distance_m AND its time to closest approach is within the
level's TTC limit (max_ttc_s; legacy distance_ttc_s values are folded into
max_ttc_s on load — the larger of the two wins). CRITICAL therefore needs a
short TTC *and* a small miss distance.
Scenario multipliers scale every distance/TTC threshold for one encounter
geometry (>1 = alert earlier, <1 = later): head-on (relative heading
≥135°), crossing (45–135°), same direction (<45°).

Genuine-conflict gates (see detector.assess_conflict):
  min_closing_speed_mps      vehicles must approach each other at least this
                             fast (average range rate toward the CPA) —
                             separating pairs are never alerted
  max_horizon_s              the CPA must lie in the future, within this many
                             seconds
  road_link_margin_m         two predicted road paths closer than this are
                             considered connected (same road / junction);
                             farther apart + both reliably map-matched =
                             unrelated roads → no alert
  unrelated_monitor_distance_m  …except MONITOR when the CPA on unrelated
                             roads is still this close
  lateral_margin_m           opposite-direction vehicles on the same road
                             whose lateral (lane) separation is at least this
                             are passing, not colliding → no alert
  lateral_max_accuracy_m     lateral offsets are only trusted when both GPS
                             fixes report an accuracy at or below this
Hysteresis (alerts/engine.py, alert_service.py):
  deescalate_after_s         a lower level must persist this long before the
                             alert de-escalates (escalation is immediate)
  resolve_after_s            a pair that merely dropped below the thresholds
                             is resolved after this long (separating /
                             passing / unrelated-road pairs resolve at once)
"""
import logging
import time
from typing import Optional

from pydantic import BaseModel, Field, model_validator

from app.core.config import settings

logger = logging.getLogger(__name__)

CACHE_TTL_S = 10.0
LEVELS = ("CRITICAL", "HIGH_WARNING", "WARNING", "MONITOR")   # most severe first
SCENARIOS = ("head_on", "crossing", "same_direction")


class CollisionLevel(BaseModel):
    max_distance_m: float = Field(..., gt=0, le=5000)
    distance_ttc_s: Optional[float] = Field(None, gt=0, le=120)
    max_ttc_s:      Optional[float] = Field(None, gt=0, le=120)


def _default_levels() -> dict[str, CollisionLevel]:
    return {
        "CRITICAL": CollisionLevel(max_distance_m=settings.COLLISION_CRITICAL_DISTANCE_M,
                                   distance_ttc_s=settings.COLLISION_CRITICAL_TTC_S,
                                   max_ttc_s=settings.ALERT_HIGH_WARNING_TTC_S),
        "HIGH_WARNING": CollisionLevel(max_distance_m=settings.COLLISION_HIGH_DISTANCE_M,
                                       distance_ttc_s=settings.COLLISION_HIGH_TTC_S,
                                       max_ttc_s=settings.ALERT_WARNING_TTC_S),
        "WARNING": CollisionLevel(max_distance_m=settings.COLLISION_MODERATE_DISTANCE_M,
                                  max_ttc_s=settings.ALERT_SAFE_TTC_S),
        "MONITOR": CollisionLevel(max_distance_m=settings.COLLISION_LOW_DISTANCE_M),
    }


def _default_multipliers() -> dict[str, float]:
    return {s: 1.0 for s in SCENARIOS}


def _fold_ttc(level: CollisionLevel) -> CollisionLevel:
    """One TTC limit per level: fold the legacy distance_ttc_s into max_ttc_s
    (the larger wins) so a level is simply 'distance ≤ X AND TTC ≤ Y'."""
    if level.distance_ttc_s is None:
        return level
    ttcs = [v for v in (level.distance_ttc_s, level.max_ttc_s) if v is not None]
    return CollisionLevel(max_distance_m=level.max_distance_m, distance_ttc_s=None, max_ttc_s=max(ttcs))


class CollisionSettings(BaseModel):
    candidate_radius_m:        float = Field(default_factory=lambda: settings.COLLISION_CANDIDATE_RADIUS_M, ge=50, le=10000)
    fresh_after_s:             float = Field(default_factory=lambda: settings.TRAJECTORY_FRESH_TELEMETRY_S, gt=0, le=300)
    stale_after_s:             float = Field(default_factory=lambda: settings.TRAJECTORY_STALE_TELEMETRY_S, gt=0, le=600)
    min_confidence_to_report:  float = Field(default_factory=lambda: settings.COLLISION_MIN_CONFIDENCE_TO_REPORT, ge=0, le=1)
    segment_trust_confidence:  float = Field(default_factory=lambda: settings.COLLISION_SEGMENT_TRUST_CONFIDENCE, ge=0, le=1)
    min_closing_speed_mps:     float = Field(0.5, ge=0, le=20)
    max_horizon_s:             float = Field(default_factory=lambda: float(max(settings.TRAJECTORY_HORIZONS_S)), gt=0, le=120)
    road_link_margin_m:        float = Field(8.0, ge=0, le=100)
    unrelated_monitor_distance_m: float = Field(10.0, ge=0, le=200)
    lateral_margin_m:          float = Field(2.5, ge=0, le=20)
    lateral_max_accuracy_m:    float = Field(10.0, gt=0, le=100)
    deescalate_after_s:        float = Field(3.0, ge=0, le=60)
    resolve_after_s:           float = Field(3.0, ge=0, le=120)
    levels:                    dict[str, CollisionLevel] = Field(default_factory=_default_levels)
    scenario_multipliers:      dict[str, float] = Field(default_factory=_default_multipliers)

    @model_validator(mode="after")
    def _consistent(self):
        unknown = set(self.levels) - set(LEVELS)
        if unknown:
            raise ValueError(f"levels: unknown level(s) {sorted(unknown)}")
        defaults = _default_levels()
        self.levels = {lvl: _fold_ttc(self.levels.get(lvl, defaults[lvl])) for lvl in LEVELS}
        # thresholds must widen from CRITICAL to MONITOR
        dists = [self.levels[l].max_distance_m for l in LEVELS]
        if dists != sorted(dists):
            raise ValueError("levels: max_distance_m must increase from CRITICAL to MONITOR")
        ttcs = [self.levels[l].max_ttc_s for l in LEVELS if self.levels[l].max_ttc_s is not None]
        if ttcs != sorted(ttcs):
            raise ValueError("levels: max_ttc_s must increase from CRITICAL to MONITOR")
        unknown = set(self.scenario_multipliers) - set(SCENARIOS)
        if unknown:
            raise ValueError(f"scenario_multipliers: unknown scenario(s) {sorted(unknown)}")
        for key, v in self.scenario_multipliers.items():
            if not isinstance(v, (int, float)) or not 0.25 <= v <= 4:
                raise ValueError(f"scenario_multipliers.{key} must be between 0.25 and 4")
        self.scenario_multipliers = {s: float(self.scenario_multipliers.get(s, 1.0)) for s in SCENARIOS}
        if self.fresh_after_s >= self.stale_after_s:
            raise ValueError("fresh_after_s must be lower than stale_after_s")
        return self

    def multiplier(self, relative_heading_deg: Optional[float]) -> float:
        s = scenario(relative_heading_deg)
        return self.scenario_multipliers.get(s, 1.0) if s else 1.0


def scenario(relative_heading_deg: Optional[float]) -> Optional[str]:
    if relative_heading_deg is None:
        return None
    if relative_heading_deg >= 135:
        return "head_on"
    if relative_heading_deg >= 45:
        return "crossing"
    return "same_direction"


# ── process-wide snapshot ────────────────────────────────────────────────────
_current: Optional[CollisionSettings] = None
_loaded_at: float = 0.0


def current() -> CollisionSettings:
    """Thresholds to use right now — the last loaded section, or the env
    defaults until one has been loaded (pure code and unit tests)."""
    return _current if _current is not None else CollisionSettings()


def set_current(cfg: Optional[CollisionSettings]) -> None:
    """Install a snapshot (after an admin update, or None to reset to defaults)."""
    global _current, _loaded_at
    _current = cfg
    _loaded_at = time.monotonic() if cfg is not None else 0.0


async def refresh(db=None, force: bool = False) -> CollisionSettings:
    """Reload from the settings table if the snapshot is older than CACHE_TTL_S.
    A database error keeps the previous snapshot (never breaks the pipeline)."""
    global _loaded_at
    if not force and _loaded_at > 0 and time.monotonic() - _loaded_at < CACHE_TTL_S:
        return current()
    from app.services.settings_service import get_section
    try:
        if db is not None:
            cfg = await get_section(db, "collision")
        else:
            from app.db.session import AsyncSessionLocal
            async with AsyncSessionLocal() as session:
                cfg = await get_section(session, "collision")
        set_current(cfg)  # type: ignore[arg-type]
    except Exception:
        logger.warning("Collision thresholds reload failed — keeping the previous values", exc_info=True)
        _loaded_at = time.monotonic()   # don't hammer a failing database
    return current()
