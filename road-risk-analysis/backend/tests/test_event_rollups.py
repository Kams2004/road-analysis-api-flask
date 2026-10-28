"""
Daily summaries of the driving events: every Driver behaviour / Historical answer is the same
whether it is read from the summaries or from the raw events (SQLite here, PostgreSQL in
production — the same portable SQL), a day rebuild keeps them right after an import, and the
country filter applies to both.
"""
import os
from datetime import datetime, timedelta

import pytest

from app.modules.m08_historical import aggregates as G
from app.modules.m08_historical import historical_service, rollups
from app.modules.m09_driver_behaviour import views
from app.modules.m09_driver_behaviour.settings import BEHAVIOUR_TYPES, DriverBehaviourSettings

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

        def add(t, days, hours, driver, vehicle, pos=DOUALA, country="CAMEROUN", speed=None, limit=None,
                severity=None, road="N3"):
            nonlocal n
            n += 1
            s.add(E(source="ymane", source_event_id=f"e{n}", event_type=t,
                    occurred_at=NOW - timedelta(days=days, hours=hours), latitude=pos[0] + n * 1e-5,
                    longitude=pos[1], driver_external_id=driver, vehicle_external_id=vehicle, speed_mps=speed,
                    speed_limit_mps=limit, severity=severity, grid_cell_id="x", road_name=road, country=country,
                    transporter="T1", raw={}))
        for i in range(8):
            add("HARD_BRAKING", 2, i, "D1", "V1", severity="3")
        for i in range(5):
            add("SPEEDING", 3, i, "D2", "V2", speed=25.0 + i, limit=20.0)
        add("SPEEDING", 40, 0, "D2", "V2", severity="1")                          # previous period
        add("HARSH_CORNERING", 5, 1, None, "V3", pos=TANA, country="MADAGASIKARA", road=None)
        add("HARD_ACCELERATION", 6, 2, "D3", None, pos=TANA, country="MADAGASIKARA", severity="2")
        add("ACCIDENT", 1, 0, "D1", "V1")                                         # Module 4 only
        await s.commit()
        yield s
    await engine.dispose()


async def answers(db, cfg, use: bool, countries=None) -> dict:
    s = G.Scope(START, NOW + timedelta(seconds=1), list(BEHAVIOUR_TYPES), countries, rollups=use)
    prev = s.shifted(START - timedelta(days=30), START)
    every = G.Scope(None, NOW + timedelta(seconds=1), None, countries, rollups=use)
    rank, unidentified = await views.ranking(db, cfg, s, prev, "driver")
    days = await G.entity_rows(db, cfg, every, ("day", "event_type"))
    return {
        "overview": await views.overview(db, cfg, s, prev, BEHAVIOUR_TYPES),
        "ranking": (rank, unidentified),
        "vehicles": await views.ranking(db, cfg, s, prev, "vehicle"),
        "trends": await views.trends(db, cfg, s, BEHAVIOUR_TYPES, "day"),
        "heatmap": await views.heatmap(db, cfg, s, 250.0),
        "zones": await views.zones(db, cfg, s, 50),
        "cells": [p.model_dump() for p in historical_service.profiles_from_squares(
            await G.cell_rows(db, cfg, every, ("k_lat", "k_lon", "event_type", "road_name")), 150.0)],
        "days": sorted((str(G.as_date(r.day)), r.event_type, int(r.n)) for r in days),
        "squares": await G.squares_per_bucket(db, cfg, every, "week"),
        "countries": await G.countries(db, use),
    }


async def test_summaries_give_the_same_answers_as_the_raw_events(db):
    cfg = DriverBehaviourSettings(zone_min_events=2)
    assert not await rollups.ready(db, cfg)
    raw = await answers(db, cfg, False)
    await rollups.rebuild(db, cfg)
    assert await rollups.ready(db, cfg)
    assert await answers(db, cfg, True) == raw

    o = raw["overview"]
    assert o["total_events"] == 15 and o["previous_total_events"] == 1 and o["drivers"] == 3
    braking = next(t for t in o["by_type"] if t["event_type"] == "HARD_BRAKING")
    assert braking["by_severity"]["high"] == 8 and braking["top_roads"] == [{"road_name": "N3", "count": 8}]
    speeding = next(t for t in o["by_type"] if t["event_type"] == "SPEEDING")
    assert speeding["avg_excess_pct"] == 35.0 and speeding["max_speed_kmh"] == round(29 * 3.6, 1)
    assert raw["ranking"][0][0]["id"] == "D1" and raw["ranking"][1] == 1           # V3's cornering has no driver
    assert raw["countries"] == [("CAMEROUN", 15), ("MADAGASIKARA", 2)]
    assert sum(r["total"] for r in raw["trends"]["by_hour"]) == 15


async def test_country_filter_and_new_severity_rules(db):
    cfg = DriverBehaviourSettings()
    await rollups.rebuild(db, cfg)
    mg = await answers(db, cfg, True, ["MADAGASIKARA"])
    assert mg["overview"]["total_events"] == 2 and mg == await answers(db, cfg, False, ["MADAGASIKARA"])
    stricter = DriverBehaviourSettings(speeding_high_excess_pct=40.0)
    assert not await rollups.ready(db, stricter)                    # summaries of the old rules: not used


async def test_day_rebuild_after_an_import(db):
    from app.modules.m08_historical.models.historical_event import HistoricalEventRecord as E
    cfg = DriverBehaviourSettings(zone_min_events=2)
    await rollups.rebuild(db, cfg)
    when = NOW - timedelta(days=2, hours=3)
    db.add(E(source="ymane", source_event_id="late", event_type="HARD_BRAKING", occurred_at=when,
             latitude=DOUALA[0], longitude=DOUALA[1], driver_external_id="D9", vehicle_external_id="V9",
             severity="3", grid_cell_id="x", country="CAMEROUN", raw={}))
    await db.commit()
    assert await rollups.rebuild(db, cfg, {when.date()}) == 1
    assert await answers(db, cfg, True) == await answers(db, cfg, False)
    assert (await answers(db, cfg, True))["overview"]["total_events"] == 16


async def test_summaries_of_purged_days_survive_a_full_rebuild(db):
    from sqlalchemy import delete, func, select
    from app.modules.m08_historical.models.event_rollup import EventDaily
    from app.modules.m08_historical.models.historical_event import HistoricalEventRecord as E
    cfg = DriverBehaviourSettings()
    await rollups.rebuild(db, cfg)
    before = (await db.execute(select(func.sum(EventDaily.n)))).scalar()
    await db.execute(delete(E).where(E.occurred_at < NOW - timedelta(days=30)))      # retention purge
    await db.commit()
    await rollups.rebuild(db, cfg)
    assert (await db.execute(select(func.sum(EventDaily.n)))).scalar() == before      # day −40 kept
