import logging
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from app.api.routes import jobs, detections, health, datasets, stats, rejection_reasons, signalements, cluster_config, vehicles, road_state, trajectory, collisions, alerts, historical
from app.api.routes import auth, users, roles, platform_settings, api_clients, audit_logs, map_config
from app.api.routes import hazards, cluster_areas, clustering, mobile_config
from app.api.routes import cloud_connections, cloud_imports, streams
from app.api.routes import route_sections
from app.api.routes import ai_models
from app.api.deps import client_route_guard, module_guard, route_template
from app.api import openapi as doc
from app.core.config import settings
from app.services import audit as audit_service
from app.services.auth_service import seed_roles, bootstrap_admin
from app.services import hazard_service
from app.db.session import init_db, AsyncSessionLocal
from app.services.watchdog import mark_stale_jobs
from app.models.rejection_reason import RejectionReason
from sqlalchemy import select

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    async with AsyncSessionLocal() as db:
        marked = await mark_stale_jobs(db)
        if marked:
            import logging as _l
            _l.getLogger(__name__).warning(f"Watchdog: {marked} stale job(s) marked as failed")
        await _seed_rejection_reasons(db)
        await seed_roles(db)
        await bootstrap_admin(db)
        try:
            from app.services.clustering import service as clustering_service
            await clustering_service.migrate_legacy(db)   # once: legacy radius/areas → Cluster Configuration
        except Exception:
            await db.rollback()
            logging.getLogger(__name__).exception("Cluster configuration migration failed")
        try:
            await hazard_service.ensure_built(db)
        except Exception:  # a failed back-fill must not keep the API down
            logging.getLogger(__name__).exception("Hazard back-fill failed — run `python -m app.cli rebuild-hazards`")
    # Structured addresses of detections stored before they existed: a
    # bounded, throttled background run (app.services.geocoding_backfill).
    if settings.GEOCODING_ENABLED and settings.GEOCODING_BACKFILL_ON_STARTUP > 0:
        from app.services import geocoding_backfill
        geocoding_backfill.start_background(settings.GEOCODING_BACKFILL_ON_STARTUP)
    # Training-data objects left unwritten before a restart (docs/TRAINING-DATA.md)
    from app.services.training_data import sync as training_sync
    training_sync.kick(delay_s=15)
    if settings.SECRET_KEY == "dev-insecure-secret-change-me":
        logging.getLogger(__name__).warning("SECRET_KEY is the development default — set it in .env for production")
    if not settings.AUTH_ENFORCED:
        logging.getLogger(__name__).warning("AUTH_ENFORCED=false — API routes are open to anonymous callers")
    yield

app = FastAPI(
    **doc.APP_METADATA,
    description=doc.api_description(),
    openapi_tags=doc._tags(),
    generate_unique_id_function=doc.operation_id,
    lifespan=lifespan,
)
# Swagger UI at /docs, ReDoc at /redoc, schema at /openapi.json — security
# requirements and permissions are derived from the guards below.
doc.install_openapi(app)

