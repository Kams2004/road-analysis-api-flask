"""
Step 6.1 (Historical Data & AI Dataset Preparation) scenarios. Grid math,
map-matching, source adapters' pure logic, and aggregation/scoring are all
unit-tested directly — no network, no database. Orchestration
(historical_service.import_events / get_zone_risk_profiles*, which need a
real AsyncSession) is validated live against the running stack instead,
the same precedent Steps 2-5's orchestration layers (road_state_service,
trajectory_service, collision_service, alert_service) followed — only their
pure core logic has automated tests here.
"""
from datetime import date, datetime, timezone

import pytest

from app.modules.m08_historical.models.historical_event import HistoricalEventRecord
from app.modules.m08_historical import matcher
from app.modules.m08_historical.grid import grid_cell_center, grid_cell_id
from app.modules.m08_historical.domain import HistoricalEvent, HistoricalEventType
from app.modules.m08_historical.sources.manual import ManualEventSource
from app.modules.m08_historical.sources.ymane import _map_raw_event_fields, YmaneEventSource
from app.modules.m08_historical.historical_service import _aggregate_by_cell, _risk_score
from tests.fakes import FakeOSRMClient, nearest_response

KAMBO = (4.0412, 9.8107)


# ── Grid ─────────────────────────────────────────────────────────────────────

def test_grid_cell_id_stable_for_same_point():
    a = grid_cell_id(*KAMBO, 150.0)
    b = grid_cell_id(*KAMBO, 150.0)
    assert a == b


def test_grid_cell_id_groups_nearby_points_together():
    nearby = (KAMBO[0] + 0.0002, KAMBO[1] + 0.0002)  # ~25m away
    assert grid_cell_id(*KAMBO, 150.0) == grid_cell_id(*nearby, 150.0)


def test_grid_cell_id_separates_distant_points():
    far = (KAMBO[0] + 0.02, KAMBO[1] + 0.02)  # ~2.5km away
    assert grid_cell_id(*KAMBO, 150.0) != grid_cell_id(*far, 150.0)


def test_grid_cell_id_differs_by_cell_size():
    assert grid_cell_id(*KAMBO, 150.0) != grid_cell_id(*KAMBO, 300.0)


def test_grid_cell_center_round_trips_close_to_original():
    cell = grid_cell_id(*KAMBO, 150.0)
    center_lat, center_lon = grid_cell_center(cell)
    # centre of the containing cell should be within ~1.5 cell-widths of the original point
    dlat = abs(center_lat - KAMBO[0])
    dlon = abs(center_lon - KAMBO[1])
    assert dlat < 0.003
    assert dlon < 0.003


# ── Matcher (reuses the Step 2 FakeOSRMClient test double) ─────────────────

async def test_matcher_populates_road_name_and_segment_on_success():
    event = HistoricalEvent(
        source="manual", source_event_id="1", event_type=HistoricalEventType.SPEEDING,
        occurred_at=datetime.now(timezone.utc), latitude=KAMBO[0], longitude=KAMBO[1],
    )
    client = FakeOSRMClient(nearest_responses=[nearest_response(KAMBO, name="Route de Kambo")])

    matched = await matcher.match_event(event, client=client)

    assert matched.road_name == "Route de Kambo"
    assert matched.road_segment_id is not None
    assert matched.grid_cell_id == grid_cell_id(*KAMBO, matched_cell_size(matched))
    assert matched.match_confidence is not None


def matched_cell_size(matched) -> float:
    # helper: pull the configured default cell size back out for comparison
    from app.core.config import settings
    return settings.HISTORICAL_GRID_CELL_SIZE_M


async def test_matcher_falls_back_gracefully_when_osrm_unavailable():
    from app.modules.m03_maps.osrm.client import OSRMUnavailableError

    event = HistoricalEvent(
        source="manual", source_event_id="2", event_type=HistoricalEventType.HARD_BRAKING,
        occurred_at=datetime.now(timezone.utc), latitude=KAMBO[0], longitude=KAMBO[1],
    )
    client = FakeOSRMClient(nearest_responses=[OSRMUnavailableError("down")])

    matched = await matcher.match_event(event, client=client)  # must not raise

    assert matched.road_name is None
    assert matched.road_segment_id is None
    assert matched.grid_cell_id  # still computed from the raw coordinate


