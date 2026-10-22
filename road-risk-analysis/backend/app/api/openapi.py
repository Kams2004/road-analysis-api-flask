"""
OpenAPI 3 / Swagger documentation of the VIASafe API (RRAP-12-006, RRAP-12-014).

Everything that describes the API as a whole lives here: metadata, the
Markdown introduction, the ordered tag list, the shared error models and the
`errors(...)` helper the routers use to document their error responses.

`install_openapi(app)` replaces `app.openapi` with a builder that
post-processes FastAPI's schema. Access rules are NOT written by hand: each
operation's `security` requirement, its "**Permission:**" line and the
`x-permission` / `x-api-key` extensions are derived from the very guards
that enforce them (`module_guard`, `client_route_guard`, `require`,
`current_user`, `account_user`, `api_client` … in app/api/deps.py, which
carry an `openapi_access` description). The documentation therefore cannot
drift from the enforcement. An operation can still state its access
explicitly with `openapi_extra={"x-access": "<kind>"}` when the handler
checks the caller itself (see `_EXPLICIT_ACCESS`).

Regenerate the committed export with `python -m app.export_openapi`.
"""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Union

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from pydantic.json_schema import models_json_schema

API_TITLE = "VIASafe Road Risk Analysis Platform API"
API_VERSION = "1.0.0"
# Build identification, injected by the image build / deployment when known.
API_BUILD = os.getenv("APP_BUILD") or os.getenv("GIT_COMMIT") or "dev"
DOCS_URL = "https://github.com/Kams2004/road-analysis-api-flask/blob/main/road-risk-analysis/docs/"

# ── Tags (one per router, ordered by platform module) ──────────────────────

TAG_HEALTH = "Health"
TAG_AUTH = "Authentication"
TAG_USERS = "Users"
TAG_ROLES = "Roles & permissions"
TAG_API_CLIENTS = "API clients"
TAG_STATS = "Dashboard & statistics"
TAG_MAP = "Map configuration"
TAG_ROUTES = "Routes & geocoding"
TAG_DETECTIONS = "Detections"
TAG_HAZARDS = "Hazards"
TAG_CLUSTERS = "Cluster configuration"
TAG_REJECTION = "Rejection reasons"
TAG_DATASETS = "Datasets"
TAG_AI_MODELS = "AI models"
TAG_JOBS = "Jobs"
TAG_STREAMS = "Streams"
TAG_CLOUD = "Cloud connections"
TAG_CLOUD_IMPORTS = "Cloud imports"
TAG_HISTORICAL = "Historical risk"
TAG_DRIVER_BEHAVIOUR = "Driver behaviour"
TAG_VEHICLES = "Vehicles & telemetry"
TAG_ROAD_STATE = "Road state"
TAG_TRAJECTORY = "Trajectory"
TAG_COLLISIONS = "Collisions"
TAG_ALERTS = "Alerts"
TAG_SIGNALEMENTS = "Community reports"
TAG_MOBILE = "Mobile configuration"
TAG_SETTINGS = "Platform settings"
TAG_AUDIT = "Audit trail"
TAG_REPORTS = "Reports"


def _review_transitions_md() -> str:
    from app.modules.m06_hazards.detection_workflow import TRANSITIONS
    rows = "\n".join(
        f"| `{frm}` | `{to}` | {', '.join(f'`{a}`' for a in sorted(actions))} |"
        for (frm, to), actions in TRANSITIONS.items()
    )
    return "| From | To | Actions |\n|---|---|---|\n" + rows


