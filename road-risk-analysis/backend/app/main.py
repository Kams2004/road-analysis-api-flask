import logging
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from app.api.deps import route_template
from app.api.router import register_routes
from app.api import openapi as doc
from app.core.config import settings
from app.core import audit as audit_service
from app.core.db.session import init_db, AsyncSessionLocal
from app.modules.m01_security.auth_service import seed_roles, bootstrap_admin
from app.modules.m06_hazards import hazard_service
from app.modules.m06_hazards.models.rejection_reason import RejectionReason
from app.modules.m07_ingestion.watchdog import mark_stale_jobs
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
            from app.modules.m06_hazards.clustering import service as clustering_service
            await clustering_service.migrate_legacy(db)   # once: legacy radius/areas → Cluster Configuration
        except Exception:
            await db.rollback()
            logging.getLogger(__name__).exception("Cluster configuration migration failed")
        try:
            await hazard_service.ensure_built(db)
        except Exception:  # a failed back-fill must not keep the API down
            logging.getLogger(__name__).exception("Hazard back-fill failed — run `python -m app.cli rebuild-hazards`")
    # Structured addresses of detections stored before they existed: a
    # bounded, throttled background run (app.modules.m03_maps.geocoding_backfill).
    if settings.GEOCODING_ENABLED and settings.GEOCODING_BACKFILL_ON_STARTUP > 0:
        from app.modules.m03_maps import geocoding_backfill
        geocoding_backfill.start_background(settings.GEOCODING_BACKFILL_ON_STARTUP)
    # Training-data objects left unwritten before a restart (docs/TRAINING-DATA.md)
    from app.modules.m06_hazards.training_data import sync as training_sync
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


# Every module router with its access rules — app/api/router.py.
register_routes(app)


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
    # ── parked vehicle (rules of app.modules.m05_ai_detection.parked_vehicle) ────
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
