"""
Module 5 — Driver Behaviour Analytics: severity normalisation, scoring,
rankings with trend, trends / heatmap / zones, and the /driver-behaviour
endpoints against an in-memory SQLite copy of `historical_events` (the single
event store shared with Module 4).
"""
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.modules.m09_driver_behaviour import analytics as A
from app.modules.m09_driver_behaviour.settings import DriverBehaviourSettings

CFG = DriverBehaviourSettings()
NOW = datetime(2026, 10, 20, 12, 0)
DOUALA = (4.0511, 9.7679)


def ev(event_type="SPEEDING", days_ago=1, driver="D1", vehicle="V1", speed=None, limit=None, severity=None,
       lat=DOUALA[0], lon=DOUALA[1], road="Boulevard de la Liberté", hour=12, i=0):
    ts = (NOW - timedelta(days=days_ago)).replace(hour=hour)
    return SimpleNamespace(id=f"e{i}-{event_type}-{days_ago}-{driver}", source="manual", event_type=event_type,
                           occurred_at=ts, latitude=lat, longitude=lon, driver_external_id=driver,
                           vehicle_external_id=vehicle, speed_mps=speed, speed_limit_mps=limit,
                           severity=severity, road_name=road, grid_cell_id="150m:1:1")


# ── pure analytics ───────────────────────────────────────────────────────────

def test_severity_from_source_and_speed_excess():
    assert A.normalize_severity(ev(severity="SEVERE"), CFG) == "high"
    assert A.normalize_severity(ev(severity="minor"), CFG) == "low"
    assert A.normalize_severity(ev(severity="2"), CFG) == "medium"
    # speeding without severity: excess over the limit (10 % / 25 % thresholds)
    assert A.normalize_severity(ev(speed=30.0, limit=25.0), CFG) == "medium"    # +20 %
    assert A.normalize_severity(ev(speed=35.0, limit=25.0), CFG) == "high"      # +40 %
    assert A.normalize_severity(ev(speed=26.0, limit=25.0), CFG) == "low"       # +4 %
    assert A.normalize_severity(ev("HARD_BRAKING"), CFG) == "unknown"


def test_points_and_score_are_weighted_and_saturate():
    assert A.event_points(ev("HARD_BRAKING", severity="high"), CFG) == pytest.approx(1.5 * 2.0)
    assert A.event_points(ev("SPEEDING", severity="low"), CFG) == pytest.approx(1.0 * 0.5)
    assert A.score_from_points(20, CFG) == 50.0          # saturation 40 points
    assert A.score_from_points(400, CFG) == 100.0
    assert A.risk_level(70, CFG) == "high" and A.risk_level(90, CFG) == "very_high"
    assert A.risk_level(35, CFG) == "medium" and A.risk_level(5, CFG) == "low"


def test_settings_validation():
    with pytest.raises(ValueError):
        DriverBehaviourSettings(type_weights={"ACCIDENT": 3})
    with pytest.raises(ValueError):
        DriverBehaviourSettings(speeding_medium_excess_pct=30, speeding_high_excess_pct=20)
    with pytest.raises(ValueError):
        DriverBehaviourSettings(timezone="Mars/Olympus")


def test_ranking_with_trend_against_previous_period():
    cur = [ev("HARD_BRAKING", severity="high", driver="D1", i=i) for i in range(10)] + \
          [ev("SPEEDING", severity="low", driver="D2", i=i) for i in range(2)] + \
          [ev("SPEEDING", driver=None, vehicle="V9")]
    prev = [ev("HARD_BRAKING", severity="high", driver="D1", days_ago=40, i=i) for i in range(2)] + \
           [ev("SPEEDING", severity="high", driver="D2", days_ago=40, i=i) for i in range(10)]
    rows = A.rank_entities(cur, prev, "driver", CFG)
    assert [r["id"] for r in rows] == ["D1", "D2"]          # unidentified event not ranked
    d1, d2 = rows
    assert d1["score"] == 75.0 and d1["level"] == "high" and d1["trend"] == "worse"
    assert d2["trend"] == "better" and d2["score_delta"] < 0
    assert d1["by_type"]["HARD_BRAKING"] == 10 and d1["rank"] == 1
    vehicles = A.rank_entities(cur, None, "vehicle", CFG)
    assert {r["id"] for r in vehicles} == {"V1", "V9"} and vehicles[0]["trend"] is None