def _tags() -> list[dict[str, Any]]:
    docs = DOCS_URL
    return [
        {"name": TAG_HEALTH, "description": "**Module 0** — liveness probe for load balancers and monitoring."},
        {"name": TAG_AUTH, "description": (
            "**Module 1** — sign-in (password + CAPTCHA, mobile app, Google), multi-factor authentication "
            "(TOTP), sessions, the signed-in user's own profile and password reset. See the *Authentication* "
            "section of the introduction for the complete flow."),
         "externalDocs": {"description": "Security, users and roles", "url": docs + "security-and-users.md"}},
        {"name": TAG_USERS, "description": (
            "**Module 1** — user administration (RRAP-01-010/011/012). Every change is audited. You cannot "
            "deactivate or delete yourself and the last active Administrator cannot be removed.")},
        {"name": TAG_ROLES, "description": (
            "**Module 1** — roles and their View/Add/Edit/Delete matrix per permission module "
            "(RRAP-01-007/008/009/013). `GET /roles/modules` lists the module keys used in every "
            "`**Permission:** module:action` line of this documentation.")},
        {"name": TAG_API_CLIENTS, "description": (
            "**Module 1 / 12** — API keys for the mobile app, the simulator and integrations (RRAP-12-015). "
            "The clear key is returned once, at creation or rotation; only its prefix is stored in clear.")},
        {"name": TAG_STATS, "description": "**Module 2** — operational dashboard KPIs, time series and counters."},
        {"name": TAG_MAP, "description": (
            "**Module 3** — base-map tiles the caller should display (OpenStreetMap or Google Map Tiles, "
            "per user preference — RRAP-01-012 / RRAP-03-001).")},
        {"name": TAG_ROUTES, "description": (
            "**Module 3** — route-search helpers: predefined road sections (tronçons), OSRM driving route "
            "preview and Nominatim geocoding proxied by the server (cache, ≤ 1 upstream request/s).")},
        {"name": TAG_DETECTIONS, "description": (
            "**Modules 5 & 6** — AI detections (potholes, traffic signs, speed bumps …) with their evidence, "
            "the human validation workflow and the spatial queries used by the mobile app.\n\n"
            "**Review statuses** — `location_pending` (no confirmed position yet), `pending` (awaiting a "
            "decision), `validated` (accepted as predicted), `corrected` (accepted after a class or location "
            "correction), `rejected` (not a hazard — human or automatic rule), `resolved` (accepted hazard "
            "later fixed). Every transition writes a review-history row and an audit entry; anything outside "
            "this table is refused with `409 invalid_transition`:\n\n" + _review_transitions_md()),
         "externalDocs": {"description": "Module 6 specification", "url": docs + "spec-module6-hazard-validation.md"}},
        {"name": TAG_HAZARDS, "description": (
            "**Module 6** — hazards: the de-duplicated physical road hazards that accepted detections are "
            "clustered into (status `active` → `resolved`, reopenable).")},
        {"name": TAG_CLUSTERS, "description": (
            "**Module 6** — the global Cluster Configuration (RRAP-06-009/010/011): one configuration with an "
            "**urban** (in agglomeration) and a **rural** (out of agglomeration) profile — default radius plus "
            "per-detection-type and per-report-type values — and **cluster zones** where it applies: "
            "agglomerations (city outlines from OpenStreetMap), road-section corridors along a tronçon "
            "(OSRM route) and custom polygons. Every detection (hazard clustering, processor de-duplication, "
            "re-sightings) and every community report (merging) is clustered with the radius resolved at its "
            "own position (`GET /cluster-zones/resolve`). `/cluster-config` and `/cluster-areas` are "
            "deprecated wrappers.")},
        {"name": TAG_REJECTION, "description": (
            "**Module 6** — catalogue of rejection reasons offered to validators, per detection type "
            "(`all` applies to every type). Seeded reasons cannot be removed.")},
        {"name": TAG_DATASETS, "description": (
            "**Module 5** — training data for model improvement. *Live training data*: every decision on a "
            "detection (accept / correct incl. a moved box / reject, human or automatic; reopen removes it) "
            "keeps a training sample and writes, in the `datasets` bucket, the clean frame + YOLO label "
            "(`<type>/detector/`) and the padded crop (`<type>/classifier/<class>/`); list them with "
            "`GET /datasets/samples`, download with `GET /datasets/export`, write Label Studio box reviews "
            "back with `PUT /datasets/frames/…/labels` (docs/TRAINING-DATA.md). *Dataset versions*: "
            "immutable crop snapshots built from the validation labels (`POST /datasets/export`).")},
        {"name": TAG_AI_MODELS, "description": (
            "**Modules 5 & 13** — model registry and AI validation quality (RRAP-05-010/011/012/016, "
            "RRAP-13-016). Two-stage pothole pipeline: the detector proposes boxes, a crop classifier scores "
            "each crop (`verifier_score` = P(pothole)). Each weights file may come with a JSON *model card* "
            "(version, datasets, metrics, proposed thresholds). Settings section `ai_validation` picks the "
            "**mode** — `manual` (humans decide everything), `assisted` (default: automatic decision only "
            "when detector and classifier agree and the location is confirmed) or `automatic` (decide "
            "whenever the classifier is confident) — and the share of automatic decisions drawn for a human "
            "**audit**. Thresholds: `ai_validation.type_overrides`, else the model cards, else section "
            "`validation`. Automatic decisions are taken by actor `system` and reversible with "
            "`POST /detections/{id}/reopen`.")},
        {"name": TAG_JOBS, "description": (
            "**Module 7** — video processing jobs (upload, cloud import, stream segment).\n\n"
            "**Job statuses** — `pending` → `processing` → `done` | `failed` | `cancelled`. A `pending` or "
            "`processing` job can be cancelled; a `failed` or `cancelled` job can be retried while its source "
            "media is retained and `attempts < max_attempts`."),
         "externalDocs": {"description": "Module 7 specification", "url": docs + "spec-module7-jobs.md"}},
        {"name": TAG_STREAMS, "description": "**Module 7** — RTSP/HTTP video streams recorded in segments and processed as jobs."},
        {"name": TAG_CLOUD, "description": (
            "**Module 7 — input sources.** Google Drive / OneDrive accounts connected with OAuth: browse "
            "folders and files (`/browse`, `/thumbnail`), import a selection (`POST /{id}/imports`), and "
            "optionally auto-import a watched folder (`auto_import`). The OAuth *client* credentials stay in "
            "Platform settings → `cloud`.\n\n"
            "**SFTP servers** (provider `sftp`) use the same browse / import endpoints (file and folder ids "
            "are absolute remote paths, confined to the server's base path). Create one with "
            "`POST /cloud-connections/sftp`; the password is never sent to the API — it is read from "
            "`infra/secrets/sftp.env` (`SFTP_PASSWORD_<KEY>=…`) at each connection. `…/sftp/connect`, "
            "`/disconnect`, `/forget-host-key` (host key trusted on first use) and `/log` (FileZilla-style "
            "connection log + live link state) complete it. See docs/SFTP-SOURCE.md.")},
        {"name": TAG_CLOUD_IMPORTS, "description": (
            "**Module 7 — input sources.** Import batches created from a drive selection or a watched folder.\n\n"
            "**Item statuses** — `queued` → `downloading` → `uploading_to_storage` → `queued_for_processing` "
            "→ `processing` → `done`, or `failed` / `cancelled` / `skipped_duplicate`. Download progress "
            "(`bytes_done`, `percent`, `speed_bps`, `eta_s`) is live while downloading; afterwards the status "
            "and `job_progress` follow the processing job. Poll `GET /imports/{batch_id}` every 1–2 s while "
            "`active`.")},
        {"name": TAG_HISTORICAL, "description": (
            "**Module 8** (functional Module 4, Historical Risk Analysis) — historical driving events "
            "imported into stable grid cells and turned into normalised 0–1 risk scores: zone, route and "
            "heatmap queries, top risk zones, risk trend over time, import history.")},
        {"name": TAG_DRIVER_BEHAVIOUR, "description": (
            "**Module 9** (functional Module 5, Driver Behaviour Analytics, RRAP-09) — speeding, harsh "
            "braking, hard acceleration and harsh cornering events (stored in the historical event store): "
            "KPIs per behaviour, driver / vehicle risk scores and rankings, heatmaps, time trends and "
            "high-risk behaviour zones. Scores are event-based indicators, not accident probabilities.")},
        {"name": TAG_VEHICLES, "description": (
            "**Module 10** — vehicle/device registration and live telemetry pings (WGS84, m/s). A ping feeds "
            "the road-state → trajectory → collision → alert pipeline.")},
        {"name": TAG_ROAD_STATE, "description": "**Module 10** — map-matched road context of a live vehicle (segment, lane direction, road class …)."},
        {"name": TAG_TRAJECTORY, "description": "**Module 10** — short-horizon trajectory prediction of a live vehicle along the road network."},
        {"name": TAG_COLLISIONS, "description": "**Module 10** — pairwise conflict analysis between live vehicles (distance, time-to-collision, risk level)."},
        {"name": TAG_ALERTS, "description": (
            "**Module 10** — driver alerts derived from conflicts. **Levels**: `SAFE` < `MONITOR` < `WARNING` "
            "< `HIGH_WARNING` < `CRITICAL`; **lifecycle**: `DETECTED` → `ACTIVE` → `RESOLVED`.")},
        {"name": TAG_SIGNALEMENTS, "description": (
            "**Module 11** — community reports (*signalements*) from the mobile app and their community "
            "validation: present/absent votes counted only within `community.vote_max_distance_m`, one per "
            "voter; passes (driving by without voting) mark a report *uncertain*. **Statuses**: `actif`, "
            "`annule`, `rejete` (moderation). **Vote outcomes** (`reason`): `too_far`, `duplicate`, "
            "`no_position`."),
         "externalDocs": {"description": "Mobile app specification", "url": docs + "spec-mobile-app.md"}},
        {"name": TAG_MOBILE, "description": (
            "**Module 12** — runtime distances and thresholds the mobile app works with "
            "(Settings → Mobile app distances / Community reporting / Collision prevention).")},
        {"name": TAG_SETTINGS, "description": (
            "**Module 13** — platform settings by section (CAPTCHA, SMTP, maps, security policy, validation "
            "rules, AI validation mode, mobile, community, collision …). Secrets are write-only; changes are "
            "audited.")},
        {"name": TAG_AUDIT, "description": "**Module 13** — audit trail of sign-ins, administrative changes and every state-changing user call (RRAP-13-006)."},
        {"name": TAG_REPORTS, "description": (
            "**Module 14** — PDF reports generated server-side (AI hazards, community reports, historical "
            "risk, driver behaviour, collision prevention) with type, period, area and path (road section "
            "or from → to corridor) filters; generated files are kept for re-download (RRAP-13-011/012/013).")},
    ]


