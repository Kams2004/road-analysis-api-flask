"""
Collision prevention — delivery and data: the WebSocket /alerts/ws (snapshot, transitions pushed
from Redis pub/sub, keepalive, authentication errors), driver feedback (idempotent) and the
metrics endpoint, and the Redis context map precomputed from fleet events, hazards, community
reports and driver scores (SQLite — the same portable SQL as PostgreSQL), plus the calibration.
"""
import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import Principal, get_principal
from app.core.db.session import get_db
from app.modules.m03_maps.osrm import geo
from app.modules.m08_historical.grid import grid_cell_id
from app.modules.m10_collision import alert_service, context_service, metrics_service
from app.modules.m10_collision.alerts.models import (
    AlertLevel, AlertLifecycleStatus, AlertTransition, ConflictAlert, RecommendedAction,
)
from app.modules.m10_collision.collision import thresholds
from app.modules.m10_collision.routes import alerts as alert_routes
from tests.fake_redis import FakeRedis

NOW = datetime.now(timezone.utc)
DOUALA = (4.0511, 9.7679)


def make_alert(conflict_id="CF-20261029-0a1b2c3d", a="A", b="B", level=AlertLevel.WARNING,
               transition=AlertTransition.NEW) -> ConflictAlert:
    return ConflictAlert(conflict_id=conflict_id, vehicle_a_id=a, vehicle_b_id=b, status=AlertLifecycleStatus.DETECTED,
                         level=level, transition=transition, reason="test", physical_level=AlertLevel.WARNING,
                         context_index=0.4, recommended_action=RecommendedAction.ADVISE_CAUTION,
                         first_detected_at=NOW, last_updated_at=NOW)


@pytest.fixture
def fake(monkeypatch):
    r = FakeRedis()
    for m in (alert_service, alert_routes, context_service):
        monkeypatch.setattr(m, "get_redis", lambda: r.aio)
    return r


# ── WebSocket ────────────────────────────────────────────────────────────────

@pytest.fixture
def ws_app(fake, monkeypatch):
    async def allow(headers):
        if headers.get("x-api-key") != "good":
            raise alert_routes.WsAuthError(4401, "invalid_api_key", "Unknown or revoked API key.")
        return Principal(kind="client", id="c1", label="VIASafe mobile")
    monkeypatch.setattr(alert_routes, "authorize_ws", allow)
    app = FastAPI()
    app.include_router(alert_routes.ws_router, prefix="/alerts")
    return app


async def _store_active(fake, alert):
    key = alert_service.pair_key(alert.vehicle_a_id, alert.vehicle_b_id)
    fake.set(f"alert:{key}", alert.model_dump_json())
    fake.sadd(f"alert:active_pairs:{alert.vehicle_a_id}", key)


def test_ws_snapshot_then_pushed_transition(ws_app, fake):
    import asyncio
    asyncio.run(_store_active(fake, make_alert()))
    with TestClient(ws_app) as client:
        with client.websocket_connect("/alerts/ws?vehicle_id=A", headers={"X-API-Key": "good"}) as ws:
            snap = ws.receive_json()
            assert snap["type"] == "snapshot" and snap["vehicle_id"] == "A"
            assert [a["conflict_id"] for a in snap["alerts"]] == ["CF-20261029-0a1b2c3d"]
            escalated = make_alert(level=AlertLevel.CRITICAL, transition=AlertTransition.ESCALATED)
            fake.publish(alert_service.alert_channel("A"), json.dumps(alert_service.alert_message(escalated, "A")))
            msg = ws.receive_json()
            assert msg["type"] == "alert" and msg["other_vehicle_id"] == "B"
            assert msg["alert"]["level"] == "CRITICAL" and msg["alert"]["transition"] == "ESCALATED"
            ws.send_text(json.dumps({"type": "ping"}))
            assert ws.receive_json()["type"] == "pong"


def test_ws_subscribe_by_message_and_switch(ws_app, fake):
    with TestClient(ws_app) as client:
        with client.websocket_connect("/alerts/ws", headers={"X-API-Key": "good"}) as ws:
            ws.send_text(json.dumps({"vehicle_id": "B"}))
            snap = ws.receive_json()
            assert snap == {**snap, "type": "snapshot", "vehicle_id": "B", "alerts": []}
            ws.send_text(json.dumps({"vehicle_id": "C"}))
            assert ws.receive_json()["vehicle_id"] == "C"
            fake.publish(alert_service.alert_channel("B"), json.dumps({"type": "alert", "vehicle_id": "B"}))
            fake.publish(alert_service.alert_channel("C"), json.dumps({"type": "alert", "vehicle_id": "C"}))
            assert ws.receive_json()["vehicle_id"] == "C"                     # B was unsubscribed