def test_type_summaries_speed_and_peak_hour():
    rows = [ev(speed=30.0, limit=25.0, hour=7, i=1), ev(speed=35.0, limit=25.0, hour=7, i=2),
            ev("HARSH_CORNERING", hour=18, i=3)]
    s = {x["event_type"]: x for x in A.type_summaries(rows, CFG)}
    sp = s["SPEEDING"]
    assert sp["count"] == 2 and sp["avg_speed_kmh"] == 117.0 and sp["avg_limit_kmh"] == 90.0
    assert sp["avg_excess_pct"] == 30.0 and sp["by_severity"]["high"] == 1
    assert sp["peak_hour"] == 8          # 07:00 UTC = 08:00 Africa/Douala
    assert s["HARD_ACCELERATION"]["count"] == 0
    assert s["HARSH_CORNERING"]["share"] == pytest.approx(1 / 3, abs=1e-3)


def test_trends_buckets_hours_weekdays():
    rows = [ev(days_ago=d, i=d) for d in (1, 1, 3)] + [ev("HARD_BRAKING", days_ago=2)]
    win = A.resolve_window("7d", None, None, NOW)
    t = A.trends(rows, win.start, win.end, CFG)
    assert t["unit"] == "day" and len(t["series"]) == 8
    assert sum(r["total"] for r in t["series"]) == 4
    assert sum(r["SPEEDING"] for r in t["series"]) == 3
    assert sum(h["total"] for h in t["by_hour"]) == 4 and len(t["by_weekday"]) == 7


def test_resolve_window_custom_and_errors():
    w = A.resolve_window(None, date(2026, 9, 1), date(2026, 9, 30), NOW)
    assert w.start == datetime(2026, 9, 1) and w.end == datetime(2026, 10, 1)
    assert w.prev_start == datetime(2026, 8, 2)
    assert A.resolve_window("all", None, None, NOW).has_previous is False
    with pytest.raises(ValueError):
        A.resolve_window("5d", None, None, NOW)


def test_heatmap_and_zones_criteria():
    hot = [ev("HARD_BRAKING", driver=f"D{i}", i=i) for i in range(6)] + [ev("SPEEDING", i=99)]
    far = [ev(lat=DOUALA[0] + 0.05, i=i) for i in range(2)]
    cells = A.heatmap(hot + far, 250, CFG)
    assert cells[0]["count"] == 7 and cells[0]["by_type"]["HARD_BRAKING"] == 6
    zones = A.zones(hot + far, CFG)
    assert len(zones) == 1                                   # the 2-event cell is below zone_min_events
    z = zones[0]
    assert z["dominant_type"] == "HARD_BRAKING" and z["multi_behaviour"] and z["drivers"] == 6
    strict = CFG.model_copy(update={"zone_min_drivers": 10})
    assert A.zones(hot, strict) == []


# ── endpoints (SQLite) ───────────────────────────────────────────────────────

@pytest.fixture
async def client():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.core import settings_service
    from app.core.db.session import Base, get_db
    from app.modules.m08_historical.models.historical_event import HistoricalEventRecord as E
    from app.modules.m09_driver_behaviour.routes.driver_behaviour import router
    from app.modules.m13_admin.models.system_setting import SystemSetting

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[E.__table__, SystemSetting.__table__]))
    Session = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.utcnow()
    async with Session() as s:
        n = 0
        def add(t, days, driver, vehicle, speed=None, limit=None, severity=None, lat=DOUALA[0]):
            nonlocal n
            n += 1
            s.add(E(source="pytest", source_event_id=f"x{n}", event_type=t, occurred_at=now - timedelta(days=days),
                    latitude=lat, longitude=DOUALA[1], driver_external_id=driver, vehicle_external_id=vehicle,
                    speed_mps=speed, speed_limit_mps=limit, severity=severity, grid_cell_id="c", road_name="N3",
                    raw={}))
        for _ in range(6):
            add("HARD_BRAKING", 2, "D1", "V1", severity="high")
        add("SPEEDING", 3, "D2", "V2", speed=35.0, limit=25.0)
        add("SPEEDING", 40, "D2", "V2", severity="high")          # previous 30-day window
        add("ACCIDENT", 1, "D1", "V1")                            # Module 4 only — never in Module 5
        add("HARSH_CORNERING", 5, None, None, lat=DOUALA[0] + 0.1)
        await s.commit()
    settings_service.clear_cache()

    async def _db():
        async with Session() as s:
            yield s

    app = FastAPI()
    app.include_router(router, prefix="/driver-behaviour")
    app.dependency_overrides[get_db] = _db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c
    settings_service.clear_cache()
    await engine.dispose()