TAG_NAMES = tuple(t["name"] for t in _tags())


_DESCRIPTION = """
REST/JSON API of the **VIASafe Road Risk Analysis Platform** (CAMTRACK). It serves the web console,
the VIASafe mobile app, the vehicle simulator and approved integrations.

## Platform modules

| # | Module | Tags |
|---|---|---|
| 1 | Security, users & roles | Authentication · Users · Roles & permissions · API clients |
| 2 | Dashboard | Dashboard & statistics |
| 3 | Maps, routes & geospatial | Map configuration · Routes & geocoding |
| 4 | Preliminary road-risk assessment (ARR) | *(web console — no dedicated endpoints yet)* |
| 5 | AI hazard detection | Detections · Datasets · AI models |
| 6 | Hazard track & human validation | Detections · Hazards · Cluster areas · Rejection reasons |
| 7 | Video/data ingestion & processing jobs | Jobs · Streams · Cloud connections |
| 8 | Historical risk analysis | Historical risk |
| 9 | Driver behaviour | *(uses Historical risk data)* |
| 10 | Connected vehicles & collision prevention | Vehicles & telemetry · Road state · Trajectory · Collisions · Alerts |
| 11 | Community reporting | Community reports |
| 12 | APIs & integration | API clients · Mobile configuration · this document |
| 13 | Configuration, audit & reporting | Platform settings · Audit trail · AI models |

## Authentication

Every operation lists the credentials it accepts (padlock icon) and, below its description, the
**Permission** it needs as `module:action` (actions: `view`, `add`, `edit`, `delete`; see
`GET /roles/modules`). A user's permissions are the union of their active roles.

### Users — session token (`BearerAuth`)

1. `POST /auth/login` with `{identifier, password, captcha_token}` (identifier = e-mail or username).
2. The answer's `status` is one of:
   * `ok` — `token` is an opaque session token valid until `expires_at`;
   * `mfa_required` — send the authenticator code with the returned `challenge_token` to
     `POST /auth/mfa/verify` (the challenge is valid a few minutes);
   * `mfa_setup_required` — the user's role requires MFA: call `POST /auth/mfa/setup` then
     `POST /auth/mfa/enable` with the `challenge_token`; enabling completes the sign-in.
3. Send `Authorization: Bearer <token>` on every call. `POST /auth/logout`, a password change/reset or
   deactivation revokes the session immediately (`401 session_expired` afterwards).

Failed sign-ins count towards a lockout (`423 account_locked`, `locked_until` in the error).

### Mobile app & integrations — API key (`ApiKeyAuth`)

Keys are issued in *Administration → API clients* and sent as `X-API-Key: <key>`. A key only reaches
an explicit allow-list of endpoints (operations showing *API key* in their security); any other call
gets `403 forbidden`. The mobile app signs users in with `POST /auth/mobile/login`,
`POST /auth/mobile/register` or `POST /auth/google` and then sends **both** headers — the key never
gains the user's permissions; the user's token only identifies them (`/auth/me`, votes, reports).

### Development mode

When the server runs with `AUTH_ENFORCED=false` (demo stack) the permission checks of the module
routers are skipped and anonymous calls are accepted; user-administration endpoints still require a
signed-in user. API-key allow-lists always apply.

## Errors

Errors use HTTP status codes and a JSON body `{"detail": {"code": "<code>", "message": "<text>", …}}`
(model `ErrorResponse`). `code` is stable and meant for programs; `message` is for people; some errors
add fields (e.g. `module`/`action` on `forbidden`, `locked_until`, `requirements`, `errors`).
A few older endpoints still answer `{"detail": "<text>"}` (model `LegacyErrorResponse`), and request
validation errors use FastAPI's `{"detail": [{"loc", "msg", "type"}]}` (model `HTTPValidationError`).

| Status | Common codes |
|---|---|
| 400 | `captcha_failed`, `invalid_code`, `invalid_password`, `invalid_reset_token`, `mfa_not_started`, `google_not_configured`, `smtp_not_configured`, `cloud_not_configured`, `invalid_state`, `state_expired` |
| 401 | `not_authenticated`, `session_expired`, `invalid_api_key`, `api_key_required`, `invalid_credentials`, `challenge_expired`, `invalid_google_token` |
| 403 | `forbidden`, `account_inactive`, `mfa_required_by_role` |
| 404 | `not_found` |
| 409 | `invalid_transition`, `email_taken`, `username_taken`, `last_admin`, `role_in_use`, `system_role`, `signalement_inactive`, `not_authorized`, `connection_disabled` |
| 422 | `weak_password`, `invalid_sort`, `invalid_status`, `invalid_filter`, `invalid_reason`, `invalid_location`, `invalid_settings`, `unknown_role`, `device_id_required` |
| 423 | `account_locked` |
| 429 | `rate_limited` |
| 502 | `geocoding_failed`, `smtp_failed`, `google_failed`, `oauth_failed`, `cloud_failed` |
| 503 | `captcha_unavailable`, `google_unavailable`, `queue_unavailable` |

## Conventions

* **Pagination** — list endpoints take `skip` (offset, ≥ 0) and `limit` (page size; the maximum is
  given on each endpoint, typically 200, 500 for detection lists) and answer `{"total": n, "items": […]}`
  where `total` counts every match before paging.
* **Rate limits** — `POST /auth/mobile/register`: `MOBILE_REGISTER_PER_HOUR` attempts per client IP
  per hour (default 5) → `429 rate_limited`. Forgot-password requests send at most one e-mail per
  account per minute (the answer is identical either way). Geocoding is throttled upstream to one
  request per second (cached).
* **Time** — timestamps are ISO 8601 in **UTC**; naive values (no offset) are UTC.
* **Units** — distances and radii in **metres**, speeds in **m/s** (`speed_kmh` fields are km/h),
  accelerations in m/s², headings/bearings in degrees clockwise from north (0–360), durations in
  seconds unless the field name says otherwise.
* **Coordinates** — WGS84 decimal degrees, `latitude` ∈ [-90, 90], `longitude` ∈ [-180, 180].
* **Identifiers** — UUID strings unless stated otherwise.
* **Runtime settings** — distances and thresholds used by the endpoints (visibility radius, route
  corridor, vote distance, validation thresholds, collision levels …) come from *Platform settings*
  sections (`mobile`, `community`, `validation`, `collision`, `routes`, `security` …), editable at run time
  via `PUT /platform-settings/{section}` and published to the app by `GET /mobile-config/`.

## Interactive use

Click **Authorize**, paste a session token (from `POST /auth/login`) and/or an API key, then use
**Try it out**. Authorizations persist across page reloads in this browser. The same document is
served as JSON at `/openapi.json` and rendered by ReDoc at `/redoc`; a copy is committed in
`docs/openapi.json` (`python -m app.export_openapi`).
"""