async def test_matcher_handles_no_match_code_gracefully():
    event = HistoricalEvent(
        source="manual", source_event_id="3", event_type=HistoricalEventType.SPEEDING,
        occurred_at=datetime.now(timezone.utc), latitude=KAMBO[0], longitude=KAMBO[1],
    )
    client = FakeOSRMClient(nearest_responses=[nearest_response(KAMBO, code="NoSegment")])

    matched = await matcher.match_event(event, client=client)

    assert matched.road_name is None
    assert matched.grid_cell_id


# ── ManualEventSource ────────────────────────────────────────────────────────

async def test_manual_source_filters_by_date_range():
    events = [
        HistoricalEvent(
            source="manual", source_event_id=str(i), event_type=HistoricalEventType.SPEEDING,
            occurred_at=datetime(2026, 1, i + 1, tzinfo=timezone.utc),
            latitude=KAMBO[0], longitude=KAMBO[1],
        )
        for i in range(5)
    ]
    source = ManualEventSource(events)

    result = await source.fetch(date(2026, 1, 2), date(2026, 1, 4))

    assert {e.source_event_id for e in result} == {"1", "2", "3"}


# ── Ymane adapter's pure field-mapping (no network) ─────────────────────────
# Field names below are the confirmed real schema (a live request/response
# pair captured from the user's own machine — see ymane.py's module
# docstring), not guesses.

_YMANE_SAMPLE = {
    "exceptionid": 21790527, "affiliateid": 107, "transporterid": 2885,
    "vehicleid": 1611, "driverid": 3439870,
    "startdatetime": 1789001922000, "enddatetime": 1789001923000,
    "exceptiontype": 19, "totalduration": 0.0, "totaldistance": 0.0,
    "level": 3, "threshold": 0.0, "maxvalue": 0.0,
    "distanceunderexception": 0.0, "timeexceeded": None, "requiredbreak": None,
    "nobreak": 0.0, "maxbreak": None,
    "startgps": "3.78508,11.282333", "endgps": "3.78508,11.282333",
    "vmax": 75.0, "speedid": "178900192307000500000000Mint0607",
    "invaliddata": 0, "unitid": "Mint0607", "isrestored": None,
    "restoreduserid": None, "usercoments": None, "restauredate": None,
    "evidence": "{}", "status": 1,
}


def test_ymane_maps_real_field_names():
    mapped = _map_raw_event_fields(_YMANE_SAMPLE)
    assert mapped is not None
    assert mapped.latitude == pytest.approx(3.78508)
    assert mapped.longitude == pytest.approx(11.282333)
    assert mapped.vehicle_external_id == "1611"
    assert mapped.driver_external_id == "3439870"
    assert mapped.speed_mps == pytest.approx(75 / 3.6, abs=0.01)
    assert mapped.event_type == HistoricalEventType.SPEEDING
    assert mapped.source_event_id == "21790527"


def test_ymane_treats_zero_threshold_as_absent_speed_limit():
    # Every 0.0-valued numeric field observed in real samples means "not
    # applicable to this exception", not a real zero — see ymane.py.
    mapped = _map_raw_event_fields(_YMANE_SAMPLE)
    assert mapped is not None
    assert mapped.speed_limit_mps is None


def test_ymane_falls_back_to_other_when_vmax_absent():
    raw = {**_YMANE_SAMPLE, "vmax": None}
    mapped = _map_raw_event_fields(raw)
    assert mapped is not None
    assert mapped.event_type == HistoricalEventType.OTHER


def test_ymane_skips_event_with_no_recognizable_coordinate():
    raw = {"somefield": "value", "another": 123}
    assert _map_raw_event_fields(raw) is None


def test_ymane_skips_event_with_unparsable_gps():
    raw = {**_YMANE_SAMPLE, "startgps": "not-a-coordinate", "endgps": None}
    assert _map_raw_event_fields(raw) is None


