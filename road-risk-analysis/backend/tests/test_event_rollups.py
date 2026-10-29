"""
Daily summaries of the driving events: every Exception patterns / Historical answer is the same
whether it is read from the summaries or from the raw events (SQLite here, PostgreSQL in
production — the same portable SQL), a day rebuild keeps them right after an import, and the
country / source / severity filters apply to both. The summaries carry no driver or vehicle.
"""
import os
from datetime import datetime, timedelta

import pytest

from app.modules.m08_historical import aggregates as G
from app.modules.m08_historical import historical_service, rollups
from app.modules.m09_exception_patterns import views
from app.modules.m09_exception_patterns.settings import EXCEPTION_TYPES, ExceptionPatternSettings

DOUALA = (4.0511, 9.7679)
TANA = (-18.9137, 47.5361)
NOW = datetime.utcnow().replace(microsecond=0)
START = (NOW - timedelta(days=30)).replace(hour=0, minute=0, second=0)


@pytest.fixture
async def db():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.core.db.session import Base
    from app.modules.m08_historical.models import event_rollup as M
    from app.modules.m08_historical.models.historical_event import HistoricalEventRecord as E

    # VIASAFE_TEST_PG_URL=postgresql+asyncpg://… runs the same checks on a throwaway PostgreSQL.
    engine = create_async_engine(os.getenv("VIASAFE_TEST_PG_URL", "sqlite+aiosqlite:///:memory:"))
    tables = [E.__table__, M.EventDaily.__table__, M.EventDailyHour.__table__, M.EventDailyCell.__table__,
              M.EventRollupState.__table__]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.drop_all(c, tables=tables))
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        n = 0

        def add(t, days, hours, pos=DOUALA, country="CAMEROUN", speed=None, limit=None,
                severity=None, road="N3", source="ymane"):
            nonlocal n
            n += 1
            s.add(E(source=source, source_event_id=f"e{n}", event_type=t,
                    occurred_at=NOW - timedelta(days=days, hours=hours), latitude=pos[0] + n * 1e-5,
                    longitude=pos[1], speed_mps=speed, speed_limit_mps=limit, severity=severity, grid_cell_id="x",
                    road_name=road, country=country, transporter="T1", raw={}))
        for i in range(8):
            add("HARD_BRAKING", 2, i, severity="3")
        for i in range(5):
            add("SPEEDING", 3, i, speed=25.0 + i, limit=20.0)                    # 90 … 104 km/h, +25 … +45 %
        add("SPEEDING", 40, 0, severity="1")                                      # previous period
        add("HARSH_CORNERING", 5, 1, pos=TANA, country="MADAGASIKARA", road=None, source="besafe")
        add("HARD_ACCELERATION", 6, 2, pos=TANA, country="MADAGASIKARA", severity="2", source="besafe")
        add("ACCIDENT", 1, 0)
        await s.commit()
        yield s
    await engine.dispose()


async def answers(db, cfg, use: bool, countries=None, sources=None, severities=None) -> dict:
    s = G.Scope(START, NOW + timedelta(seconds=1), list(EXCEPTION_TYPES), countries, sources, severities,
                rollups=use, cfg=cfg)
    prev = s.shifted(START - timedelta(days=30), START)
    every = G.Scope(None, NOW + timedelta(seconds=1), None, countries, sources, severities, rollups=use, cfg=cfg)
    days = await G.entity_rows(db, cfg, every, ("day", "event_type"))
    return {
        "overview": await views.overview(db, cfg, s, prev, EXCEPTION_TYPES),
        "breakdowns": await views.breakdowns(db, cfg, s, EXCEPTION_TYPES),
        "trends": await views.trends(db, cfg, s, EXCEPTION_TYPES, "day"),
        "heatmap": await views.heatmap(db, cfg, s, 250.0),
        "zones": await views.zones(db, cfg, s, 50),
        "cells": [p.model_dump() for p in historical_service.profiles_from_squares(
            await G.cell_rows(db, cfg, every, ("k_lat", "k_lon", "event_type", "road_name")), 150.0)],
        "days": sorted((str(G.as_date(r.day)), r.event_type, int(r.n)) for r in days),
        "squares": await G.squares_per_bucket(db, cfg, every, "week"),
        "countries": await G.countries(db, use),
        "sources": await G.sources(db, use),
    }