def api_description() -> str:
    return _DESCRIPTION.strip() + f"\n\n---\nVersion `{API_VERSION}` · build `{API_BUILD}`"


APP_METADATA: dict[str, Any] = dict(
    title=API_TITLE,
    version=API_VERSION,
    summary="Road hazard detection, validation, connected-vehicle collision prevention and community reporting.",
    contact={"name": "CAMTRACK — Information Technology & Innovation"},
    # Placeholders until the legal terms of the public API are published.
    license_info={"name": "Proprietary — © CAMTRACK, all rights reserved"},
    terms_of_service="https://viasafe.example.com/terms",
    servers=[
        {"url": "http://localhost:8080", "description": "Local / demo stack"},
        {"url": "https://api.viasafe.example.com", "description": "Production (placeholder)"},
    ],
    swagger_ui_parameters={
        "persistAuthorization": True,
        "docExpansion": "none",
        "filter": True,
        "tryItOutEnabled": True,
        "displayRequestDuration": True,
        "defaultModelsExpandDepth": 0,
    },
)

EXTERNAL_DOCS = {
    "description": "Platform documentation (docs/*.md)",
    "url": DOCS_URL,
}


# ── Error models ────────────────────────────────────────────────────────────

class ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="allow", json_schema_extra={
        "additionalProperties": True,
        "examples": [{"code": "forbidden", "message": "You do not have permission for this action.",
                      "module": "validations", "action": "edit"}],
    })
    code: str = Field(..., description="Stable machine-readable error code.", examples=["not_found"])
    message: str = Field(..., description="Human-readable explanation (English).", examples=["Detection not found."])