async def test_overview_endpoint(client):
    r = await client.get("/driver-behaviour/overview", params={"period": "30d"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["total_events"] == 8 and d["previous_total_events"] == 1
    by = {x["event_type"]: x for x in d["by_type"]}
    assert by["HARD_BRAKING"]["count"] == 6 and by["SPEEDING"]["avg_excess_pct"] == 40.0
    assert "ACCIDENT" not in by and d["drivers"] == 2 and d["vehicles"] == 2
    assert d["identified_share"] == pytest.approx(7 / 8, abs=1e-3)


async def test_events_filters_and_pagination(client):
    r = await client.get("/driver-behaviour/events", params={"period": "30d", "types": "SPEEDING,HARD_BRAKING",
                                                             "limit": 5})
    d = r.json()
    assert d["total"] == 7 and len(d["items"]) == 5
    r = await client.get("/driver-behaviour/events", params={"period": "30d", "driver": "D2"})
    item = r.json()["items"][0]
    assert item["speed_kmh"] == 126.0 and item["severity"] == "high" and item["points"] == 2.0
    r = await client.get("/driver-behaviour/events", params={"period": "all", "severity": "unknown"})
    assert r.json()["total"] == 1
    r = await client.get("/driver-behaviour/events", params={"bbox": "4.1,9.7,4.2,9.8", "period": "all"})
    assert r.json()["total"] == 1
    assert (await client.get("/driver-behaviour/events", params={"bbox": "x"})).status_code == 422
    assert (await client.get("/driver-behaviour/events", params={"period": "5d"})).status_code == 422


async def test_rankings_detail_trends_zones(client):
    d = (await client.get("/driver-behaviour/drivers", params={"period": "30d"})).json()
    assert [x["id"] for x in d["items"]] == ["D1", "D2"] and d["unidentified_events"] == 1
    assert d["items"][0]["score"] == 45.0 and d["items"][1]["trend"] == "stable"
    v = (await client.get("/driver-behaviour/vehicles", params={"period": "30d"})).json()
    assert v["total"] == 2
    det = (await client.get("/driver-behaviour/drivers/D1", params={"period": "30d"})).json()
    assert det["ranking"]["rank"] == 1 and len(det["recent_events"]) == 6
    assert (await client.get("/driver-behaviour/drivers/nobody")).status_code == 404
    t = (await client.get("/driver-behaviour/trends", params={"period": "7d"})).json()
    assert t["unit"] == "day" and sum(s["total"] for s in t["series"]) == 8
    h = (await client.get("/driver-behaviour/heatmap", params={"period": "30d", "types": "HARD_BRAKING"})).json()
    assert h["max_count"] == 6 and len(h["cells"]) == 1
    z = (await client.get("/driver-behaviour/zones", params={"period": "30d"})).json()
    assert len(z["items"]) == 1 and z["items"][0]["dominant_type"] == "HARD_BRAKING"
    m = (await client.get("/driver-behaviour/methodology")).json()
    assert m["version"] == "dba-1" and m["settings"]["saturation_points"] == 40.0


async def test_csv_export(client):
    r = await client.get("/driver-behaviour/events/export", params={"period": "30d"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    lines = r.text.strip().splitlines()
    assert lines[0].startswith("occurred_at_utc,event_type") and len(lines) == 9