app.add_middleware(
    CORSMiddleware,
    # The web console calls the API through its own server-side proxy and the
    # native app is not subject to CORS, so only explicit origins (Expo web,
    # local dev) need listing — see CORS_ORIGINS.
    allow_origins=[o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers whose handlers write their own detailed audit entries.
_SELF_AUDITED = ("/auth", "/users", "/roles", "/platform-settings", "/api-clients",
                 "/detections", "/hazards", "/cluster-areas", "/cluster-zones", "/clustering",
                 "/cluster-config", "/jobs", "/cloud-connections", "/imports", "/streams")


@app.middleware("http")
async def audit_mutations(request: Request, call_next):
    """Trail of every state-changing call made by a signed-in user
    (RRAP-13-006). Telemetry from API clients is not logged here — it
    arrives every few seconds per vehicle and carries no user action."""
    response = await call_next(request)
    principal = getattr(request.state, "principal", None)
    if (
        request.method in ("POST", "PUT", "PATCH", "DELETE")
        and principal is not None and principal.kind == "user"
        and not request.url.path.startswith(_SELF_AUDITED)
    ):
        try:
            await audit_service.record_now(
                request, f"api.{request.method.lower()}", principal=principal,
                outcome="success" if response.status_code < 400 else "failure",
                object_type=route_template(request) if request.scope.get("route") else None,
                details={"path": request.url.path, "status": response.status_code},
            )
        except Exception:  # auditing must never break the request itself
            logging.getLogger(__name__).exception("audit write failed")
    return response


# ── Access rules ─────────────────────────────────────────────────────────────
# Each router maps to one permission module; GET=view, POST=add, PUT/PATCH=
# edit, DELETE=delete, with the overrides below. `client_routes` are the only
# endpoints the mobile app / simulator may call with an API key.
VIEW, EDIT, DELETE = "view", "edit", "delete"

app.include_router(health.router, tags=[doc.TAG_HEALTH])
# /auth is public (login) or checks the user itself; an API key (mobile app)
# may only reach these endpoints. /auth/me, /auth/me/password and /auth/logout accept the key
# together with the signed-in user's Bearer token.
_AUTH_CLIENT = {
    ("GET", "/auth/config"), ("POST", "/auth/mobile/register"), ("POST", "/auth/mobile/login"),
    ("POST", "/auth/google"), ("POST", "/auth/mfa/verify"),
    ("GET", "/auth/me"), ("PATCH", "/auth/me"), ("POST", "/auth/me/password"), ("POST", "/auth/logout"),
}
app.include_router(auth.router, prefix="/auth", tags=[doc.TAG_AUTH], dependencies=[client_route_guard(_AUTH_CLIENT)])
app.include_router(users.router, prefix="/users", tags=[doc.TAG_USERS])
app.include_router(roles.router, prefix="/roles", tags=[doc.TAG_ROLES])
app.include_router(platform_settings.router, prefix="/platform-settings", tags=[doc.TAG_SETTINGS])
app.include_router(api_clients.router, prefix="/api-clients", tags=[doc.TAG_API_CLIENTS])
app.include_router(audit_logs.router, prefix="/audit-logs", tags=[doc.TAG_AUDIT])
app.include_router(map_config.router, prefix="/map-config", tags=[doc.TAG_MAP])
app.include_router(mobile_config.router, prefix="/mobile-config", tags=[doc.TAG_MOBILE],
                   dependencies=[client_route_guard({("GET", "/mobile-config/")})])

app.include_router(jobs.router, prefix="/jobs", tags=[doc.TAG_JOBS], dependencies=[module_guard(
    "jobs",
    overrides={
        ("POST", "/jobs/{job_id}/cancel"): ("jobs", EDIT),
        ("POST", "/jobs/{job_id}/retry"):  ("jobs", EDIT),
    },
)])
app.include_router(cloud_connections.router, prefix="/cloud-connections", tags=[doc.TAG_CLOUD],
                   dependencies=[module_guard(
    "jobs",
    overrides={
        ("POST", "/cloud-connections/{connection_id}/sync"):      ("jobs", EDIT),
        ("POST", "/cloud-connections/{connection_id}/authorize"): ("jobs", EDIT),
        # SFTP servers: connecting / disconnecting only opens a read session (view);
        # forgetting the host key and clearing the log change the source (edit).
        ("POST", "/cloud-connections/{connection_id}/sftp/connect"):         ("jobs", VIEW),
        ("POST", "/cloud-connections/{connection_id}/sftp/disconnect"):      ("jobs", VIEW),
        ("POST", "/cloud-connections/{connection_id}/sftp/forget-host-key"): ("jobs", EDIT),
        ("DELETE", "/cloud-connections/{connection_id}/sftp/log"):           ("jobs", EDIT),
        # POST /cloud-connections/{connection_id}/imports and POST /cloud-connections/sftp
        # keep the default jobs:add.
    },
)])
# Cloud imports (input sources): view to follow, add via POST …/imports above,
# edit to cancel / retry, delete to clear finished imports from the history.
app.include_router(cloud_imports.router, prefix="/imports", tags=[doc.TAG_CLOUD_IMPORTS], dependencies=[module_guard(
    "jobs",
    overrides={
        ("POST", "/imports/{batch_id}/cancel"):                  ("jobs", EDIT),
        ("POST", "/imports/{batch_id}/retry"):                   ("jobs", EDIT),
        ("POST", "/imports/{batch_id}/items/{item_id}/cancel"):  ("jobs", EDIT),
        ("POST", "/imports/{batch_id}/items/{item_id}/retry"):   ("jobs", EDIT),
    },
)])
app.include_router(streams.router, prefix="/streams", tags=[doc.TAG_STREAMS], dependencies=[module_guard(
    "streams",
    read_posts={"/streams/{stream_id}/test"},
    overrides={
        ("POST", "/streams/{stream_id}/start"): ("streams", EDIT),
        ("POST", "/streams/{stream_id}/pause"): ("streams", EDIT),
    },
)])
app.include_router(detections.router, prefix="/detections", tags=[doc.TAG_DETECTIONS], dependencies=[module_guard(
    "detections",
    read_posts={"/detections/nearby", "/detections/along-route", "/detections/clusters"},
    overrides={
        ("GET",   "/detections/pending"):                       ("validations", VIEW),
        ("GET",   "/detections/queue"):                         ("validations", VIEW),
        ("GET",   "/detections/rejected"):                      ("validations", VIEW),
        ("GET",   "/detections/rejected/export"):               ("datasets", VIEW),
        ("GET",   "/detections/{detection_id}/trajectory"):     ("validations", VIEW),
        ("POST",  "/detections/{detection_id}/accept"):         ("validations", EDIT),
        ("POST",  "/detections/{detection_id}/correct"):        ("validations", EDIT),
        ("POST",  "/detections/{detection_id}/reject"):         ("validations", EDIT),
        ("POST",  "/detections/{detection_id}/location"):       ("validations", EDIT),
        # Reversing a finalized decision is the "delete" right of validations.
        ("POST",  "/detections/{detection_id}/reopen"):         ("validations", DELETE),
        ("PATCH", "/detections/{detection_id}/location"):       ("validations", EDIT),
        ("PATCH", "/detections/{detection_id}/review"):         ("validations", EDIT),
        ("GET",   "/detections/{detection_id}/label"):          ("validations", VIEW),
        ("GET",   "/detections/labels/dataset"):                ("datasets", VIEW),
        ("POST",  "/detections/clusters/resolve"):              ("detections", EDIT),
    },
    client_routes={("POST", "/detections/nearby"), ("POST", "/detections/along-route")},
)])
app.include_router(hazards.router, prefix="/hazards", tags=[doc.TAG_HAZARDS], dependencies=[module_guard(
    "detections",
    overrides={
        ("POST", "/hazards/{hazard_id}/resolve"): ("detections", EDIT),
        ("POST", "/hazards/{hazard_id}/reopen"):  ("detections", EDIT),
        ("POST", "/hazards/{hazard_id}/votes"):   ("detections", EDIT),
        ("POST", "/hazards/geocode/backfill"):    ("detections", EDIT),
    },
    # Mobile app: driver votes (the route also requires a signed-in user).
    client_routes={("POST", "/hazards/{hazard_id}/votes")},
)])
app.include_router(datasets.router, prefix="/datasets", tags=[doc.TAG_DATASETS], dependencies=[module_guard("datasets")])
# Model registry + live AI quality: settings:view or detections:view, checked
# per endpoint (ai_models.model_reader).
app.include_router(ai_models.router, prefix="/models", tags=[doc.TAG_AI_MODELS])
app.include_router(stats.router, prefix="/stats", tags=[doc.TAG_STATS], dependencies=[module_guard("dashboard")])
# Global Cluster Configuration (Module 6): settings:view to read, settings:edit
# to change (zones included — settings has no add/delete right).
app.include_router(clustering.config_router, prefix="/clustering", tags=[doc.TAG_CLUSTERS], dependencies=[module_guard(
    "settings",
    overrides={("POST", "/clustering/rebuild-hazards"): ("settings", EDIT)},
)])
app.include_router(clustering.zones_router, prefix="/cluster-zones", tags=[doc.TAG_CLUSTERS], dependencies=[module_guard(
    "settings",
    overrides={
        ("POST",   "/cluster-zones/"):              ("settings", EDIT),
        ("POST",   "/cluster-zones/road-section"):  ("settings", EDIT),
        ("DELETE", "/cluster-zones/{zone_id}"):     ("settings", EDIT),
    },
)])
app.include_router(cluster_config.router, prefix="/cluster-config", tags=[doc.TAG_CLUSTERS],
                   dependencies=[module_guard("settings")])
# settings has no add/delete right — creating or removing an area is an edit.
app.include_router(cluster_areas.router, prefix="/cluster-areas", tags=[doc.TAG_CLUSTERS], dependencies=[module_guard(
    "settings",
    overrides={
        ("POST",   "/cluster-areas/"):          ("settings", EDIT),
        ("DELETE", "/cluster-areas/{area_id}"): ("settings", EDIT),
    },
)])
app.include_router(rejection_reasons.router, prefix="/rejection-reasons", tags=[doc.TAG_REJECTION],
                   dependencies=[module_guard("validations")])
app.include_router(signalements.router, prefix="/signalements", tags=[doc.TAG_SIGNALEMENTS], dependencies=[module_guard(
    "signalements",
    read_posts={"/signalements/nearby", "/signalements/along-route"},
    overrides={
        ("POST", "/signalements/{signalement_id}/confirm"):   ("signalements", EDIT),
        ("POST", "/signalements/{signalement_id}/not-there"): ("signalements", EDIT),
        ("POST", "/signalements/{signalement_id}/vote"):      ("signalements", EDIT),
        ("POST", "/signalements/{signalement_id}/pass"):      ("signalements", EDIT),
    },
    client_routes={
        ("POST", "/signalements/"), ("POST", "/signalements/nearby"), ("POST", "/signalements/along-route"),
        ("POST", "/signalements/{signalement_id}/confirm"), ("POST", "/signalements/{signalement_id}/not-there"),
        ("POST", "/signalements/{signalement_id}/vote"), ("POST", "/signalements/{signalement_id}/pass"),
        ("GET", "/signalements/{signalement_id}"),
    },
)])

_LIVE_REFRESH = {
    ("POST", "/vehicles/register"):                  ("collisions", EDIT),
    ("POST", "/vehicles/{vehicle_id}/ping"):         ("collisions", EDIT),
    ("POST", "/road-state/{vehicle_id}/refresh"):    ("collisions", EDIT),
    ("POST", "/trajectory/{vehicle_id}/refresh"):    ("collisions", EDIT),
    ("POST", "/collisions/{vehicle_id}/refresh"):    ("collisions", EDIT),
    ("POST", "/alerts/{vehicle_id}/refresh"):        ("collisions", EDIT),
}
_LIVE_CLIENT = {
    ("POST", "/vehicles/register"), ("POST", "/vehicles/{vehicle_id}/ping"), ("POST", "/vehicles/nearby"),
    ("GET", "/alerts/{vehicle_id}"), ("GET", "/road-state/{vehicle_id}"),
    ("GET", "/trajectory/{vehicle_id}"), ("GET", "/collisions/{vehicle_id}"),
}
_live_guard = module_guard("collisions", read_posts={"/vehicles/nearby"},
                           overrides=_LIVE_REFRESH, client_routes=_LIVE_CLIENT)
app.include_router(vehicles.router, prefix="/vehicles", tags=[doc.TAG_VEHICLES], dependencies=[_live_guard])
app.include_router(road_state.router, prefix="/road-state", tags=[doc.TAG_ROAD_STATE], dependencies=[_live_guard])
app.include_router(trajectory.router, prefix="/trajectory", tags=[doc.TAG_TRAJECTORY], dependencies=[_live_guard])
app.include_router(collisions.router, prefix="/collisions", tags=[doc.TAG_COLLISIONS], dependencies=[_live_guard])
app.include_router(alerts.router, prefix="/alerts", tags=[doc.TAG_ALERTS], dependencies=[_live_guard])
# Route-search helpers (tronçons, OSRM preview, geocoding) — View right on
# any page with a route search, checked per endpoint (route_reader).
app.include_router(route_sections.router, prefix="/routes", tags=[doc.TAG_ROUTES])
app.include_router(historical.router, prefix="/historical", tags=[doc.TAG_HISTORICAL], dependencies=[module_guard(
    "historical",
    read_posts={"/historical/risk/route"},
    client_routes={("POST", "/historical/risk/route"), ("GET", "/historical/risk/zone")},
)])


SEED_REASONS = [
    # ── applies to all types ───────────────────────────────────────────────
    ("all", "not_road_side",      "Not road-side",         "Detection is outside the road area (farm land, courtyard, private property)"),
    ("all", "image_quality",     "Poor image quality",    "Frame is too blurry, dark, or overexposed to confirm"),
    ("all", "duplicate",         "Duplicate detection",   "Same hazard already captured in a nearby frame"),
    ("all", "out_of_frame",      "Out of frame",          "Object is partially cut off and cannot be confirmed"),
    ("all", "low_confidence",    "Low confidence",        "Automatically rejected: confidence below the configured threshold"),
    ("all", "ai_verifier",       "Rejected by AI verifier", "Automatically rejected: the crop classifier judged it is not this hazard type"),
    # ── pothole ────────────────────────────────────────────────────────────
    ("pothole", "shadow",           "Shadow / lighting",   "Dark patch caused by shadow, not a pothole"),
    ("pothole", "water_reflection", "Water reflection",    "Puddle reflection misidentified as a pothole"),
    ("pothole", "manhole",          "Manhole / drain",     "Manhole cover or drain grating, not a pothole"),
    ("pothole", "road_marking",     "Road marking",        "Painted road marking misidentified as a pothole"),
    ("pothole", "dirt_patch",       "Dirt / gravel patch", "Dirt or gravel patch on the road surface"),
    ("pothole", "ai_off_road",      "Off the road area",   "Automatically rejected: the box lies above the road-area line (trees, sky, roadside), not on the road surface"),
    ("pothole", "ai_box_too_large", "Box too large / roadside", "Automatically rejected: the box is too large for a pothole, or a wide box touching the image side (roadside earth, bushes, building edge)"),
    # ── traffic sign ──────────────────────────────────────────────────────
    ("traffic_sign", "ai_not_a_sign",    "Not a traffic sign", "Automatically rejected: the sign classifier judged the crop is not a traffic sign (sticker, billboard, person, vehicle, tree …)"),
    ("traffic_sign", "person_as_sign",   "Person as sign",   "A pedestrian or person misidentified as a traffic sign"),
    ("traffic_sign", "bike_as_stop",     "Bike as stop sign", "A bicycle or motorbike misidentified as a stop sign"),
    ("traffic_sign", "pole_as_sign",     "Pole / post",       "Utility pole or fence post misidentified as a sign"),
    ("traffic_sign", "street_light",     "Street light",      "Street light or lamp post misidentified as a sign"),
    ("traffic_sign", "sign_not_visible", "Sign not visible",  "Sign is present but too distant or obstructed to read"),
    # ── speed bump ────────────────────────────────────────────────────────
    ("speed_bump", "road_edge",    "Road edge / curb",   "Road edge or kerb misidentified as a speed bump"),
    ("speed_bump", "ditch",        "Ditch / trench",      "Drainage ditch or trench across the road"),
    ("speed_bump", "speed_bump_ok","Already validated",  "Speed bump detected but previously validated nearby"),
    # ── parked vehicle (rules of app.services.detection.parked_vehicle) ────
    ("parked_vehicle", "ai_off_road",       "Off the road",       "Automatically rejected: the vehicle stands too far from the road (yard, garage, fuel station, car park)"),
    ("parked_vehicle", "ai_not_grounded",   "Not a real vehicle", "Automatically rejected: the vehicle does not stand on the ground (billboard / poster, car on a carrier, reflection)"),
    ("parked_vehicle", "ai_traffic_queue",  "Traffic queue",      "Automatically rejected: vehicle stopped in traffic ahead of us, not parked"),
    ("parked_vehicle", "ai_moving_vehicle", "Moving vehicle",     "Rejected: the vehicle was moving (same direction or oncoming), not parked"),
]


async def _seed_rejection_reasons(db):
    from sqlalchemy import select
    for det_type, code, label, description in SEED_REASONS:
        exists = (await db.execute(
            select(RejectionReason).where(
                RejectionReason.code == code,
                RejectionReason.detection_type == det_type,
            )
        )).scalar_one_or_none()
        if not exists:
            db.add(RejectionReason(
                detection_type=det_type, code=code,
                label=label, description=description, is_custom=False,
            ))
    await db.commit()