def test_ws_keepalive_ping(ws_app, monkeypatch):
    monkeypatch.setattr(alert_routes, "KEEPALIVE_S", 0.05)
    with TestClient(ws_app) as client:
        with client.websocket_connect("/alerts/ws?vehicle_id=A", headers={"X-API-Key": "good"}) as ws:
            assert ws.receive_json()["type"] == "snapshot"
            assert ws.receive_json()["type"] == "ping"


def test_ws_rejects_a_bad_key(ws_app):
    from starlette.websockets import WebSocketDisconnect
    with TestClient(ws_app) as client:
        with client.websocket_connect("/alerts/ws?vehicle_id=A", headers={"X-API-Key": "bad"}) as ws:
            assert ws.receive_json() == {"type": "error", "code": "invalid_api_key",
                                         "message": "Unknown or revoked API key."}
            with pytest.raises(WebSocketDisconnect) as e:
                ws.receive_json()
            assert e.value.code == 4401


def test_ws_needs_a_vehicle(ws_app, monkeypatch):
    monkeypatch.setattr(alert_routes, "SUBSCRIBE_TIMEOUT_S", 0.05)
    with TestClient(ws_app) as client:
        with client.websocket_connect("/alerts/ws", headers={"X-API-Key": "good"}) as ws:
            assert ws.receive_json()["code"] == "vehicle_id_required"


def test_ws_route_is_mounted_on_the_real_app(monkeypatch):
    """The real app routes /alerts/ws to the socket (not to GET /alerts/{vehicle_id}), and the
    HTTP router guard does not get in the way — the socket authenticates itself."""
    from app.main import app

    async def deny(headers):
        raise alert_routes.WsAuthError(4401, "api_key_required", "This endpoint requires an API key.")
    monkeypatch.setattr(alert_routes, "authorize_ws", deny)
    with TestClient(app).websocket_connect("/alerts/ws?vehicle_id=A") as ws:
        assert ws.receive_json()["code"] == "api_key_required"


# ── SQLite database (feedback, precomputation, calibration) ──────────────────

@pytest.fixture
async def db():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.core.db.session import Base
    from app.modules.m06_hazards.models.hazard import Hazard
    from app.modules.m08_historical.models import event_rollup as M
    from app.modules.m08_historical.models.historical_event import HistoricalEventRecord as E
    from app.modules.m10_collision.models.alert_feedback import AlertFeedback
    from app.modules.m11_community.models.signalement import Signalement
    from app.modules.m13_admin.models.system_setting import SystemSetting

    url = os.getenv("VIASAFE_TEST_PG_URL", "sqlite+aiosqlite:///:memory:")
    engine = create_async_engine(url)
    tables = [E.__table__, M.EventDaily.__table__, M.EventDailyHour.__table__, M.EventDailyCell.__table__,
              M.EventRollupState.__table__, Hazard.__table__, Signalement.__table__, SystemSetting.__table__,
              AlertFeedback.__table__]
    async with engine.begin() as conn:
        if url.startswith("postgresql"):        # throwaway database: start from an empty schema (enum types)
            await conn.exec_driver_sql("DROP SCHEMA public CASCADE")
            await conn.exec_driver_sql("CREATE SCHEMA public")
            await conn.exec_driver_sql("CREATE SEQUENCE IF NOT EXISTS hazards_number_seq")
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        yield s
    await engine.dispose()


@pytest.fixture
def fresh_thresholds(monkeypatch):
    thresholds.set_current(None)

    async def no_reload(db=None, force=False):
        return thresholds.current()
    monkeypatch.setattr(thresholds, "refresh", no_reload)
    yield
    thresholds.set_current(None)


