"""
Stored place names of positions (reverse geocoding once per ~110 m square): lookup of named
squares, queuing of the others, the lookup run (queued first, then busiest event squares, daily
budget, failures retried after a week).
"""
from datetime import date, datetime, timedelta

import pytest

from app.modules.m03_maps import geocoding, place_names as P


class FakeRedis:
    def __init__(self):
        self.sets, self.kv = {}, {}

    def sadd(self, k, *v):
        self.sets.setdefault(k, set()).update(v)

    def spop(self, k):
        s = self.sets.get(k)
        return s.pop() if s else None

    def get(self, k):
        return self.kv.get(k)

    def incrby(self, k, n):
        self.kv[k] = int(self.kv.get(k) or 0) + n

    def expire(self, k, ttl):
        pass

    def incr(self, k):
        self.incrby(k, 1)


@pytest.fixture
async def db(monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.core.db.session import Base
    from app.modules.m08_historical.models.event_rollup import EventDailyCell
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(lambda c: Base.metadata.create_all(c, tables=[P.PlaceName.__table__,
                                                                      EventDailyCell.__table__]))
    r = FakeRedis()
    monkeypatch.setattr(P, "_redis", lambda: r)
    monkeypatch.setattr("app.modules.m07_ingestion.job_slots.client", lambda: r)
    monkeypatch.setattr(P.settings, "GEOCODING_ENABLED", True)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        s.redis = r
        yield s
    await engine.dispose()


async def test_named_squares_are_returned_and_the_others_queued(db):
    db.add(P.PlaceName(k_lat=4051, k_lon=9768, display="Rue Joss, Akwa, Douala", ok=True))
    db.add(P.PlaceName(k_lat=1, k_lon=2, display=None, ok=False))
    await db.commit()
    rows = [{"latitude": 4.05112, "longitude": 9.76791}, {"latitude": -18.9137, "longitude": 47.5361},
            {"latitude": 0.0011, "longitude": 0.0019}]
    await P.attach(db, rows)
    assert [r["place"] for r in rows] == ["Rue Joss, Akwa, Douala", None, None]
    assert db.redis.sets[P.WANTED_KEY] == {"-18914:47536"}          # the failed one is not queued again


async def test_fill_queued_first_then_busiest_within_budget(db, monkeypatch):
    from app.modules.m08_historical.models.event_rollup import EventDailyCell
    calls = []

    def geo(lat, lon):
        calls.append((round(lat, 3), round(lon, 3)))
        return {"display": f"P{len(calls)}", "road": None, "town": "T", "region": None, "country": "C"}

    monkeypatch.setattr(geocoding, "reverse_geocode_details", geo)
    for i, n in enumerate((5, 50, 20)):                  # three event squares, busiest = second
        db.add(EventDailyCell(day=date(2026, 10, 27), country="CAMEROUN", k_lat=4000 + i, k_lon=9000,
                              event_type="SPEEDING", severity="low", road_name="", n=n,
                              lat_sum=(4.0 + i / 1000) * n, lon_sum=9.0 * n,
                              first_at=datetime(2026, 10, 27), last_at=datetime(2026, 10, 27)))
    await db.commit()
    db.redis.sadd(P.WANTED_KEY, "-18914:47536")
    monkeypatch.setattr(P.settings, "GEOCODING_DAILY_BUDGET", 3)
    out = await P.fill(db, limit=10)
    assert out["looked_up"] == 3 and calls[0] == (-18.914, 47.536) and calls[1] == (4.001, 9.0)
    assert (await P.fill(db, limit=10))["looked_up"] == 0             # budget of the day used up
    assert (await db.get(P.PlaceName, (4001, 9000))).display == "P2"


async def test_failed_lookups_are_retried_after_a_week(db, monkeypatch):
    monkeypatch.setattr(geocoding, "reverse_geocode_details", lambda la, lo: {"display": "Later"})
    db.add(P.PlaceName(k_lat=10, k_lon=20, ok=False, looked_up_at=datetime.utcnow() - timedelta(days=1)))
    db.add(P.PlaceName(k_lat=11, k_lon=21, ok=False, looked_up_at=datetime.utcnow() - timedelta(days=8)))
    await db.commit()
    db.redis.sadd(P.WANTED_KEY, "10:20", "11:21")
    assert (await P.fill(db))["looked_up"] == 1
    assert (await db.get(P.PlaceName, (11, 21))).display == "Later"