class ErrorResponse(BaseModel):
    """Error body of the platform: `{"detail": {"code", "message", …}}`."""
    detail: ErrorDetail


class LegacyErrorResponse(BaseModel):
    """Error body of older endpoints: `{"detail": "<message>"}`."""
    detail: str = Field(..., examples=["Job not found"])


_STATUS_TEXT = {
    400: "Bad request", 401: "Not authenticated", 403: "Forbidden", 404: "Not found", 409: "Conflict",
    422: "Unprocessable entity", 423: "Locked", 429: "Too many requests", 502: "Upstream service failed",
    503: "Service unavailable",
}
_DEFAULT_ERRORS = {
    401: ("not_authenticated", "Please sign in."),
    403: ("forbidden", "You do not have permission for this action."),
    404: ("not_found", "Not found."),
    409: ("conflict", "The request conflicts with the current state."),
    422: ("invalid_request", "Some values are invalid."),
    429: ("rate_limited", "Too many requests. Please try again later."),
    502: ("upstream_failed", "An upstream service failed."),
    503: ("unavailable", "The service is temporarily unavailable."),
}


@dataclass(frozen=True)
class Err:
    status: int
    code: Optional[str]          # None = legacy plain-string detail
    message: str
    extra: tuple = ()
    summary: Optional[str] = None  # example title in Swagger UI (default: the code)


