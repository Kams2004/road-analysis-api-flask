"""
Settings section `clustering` — the global Cluster Configuration
(RRAP-06-009/010/011). Two environment profiles:

  urban  in agglomeration (inside an agglomeration / custom urban zone):
         dense streets, many distinct hazards a few metres apart → tighter.
  rural  out of agglomeration (open road, inter-city tronçons): GPS fixes
         from fast vehicles spread wider and hazards are sparse → wider.

Each profile has `default_radius_m` plus per-type overrides for AI
detections (`type_radius`, keyed by detection type) and community reports
(`signalement_radius`, keyed by SignalementType). Zones
(app.modules.m06_hazards.models.cluster_zone) may override any of them locally.

Defaults: urban 30 m / rural 60 m. Urban 30 m keeps two potholes on
neighbouring street corners apart while absorbing the ~5–15 m spread of
dash-cam OSD GPS; rural 60 m roughly matches the previous ~55 m geo
de-duplication of the processor at highway speeds (a 5 fps sampling at
90 km/h is ~5 m per frame plus GPS lag). Community reports describe
situations rather than objects, so a few report types that extend along
the road (traffic jams, closures, police checks, works) default wider.
"""
import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.modules.m11_community.models.signalement import SignalementType

Environment = Literal["urban", "rural"]
ENVIRONMENTS: tuple[str, ...] = ("urban", "rural")

# Detection types the console offers (others are accepted if well-formed:
# a new detector type can be configured before its model ships).
DETECTION_TYPES: tuple[str, ...] = ("pothole", "traffic_sign", "speed_bump", "parked_vehicle")
SIGNALEMENT_TYPES: tuple[str, ...] = tuple(t.value for t in SignalementType)

MIN_RADIUS_M = 1.0
MAX_RADIUS_M = 10_000.0
_TYPE_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def check_radius_map(v: Optional[dict], *, signalements: bool, field: str) -> dict[str, float]:
    """Validate a {type: radius_m} map; None/empty → {}."""
    out: dict[str, float] = {}
    for key, r in (v or {}).items():
        if signalements and key not in SIGNALEMENT_TYPES:
            raise ValueError(f"{field}: unknown report type '{key}'")
        if not signalements and not _TYPE_KEY.match(str(key)):
            raise ValueError(f"{field}: invalid detection type '{key}'")
        if r is None:
            continue                      # null = inherit (dropped)
        if not isinstance(r, (int, float)) or not MIN_RADIUS_M <= float(r) <= MAX_RADIUS_M:
            raise ValueError(f"{field}.{key} must be between {MIN_RADIUS_M:g} and {MAX_RADIUS_M:g} m")
        out[str(key)] = float(r)
    return out


class ClusterProfile(BaseModel):
    """Clustering parameters of one environment (urban or rural)."""
    default_radius_m:   float = Field(..., ge=MIN_RADIUS_M, le=MAX_RADIUS_M,
                                      description="Radius for every type without an override, metres.")
    type_radius:        dict[str, float] = Field(default_factory=dict, description=(
        "Per AI-detection-type radius, metres (pothole, traffic_sign, speed_bump, parked_vehicle …)."))
    signalement_radius: dict[str, float] = Field(default_factory=dict, description=(
        "Per community-report-type radius, metres (keys: SignalementType values)."))

    @field_validator("type_radius", mode="before")
    @classmethod
    def _types(cls, v):
        return check_radius_map(v, signalements=False, field="type_radius")

    @field_validator("signalement_radius", mode="before")
    @classmethod
    def _sig(cls, v):
        return check_radius_map(v, signalements=True, field="signalement_radius")


def _urban() -> ClusterProfile:
    return ClusterProfile(default_radius_m=30, type_radius={}, signalement_radius={
        "embouteillage": 150, "route_fermee": 100, "travaux": 100, "police": 100, "accident": 75,
        "inondation": 100,
    })


def _rural() -> ClusterProfile:
    return ClusterProfile(default_radius_m=60, type_radius={}, signalement_radius={
        "embouteillage": 500, "route_fermee": 300, "travaux": 300, "police": 250, "accident": 150,
        "inondation": 200, "eboulement": 200,
    })


class ClusteringSettings(BaseModel):
    """The global Cluster Configuration (Settings → Cluster configuration)."""
    urban: ClusterProfile = Field(default_factory=_urban, description="In agglomeration.")
    rural: ClusterProfile = Field(default_factory=_rural, description="Out of agglomeration.")
    unzoned_environment: Environment = Field("rural", description=(
        "Profile used where no zone contains the point. `rural`: anything outside every "
        "agglomeration is out of agglomeration."))
    merge_signalements: bool = Field(True, description=(
        "Merge a new community report into an active report of the same type within the resolved "
        "radius (the new report is kept, linked to the existing one, and hidden from the app)."))
    default_corridor_width_m: float = Field(300, ge=20, le=5000, description=(
        "Default total width of a road-section corridor (half on each side of the road), metres."))

    @model_validator(mode="before")
    @classmethod
    def _profiles(cls, data):
        # Accept partial profile dicts (missing keys keep the defaults).
        if isinstance(data, dict):
            data = dict(data)
            for env, factory in (("urban", _urban), ("rural", _rural)):
                cur = data.get(env)
                if isinstance(cur, dict):
                    data[env] = {**factory().model_dump(), **cur}
        return data

    def profile(self, environment: str) -> ClusterProfile:
        return self.urban if environment == "urban" else self.rural

    @classmethod
    def merge_patch(cls, current: dict, patch: dict) -> dict:
        """settings_service.update_section: a profile is merged field by field."""
        merged = dict(current)
        for key, val in patch.items():
            if key in ENVIRONMENTS and isinstance(val, dict):
                merged[key] = {**(current.get(key) or {}), **val}
            else:
                merged[key] = val
        return merged
