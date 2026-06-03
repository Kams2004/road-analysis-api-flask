import logging
from fastapi import FastAPI
from contextlib import asynccontextmanager
from app.api.routes import jobs, detections, health, datasets
from app.core.config import settings
from app.db.session import init_db, AsyncSessionLocal
from app.services.watchdog import mark_stale_jobs

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
    yield

app = FastAPI(
    title="Road Analysis API",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(health.router, tags=["health"])
app.include_router(jobs.router, prefix="/jobs", tags=["jobs"])
app.include_router(detections.router, prefix="/detections", tags=["detections"])
app.include_router(datasets.router, prefix="/datasets", tags=["datasets"])