def err(status: int, code: str, message: str, *, summary: Optional[str] = None, **extra: Any) -> Err:
    """A structured error `{"detail": {"code", "message", **extra}}` raised by a
    route. `summary` titles the example when one status has several."""
    return Err(status, code, message, tuple(extra.items()), summary)


def legacy(status: int, message: str, *, summary: Optional[str] = None) -> Err:
    """A plain `HTTPException(status, "<message>")` (`{"detail": "<message>"}`)."""
    return Err(status, None, message, (), summary)


def errors(*specs: Union[int, Err]) -> dict[int, dict[str, Any]]:
    """
    `responses=` value documenting the errors a route raises, e.g.
    `errors(404, err(409, "invalid_transition", "Cannot accept …"), legacy(422, "…"))`.
    A bare status uses the platform's generic code for it (404 → not_found …).
    401/403 for the caller's credentials are added automatically from the guards.
    """
    grouped: dict[int, list[Err]] = {}
    for s in specs:
        if isinstance(s, int):
            code, msg = _DEFAULT_ERRORS.get(s, ("error", _STATUS_TEXT.get(s, "Error")))
            s = Err(s, code, msg)
        grouped.setdefault(s.status, []).append(s)
    return {status: _response(items) for status, items in grouped.items()}


def _example_key(e: Err, taken: dict) -> str:
    key = e.code or "legacy"
    n = 2
    while key in taken:
        key, n = f"{e.code or 'legacy'}_{n}", n + 1
    return key


def _response(items: list[Err]) -> dict[str, Any]:
    refs = []
    if any(e.code for e in items):
        refs.append({"$ref": "#/components/schemas/ErrorResponse"})
    if any(e.code is None for e in items):
        refs.append({"$ref": "#/components/schemas/LegacyErrorResponse"})
    examples: dict[str, Any] = {}
    for e in items:
        value = {"detail": {"code": e.code, "message": e.message, **dict(e.extra)}} if e.code else {"detail": e.message}
        examples[_example_key(e, examples)] = {"summary": e.summary or e.code or e.message, "value": value}
    codes = sorted({e.code for e in items if e.code})
    desc = _STATUS_TEXT.get(items[0].status, "Error")
    if codes:
        desc += " — " + ", ".join(f"`{c}`" for c in codes)
    return {
        "description": desc,
        "content": {"application/json": {
            "schema": refs[0] if len(refs) == 1 else {"anyOf": refs},
            "examples": examples,
        }},
    }


# ── Access derived from the guards ──────────────────────────────────────────

BEARER = {"BearerAuth": []}
API_KEY = {"ApiKeyAuth": []}
BOTH = {"BearerAuth": [], "ApiKeyAuth": []}

# x-access kinds an operation may declare explicitly (handler checks itself)
_EXPLICIT_ACCESS: dict[str, tuple[list[dict], str]] = {
    "public": ([], "None — public endpoint."),
    "challenge_or_user": (
        [{}, BEARER],
        "A `challenge_token` in the body (sign-in that requires MFA enrolment) **or** a signed-in user "
        "(`Authorization: Bearer`)."),
    "any_caller": (
        [BEARER, API_KEY],
        "A signed-in user (Bearer) or an API key. Anonymous calls are accepted only when "
        "`AUTH_ENFORCED=false`."),
}


@dataclass
class Access:
    security: list[dict]
    auth_text: str
    permission: Optional[str] = None
    api_key: bool = False
    public: bool = False
    key_forbidden: bool = False   # an API key presented here gets 403 (router allow-list)


