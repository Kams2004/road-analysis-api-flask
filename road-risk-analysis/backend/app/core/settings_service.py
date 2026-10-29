"""
Runtime platform settings, editable by an administrator at any time from
the web console (Settings → Platform): CAPTCHA, SMTP e-mail, map providers,
the security policy, the detection validation rules, the AI validation
mode, job processing
(queue capacity, frame sampling, retries), cloud storage OAuth clients,
Google sign-in client IDs, community reporting (votes, expiry), mobile app
distances, the collision-prevention thresholds and the predefined road
sections (tronçons) offered by the route searches of the console and the
global Cluster Configuration (`clustering`, app.modules.m06_hazards.clustering)
and the dashcam overlay layouts (`osd`, app.modules.m05_ai_detection.osd_settings)
and the exception-pattern rules (`driver_behaviour` — key kept —, app.modules.m09_exception_patterns.settings).

Each section is a validated pydantic model persisted as JSON in
`system_settings`. Fields listed in SECRET_FIELDS are encrypted at rest and
are never returned by the API — only a `<field>_set` flag is.
"""
import time
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import encrypt_secret, decrypt_secret
from app.modules.m11_community.models.signalement import SignalementType
from app.modules.m13_admin.models.system_setting import SystemSetting
from app.modules.m10_collision.collision.thresholds import CollisionSettings, set_current as _set_collision_thresholds
from app.modules.m06_hazards.lifecycle.settings import HazardLifecycleSettings
from app.modules.m05_ai_detection.parked_vehicle.settings import ParkedVehicleSettings
from app.modules.m05_ai_detection.osd_settings import OsdSettings
from app.modules.m06_hazards.clustering.settings import ClusteringSettings
from app.modules.m09_exception_patterns.settings import ExceptionPatternSettings

# Cloudflare's documented always-pass Turnstile test keys. They make login
# work out of the box in development; the Settings page shows a warning
# until real keys are entered.
TURNSTILE_TEST_SITE_KEY = "1x00000000000000000000AA"
TURNSTILE_TEST_SECRET   = "1x0000000000000000000000000000000AA"


class CaptchaSettings(BaseModel):
    provider:   Literal["turnstile", "recaptcha", "hcaptcha", "disabled"] = "turnstile"
    site_key:   str = TURNSTILE_TEST_SITE_KEY
    secret_key: str = TURNSTILE_TEST_SECRET


class SmtpSettings(BaseModel):
    # Gmail defaults: use an App Password (Google Account → Security →
    # 2-Step Verification → App passwords), not the account password.
    host:       str = "smtp.gmail.com"
    port:       int = Field(587, ge=1, le=65535)
    security:   Literal["starttls", "ssl", "none"] = "starttls"
    username:   str = ""
    password:   str = ""
    from_email: str = ""
    from_name:  str = "VIASafe"


class MapSettings(BaseModel):
    default_provider: Literal["osm", "google"] = "osm"
    osm_tile_url:     str = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
    osm_subdomains:   str = "abc"
    osm_attribution:  str = "© OpenStreetMap contributors"
    google_api_key:   str = ""
    google_map_type:  Literal["roadmap", "satellite", "terrain"] = "roadmap"
    google_language:  str = "en-US"
    google_region:    str = "CM"


class SecuritySettings(BaseModel):
    session_hours:        int  = Field(12, ge=1, le=720)
    lockout_threshold:    int  = Field(5, ge=1, le=100)
    lockout_minutes:      int  = Field(15, ge=1, le=1440)
    password_min_length:  int  = Field(8, ge=6, le=128)
    reset_token_minutes:  int  = Field(30, ge=5, le=1440)
    mfa_required_for_all: bool = False


class ValidationThresholds(BaseModel):
    # None = inherit the global value
    auto_reject_below: float | None = Field(None, ge=0, le=1)
    auto_accept_above: float | None = Field(None, ge=0, le=1)


class ValidationSettings(BaseModel):
    """Confidence routing of new detections (RRAP-06-007). Since the AI
    validation mode (section `ai_validation`) these two thresholds are the
    FALLBACK detector thresholds, used when neither `ai_validation` nor a model
    card sets one; `clip_seconds` and `distinct_subtype_types` apply as is."""
    auto_reject_below:      float        = Field(0.30, ge=0, le=1)
    auto_accept_above:      float | None = Field(None, ge=0, le=1)   # None = auto-accept disabled
    type_overrides:         dict[str, ValidationThresholds] = {}
    clip_seconds:           int          = Field(5, ge=1, le=30)      # ±context clip for location review
    # subtype must match to share a hazard (parked vehicles: blocking / wrong_side / roadside stay apart)
    distinct_subtype_types: list[str]    = ["traffic_sign", "parked_vehicle"]

    @model_validator(mode="after")
    def _accept_above_reject(self):
        if self.auto_accept_above is not None and self.auto_accept_above <= self.auto_reject_below:
            raise ValueError("auto_accept_above must be greater than auto_reject_below")
        return self