async def test_feedback_is_idempotent_and_feeds_metrics(db, fake, fresh_thresholds):
    await _store_active(fake, make_alert())
    app = FastAPI()
    app.include_router(alert_routes.router, prefix="/alerts")

    async def _db():
        yield db
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_principal] = lambda: Principal(kind="client", id="c1", label="VIASafe mobile")
    from httpx import ASGITransport, AsyncClient
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        url = "/alerts/CF-20261029-0a1b2c3d/feedback"
        r1 = await client.post(url, json={"vehicle_id": "A", "verdict": "useful"})
        assert r1.status_code == 200, r1.text
        assert r1.json()["level"] == "WARNING" and r1.json()["context_index"] == 0.4
        r2 = await client.post(url, json={"vehicle_id": "A", "verdict": "false_alert", "comment": " too early "})
        assert r2.json()["verdict"] == "false_alert" and r2.json()["comment"] == "too early"
        assert r2.json()["created_at"] == r1.json()["created_at"]
        await client.post(url, json={"vehicle_id": "B", "verdict": "useful"})
        bad = await client.post("/alerts/nope/feedback", json={"vehicle_id": "A", "verdict": "useful"})
        assert bad.status_code == 422
        assert (await client.post(url, json={"vehicle_id": "A", "verdict": "maybe"})).status_code == 422

    await metrics_service.record(fake.aio, make_alert())
    out = await metrics_service.metrics(db, fake.aio, "24h")
    assert out["alerts_total"] == 1 and out["by_level"]["WARNING"] == 1
    assert out["feedback"]["total"] == 2 and out["feedback"]["false_alert_rate"] == 0.5
    assert out["feedback"]["by_level"]["WARNING"] == {"false_alert": 1, "useful": 0}


# ── precomputation ───────────────────────────────────────────────────────────

async def _seed(db, now):
    from app.modules.m06_hazards.models.hazard import Hazard
    from app.modules.m08_historical.models.historical_event import HistoricalEventRecord as E
    from app.modules.m11_community.models.signalement import Signalement, SignalementStatus, SignalementType
    naive = now.replace(tzinfo=None)
    n = 0

    def ev(t, pos, days, vehicle="LTTR 995 AR", speed=None, limit=None, severity="2"):
        nonlocal n
        n += 1
        db.add(E(source="ymane", source_event_id=f"e{n}", event_type=t, occurred_at=naive - timedelta(days=days),
                 latitude=pos[0], longitude=pos[1], vehicle_external_id=vehicle, driver_external_id=None,
                 speed_mps=speed, speed_limit_mps=limit, severity=severity, grid_cell_id="x", raw={}))
    hot = DOUALA
    calm = geo.destination(*DOUALA, 90.0, 2000.0)
    for i in range(12):
        ev("HARD_BRAKING", hot, 5 + i)
    for i in range(6):
        ev("SPEEDING", hot, 3 + i, speed=19.0, limit=50 / 3.6, severity=None)
    ev("SPEEDING", hot, 4, speed=25.0, limit=80 / 3.6, severity=None)
    ev("HARD_ACCELERATION", calm, 10, vehicle="CALM 1")
    db.add(Hazard(id="h1", number=1, code="HZ-000001", type="pothole", status="active",
                  latitude=geo.destination(*hot, 0.0, 50.0)[0], longitude=hot[1], radius_m=10.0))
    db.add(Hazard(id="h2", number=2, code="HZ-000002", type="pothole", status="resolved",
                  latitude=hot[0], longitude=hot[1], radius_m=10.0))
    db.add(Signalement(id="s1", type=SignalementType.accident, status=SignalementStatus.actif,
                       latitude=geo.destination(*hot, 180.0, 150.0)[0], longitude=hot[1], reported_at=naive))
    db.add(Signalement(id="s2", type=SignalementType.accident, status=SignalementStatus.actif,
                       latitude=hot[0], longitude=hot[1], reported_at=naive - timedelta(days=5)))   # expired
    db.add(Signalement(id="s3", type=SignalementType.police, status=SignalementStatus.actif,
                       latitude=hot[0], longitude=hot[1], reported_at=naive))                      # not a kind
    await db.commit()
    return hot, calm


async def test_precomputed_context_map_round_trip(db, fake, fresh_thresholds):
    hot, calm = await _seed(db, NOW)
    data = await context_service.build(db, NOW)
    context_service.write(fake, data, 900)

    cell = await context_service.place_at(fake.aio, *hot)
    assert cell is not None and cell.history_dominant == "HARD_BRAKING"
    assert cell.history_counts == {"HARD_BRAKING": 12, "SPEEDING": 7}
    assert cell.local_limit_mps == pytest.approx(13.89, abs=0.01)        # median of 6 × 50 and 1 × 80 km/h
    assert [h[0] for h in cell.hazards] == ["pothole"]                    # the resolved one is not there
    assert [r[1] for r in cell.reports] == ["ACCIDENT_REPORTED"]          # expired / police reports left out
    assert cell.score > 0 and cell.reasons[0].code == "HISTORY_HARSH_BRAKING"
    calm_cell = await context_service.place_at(fake.aio, *calm)
    assert calm_cell.history < cell.history

    scores = await context_service.driver_scores(fake.aio, [("lttr-995 ar", None), ("CALM 1", None), (None, None)])
    assert scores[0][0] > scores[1][0] and scores[2] == (None, False, None)
    meta = json.loads(fake.get(context_service.META_KEY))
    assert meta["tz"] == "Africa/Douala" and meta["cells"] == len(data["cells"])