def _calls(dependant) -> Iterable[Any]:
    for d in dependant.dependencies:
        if d.call is not None:
            yield d.call
        yield from _calls(d)


def route_access(route: APIRoute, method: str) -> Access:
    """Credentials and permission of one operation, read from its guards."""
    method, path = method.upper(), route.path_format
    infos = [getattr(c, "openapi_access", None) for c in _calls(route.dependant)]
    infos = [i for i in infos if i]
    by_kind = {i["kind"]: i for i in infos}

    if "module" in by_kind:
        g = by_kind["module"]
        module, action = g["resolve"](method, path)
        client = (method, path) in g["client_routes"]
        return Access(
            [BEARER, API_KEY] if client else [BEARER],
            "Signed-in user (`Authorization: Bearer`)" + (" **or** API key (`X-API-Key`)." if client else "."),
            f"{module}:{action}", api_key=client, key_forbidden=not client)
    has_client_guard = "client_routes" in by_kind
    client_ok = (method, path) in by_kind.get("client_routes", {}).get("client_routes", set())
    access = _route_access(by_kind, client_ok)
    access.key_forbidden = has_client_guard and not client_ok
    return access


def _route_access(by_kind: dict, client_ok: bool) -> Access:
    if "permission" in by_kind:
        p = by_kind["permission"]
        return Access([BEARER], "Signed-in user (`Authorization: Bearer`).", f"{p['module']}:{p['action']}")
    if "any_view" in by_kind:
        mods = by_kind["any_view"]["modules"]
        return Access([BEARER], "Signed-in user (`Authorization: Bearer`).",
                      " | ".join(f"{m}:view" for m in mods))
    if "user" in by_kind:
        return Access([BEARER], "Any signed-in user (`Authorization: Bearer`) — no module permission needed.")
    if "account" in by_kind:
        return Access(
            [BEARER, BOTH] if client_ok else [BEARER],
            "The signed-in user's own session (`Authorization: Bearer`)"
            + (", directly or through the mobile app (`X-API-Key` + `Authorization: Bearer`)." if client_ok else "."),
            api_key=client_ok)
    if "client" in by_kind:
        return Access([API_KEY], "API key (`X-API-Key`) of a registered client (mobile app).", api_key=True)
    if client_ok:
        return Access([{}, API_KEY], "None — public endpoint (also reachable with an API key).",
                      api_key=True, public=True)
    return Access([], "None — public endpoint.", public=True)


def _merge_error(op: dict, status: int, specs: list[Err]) -> None:
    responses = op.setdefault("responses", {})
    new = _response(specs)
    key = str(status)
    if key not in responses:
        responses[key] = new
        return
    content = responses[key].setdefault("content", {}).setdefault("application/json", {})
    examples = content.setdefault("examples", {})
    for name, ex in new["content"]["application/json"]["examples"].items():
        if not any(v.get("value") == ex["value"] for v in examples.values()):
            examples[name if name not in examples else f"{name}_auth"] = ex


def _auth_errors(access: Access) -> dict[int, list[Err]]:
    if access.public:
        out: dict[int, list[Err]] = {}
        if access.api_key:
            out[401] = [err(401, "invalid_api_key", "Unknown or revoked API key.")]
        return out
    e401 = [err(401, "not_authenticated", "Please sign in.", summary="not_authenticated — no credentials"),
            err(401, "session_expired", "Your session has expired. Please sign in again.",
                summary="session_expired — token unknown, expired or revoked")]
    if access.api_key or any("ApiKeyAuth" in s for s in access.security):
        e401.append(err(401, "invalid_api_key", "Unknown or revoked API key."))
    if access.security == [API_KEY]:
        e401 = [err(401, "api_key_required", "This endpoint requires an API key."),
                err(401, "invalid_api_key", "Unknown or revoked API key.")]
    e403 = []
    if access.permission:
        mod, _, act = access.permission.split(" | ")[0].partition(":")
        e403.append(err(403, "forbidden", "You do not have permission for this action.",
                        summary=f"forbidden — user lacks {mod}:{act}", module=mod, action=act))
    if access.key_forbidden:
        e403.append(err(403, "forbidden", "This API key is not allowed to call this endpoint.",
                        summary="forbidden — API key not allowed on this endpoint"))
    out = {401: e401}
    if e403:
        out[403] = e403
    return out


def _component_schemas() -> dict[str, Any]:
    _, top = models_json_schema(
        [(m, "validation") for m in (ErrorResponse, LegacyErrorResponse)],
        ref_template="#/components/schemas/{model}",
    )
    return top.get("$defs", {})