class AiClassThresholds(BaseModel):
    """Per-class thresholds of a multi-class crop classifier (traffic signs),
    on P(predicted class); None = inherit (card per-class, then type)."""
    auto_accept: float | None = Field(None, ge=0, le=1)  # P ≥ → the classifier is sure of this class
    auto_reject: float | None = Field(None, ge=0, le=1)  # P < → class unknown (unknown_sign, review)

    @model_validator(mode="after")
    def _ordered(self):
        if self.auto_accept is not None and self.auto_reject is not None and self.auto_accept <= self.auto_reject:
            raise ValueError("auto_accept must be greater than auto_reject")
        return self


class AiTypeThresholds(BaseModel):
    """Per detection type thresholds of the AI decision (None = inherit:
    model card, then section `validation`)."""
    auto_accept:     float | None = Field(None, ge=0, le=1)  # verifier P(class) ≥ → accept
    auto_reject:     float | None = Field(None, ge=0, le=1)  # verifier P(class) ≤ → reject
    detector_accept: float | None = Field(None, ge=0, le=1)  # detector confidence ≥ → may accept
    detector_reject: float | None = Field(None, ge=0, le=1)  # detector confidence < → reject
    # traffic_sign only: {sign code: thresholds}, overriding the classifier card's per_class
    per_class:       dict[str, AiClassThresholds] = {}

    @model_validator(mode="after")
    def _ordered(self):
        if self.auto_accept is not None and self.auto_reject is not None and self.auto_accept <= self.auto_reject:
            raise ValueError("auto_accept must be greater than auto_reject")
        if (self.detector_accept is not None and self.detector_reject is not None
                and self.detector_accept <= self.detector_reject):
            raise ValueError("detector_accept must be greater than detector_reject")
        return self


class AiValidationSettings(BaseModel):
    """AI validation mode (RRAP-05-012/016, RRAP-06-007, RRAP-13-001) — the
    SINGLE source of the automatic decisions the video processor takes on new
    detections (see app.modules.m05_ai_detection.ai_validation):

      manual     every detection goes to human review; the AI only suggests;
      assisted   auto-accept only when the detector AND (if required) the crop
                 classifier are confident and the location is confirmed;
                 auto-reject when the classifier (or a very low detector
                 confidence) says no; everything else → review;
      automatic  decide whenever the classifier is confident either way.

    Thresholds per type: `type_overrides` here, else the model cards
    (app.modules.m05_ai_detection.model_registry), else section `validation`
    (auto_accept_above / auto_reject_below), which remains the fallback when
    no model card is installed. A random `audit_sample_rate` share of the
    automatic decisions is left pending for a human (audit sample).

    traffic_sign (two-stage: detector + sign-code classifier): the classifier
    score is P(predicted code); thresholds per code (`type_overrides.traffic_sign
    .per_class`, else the card's `thresholds.per_class`, else the type values).
    P < auto_reject (or `unknown_sign` predicted) → subtype `unknown_sign`,
    review; the card's reject class (`not_a_sign`) predicted confidently →
    auto-reject `ai_not_a_sign`.

    Video consistency: an auto-accept needs the object in ≥
    `min_frames_for_auto_accept` sampled frames with an estimated position
    drifting ≤ `max_static_drift_m` while the vehicle moves (else review,
    `not_static`); a pothole box centre must lie below
    `pothole_min_y_ratio` × frame height (else `off_road_area`, see below).

    Pothole false-positive rules (app.modules.m05_ai_detection.static_check.pothole_rule_rejects),
    on the best box of a finished track: box centre above the road-area line →
    `pothole_off_road_action` `reject` (auto-reject `ai_off_road`) or `review`
    (accept blocker `off_road_area` only); box wider than
    `pothole_max_width_ratio` of the frame, larger than `pothole_max_area_ratio`
    of it, or — with `pothole_reject_edge_touching` — touching the left/right
    border (within 1 %) while wider than 25 % of the frame → auto-reject
    `ai_box_too_large` (roadside earth, bushes, building edges). These
    auto-rejects apply only in assisted / automatic mode; in manual mode they
    are review hints (AI suggestion)."""
    mode:                         Literal["manual", "assisted", "automatic"] = "assisted"
    audit_sample_rate:            float = Field(0.10, ge=0, le=1)
    require_classifier_agreement: bool  = True
    type_overrides:               dict[str, AiTypeThresholds] = {}
    # Video consistency (accept blockers, app.modules.m05_ai_detection.static_check): a real
    # sign / pothole stays put — seen in ≥ N sampled frames, estimated position
    # stable while the vehicle moves; potholes lie on the road surface.
    min_frames_for_auto_accept:   int   = Field(2, ge=1, le=50)
    max_static_drift_m:           float = Field(8.0, gt=0, le=500)
    pothole_min_y_ratio:          float = Field(0.45, ge=0, le=1)
    # Pothole false-positive rules (roadside / oversized boxes, see above).
    pothole_off_road_action:      Literal["review", "reject"] = "reject"
    pothole_max_width_ratio:      float = Field(0.40, gt=0, le=1)    # 1 = rule off
    pothole_max_area_ratio:       float = Field(0.12, gt=0, le=1)    # 1 = rule off
    pothole_reject_edge_touching: bool  = True
    # A box rejected as too large / roadside goes to human review instead when the pothole crop
    # classifier is at least this sure it is a pothole (large damaged areas close to the camera);
    # 1 = the size rules always reject.
    pothole_large_box_review_score: float = Field(0.8, ge=0, le=1)
    # Dashcam text overlay: pothole / sign / speed-bump boxes whose centre lies in a text field of
    # the job's OSD layout (date, GPS, plate, company …) are ignored — the overlay is not a road object.
    ignore_osd_text_boxes:        bool  = True


