"""
Module 9 — Exception patterns (formerly Driver behaviour): severity normalisation, type
summaries, breakdowns (provider, country, severity, speed / excess bands), trends, heatmap and
zones, and the /exception-patterns endpoints against an in-memory SQLite copy of
`historical_events`. Anonymous: no answer carries a driver or a vehicle.
"""
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.modules.m09_exception_patterns import analytics as A
from app.modules.m09_exception_patterns.settings import ExceptionPatternSettings

CFG = ExceptionPatternSettings()
NOW = datetime(2026, 10, 20, 12, 0)
DOUALA = (4.0511, 9.7679)


def ev(event_type="SPEEDING", days_ago=1, speed=None, limit=None, severity=None, lat=DOUALA[0], lon=DOUALA[1],
       road="Boulevard de la Liberté", hour=12, i=0, source="ymane", country="CAMEROUN"):
    ts = (NOW - timedelta(days=days_ago)).replace(hour=hour)
    return SimpleNamespace(id=f"e{i}-{event_type}-{days_ago}", source=source, event_type=event_type,
                           occurred_at=ts, latitude=lat, longitude=lon, speed_mps=speed, speed_limit_mps=limit,
                           severity=severity, road_name=road, grid_cell_id="150m:1:1", country=country)


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


def test_settings_validation_and_retired_scoring_fields():
    with pytest.raises(ValueError):
        ExceptionPatternSettings(speeding_medium_excess_pct=30, speeding_high_excess_pct=20)
    with pytest.raises(ValueError):
        ExceptionPatternSettings(timezone="Mars/Olympus")
    with pytest.raises(ValueError):
        ExceptionPatternSettings(severity_multipliers={"extreme": 3})
    # a section stored by the former driver scoring still loads; its scoring fields are gone
    old = ExceptionPatternSettings(**{"type_weights": {"SPEEDING": 1}, "high_risk_score": 60,
                                      "saturation_points": 40, "zone_min_drivers": 2, "zone_min_events": 7})
    dumped = old.model_dump()
    assert dumped["zone_min_events"] == 7
    assert not {"type_weights", "high_risk_score", "saturation_points", "zone_min_drivers"} & set(dumped)


def test_type_summaries_speed_and_peak_hour():
    rows = [ev(speed=30.0, limit=25.0, hour=7, i=1), ev(speed=35.0, limit=25.0, hour=7, i=2),
            ev("HARSH_CORNERING", hour=18, i=3)]
    s = {x["event_type"]: x for x in A.type_summaries(rows, CFG)}
    sp = s["SPEEDING"]
    assert sp["count"] == 2 and sp["avg_speed_kmh"] == 117.0 and sp["avg_limit_kmh"] == 90.0
    assert sp["avg_excess_pct"] == 30.0 and sp["by_severity"]["high"] == 1
    assert sp["peak_hour"] == 8          # 07:00 UTC = 08:00 Africa/Douala
    assert s["HARD_ACCELERATION"]["count"] == 0 and s["ACCIDENT"]["count"] == 0
    assert s["HARSH_CORNERING"]["share"] == pytest.approx(1 / 3, abs=1e-3)


def test_breakdowns_by_provider_country_severity_and_bands():
    rows = [ev(speed=13.0, limit=12.5, i=1), ev(speed=30.0, limit=25.0, i=2, source="besafe"),
            ev(speed=35.0, limit=25.0, i=3, country=None), ev("HARD_BRAKING", severity="3", i=4)]
    b = A.breakdowns(rows, CFG)
    assert [(r["key"], r["count"]) for r in b["by_source"]] == [("ymane", 3), ("besafe", 1)]
    assert [(r["key"], r["count"]) for r in b["by_country"]] == [("CAMEROUN", 3), ("", 1)]
    assert {r["key"]: r["count"] for r in b["by_severity"]} == {"low": 1, "medium": 1, "high": 2, "unknown": 0}
    bands = {r["key"]: r["count"] for r in b["by_speed_band"]}
    assert bands["30-50"] == 1 and bands["90-110"] == 1 and bands["110+"] == 1 and sum(bands.values()) == 3
    assert {r["key"]: r["count"] for r in b["by_excess_band"]} == {
        "0-10": 1, "10-20": 0, "20-30": 1, "30-50": 1, "50+": 0}
    assert b["by_source"][0]["by_type"]["HARD_BRAKING"] == 1 and b["by_source"][0]["share"] == 0.75


def test_overview_has_no_identity():
    o = A.overview([ev(i=1), ev("HARD_BRAKING", severity="high", i=2, lat=DOUALA[0] + 0.01)], [ev(i=3)], CFG)
    assert o["total_events"] == 2 and o["change_pct"] == 100.0 and o["places"] == 2 and o["sources"] == 1
    assert o["high_severity_events"] == 1 and o["high_severity_share"] == 0.5
    assert not [k for k in o if "driver" in k or "vehicle" in k]


def test_trends_buckets_hours_weekdays():
    rows = [ev(days_ago=d, i=d) for d in (1, 1, 3)] + [ev("HARD_BRAKING", days_ago=2, severity="high")]
    win = A.resolve_window("7d", None, None, NOW)
    t = A.trends(rows, win.start, win.end, CFG)
    assert t["unit"] == "day" and len(t["series"]) == 8
    assert sum(r["total"] for r in t["series"]) == 4 and sum(r["high_severity"] for r in t["series"]) == 1
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
    hot = [ev("HARD_BRAKING", severity="high", i=i) for i in range(6)] + [ev("SPEEDING", i=99)]
    far = [ev(lat=DOUALA[0] + 0.05, i=i) for i in range(2)]
    cells = A.heatmap(hot + far, 250, CFG)
    assert cells[0]["count"] == 7 and cells[0]["by_type"]["HARD_BRAKING"] == 6 and cells[0]["high_severity"] == 6
    zones = A.zones(hot + far, CFG)
    assert len(zones) == 1                                   # the 2-event cell is below zone_min_events
    z = zones[0]
    assert z["dominant_type"] == "HARD_BRAKING" and z["multi_type"] and z["rank"] == 1
    assert z["high_severity"] == 6 and z["high_severity_share"] == round(6 / 7, 3)