def api_routes(app: FastAPI) -> list[Any]:
    """The application's API routes with their full (prefixed) path and the
    dependencies of the router they were included with. Recent FastAPI
    versions include routers lazily (`_IncludedRouter`); older ones copy
    flat `APIRoute`s into `app.routes`."""
    try:
        from fastapi.openapi.utils import _get_api_route_for_openapi
        from fastapi.routing import iter_route_contexts
    except ImportError:  # FastAPI without lazy router inclusion
        return [r for r in app.routes if isinstance(r, APIRoute)]
    out = []
    for ctx in iter_route_contexts(app.routes):
        route = _get_api_route_for_openapi(ctx)
        if route is not None:
            out.append(route)
    return out


def build_openapi(app: FastAPI) -> dict[str, Any]:
    schema = get_openapi(
        title=app.title, version=app.version, summary=app.summary, description=app.description,
        routes=app.routes, tags=app.openapi_tags, servers=app.servers, contact=app.contact,
        license_info=app.license_info, terms_of_service=app.terms_of_service,
        openapi_version=app.openapi_version,
    )
    schema["externalDocs"] = EXTERNAL_DOCS
    schema["info"]["x-build"] = API_BUILD
    comps = schema.setdefault("components", {})
    comps.setdefault("schemas", {}).update(_component_schemas())
    comps["securitySchemes"] = {
        "BearerAuth": {
            "type": "http", "scheme": "bearer", "bearerFormat": "opaque session token",
            "description": "Session token returned by `POST /auth/login` (or the mobile sign-in endpoints) "
                           "when `status` is `ok`. Server-side session: revoked on logout, password change "
                           "or deactivation.",
        },
        "ApiKeyAuth": {
            "type": "apiKey", "in": "header", "name": "X-API-Key",
            "description": "Key of a registered API client (Administration → API clients). Limited to the "
                           "endpoints that list it; combine with `BearerAuth` to act for a signed-in "
                           "mobile user.",
        },
    }

    for route in api_routes(app):
        if not route.include_in_schema:
            continue
        item = schema["paths"].get(route.path_format, {})
        for method in route.methods:
            op = item.get(method.lower())
            if op is None:
                continue
            explicit = op.pop("x-access", None)
            access = route_access(route, method)
            if explicit:
                sec, text = _EXPLICIT_ACCESS[explicit]
                access = Access(copy.deepcopy(sec), text, access.permission, api_key=API_KEY in sec,
                                public=explicit == "public" or {} in sec, key_forbidden=access.key_forbidden)
            op["security"] = access.security
            if access.permission:
                op["x-permission"] = access.permission
            op["x-api-key"] = access.api_key
            lines = [f"**Authentication:** {access.auth_text}"]
            if access.permission:
                perms = " or ".join(f"`{p}`" for p in access.permission.split(" | "))
                lines.append(f"**Permission:** {perms}")
            op["description"] = (op.get("description", "").rstrip() + "\n\n---\n" + "  \n".join(lines)).lstrip()
            for status, specs in _auth_errors(access).items():
                _merge_error(op, status, specs)
            # A route-declared 422 replaces FastAPI's automatic validation-error
            # response — keep both shapes documented.
            r422 = op.get("responses", {}).get("422")
            if r422 and (route.body_field is not None or route.dependant.query_params
                         or route.dependant.path_params):
                c = r422.setdefault("content", {}).setdefault("application/json", {})
                sch = c.get("schema", {})
                refs = sch.get("anyOf", [sch] if sch else [])
                ve = {"$ref": "#/components/schemas/HTTPValidationError"}
                if ve not in refs:
                    c["schema"] = {"anyOf": refs + [ve]}
                    comps["schemas"].setdefault("HTTPValidationError", _validation_error_schema())
                    comps["schemas"].setdefault("ValidationError", _validation_item_schema())
            op["responses"] = dict(sorted(op["responses"].items(), key=lambda kv: kv[0]))
    return schema


def _validation_error_schema() -> dict:
    return {"title": "HTTPValidationError", "type": "object", "properties": {
        "detail": {"title": "Detail", "type": "array", "items": {"$ref": "#/components/schemas/ValidationError"}}}}


def _validation_item_schema() -> dict:
    return {"title": "ValidationError", "type": "object", "required": ["loc", "msg", "type"], "properties": {
        "loc": {"title": "Location", "type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "integer"}]}},
        "msg": {"title": "Message", "type": "string"},
        "type": {"title": "Error Type", "type": "string"}}}


def operation_id(route: APIRoute) -> str:
    """Readable, stable operationId: the handler's function name."""
    return route.name


def install_openapi(app: FastAPI) -> None:
    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            app.openapi_schema = build_openapi(app)
        return app.openapi_schema
    app.openapi = custom_openapi  # type: ignore[method-assign]