_FRAME_SKIP_SOURCES = ("upload", "google_drive", "onedrive", "sftp", "stream")


class ProcessingSettings(BaseModel):
    """Job queue and frame sampling (RRAP-07-009/010/013/014) — read by the
    jobs API at submission and by the worker for every job."""
    max_concurrent_jobs:  int            = Field(2, ge=1, le=32)
    max_attempts:         int            = Field(3, ge=1, le=10)
    frame_skip_default:   int            = Field(5, ge=1, le=60)
    frame_skip_by_source: dict[str, int] = {"upload": 5, "google_drive": 5, "onedrive": 5, "sftp": 5, "stream": 10}
    frame_skip_by_model:  dict[str, int] = {}
    retain_failed_hours:  int            = Field(72, ge=1, le=24 * 90)
    # Cloud imports: files downloaded from drives at once (across workers).
    cloud_import_concurrency: int        = Field(2, ge=1, le=16)

    @model_validator(mode="after")
    def _skips_in_range(self):
        for name, values in (("frame_skip_by_source", self.frame_skip_by_source),
                             ("frame_skip_by_model", self.frame_skip_by_model)):
            for key, v in values.items():
                if not isinstance(v, int) or not 1 <= v <= 60:
                    raise ValueError(f"{name}.{key} must be an integer between 1 and 60")
        unknown = set(self.frame_skip_by_source) - set(_FRAME_SKIP_SOURCES)
        if unknown:
            raise ValueError(f"frame_skip_by_source: unknown source(s) {sorted(unknown)}")
        return self


class CloudSettings(BaseModel):
    """OAuth client credentials for cloud storage sources (RRAP-07-002/004)."""
    google_client_id:        str = ""
    google_client_secret:    str = ""
    microsoft_client_id:     str = ""
    microsoft_client_secret: str = ""
    microsoft_tenant:        str = "common"
    sync_interval_minutes:   int = Field(15, ge=1, le=24 * 60)


class GoogleSigninSettings(BaseModel):
    """"Sign in with Google" (mobile app, later the web console). Client IDs
    are public OAuth identifiers from Google Cloud → Credentials; an ID token
    is accepted only when its `aud` is one of them."""
    enabled:           bool = False
    web_client_id:     str  = ""
    android_client_id: str  = ""
    ios_client_id:     str  = ""

    def client_ids(self) -> list[str]:
        return [c.strip() for c in (self.web_client_id, self.android_client_id, self.ios_client_id) if c.strip()]

    def configured(self) -> bool:
        return self.enabled and bool(self.client_ids())


