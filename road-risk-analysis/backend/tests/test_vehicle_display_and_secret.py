"""
Live vehicles: the per-device secret (device_auth) and where vehicles are drawn (display.py) —
held still while stopped despite GPS jitter, snapped onto the road they drive on, never forced
onto a road they are not on.
"""
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI

from app.api.deps import Principal, get_principal
from app.core.db.session import get_db
from app.modules.m03_maps.osrm import geo
from app.modules.m10_collision import display, vehicle_service
from app.modules.m10_collision.routes import vehicles as vehicle_routes
from tests.test_collision_pipeline import KAMBO, ew_road, world  # noqa: F401  (fixture)


def east(d, origin=KAMBO):
    return geo.destination(*origin, 90.0, d)


def north(d, origin=KAMBO):
    return geo.destination(*origin, 0.0, d)


@pytest.fixture
def shown(world, monkeypatch):
    monkeypatch.setattr(display, "get_redis", lambda: world.redis.aio)

    async def ping(vid, point, speed, heading, accuracy=5.0):
        previous = await vehicle_service.get_live(vid)
        await world.ping(vid, point, speed, heading, accuracy=accuracy)
        out = await display.update(vid, await vehicle_service.get_live(vid), previous)
        await world.chain(vid)              # the pipeline after the ping (road state for the next one)
        return out
    return ping


# ── display position ─────────────────────────────────────────────────────────

async def test_a_stopped_vehicle_does_not_move_with_gps_jitter(world, shown):
    world.use(ew_road())
    first = await shown("A", KAMBO, 0.0, 90.0, accuracy=10.0)
    for brg, d in ((30, 6.0), (200, 7.5), (120, 4.0), (300, 9.0)):
        out = await shown("A", geo.destination(*KAMBO, brg, d), 0.3, (brg + 77) % 360, accuracy=10.0)
        assert (out["latitude"], out["longitude"]) == (first["latitude"], first["longitude"])
        assert out["stationary"]


async def test_indoor_gps_with_fake_speeds_does_not_move_the_point(world, shown):
    # Indoors: ±60–120 m fixes reporting 3–4 m/s that are pure noise (seen on a real phone).
    world.use(ew_road())
    first = await shown("A", north(30), 0.3, 0.0, accuracy=60.0)
    for brg, d, v, acc in ((40, 55.0, 3.2, 105.0), (250, 35.0, 4.4, 121.0), (130, 50.0, 4.0, 110.0)):
        out = await shown("A", geo.destination(*north(30), brg, d), v, (brg + 90) % 360, accuracy=acc)
        assert (out["latitude"], out["longitude"]) == (first["latitude"], first["longitude"])
        assert not out["snapped"] and out["stationary"]


async def test_a_held_point_is_replaced_by_a_much_better_fix(world, shown):
    world.use(ew_road())
    await shown("A", north(40), 0.0, 0.0, accuracy=60.0)
    out = await shown("A", north(30), 0.0, 0.0, accuracy=15.0)
    assert geo.haversine_m(out["latitude"], out["longitude"], *north(30)) < 0.5


async def test_a_stopped_vehicle_moves_when_it_is_really_elsewhere(world, shown):
    world.use(ew_road())
    first = await shown("A", KAMBO, 0.0, 90.0, accuracy=5.0)
    out = await shown("A", east(30), 0.2, 90.0, accuracy=5.0)
    assert geo.haversine_m(first["latitude"], first["longitude"], out["latitude"], out["longitude"]) > 20


async def test_a_moving_vehicle_is_snapped_onto_its_road(world, shown):
    world.use(ew_road())
    await shown("A", north(6, east(-40)), 12.0, 90.0, accuracy=8.0)
    out = await shown("A", north(7, east(-28)), 12.0, 92.0, accuracy=8.0)        # GPS 7 m beside the road
    assert out["snapped"] and abs(out["latitude"] - KAMBO[0]) < 1e-6
    assert out["road"] and len(out["road"]) >= 2


async def test_a_vehicle_off_the_road_is_not_forced_onto_it(world, shown):
    world.use(ew_road())
    await shown("A", north(45, east(-40)), 4.0, 90.0, accuracy=5.0)
    out = await shown("A", north(45, east(-36)), 4.0, 90.0, accuracy=5.0)         # car park 45 m away
    assert not out["snapped"] and geo.haversine_m(out["latitude"], out["longitude"], *north(45, east(-36))) < 0.5


async def test_a_vehicle_crossing_the_road_is_not_snapped_along_it(world, shown):
    world.use(ew_road())
    await shown("A", north(10, east(-30)), 5.0, 180.0, accuracy=5.0)
    out = await shown("A", north(6, east(-30)), 5.0, 180.0, accuracy=5.0)          # heading across the road
    assert not out["snapped"]


# ── device secret ────────────────────────────────────────────────────────────

@pytest.fixture
async def api(world, monkeypatch, tmp_path):
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.core.db.session import Base
    from app.modules.m10_collision.models.vehicle import Vehicle

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[Vehicle.__table__]))
    Session = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(display, "get_redis", lambda: world.redis.aio)

    async def no_pipeline(vehicle_id, db):
        pass
    monkeypatch.setattr(vehicle_routes, "_update_pipeline", no_pipeline)
    world.use(ew_road())

    app = FastAPI()
    app.include_router(vehicle_routes.router, prefix="/vehicles")
    async with Session() as s:
        async def _db():
            yield s
        app.dependency_overrides[get_db] = _db
        app.dependency_overrides[get_principal] = lambda: Principal(kind="client", id="c1", label="mobile")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            yield client
    await engine.dispose()


PING = {"latitude": KAMBO[0], "longitude": KAMBO[1], "speed_mps": 0.0, "heading": 90.0, "accuracy_m": 5.0}


async def test_the_device_secret_is_given_once_and_required_after(api):
    r = await api.post("/vehicles/register", json={"vehicle_id": "phone-1", "vehicle_type": "private"})
    secret = r.json()["device_secret"]
    assert r.status_code == 200 and len(secret) >= 32

    ok = await api.post("/vehicles/phone-1/ping", json=PING, headers={"X-Device-Secret": secret})
    assert ok.status_code == 200, ok.text
    assert ok.json()["stationary"] and ok.json()["alert_level"] is None

    for headers in ({}, {"X-Device-Secret": "guess"}):
        assert (await api.post("/vehicles/phone-1/ping", json=PING, headers=headers)).status_code == 403
        again = await api.post("/vehicles/register", json={"vehicle_id": "phone-1", "vehicle_type": "taxi"},
                               headers=headers)
        assert again.status_code == 403 and again.json()["detail"]["code"] == "device_secret_invalid"

    mine = await api.post("/vehicles/register", json={"vehicle_id": "phone-1", "vehicle_type": "taxi"},
                          headers={"X-Device-Secret": secret})
    assert mine.status_code == 200 and mine.json()["device_secret"] is None     # never answered twice


async def test_a_vehicle_registered_before_secrets_is_claimed_once(api):
    from app.modules.m10_collision.models.vehicle import Vehicle, VehicleType
    db = api._transport.app.dependency_overrides[get_db]
    async for s in db():
        s.add(Vehicle(id="old-phone", vehicle_type=VehicleType.private, created_at=datetime.now(timezone.utc)))
        await s.commit()
    assert (await api.post("/vehicles/old-phone/ping", json=PING)).status_code == 200   # not claimed yet
    claim = await api.post("/vehicles/register", json={"vehicle_id": "old-phone", "vehicle_type": "private"})
    assert claim.json()["device_secret"]
    assert (await api.post("/vehicles/old-phone/ping", json=PING)).status_code == 403