# ── Risk scoring / aggregation (pure) ───────────────────────────────────────

def _record(event_type: str, lat=KAMBO[0], lon=KAMBO[1], cell="150m:0:0", road_name="Route de Kambo", when=None):
    return HistoricalEventRecord(
        source="manual", source_event_id="x", event_type=event_type,
        occurred_at=when or datetime(2026, 1, 1), latitude=lat, longitude=lon,
        grid_cell_id=cell, road_name=road_name, raw={},
    )


def test_risk_score_zero_for_no_events():
    assert _risk_score({}) == 0.0


def test_risk_score_weights_accidents_higher_than_speeding():
    accident_score = _risk_score({"ACCIDENT": 1})
    speeding_score = _risk_score({"SPEEDING": 1})
    assert accident_score > speeding_score


def test_risk_score_saturates_at_one():
    score = _risk_score({"ACCIDENT": 100})
    assert score == 1.0


def test_aggregate_by_cell_groups_and_sorts_by_risk():
    rows = [
        _record("SPEEDING", cell="150m:0:0"),
        _record("SPEEDING", cell="150m:0:0"),
        _record("ACCIDENT", cell="150m:1:1"),
    ]
    profiles = _aggregate_by_cell(rows)

    assert len(profiles) == 2
    # the single-accident cell should outrank the two-speeding-event cell
    assert profiles[0].grid_cell_id == "150m:1:1"
    assert profiles[0].total_events == 1
    assert profiles[1].total_events == 2
    assert profiles[1].road_name == "Route de Kambo"


# ── Route preview proxy (web Historical Risk Analysis) ──────────────────────

class _RouteOSRM:
    def __init__(self, data):
        self.data = data
        self.calls = []

    async def route_overview(self, points, alternatives=True):
        from app.modules.m03_maps.osrm.client import OSRMResult
        self.calls.append((list(points), alternatives))
        return OSRMResult(self.data, 5.0)


async def test_route_preview_returns_latlon_geometry_and_alternatives(monkeypatch):
    from app.modules.m08_historical.routes import historical as routes
    fake = _RouteOSRM({"code": "Ok", "routes": [
        {"geometry": {"coordinates": [[9.70, 4.05], [9.71, 4.06]]}, "distance": 1500.0, "duration": 180.0},
        {"geometry": {"coordinates": [[9.70, 4.05], [9.72, 4.06]]}, "distance": 1900.0, "duration": 240.0},
    ]})
    monkeypatch.setattr(routes, "get_osrm_client", lambda: fake)

    out = await routes.route_preview(from_="4.05,9.70", to="4.06,9.71", alternatives=True)

    assert fake.calls[0][0] == [(4.05, 9.70), (4.06, 9.71)]
    assert len(out.routes) == 2
    assert out.routes[0].geometry[0] == (4.05, 9.70)        # lat, lon order
    assert out.routes[0].distance_m == 1500.0


async def test_route_preview_rejects_bad_input_and_no_route(monkeypatch):
    from fastapi import HTTPException
    from app.modules.m08_historical.routes import historical as routes
    monkeypatch.setattr(routes, "get_osrm_client", lambda: _RouteOSRM({"code": "NoRoute", "routes": []}))

    with pytest.raises(HTTPException) as e:
        await routes.route_preview(from_="nope", to="4.06,9.71", alternatives=True)
    assert e.value.status_code == 422
    with pytest.raises(HTTPException) as e:
        await routes.route_preview(from_="4.05,9.70", to="4.06,9.71", alternatives=True)
    assert e.value.status_code == 404


# ── Module 4 dashboard endpoints (SQLite) ────────────────────────────────────