_DEFAULT_EXPIRY_HOURS_BY_TYPE: dict[str, float] = {
    "police": 2, "embouteillage": 1, "accident": 3, "voie_bloquee": 3, "vehicule_panne": 2,
    "animaux": 1, "mauvais_temps": 3, "route_glissante": 6, "obstacle": 6, "danger": 6,
    "inondation": 12, "route_fermee": 24, "eboulement": 48, "travaux": 168,
    "nid_de_poule": 720, "dos_d_ane": 720,
}


class CommunitySettings(BaseModel):
    """Community reports (signalements) are validated by the COMMUNITY'S
    VOTES — "still there" / "not there" cast from near the report — unlike
    AI detections, which go through AI confidence/severity routing and human
    review (section `validation`). Read by the signalements API."""
    vote_max_distance_m:        int   = Field(300, ge=10, le=50_000)   # a vote / pass counts only this close
    hide_after_absent_votes:    int   = Field(3, ge=1, le=100)          # counted "not there" → hidden (status annule, restorable)
    cancel_threshold:           int   = Field(3, ge=1, le=100)          # legacy alias of hide_after_absent_votes
    keep_visible_present_votes: int   = Field(2, ge=1, le=100)          # counted "still there" → confirmed, never marked uncertain
    confirm_extends_minutes:    int   = Field(30, ge=0, le=24 * 60)     # each counted "still there" keeps it visible this long
    ignore_threshold:           int   = Field(5, ge=1, le=1000)         # devices passing without voting → uncertain
    ignore_window_minutes:      int   = Field(60, ge=1, le=7 * 24 * 60) # only passes this recent count as ignores
    hide_when_ignored:          bool  = False                           # hide uncertain reports from the app
    default_expiry_hours:       float = Field(6, ge=0.25, le=24 * 365)
    expiry_hours_by_type:       dict[str, float] = Field(default_factory=lambda: dict(_DEFAULT_EXPIRY_HOURS_BY_TYPE))

    @model_validator(mode="before")
    @classmethod
    def _alias(cls, data):
        # Old clients/rows only know cancel_threshold.
        if isinstance(data, dict) and "cancel_threshold" in data and "hide_after_absent_votes" not in data:
            data = {**data, "hide_after_absent_votes": data["cancel_threshold"]}
        return data

    @model_validator(mode="after")
    def _types(self):
        self.cancel_threshold = self.hide_after_absent_votes
        known = {t.value for t in SignalementType}
        unknown = set(self.expiry_hours_by_type) - known
        if unknown:
            raise ValueError(f"expiry_hours_by_type: unknown report type(s) {sorted(unknown)}")
        for key, v in self.expiry_hours_by_type.items():
            if not isinstance(v, (int, float)) or not 0.25 <= v <= 24 * 365:
                raise ValueError(f"expiry_hours_by_type.{key} must be between 0.25 and 8760 hours")
        return self

    def expiry_hours(self, report_type: str) -> float:
        return float(self.expiry_hours_by_type.get(report_type, self.default_expiry_hours))


class MobileSettings(BaseModel):
    """Distances the mobile app works with (served by GET /mobile-config)."""
    visibility_radius_m: int = Field(1000, ge=100, le=50_000)  # nearby detections/reports/vehicles; also the server cap
    approach_alert_m:    int = Field(400, ge=20, le=10_000)    # first "X in N m" popup while navigating
    vote_prompt_m:       int = Field(150, ge=10, le=5_000)     # ask "Still there?" when this close
    route_corridor_m:    int = Field(60, ge=5, le=1_000)       # reports/detections along the planned route
    alert_cooldown_s:    int = Field(60, ge=0, le=3_600)       # min gap between two alerts for the same item
    # Travel-time realism: the routing engine assumes free-flowing roads at the
    # speed limit (Douala → Bafoussam ≈ 3 h 20 instead of 4 h 30 – 5 h). Each
    # route stretch is counted at most at this average speed, then × eta_factor.
    eta_max_speed_kmh:   int = Field(60, ge=20, le=130)        # realistic average speed cap per stretch
    eta_factor:          float = Field(1.1, ge=1.0, le=3.0)    # extra margin (checkpoints, trucks, towns)

    @model_validator(mode="after")
    def _ordered(self):
        if self.vote_prompt_m > self.approach_alert_m:
            raise ValueError("vote_prompt_m must not exceed approach_alert_m")
        if self.approach_alert_m > self.visibility_radius_m:
            raise ValueError("approach_alert_m must not exceed visibility_radius_m")
        return self


class RouteEndpoint(BaseModel):
    lat:   float = Field(..., ge=-90, le=90)
    lon:   float = Field(..., ge=-180, le=180)
    label: str   = Field("", max_length=120)


