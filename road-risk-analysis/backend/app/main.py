import logging
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from app.api.deps import route_template
from app.api.router import register_routes
from app.api import openapi as doc
from app.core.config import settings
from app.core import audit as audit_service
from app.core import rate_limit
from app.core.db.session import init_db, AsyncSessionLocal
from app.modules.m01_security.auth_service import seed_roles, bootstrap_admin
from app.modules.m06_hazards import hazard_service
from app.modules.m07_ingestion.watchdog import mark_stale_jobs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


STARTUP_LOCK_ID = 7_412_309_001     # pg advisory lock: one API process at a time runs the start-up work


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Several API processes (uvicorn --workers N) start together: the schema upgrade, seeds and
    # back-fills below run in one process at a time (the others wait, then find them done).
    from sqlalchemy import text as _sql
    from app.core.db.session import engine as _engine
    async with _engine.connect() as lock_conn:
        await lock_conn.execute(_sql("SELECT pg_advisory_lock(:k)"), {"k": STARTUP_LOCK_ID})
        try:
            await _startup()
        finally:
            await lock_conn.execute(_sql("SELECT pg_advisory_unlock(:k)"), {"k": STARTUP_LOCK_ID})
            await lock_conn.commit()
    if settings.SECRET_KEY == "dev-insecure-secret-change-me":
        logging.getLogger(__name__).warning("SECRET_KEY is the development default — set it in .env for production")
    if not settings.AUTH_ENFORCED:
        logging.getLogger(__name__).warning("AUTH_ENFORCED=false — API routes are open to anonymous callers")
    yield


async def _startup() -> None:
    await init_db()
    async with AsyncSessionLocal() as db:
        marked = await mark_stale_jobs(db)
        if marked:
            import logging as _l
            _l.getLogger(__name__).warning(f"Watchdog: {marked} stale job(s) marked as failed")
        await _seed_rejection_reasons(db)
        try:   # once: training samples of rejections filed before reason kinds existed
            from app.modules.m06_hazards import training_data
            n = await training_data.resync_rejections(db, only_unclassified=True)
            if n["changed"]:
                logging.getLogger(__name__).info(f"Training data: {n['changed']} rejected sample(s) re-filed by reason kind")
        except Exception:
            await db.rollback()
            logging.getLogger(__name__).exception("Training data: re-filing rejections by kind failed — "
                                                  "run `python -m app.cli resync-training-rejections`")
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
    # Structured addresses of detections stored before they existed: a bounded, throttled
    # background run (app.modules.m03_maps.geocoding_backfill) — in one API process only.
    if settings.GEOCODING_ENABLED and settings.GEOCODING_BACKFILL_ON_STARTUP > 0 and await _claim("geocoding_backfill", 900):
        from app.modules.m03_maps import geocoding_backfill
        geocoding_backfill.start_background(settings.GEOCODING_BACKFILL_ON_STARTUP)
    # Training-data objects left unwritten before a restart (docs/TRAINING-DATA.md; SKIP LOCKED)
    from app.modules.m06_hazards.training_data import sync as training_sync
    training_sync.kick(delay_s=15)


async def _claim(name: str, ttl_s: int) -> bool:
    """True in the first API process claiming `name` within `ttl_s` (Redis; True without Redis)."""
    try:
        from app.core.storage.redis_client import get_redis
        import os
        return bool(await get_redis().set(f"startup:{name}", str(os.getpid()), nx=True, ex=ttl_s))
    except Exception:
        return True

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


# Per-IP limits of the sensitive public endpoints (sign-in, sign-up, password reset,
# MFA, Label Studio images) — app/core/rate_limit.py.
app.middleware("http")(rate_limit.middleware)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


# Every module router with its access rules — app/api/router.py.
register_routes(app)


# Built-in rejection reasons and their kinds: app.modules.m06_hazards.rejection_seeds
from app.modules.m06_hazards.rejection_seeds import SEED_REASONS, seed_rejection_reasons as _seed_rejection_reasons  # noqa: E402,F401