@pytest.fixture
async def hist_client():
    from datetime import timedelta
    import httpx
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.core.db.session import Base, get_db
    from app.modules.m08_historical.routes.historical import router
    from app.modules.m13_admin.models.system_setting import SystemSetting   # severity rules of the summaries

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(
            c, tables=[HistoricalEventRecord.__table__, SystemSetting.__table__]))
    Session = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.utcnow()
    async with Session() as s:
        hot = grid_cell_id(*KAMBO, 150.0)
        for i in range(8):                       # 8 accidents in one cell → score 1.0 (5 × 8 / 20)
            s.add(HistoricalEventRecord(source="pytest", source_event_id=f"a{i}", event_type="ACCIDENT",
                                        occurred_at=now - timedelta(days=i), latitude=KAMBO[0], longitude=KAMBO[1],
                                        grid_cell_id=hot, raw={}))
        s.add(HistoricalEventRecord(source="ymane", source_event_id="z", event_type="SPEEDING",
                                    occurred_at=now - timedelta(days=200), latitude=KAMBO[0] + 0.05,
                                    longitude=KAMBO[1], grid_cell_id=grid_cell_id(KAMBO[0] + 0.05, KAMBO[1], 150.0),
                                    raw={}))
        await s.commit()

    async def _db():
        async with Session() as s:
            yield s

    app = FastAPI()
    app.include_router(router, prefix="/historical")
    app.dependency_overrides[get_db] = _db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c
    await engine.dispose()


async def test_historical_overview_zones_trends_imports(hist_client):
    c = hist_client
    o = (await c.get("/historical/overview")).json()
    assert o["total_events"] == 9 and o["cells"] == 2 and o["high_risk_cells"] == 1
    assert o["low_coverage_cells"] == 1 and o["accidents"] == 8 and o["sources"] == {"pytest": 8, "ymane": 1}
    assert (await c.get("/historical/overview", params={"period": "30d"})).json()["total_events"] == 8
    z = (await c.get("/historical/zones")).json()
    assert len(z) == 1 and z[0]["historical_risk_score"] == 1.0 and z[0]["coverage"] == "medium"
    h = (await c.get("/historical/heatmap")).json()
    assert {x["coverage"] for x in h} == {"medium", "low"}
    t = (await c.get("/historical/trends", params={"period": "30d"})).json()
    assert t["unit"] == "day" and sum(p["events"] for p in t["series"]) == 8
    assert sum(p["risk_points"] for p in t["series"]) == 40.0
    imp = (await c.get("/historical/imports")).json()
    assert {b["source"] for b in imp} == {"pytest", "ymane"}
    assert (await c.get("/historical/overview", params={"period": "x"})).status_code == 422


def test_current_risk_weighs_severity_and_recency():
    from datetime import timedelta
    from types import SimpleNamespace as NS
    from app.modules.m08_historical.historical_service import current_profiles, recency
    from app.modules.m09_driver_behaviour.settings import DriverBehaviourSettings
    now = datetime(2026, 10, 28)
    cell = grid_cell_id(*KAMBO, 150.0)

    def ev(t, days, sev):
        return NS(grid_cell_id=cell, event_type=t, occurred_at=now - timedelta(days=days), latitude=KAMBO[0],
                  longitude=KAMBO[1], road_name="N3", severity=sev, speed_mps=None, speed_limit_mps=None)

    cfg = DriverBehaviourSettings()
    assert recency(now - timedelta(days=90), now, 90) == 0.5
    fresh_high = current_profiles([ev("HARD_BRAKING", 0, "3")] * 4, cfg, now)[0]
    old_low = current_profiles([ev("HARD_BRAKING", 180, "1")] * 4, cfg, now)[0]
    # 4 × 1.5 × 2 (high) = 12 / 20 → 0.6 ; 4 × 1.5 × 0.5 (low) × 0.25 (two half-lives) = 0.75 / 20
    assert fresh_high.historical_risk_score == 0.6 and old_low.historical_risk_score == 0.0375
    assert fresh_high.high_severity == 4 and fresh_high.recent_events == 4 and old_low.recent_events == 0
    mixed = current_profiles([ev("SPEEDING", 1, "1")] * 3 + [ev("HARD_BRAKING", 1, "3")], cfg, now)[0]
    assert mixed.dominant_type == "HARD_BRAKING" and mixed.total_events == 4