class RouteSection(BaseModel):
    """A predefined road section ("tronçon"): two named endpoints the console
    fills into Départ / Destination of its route searches. Serialised with
    `from` / `to` keys (`from` is a Python keyword, hence the alias)."""
    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    id:    str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name:  str = Field(..., min_length=1, max_length=120)
    from_: RouteEndpoint = Field(..., alias="from")
    to:    RouteEndpoint

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must not be empty")
        return v


def _section(id_: str, name: str, a: tuple[float, float, str], b: tuple[float, float, str]) -> dict:
    return {"id": id_, "name": name,
            "from": {"lat": a[0], "lon": a[1], "label": a[2]},
            "to":   {"lat": b[0], "lon": b[1], "label": b[2]}}


# City-centre coordinates of the main Cameroonian corridors.
_DOUALA    = (4.0511, 9.7679, "Douala")
_YAOUNDE   = (3.8480, 11.5021, "Yaoundé")
_BAFOUSSAM = (5.4781, 10.4176, "Bafoussam")
_LIMBE     = (4.0186, 9.2043, "Limbé")
_KRIBI     = (2.9404, 9.9101, "Kribi")
_BERTOUA   = (4.5774, 13.6846, "Bertoua")

DEFAULT_ROUTE_SECTIONS: list[dict] = [
    _section("douala-yaounde-n3",    "Douala – Yaoundé (N3)",    _DOUALA,  _YAOUNDE),
    _section("douala-bafoussam-n5",  "Douala – Bafoussam (N5)",  _DOUALA,  _BAFOUSSAM),
    _section("douala-limbe-n3",      "Douala – Limbé (N3)",      _DOUALA,  _LIMBE),
    _section("douala-kribi-n7",      "Douala – Kribi (N7)",      _DOUALA,  _KRIBI),
    _section("yaounde-bafoussam-n4", "Yaoundé – Bafoussam (N4)", _YAOUNDE, _BAFOUSSAM),
    _section("yaounde-bertoua-n10",  "Yaoundé – Bertoua (N10)",  _YAOUNDE, _BERTOUA),
]


class RoutesSettings(BaseModel):
    """Predefined road sections (tronçons) for the route searches of the
    Detection map, Historical risk analysis and Community reporting pages
    (read by everyone through GET /routes/sections)."""
    sections: list[RouteSection] = Field(
        default_factory=lambda: [RouteSection.model_validate(s) for s in DEFAULT_ROUTE_SECTIONS],
        max_length=200,
    )

    @model_validator(mode="after")
    def _unique_ids(self):
        seen: set[str] = set()
        for s in self.sections:
            if s.id in seen:
                raise ValueError(f"sections: duplicate id '{s.id}'")
            seen.add(s.id)
        return self


SECTIONS: dict[str, type[BaseModel]] = {
    "captcha":    CaptchaSettings,
    "smtp":       SmtpSettings,
    "maps":       MapSettings,
    "security":   SecuritySettings,
    "validation": ValidationSettings,
    "ai_validation": AiValidationSettings,
    "processing": ProcessingSettings,
    "cloud":      CloudSettings,
    "google_signin": GoogleSigninSettings,
    "community":  CommunitySettings,
    "mobile":     MobileSettings,
    "collision":  CollisionSettings,
    "routes":     RoutesSettings,
    "hazard_lifecycle": HazardLifecycleSettings,
    "parked_vehicle": ParkedVehicleSettings,
    "clustering": ClusteringSettings,
    "osd":        OsdSettings,
    "driver_behaviour": ExceptionPatternSettings,      # Exception patterns (key kept for stored rows)
}

SECRET_FIELDS: dict[str, set[str]] = {
    "captcha": {"secret_key"},
    "smtp":    {"password"},
    "maps":    {"google_api_key"},
    "cloud":   {"google_client_secret", "microsoft_client_secret"},
}


async def get_section(db: AsyncSession, section: str) -> BaseModel:
    """Return the section with secrets decrypted — for internal use only."""
    model = SECTIONS[section]
    row = await db.get(SystemSetting, section)
    if not row:
        return model()
    stored = dict(row.value or {})
    for field in SECRET_FIELDS.get(section, ()):
        enc_key = f"{field}_enc"
        if enc_key in stored:
            stored[field] = decrypt_secret(stored.pop(enc_key)) or ""
    known = {k: v for k, v in stored.items() if k in model.model_fields}
    return model(**{**model().model_dump(), **known})