async def test_summaries_give_the_same_answers_as_the_raw_events(db):
    cfg = ExceptionPatternSettings(zone_min_events=2)
    assert not await rollups.ready(db, cfg)
    raw = await answers(db, cfg, False)
    await rollups.rebuild(db, cfg)
    assert await rollups.ready(db, cfg)
    assert await answers(db, cfg, True) == raw

    o = raw["overview"]
    assert o["total_events"] == 16 and o["previous_total_events"] == 1
    assert o["sources"] == 2 and o["countries"] == 2 and o["high_severity_events"] == 8 + 5     # speeding ≥ +25 %: high
    assert not any("driver" in k or "vehicle" in k for k in o)
    braking = next(t for t in o["by_type"] if t["event_type"] == "HARD_BRAKING")
    assert braking["by_severity"]["high"] == 8 and braking["top_roads"] == [{"road_name": "N3", "count": 8}]
    speeding = next(t for t in o["by_type"] if t["event_type"] == "SPEEDING")
    assert speeding["avg_excess_pct"] == 35.0 and speeding["max_speed_kmh"] == round(29 * 3.6, 1)
    b = raw["breakdowns"]
    assert [(r["key"], r["count"]) for r in b["by_source"]] == [("ymane", 14), ("besafe", 2)]
    assert {r["key"]: r["count"] for r in b["by_speed_band"]} == {
        "0-30": 0, "30-50": 0, "50-70": 0, "70-90": 0, "90-110": 5, "110+": 0}
    assert {r["key"]: r["count"] for r in b["by_excess_band"]} == {
        "0-10": 0, "10-20": 0, "20-30": 1, "30-50": 4, "50+": 0}
    assert [r["count"] for r in b["by_severity"]] == [0, 1, 13, 2]                  # low medium high unknown
    assert raw["countries"] == [("CAMEROUN", 15), ("MADAGASIKARA", 2)]
    assert raw["sources"] == [("ymane", 15), ("besafe", 2)]
    assert sum(r["total"] for r in raw["trends"]["by_hour"]) == 16


async def test_country_source_severity_filters_and_new_severity_rules(db):
    cfg = ExceptionPatternSettings()
    await rollups.rebuild(db, cfg)
    mg = await answers(db, cfg, True, ["MADAGASIKARA"])
    assert mg["overview"]["total_events"] == 2 and mg == await answers(db, cfg, False, ["MADAGASIKARA"])
    bs = await answers(db, cfg, True, sources=["besafe"])
    assert bs["overview"]["total_events"] == 2 and bs == await answers(db, cfg, False, sources=["besafe"])
    high = await answers(db, cfg, True, severities=["high"])
    assert high["overview"]["total_events"] == 13 and high == await answers(db, cfg, False, severities=["high"])
    stricter = ExceptionPatternSettings(speeding_high_excess_pct=40.0)
    assert not await rollups.ready(db, stricter)                    # summaries of the old rules: not used


async def test_day_rebuild_after_an_import(db):
    from app.modules.m08_historical.models.historical_event import HistoricalEventRecord as E
    cfg = ExceptionPatternSettings(zone_min_events=2)
    await rollups.rebuild(db, cfg)
    when = NOW - timedelta(days=2, hours=3)
    db.add(E(source="ymane", source_event_id="late", event_type="HARD_BRAKING", occurred_at=when,
             latitude=DOUALA[0], longitude=DOUALA[1], severity="3", grid_cell_id="x", country="CAMEROUN", raw={}))
    await db.commit()
    assert await rollups.rebuild(db, cfg, {when.date()}) == 1
    assert await answers(db, cfg, True) == await answers(db, cfg, False)
    assert (await answers(db, cfg, True))["overview"]["total_events"] == 17


async def test_summaries_of_purged_days_survive_a_full_rebuild(db):
    from sqlalchemy import delete, func, select
    from app.modules.m08_historical.models.event_rollup import EventDaily
    from app.modules.m08_historical.models.historical_event import HistoricalEventRecord as E
    cfg = ExceptionPatternSettings()
    await rollups.rebuild(db, cfg)
    before = (await db.execute(select(func.sum(EventDaily.n)))).scalar()
    await db.execute(delete(E).where(E.occurred_at < NOW - timedelta(days=30)))      # retention purge
    await db.commit()
    await rollups.rebuild(db, cfg)
    assert (await db.execute(select(func.sum(EventDaily.n)))).scalar() == before      # day −40 kept


def test_summaries_have_no_driver_or_vehicle_dimension():
    from app.modules.m08_historical.models import event_rollup as M
    for t in (M.EventDaily, M.EventDailyHour, M.EventDailyCell):
        assert not [c for c in t.__table__.columns if "driver" in c.name or "vehicle" in c.name]
    assert rollups.SPEED_BANDS == ["0-30", "30-50", "50-70", "70-90", "90-110", "110+"]
    assert rollups.band_of(None, rollups.SPEED_BANDS_KMH) == ""
    assert rollups.band_of(49.9, rollups.SPEED_BANDS_KMH) == "30-50"
    assert rollups.band_of(-5.0, rollups.EXCESS_BANDS_PCT) == "0-10"
    assert rollups.band_of(75.0, rollups.EXCESS_BANDS_PCT) == "50+"
