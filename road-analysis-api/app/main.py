import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from app.api.routes import jobs, detections, health, datasets, stats, rejection_reasons, signalements, cluster_config
from app.core.config import settings
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
    yield

app = FastAPI(
    title="Road Analysis API",
    version="1.0.0",
    lifespan=lifespan,
    redirect_slashes=False,
)

app.add_middleware(
    CORSMiddleware,
    # Allow the Next.js web interface, the Expo dev server, and any LAN IP
    # (mobile devices on the same network as the API server).
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router, tags=["health"])
app.include_router(jobs.router, prefix="/jobs", tags=["jobs"])
app.include_router(detections.router, prefix="/detections", tags=["detections"])
app.include_router(datasets.router, prefix="/datasets", tags=["datasets"])
app.include_router(stats.router, prefix="/stats", tags=["stats"])
app.include_router(cluster_config.router, prefix="/cluster-config", tags=["cluster-config"])
app.include_router(rejection_reasons.router, prefix="/rejection-reasons", tags=["rejection-reasons"])
app.include_router(signalements.router, prefix="/signalements", tags=["signalements"])


SEED_REASONS = [
    # ── applies to all types ───────────────────────────────────────────────
    ("all", "not_road_side",      "Not road-side",         "Detection is outside the road area (farm land, courtyard, private property)"),
    ("all", "image_quality",     "Poor image quality",    "Frame is too blurry, dark, or overexposed to confirm"),
    ("all", "duplicate",         "Duplicate detection",   "Same hazard already captured in a nearby frame"),
    ("all", "out_of_frame",      "Out of frame",          "Object is partially cut off and cannot be confirmed"),
    # ── pothole ────────────────────────────────────────────────────────────
    ("pothole", "shadow",           "Shadow / lighting",   "Dark patch caused by shadow, not a pothole"),
    ("pothole", "water_reflection", "Water reflection",    "Puddle reflection misidentified as a pothole"),
    ("pothole", "manhole",          "Manhole / drain",     "Manhole cover or drain grating, not a pothole"),
    ("pothole", "road_marking",     "Road marking",        "Painted road marking misidentified as a pothole"),
    ("pothole", "dirt_patch",       "Dirt / gravel patch", "Dirt or gravel patch on the road surface"),
    # ── traffic sign ──────────────────────────────────────────────────────
    ("traffic_sign", "person_as_sign",   "Person as sign",   "A pedestrian or person misidentified as a traffic sign"),
    ("traffic_sign", "bike_as_stop",     "Bike as stop sign", "A bicycle or motorbike misidentified as a stop sign"),
    ("traffic_sign", "pole_as_sign",     "Pole / post",       "Utility pole or fence post misidentified as a sign"),
    ("traffic_sign", "street_light",     "Street light",      "Street light or lamp post misidentified as a sign"),
    ("traffic_sign", "sign_not_visible", "Sign not visible",  "Sign is present but too distant or obstructed to read"),
    # ── speed bump ────────────────────────────────────────────────────────
    ("speed_bump", "road_edge",    "Road edge / curb",   "Road edge or kerb misidentified as a speed bump"),
    ("speed_bump", "ditch",        "Ditch / trench",      "Drainage ditch or trench across the road"),
    ("speed_bump", "speed_bump_ok","Already validated",  "Speed bump detected but previously validated nearby"),
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
