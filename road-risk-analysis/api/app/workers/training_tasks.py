"""
Celery task of the live training dataset (docs/TRAINING-DATA.md):

  sync_training_data   beat, every 2 minutes: writes the dirty training
                       frames (decisions whose objects are not yet in the
                       `datasets` bucket — MinIO was down, the API restarted
                       before its background sync …) and retries failures.
"""
from app.workers.celery_app import celery_app
from app.workers.job_tasks import _run


@celery_app.task(name="app.workers.celery_app.sync_training_data")
def sync_training_data(limit: int = 1000):
    from app.services.training_data.sync import sync_dirty
    return _run(lambda db: sync_dirty(db, limit=limit))