# ── endpoints (SQLite) ───────────────────────────────────────────────────────

@pytest.fixture
async def client():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.core import settings_service
    from app.core.db.session import Base, get_db
    from app.modules.m08_historical.models.historical_event import HistoricalEventRecord as E
    from app.modules.m09_exception_patterns.routes.exception_patterns import router
    from app.modules.m13_admin.models.system_setting import SystemSetting

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[E.__table__, SystemSetting.__table__]))
    Session = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.utcnow()
    async with Session() as s:
        n = 0
        def add(t, days, speed=None, limit=None, severity=None, lat=DOUALA[0], source="pytest"):
            nonlocal n
            n += 1
            s.add(E(source=source, source_event_id=f"x{n}", event_type=t, occurred_at=now - timedelta(days=days),
                    latitude=lat, longitude=DOUALA[1], speed_mps=speed, speed_limit_mps=limit, severity=severity,
                    grid_cell_id="c", road_name="N3", raw={}))
        for _ in range(6):
            add("HARD_BRAKING", 2, severity="high")
        add("SPEEDING", 3, speed=35.0, limit=25.0)
        add("SPEEDING", 40, severity="high")                      # previous 30-day window
        add("ACCIDENT", 1)
        add("HARSH_CORNERING", 5, lat=DOUALA[0] + 0.1, source="besafe")
        await s.commit()
    settings_service.clear_cache()

    async def _db():
        async with Session() as s:
            yield s

    app = FastAPI()
    app.include_router(router, prefix="/exception-patterns")
    app.dependency_overrides[get_db] = _db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c
    settings_service.clear_cache()
    await engine.dispose()


def _anonymous(text: str) -> bool:
    low = text.lower()
    return "driver" not in low and "vehicle" not in low and "plate" not in low


async def test_overview_endpoint(client):
    r = await client.get("/exception-patterns/overview", params={"period": "30d"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["total_events"] == 9 and d["previous_total_events"] == 1 and d["change_pct"] == 800.0
    by = {x["event_type"]: x for x in d["by_type"]}
    assert by["HARD_BRAKING"]["count"] == 6 and by["SPEEDING"]["avg_excess_pct"] == 40.0
    assert by["ACCIDENT"]["count"] == 1 and d["high_severity_events"] == 7
    assert d["places"] == 2 and d["sources"] == 2 and d["countries"] == 0
    assert _anonymous(r.text)


async def test_filters(client):
    async def total(**params):
        r = await client.get("/exception-patterns/overview", params={"period": "30d", **params})
        assert r.status_code == 200, r.text
        return r.json()["total_events"]
    assert await total(types="SPEEDING,HARD_BRAKING") == 7
    assert await total(source="besafe") == 1
    assert await total(severity="high") == 7
    assert await total(severity="unknown") == 2
    assert await total(bbox="4.1,9.7,4.2,9.8") == 1
    for bad in ({"severity": "extreme"}, {"bbox": "x"}, {"period": "5d"}):
        assert (await client.get("/exception-patterns/overview", params=bad)).status_code == 422


async def test_breakdowns_trends_heatmap_zones(client):
    r = await client.get("/exception-patterns/breakdowns", params={"period": "30d"})
    assert r.status_code == 200, r.text
    b = r.json()
    assert [(x["key"], x["count"]) for x in b["by_source"]] == [("pytest", 8), ("besafe", 1)]
    assert {x["key"]: x["count"] for x in b["by_severity"]} == {"low": 0, "medium": 0, "high": 7, "unknown": 2}
    assert {x["key"]: x["count"] for x in b["by_speed_band"]}["110+"] == 1          # 126 km/h
    assert {x["key"]: x["count"] for x in b["by_excess_band"]}["30-50"] == 1        # +40 %
    assert _anonymous(r.text)
    t = (await client.get("/exception-patterns/trends", params={"period": "7d"})).json()
    assert t["unit"] == "day" and sum(s["total"] for s in t["series"]) == 9
    assert sum(s["high_severity"] for s in t["series"]) == 7
    h = (await client.get("/exception-patterns/heatmap", params={"period": "30d", "types": "HARD_BRAKING"})).json()
    assert h["max_count"] == 6 and len(h["cells"]) == 1
    r = await client.get("/exception-patterns/zones", params={"period": "30d"})
    z = r.json()
    assert z["total"] == 1 and z["items"][0]["dominant_type"] == "HARD_BRAKING" and z["items"][0]["events"] == 8
    assert z["items"][0]["multi_type"] and _anonymous(r.text)


async def test_lists_and_methodology(client):
    assert (await client.get("/exception-patterns/sources")).json() == [
        {"source": "pytest", "events": 9}, {"source": "besafe", "events": 1}]
    assert (await client.get("/exception-patterns/countries")).json() == []
    m = (await client.get("/exception-patterns/methodology")).json()
    assert m["version"] == "exp-1" and m["speed_bands_kmh"][-1] == "110+"
    assert "high_risk_score" not in m["settings"] and "type_weights" not in m["settings"]
    # the former identity endpoints are gone
    for path in ("/exception-patterns/drivers", "/exception-patterns/vehicles", "/exception-patterns/events"):
        assert (await client.get(path)).status_code == 404
