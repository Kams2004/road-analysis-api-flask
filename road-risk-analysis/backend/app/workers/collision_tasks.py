"""
Celery task of the collision-prevention context (app/modules/m10_collision/context_service.py):

  refresh_collision_context   beat, every minute: rebuilds the Redis context map (per-cell place
                              context) when it is older than
                              collision.context.refresh_interval_s (5 min by default) or when new
                              fleet events were imported (events:data_version changed) — so an
                              import is reflected within a minute. `force` rebuilds now.

One build at a time (Redis lock); the hot path (every ping) only reads what this writes.
"""
from app.workers.celery_app import celery_app
from app.workers.job_tasks import _run

LOCK_S = 600


@celery_app.task(name="app.workers.celery_app.refresh_collision_context")
def refresh_collision_context(force: bool = False):
    from app.modules.m07_ingestion import job_slots
    from app.modules.m10_collision import context_service
    r = job_slots.client()
    if not r.set(context_service.LOCK_KEY, "1", nx=True, ex=LOCK_S):
        return {"skipped": "already running"}
    try:
        return _run(lambda db: context_service.refresh(db, r, force=force))
    finally:
        r.delete(context_service.LOCK_KEY)
