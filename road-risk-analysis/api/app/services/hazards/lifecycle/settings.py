"""
Settings section `hazard_lifecycle` (GET/PUT /platform-settings/hazard_lifecycle).

Global rules (expiry check period, timezone, daylight window, what makes a
video pass valid, vote eligibility, recurring spots) plus one TypePolicy per
hazard type. A TypePolicy may carry `subtype_overrides` — partial policies
({field: value}) applied on top of it for one subtype. Types without a
policy use `default_policy`.

`policies` is merged over the built-in defaults: a PUT that sends
{"policies": {"parked_vehicle": {"absences_to_resolve": 3}}} changes that
single field and keeps every other value (see merge_patch).
"""
from typing import Any, Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator

PRIORITIES = ("low", "normal", "high")


class TypePolicy(BaseModel):
    """Lifecycle policy of one hazard type (or, merged with an override, of one subtype)."""
    # Expiry: hours after the last sighting; None = never expires.
    expiry_hours:               Optional[float] = Field(None, gt=0, le=24 * 365)
    # Heavy vehicles (settings.heavy_vehicle_classes, read from the detection
    # details `vehicle_class`) use this expiry instead; None = no special case.
    heavy_vehicle_expiry_hours: Optional[float] = Field(None, gt=0, le=24 * 365)
    # Video passes without the hazard → resolution `video_absence`.
    absence_resolution:         bool  = False
    absences_to_resolve:        int   = Field(2, ge=1, le=50)
    require_same_direction:     bool  = False   # unknown original heading → pass not valid
    pass_daylight_only:         bool  = False
    # Driver votes → resolution `votes`.
    votes_enabled:              bool  = True
    not_there_to_resolve:       int   = Field(2, ge=1, le=100)
    still_there_extends:        bool  = False   # a "still there" vote counts as a sighting
    # A new accepted sighting near a RESOLVED hazard reopens it (else a new hazard opens).
    reopen_on_sighting:         bool  = False
    # … only when the sighting comes within this many hours of the resolution
    # evidence (None = the policy's expiry hours; no expiry = no limit). Later
    # sightings open a NEW hazard (counted for recurring spots).
    reopen_window_hours:        Optional[float] = Field(None, gt=0, le=24 * 365)
    recurring_enabled:          bool  = False
    # Mobile app / alerts.
    show_to_drivers:            bool  = True
    alert_priority:             Literal["low", "normal", "high"] = "normal"
    subtype_overrides:          dict[str, dict[str, Any]] = {}

    @field_validator("subtype_overrides")
    @classmethod
    def _overrides_valid(cls, v: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        allowed = set(cls.model_fields) - {"subtype_overrides"}
        for subtype, patch in v.items():
            if not isinstance(patch, dict):
                raise ValueError(f"subtype_overrides.{subtype} must be an object")
            unknown = set(patch) - allowed
            if unknown:
                raise ValueError(f"subtype_overrides.{subtype}: unknown field(s) {sorted(unknown)}")
            # Validate the values by building a policy with them.
            cls(**patch)
        return v


def _defaults() -> dict[str, dict[str, Any]]:
    persistent = dict(expiry_hours=None, absence_resolution=False, votes_enabled=True,
                      not_there_to_resolve=3, recurring_enabled=False)
    return {
        # Repairs are slow and a missed pothole in a video is common: no
        # expiry, no absence resolution; drivers can still vote it away.
        "pothole":    dict(persistent),
        "speed_bump": dict(persistent),
        "traffic_sign": dict(expiry_hours=None, absence_resolution=True, absences_to_resolve=3,
                             require_same_direction=True, votes_enabled=True, not_there_to_resolve=3),
        # Transient: gone after hours, back again at the same places.
        "parked_vehicle": dict(
            expiry_hours=6, heavy_vehicle_expiry_hours=24,
            absence_resolution=True, absences_to_resolve=2, require_same_direction=False,
            votes_enabled=True, not_there_to_resolve=2, still_there_extends=True,
            reopen_on_sighting=True, recurring_enabled=True,
            subtype_overrides={
                "blocking":   {"expiry_hours": 2, "alert_priority": "high"},
                "wrong_side": {"expiry_hours": 4, "alert_priority": "high"},
                "roadside":   {"expiry_hours": 6, "alert_priority": "low", "show_to_drivers": True},
            },
        ),
    }


DEFAULT_POLICIES: dict[str, dict[str, Any]] = _defaults()

# Hazard type → processing-job model keys (jobs.enabled_models) whose run
# could have seen it. A video pass only counts when one of them was enabled.
DEFAULT_TYPE_MODELS: dict[str, list[str]] = {
    "pothole":        ["pothole"],
    "traffic_sign":   ["signs"],
    "speed_bump":     ["speedbump"],
    "parked_vehicle": ["vehicles", "parked_vehicle"],
}


def _merge_policies(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Per type, the patch's fields over the base's (subtype_overrides merged per subtype)."""
    out = {k: dict(v) if isinstance(v, dict) else v for k, v in base.items()}
    for type_, fields in (patch or {}).items():
        if not isinstance(fields, dict):
            out[type_] = fields          # let validation report it
            continue
        cur = dict(out.get(type_) or {})
        for key, val in fields.items():
            if key == "subtype_overrides" and isinstance(val, dict):
                subs = {s: dict(p) for s, p in (cur.get("subtype_overrides") or {}).items()}
                for sub, sub_patch in val.items():
                    if sub_patch is None:
                        subs.pop(sub, None)      # null removes an override
                    elif isinstance(sub_patch, dict):
                        subs[sub] = {**subs.get(sub, {}), **sub_patch}
                    else:
                        subs[sub] = sub_patch
                cur["subtype_overrides"] = subs
            else:
                cur[key] = val
        out[type_] = cur
    return out


class HazardLifecycleSettings(BaseModel):
    """Module 6 hazard lifecycle — see docs/spec-module6-hazard-lifecycle.md."""
    # Periodic expiry (Celery beat checks every minute whether a run is due).
    expiry_check_minutes:        int   = Field(5, ge=1, le=24 * 60)
    timezone:                    str   = "Africa/Douala"
    daylight_hours:              list[int] = Field(default_factory=lambda: [6, 18])  # [start, end) local hours
    # OSD timestamps (detections.captured_at) are the camera's local time.
    osd_time_is_local:           bool  = True

    # A video pass is valid when …
    pass_max_distance_m:         float = Field(30.0, gt=0, le=1000)    # closest confirmed GPS fix this close
    pass_max_speed_kmh:          float = Field(60.0, gt=0, le=300)     # … driven at most this fast there
    pass_heading_tolerance_deg:  float = Field(60.0, ge=0, le=180)     # … in the direction of the sighting
    pass_require_recording_time: bool  = True    # unknown recording time → not a valid pass
    type_models:                 dict[str, list[str]] = Field(default_factory=lambda: {
        k: list(v) for k, v in DEFAULT_TYPE_MODELS.items()})

    # Driver votes
    vote_max_distance_m:         int   = Field(300, ge=10, le=50_000)
    vote_cooldown_minutes:       int   = Field(60, ge=0, le=7 * 24 * 60)

    # Recurring spots
    recurring_min_occurrences:   int   = Field(3, ge=2, le=1000)
    recurring_window_days:       int   = Field(30, ge=1, le=3650)
    recurring_radius_m:          Optional[float] = Field(None, gt=0, le=5000)  # None = the hazard's cluster radius

    heavy_vehicle_classes:       list[str] = Field(default_factory=lambda: ["truck", "bus"])

    default_policy: TypePolicy = Field(default_factory=lambda: TypePolicy(
        expiry_hours=None, absence_resolution=False, votes_enabled=True, not_there_to_resolve=3))
    policies: dict[str, TypePolicy] = Field(
        default_factory=lambda: {k: TypePolicy(**v) for k, v in DEFAULT_POLICIES.items()})

    @model_validator(mode="before")
    @classmethod
    def _merge_default_policies(cls, data):
        if isinstance(data, dict) and isinstance(data.get("policies"), dict):
            base = {k: dict(v) for k, v in DEFAULT_POLICIES.items()}
            raw = {k: (v.model_dump() if isinstance(v, BaseModel) else v) for k, v in data["policies"].items()}
            data = {**data, "policies": _merge_policies(base, raw)}
        return data

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown timezone '{v}'")
        return v

    @field_validator("daylight_hours")
    @classmethod
    def _daylight(cls, v: list[int]) -> list[int]:
        if len(v) != 2 or not all(isinstance(h, int) and 0 <= h <= 24 for h in v) or v[0] >= v[1]:
            raise ValueError("daylight_hours must be [start, end] with 0 ≤ start < end ≤ 24")
        return v

    @classmethod
    def merge_patch(cls, current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
        """Used by settings_service.update_section: `policies` is merged per
        type and per field into the current values instead of replacing them."""
        merged = dict(current)
        for key, val in patch.items():
            if key == "policies" and isinstance(val, dict):
                cur = {k: (v.model_dump() if isinstance(v, BaseModel) else v)
                       for k, v in (current.get("policies") or {}).items()}
                merged["policies"] = _merge_policies(cur, val)
            elif key == "default_policy" and isinstance(val, dict):
                merged["default_policy"] = _merge_policies(
                    {"_": current.get("default_policy") or {}}, {"_": val})["_"]
            else:
                merged[key] = val
        return merged