async def test_refresh_runs_when_due_or_when_events_changed(db, fake, fresh_thresholds):
    await _seed(db, NOW)
    first = await context_service.refresh(db, fake)
    assert first["cells"] > 0
    assert await context_service.refresh(db, fake) == {"skipped": "not due"}
    fake.incr(context_service.DATA_VERSION_KEY)                          # an event import bumped the version
    assert "cells" in await context_service.refresh(db, fake)


def test_due_rule():
    meta = json.dumps({"built_at": NOW.isoformat(), "data_version": "3"})
    assert context_service.due(None, "3", 300, NOW)
    assert not context_service.due(meta, "3", 300, NOW + timedelta(seconds=60))
    assert context_service.due(meta, "4", 300, NOW + timedelta(seconds=60))
    assert context_service.due(meta, "3", 300, NOW + timedelta(seconds=300))


async def test_calibration_on_synthetic_history(db, fresh_thresholds):
    from app.modules.m08_historical.models.historical_event import HistoricalEventRecord as E
    naive = NOW.replace(tzinfo=None)
    n = 0
    spots = [geo.destination(*DOUALA, 90.0, 500.0 * i) for i in range(8)]
    for i, p in enumerate(spots):
        hot = i < 3
        for k in range(10 if hot else 1):
            for days, t in ((150 + k, "HARD_BRAKING"), (10 + k, "HARD_BRAKING" if hot else "SPEEDING")):
                n += 1
                db.add(E(source="ymane", source_event_id=f"c{n}", event_type=t, occurred_at=naive - timedelta(days=days),
                         latitude=p[0], longitude=p[1], severity="2", grid_cell_id="x", raw={}))
    await db.commit()
    rep = await context_service.calibrate(db, holdout_days=90, min_events=5, now=NOW)
    assert rep["hot_cells"] == 3 and rep["verdict"] == "ok"
    assert rep["mean_index_hot"] > rep["mean_index_other"]
    from app.cli import format_calibration
    assert "AUC" in format_calibration(rep)


def test_spot_cells_are_distinct():
    assert len({grid_cell_id(*geo.destination(*DOUALA, 90.0, 500.0 * i), 150.0) for i in range(8)}) == 8


async def test_authorize_ws_uses_the_rest_credentials(monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.core.config import settings
    from app.core.db import session as session_mod
    from app.core.db.session import Base
    from app.core.security import hash_token
    from app.modules.m01_security.models.auth_token import AuthSession
    from app.modules.m12_integration.models.api_client import ApiClient

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[ApiClient.__table__, AuthSession.__table__]))
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        s.add(ApiClient(name="VIASafe mobile", key_prefix="vsk_good", key_hash=hash_token("vsk_good_key"),
                        created_by="test"))
        s.add(ApiClient(name="old", key_prefix="vsk_old", key_hash=hash_token("vsk_old_key"), created_by="test",
                        is_active=False))
        await s.commit()
    monkeypatch.setattr(session_mod, "AsyncSessionLocal", Session)
    monkeypatch.setattr(settings, "AUTH_ENFORCED", True)

    p = await alert_routes.authorize_ws({"x-api-key": "vsk_good_key"})
    assert p.kind == "client" and p.label == "VIASafe mobile"
    for headers, code in (({"x-api-key": "vsk_old_key"}, "invalid_api_key"), ({}, "not_authenticated"),
                          ({"authorization": "Bearer nope"}, "session_expired")):
        with pytest.raises(alert_routes.WsAuthError) as e:
            await alert_routes.authorize_ws(headers)
        assert (e.value.close_code, e.value.code) == (4401, code)
    await engine.dispose()


async def test_precomputation_reads_the_daily_summaries_the_same_way(db, fake, fresh_thresholds):
    from app.modules.m08_historical import rollups
    from app.modules.m09_driver_behaviour.settings import DriverBehaviourSettings
    hot, _ = await _seed(db, NOW)
    raw = await context_service.build(db, NOW)
    assert raw["meta"]["rollups"] is False
    await rollups.rebuild(db, DriverBehaviourSettings())
    summed = await context_service.build(db, NOW)
    assert summed["meta"]["rollups"] is True
    cid = grid_cell_id(*hot, context_service.cell_size_m())
    a, b = raw["cells"][cid], summed["cells"][cid]
    assert a.history_counts == b.history_counts and a.history == pytest.approx(b.history, abs=0.02)
    assert raw["drivers"] == summed["drivers"]
