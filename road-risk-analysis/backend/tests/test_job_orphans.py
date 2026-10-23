"""Jobs whose worker died (no heartbeat): cancel ends them at once; long jobs that beat stay alive."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.modules.m07_ingestion import watchdog

jobs_routes = pytest.importorskip("app.modules.m07_ingestion.routes.jobs")


def _job(started_min_ago: float):
    return SimpleNamespace(id="j1", started_at=datetime.utcnow() - timedelta(minutes=started_min_ago),
                           created_at=datetime.utcnow() - timedelta(minutes=started_min_ago))


@pytest.mark.parametrize("age, started, gone", [
    (10.0, 240, False),                                # 4 h job still beating → alive
    (settings.ORPHAN_JOB_SECONDS + 5, 30, True),       # heartbeat stopped → gone
    (None, 1, False),                                  # just started, no slot entry yet
    (None, 30, True),                                  # no heartbeat at all for 30 min → gone
])
def test_worker_gone(monkeypatch, age, started, gone):
    monkeypatch.setattr(watchdog, "heartbeat_age_s", lambda _id: age)
    assert jobs_routes._worker_gone(_job(started)) is gone