# Short in-process cache for sections read on hot paths (every nearby query,
# vote, ping). update_section clears the local entry and tells the other
# processes at once (app.core.cache_bus); CACHE_TTL_S is the safety net.
CACHE_TTL_S = 10.0
_cache: dict[str, tuple[float, BaseModel]] = {}


async def get_cached(db: AsyncSession, section: str) -> BaseModel:
    hit = _cache.get(section)
    now = time.monotonic()
    if hit is not None and now - hit[0] < CACHE_TTL_S:
        return hit[1]
    value = await get_section(db, section)
    _cache[section] = (now, value)
    return value


def clear_cache(section: str | None = None) -> None:
    if section is None:
        _cache.clear()
    else:
        _cache.pop(section, None)


def redact(section: str, value: BaseModel) -> dict:
    """Public view of a section: secrets replaced by `<field>_set` flags."""
    data = value.model_dump()
    for field in SECRET_FIELDS.get(section, ()):
        data[f"{field}_set"] = bool(data.pop(field))
    if section == "captcha":
        data["using_test_keys"] = (
            value.provider == "turnstile" and value.site_key == TURNSTILE_TEST_SITE_KEY
        )
    if section == "smtp":
        data["configured"] = smtp_configured(value)
    if section == "cloud":
        data["redirect_uri"] = cloud_redirect_uri()
        data["google_configured"] = bool(value.google_client_id and value.google_client_secret)
        data["microsoft_configured"] = bool(value.microsoft_client_id and value.microsoft_client_secret)
    if section == "google_signin":
        data["configured"] = value.configured()
    return data


def cloud_redirect_uri() -> str:
    """OAuth redirect URI to register with Google / Microsoft (shown in Settings)."""
    from app.core.config import settings
    base = settings.CLOUD_OAUTH_BASE_URL or settings.WEB_BASE_URL
    return f"{base.rstrip('/')}/settings/cloud/callback"


def smtp_configured(s: SmtpSettings) -> bool:
    return bool(s.host and s.username and s.password and (s.from_email or s.username))


async def update_section(db: AsyncSession, section: str, patch: dict, actor: str | None) -> tuple[dict, dict]:
    """
    Merge `patch` into the section and persist it. A secret field that is
    absent or null keeps its current value; an empty string clears it.
    Returns (before, after) redacted views for the audit trail.
    """
    model = SECTIONS[section]
    current = await get_section(db, section)
    before = redact(section, current)

    merged = current.model_dump()
    if section == "community" and "cancel_threshold" in patch and "hide_after_absent_votes" not in patch:
        patch = {**patch, "hide_after_absent_votes": patch["cancel_threshold"]}
    for key, val in patch.items():
        if key not in model.model_fields:
            continue
        if key in SECRET_FIELDS.get(section, ()) and val is None:
            continue
        merged[key] = val
    # Sections with nested maps (hazard_lifecycle.policies) merge them field by field.
    merge_patch = getattr(model, "merge_patch", None)
    if merge_patch is not None:
        merged = merge_patch(current.model_dump(),
                             {k: v for k, v in patch.items() if k in model.model_fields})
    updated = model(**merged)  # raises ValidationError → 422 at the route

    stored = updated.model_dump()
    for field in SECRET_FIELDS.get(section, ()):
        secret = stored.pop(field)
        if secret:
            stored[f"{field}_enc"] = encrypt_secret(secret)

    row = await db.get(SystemSetting, section)
    if row is None:
        row = SystemSetting(section=section)
        db.add(row)
    row.value = stored
    row.updated_at = datetime.utcnow()
    row.updated_by = actor
    await db.commit()
    clear_cache(section)
    if section == "collision":
        _set_collision_thresholds(updated)  # type: ignore[arg-type]
    if section == "clustering":
        from app.modules.m06_hazards.clustering import service as clustering_service
        clustering_service.invalidate(broadcast=False)
    from app.core import cache_bus
    cache_bus.publish(f"settings:{section}")      # the other API / worker processes, at once
    if section == "driver_behaviour":
        # Settings change cached answers (zones, time zone); severity rules change the daily summaries
        # (they are not used until rebuilt: app/modules/m08_historical/rollups.ready).
        from app.modules.m08_historical import agg_cache
        agg_cache.bump()
        try:
            from app.workers.event_feed_tasks import rebuild_event_rollups
            rebuild_event_rollups.delay()
        except Exception:
            import logging
            logging.getLogger(__name__).warning("summary rebuild not queued (worker queue unavailable)")
    return before, redact(section, updated)
