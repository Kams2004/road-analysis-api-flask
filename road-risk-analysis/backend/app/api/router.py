"""
Assembles the HTTP API: every module router (app/modules/m*/routes/) mounted
under its prefix and tag, with the access rules each router maps to.
app.main calls `register_routes(app)`; to add a router, import it here and
include it below with its permission module (see backend/README.md).
"""
from fastapi import FastAPI

from app.api import openapi as doc
from app.api.deps import client_route_guard, module_guard
from app.api import health
from app.modules.m01_security.routes import auth, users, roles
from app.modules.m02_dashboard.routes import stats
from app.modules.m03_maps.routes import map_config
from app.modules.m03_maps.routes import route_sections
from app.modules.m05_ai_detection.routes import ai_models
from app.modules.m06_hazards.routes import detections, datasets, rejection_reasons, cluster_config
from app.modules.m06_hazards.routes import hazards, cluster_areas, clustering
from app.modules.m06_hazards.routes import labelstudio
from app.modules.m07_ingestion.routes import cloud_connections, cloud_imports, streams
from app.modules.m07_ingestion.routes import jobs
from app.modules.m08_historical.routes import historical
from app.modules.m09_driver_behaviour.routes import driver_behaviour
from app.modules.m14_reports.routes import reports
from app.modules.m10_collision.routes import vehicles, road_state, trajectory, collisions, alerts
from app.modules.m10_collision.routes import metrics as collision_metrics
from app.modules.m11_community.routes import signalements
from app.modules.m12_integration.routes import api_clients
from app.modules.m12_integration.routes import mobile_config
from app.modules.m13_admin.routes import platform_settings, audit_logs


# ── Access rules ─────────────────────────────────────────────────────────────
# Each router maps to one permission module; GET=view, POST=add, PUT/PATCH=
# edit, DELETE=delete, with the overrides below. `client_routes` are the only
# endpoints the mobile app / simulator may call with an API key.
VIEW, EDIT, DELETE = "view", "edit", "delete"
# /auth is public (login) or checks the user itself; an API key (mobile app)
# may only reach these endpoints. /auth/me, /auth/me/password and /auth/logout accept the key
# together with the signed-in user's Bearer token.
_AUTH_CLIENT = {
    ("GET", "/auth/config"), ("POST", "/auth/mobile/register"), ("POST", "/auth/mobile/login"),
    ("POST", "/auth/google"), ("POST", "/auth/mfa/verify"),
    ("GET", "/auth/me"), ("PATCH", "/auth/me"), ("POST", "/auth/me/password"), ("POST", "/auth/logout"),
}

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
    ("GET", "/alerts/{vehicle_id}"), ("POST", "/alerts/{conflict_id}/feedback"), ("GET", "/road-state/{vehicle_id}"),
    ("GET", "/trajectory/{vehicle_id}"), ("GET", "/collisions/{vehicle_id}"),
}
_live_guard = module_guard("collisions", read_posts={"/vehicles/nearby"},
                           overrides=_LIVE_REFRESH, client_routes=_LIVE_CLIENT)


def register_routes(app: FastAPI) -> None:
    """Mount every router on `app` (order = order of the OpenAPI document)."""

    app.include_router(health.router, tags=[doc.TAG_HEALTH])
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
            # Event sources: checking now / re-importing a day changes stored data (edit).
            ("POST", "/cloud-connections/{connection_id}/event-files/sync"):               ("jobs", EDIT),
            ("POST", "/cloud-connections/{connection_id}/event-files/{file_id}/reimport"): ("jobs", EDIT),
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
            # Cluster review (validation screen) — same rights as reviewing a detection.
            ("GET",  "/hazards/review-queue"):        ("validations", VIEW),
            ("POST", "/hazards/{hazard_id}/accept"):  ("validations", EDIT),
            ("POST", "/hazards/{hazard_id}/reject"):  ("validations", EDIT),
            ("POST", "/hazards/{hazard_id}/resolve"): ("detections", EDIT),
            ("POST", "/hazards/{hazard_id}/reopen"):  ("detections", EDIT),
            ("POST", "/hazards/{hazard_id}/votes"):   ("detections", EDIT),
            ("POST", "/hazards/geocode/backfill"):    ("detections", EDIT),
        },
        # Mobile app: driver votes (the route also requires a signed-in user).
        client_routes={("POST", "/hazards/{hazard_id}/votes")},
    )])
    app.include_router(datasets.router, prefix="/datasets", tags=[doc.TAG_DATASETS], dependencies=[module_guard("datasets")])
    # Box review in the server's Label Studio (Training data page); the signed frame images
    # are public on purpose — Label Studio loads them in the annotator's browser.
    app.include_router(labelstudio.router, prefix="/labelstudio", tags=[doc.TAG_DATASETS], dependencies=[
        module_guard("datasets", overrides={("POST", "/labelstudio/{detection_type}/fetch"): ("datasets", EDIT)})])
    app.include_router(labelstudio.image_router, prefix="/labelstudio", tags=[doc.TAG_DATASETS])
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
    # Editing a reason (its kind re-files training data) is a settings change.
    app.include_router(rejection_reasons.router, prefix="/rejection-reasons", tags=[doc.TAG_REJECTION],
                       dependencies=[module_guard("validations", overrides={
                           ("PATCH", "/rejection-reasons/{reason_id}"): ("settings", EDIT),
                       })])
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
    app.include_router(vehicles.router, prefix="/vehicles", tags=[doc.TAG_VEHICLES], dependencies=[_live_guard])
    app.include_router(road_state.router, prefix="/road-state", tags=[doc.TAG_ROAD_STATE], dependencies=[_live_guard])
    app.include_router(trajectory.router, prefix="/trajectory", tags=[doc.TAG_TRAJECTORY], dependencies=[_live_guard])
    app.include_router(collisions.router, prefix="/collisions", tags=[doc.TAG_COLLISIONS], dependencies=[_live_guard])
    app.include_router(alerts.router, prefix="/alerts", tags=[doc.TAG_ALERTS], dependencies=[_live_guard])
    # WS /alerts/ws authenticates itself with the same headers (a WebSocket has no Request for the
    # router guard): any API client, or a user with collisions:view.
    app.include_router(alerts.ws_router, prefix="/alerts", tags=[doc.TAG_ALERTS])
    app.include_router(collision_metrics.router, prefix="/collision", tags=[doc.TAG_COLLISIONS],
                       dependencies=[module_guard("collisions")])
    # Route-search helpers (tronçons, OSRM preview, geocoding) — View right on
    # any page with a route search, checked per endpoint (route_reader).
    app.include_router(route_sections.router, prefix="/routes", tags=[doc.TAG_ROUTES])
    app.include_router(historical.router, prefix="/historical", tags=[doc.TAG_HISTORICAL], dependencies=[module_guard(
        "historical",
        read_posts={"/historical/risk/route"},
        client_routes={("POST", "/historical/risk/route"), ("GET", "/historical/risk/zone")},
    )])
    app.include_router(driver_behaviour.router, prefix="/driver-behaviour", tags=[doc.TAG_DRIVER_BEHAVIOUR],
                       dependencies=[module_guard("driver_behaviour")])
    # Reports: view = list / download, add = generate, delete = remove a stored report.
    # Generating also needs the view right of the module the report reads (checked per type).
    app.include_router(reports.router, prefix="/reports", tags=[doc.TAG_REPORTS],
                       dependencies=[module_guard("reports")])
