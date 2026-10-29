"""
Collision prevention — the whole ping → alert chain with fakes: an in-memory Redis
(tests/fake_redis.py) and a fake map matcher standing in for OSRM (snaps a fix onto the nearest
of a few real-looking road polylines around Kambo, Douala). Each scenario sends real pings
(vehicle_service.record_ping) and runs the background chain (road state → trajectory →
collision → alerts) for both vehicles.

Covered: head-on (missing accuracy: same lane alerts, keeping right is passing), the queue at a
red light, a parallel road, a divided carriageway, a crossing at an intersection, the context
raising WARNING → HIGH_WARNING in a risky cell but never creating an alert, the pair evaluated
once per pair of fixes, transitions published for the WebSocket, metrics and feedback.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.modules.m03_maps.osrm import geo
from app.modules.m03_maps.osrm.models import (
    MapMatchQuality, RoadAwareVehicleState, RoadContext, RoadDirection, RoadGeometry, stable_segment_id,
)
from app.modules.m08_historical.grid import grid_cell_id
from app.modules.m10_collision import (
    alert_service, collision_service, context_service, metrics_service, road_state_service, staleness_guard,
    trajectory_service, vehicle_service,
)
from app.modules.m10_collision.alerts.models import AlertLevel, AlertTransition
from app.modules.m10_collision.collision import context as ctx
from app.modules.m10_collision.collision import detector, thresholds
from app.modules.m10_collision.collision.context_settings import ContextSettings
from app.modules.m10_collision.models.vehicle import VehicleType
from app.modules.m10_collision.routes import alerts as alert_routes
from tests.fake_redis import FakeRedis

KAMBO = (4.0412, 9.8107)
REDIS_MODULES = (vehicle_service, road_state_service, trajectory_service, collision_service, alert_service,
                 staleness_guard, context_service, alert_routes)


# ── fakes ────────────────────────────────────────────────────────────────────

class Road:
    def __init__(self, name, coords, oneway=False):
        self.name, self.coords, self.oneway = name, coords, oneway


def line(start, bearing, length_m=1000.0, step_m=100.0, back_m=1000.0):
    """A straight road through `start` along `bearing`, sampled every step_m."""
    first = geo.destination(*start, (bearing + 180) % 360, back_m)
    n = int((back_m + length_m) / step_m)
    return [geo.destination(*first, bearing, i * step_m) for i in range(n + 1)]


def _project(coords, p):
    """(distance to the polyline, distance along it, matched point, bearing there)."""
    ref = p[0]
    px, py = geo.local_xy_m(*p, ref)
    best, along = None, 0.0
    for a, b in zip(coords, coords[1:]):
        ax, ay = geo.local_xy_m(*a, ref)
        bx, by = geo.local_xy_m(*b, ref)
        dx, dy = bx - ax, by - ay
        l2 = dx * dx + dy * dy
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2)) if l2 else 0.0
        mx, my = ax + t * dx, ay + t * dy
        d = ((px - mx) ** 2 + (py - my) ** 2) ** 0.5
        seg = l2 ** 0.5
        if best is None or d < best[0]:
            point = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
            best = (d, along + t * seg, point, geo.bearing_deg(*a, *b))
        along += seg
    return best


def fake_matcher(roads):
    async def match(telemetry, previous=None, client=None):
        p = (telemetry.latitude, telemetry.longitude)
        road = min(roads, key=lambda r: _project(r.coords, p)[0])
        coords = road.coords
        d, along, point, bearing = _project(coords, p)
        if not road.oneway and telemetry.heading is not None and geo.heading_diff_deg(telemetry.heading, bearing) > 90:
            coords = list(reversed(coords))
            d, along, point, bearing = _project(coords, p)
        total = sum(geo.haversine_m(*a, *b) for a, b in zip(coords, coords[1:]))
        rc = RoadContext(
            road_segment_id=stable_segment_id(road.name, coords[0], coords[-1]), road_name=road.name,
            direction=RoadDirection.FORWARD, road_bearing_deg=bearing,
            matched_latitude=point[0], matched_longitude=point[1], raw_to_matched_distance_m=d,
            distance_along_segment_m=along, distance_to_segment_end_m=total - along, segment_length_m=total,
            geometry=RoadGeometry(coordinates=[(lon, lat) for lat, lon in coords]),
        )
        return RoadAwareVehicleState(vehicle_id=telemetry.vehicle_id, telemetry=telemetry, road_context=rc,
                                     match_quality=MapMatchQuality.GOOD, match_confidence=0.9,
                                     computed_at=datetime.now(timezone.utc))
    return match


class FakeDb:
    async def get(self, *a, **kw):
        return None

    def add(self, *a):
        pass

    async def commit(self):
        pass


def collision_cfg(**ctx_kw):
    # Night / stale weights off: the scenarios must not depend on the hour the suite runs at.
    weights = {"night": 0.0, "stale_data": 0.0, **ctx_kw.pop("weights", {})}
    return thresholds.CollisionSettings(context=ContextSettings(weights=weights, **ctx_kw))


@pytest.fixture
def world(monkeypatch):
    fake = FakeRedis()
    for m in REDIS_MODULES:
        monkeypatch.setattr(m, "get_redis", lambda: fake.aio)
    thresholds.set_current(collision_cfg())

    async def no_reload(db=None, force=False):
        return thresholds.current()
    monkeypatch.setattr(thresholds, "refresh", no_reload)
    context_service.reset_meta_cache()

    class World:
        redis = fake
        db = FakeDb()
        roads = []
        clock = datetime.now(timezone.utc)

        def use(self, *roads):
            self.roads = list(roads)
            monkeypatch.setattr(road_state_service.map_matching, "match_vehicle_to_road", fake_matcher(self.roads))

        async def ping(self, vid, point, speed, heading, accuracy=None, ts=None, plate=None):
            await vehicle_service.record_ping(self.db, vid, VehicleType.private, point[0], point[1], speed, heading,
                                              ts or datetime.now(timezone.utc), accuracy, None, plate=plate)

        async def chain(self, vid):
            state = await road_state_service.compute_and_store(vid, self.db)
            traj = await trajectory_service.compute_and_store(vid, state)
            await collision_service.compute_and_store(vid, self.db, own_trajectory=traj)
            return await alert_service.process_vehicle(vid, self.db)

        async def both(self, a, b):
            await self.chain(a)
            await self.chain(b)
            return await alert_service.get_alerts(a)

    yield World()
    thresholds.set_current(None)
    context_service.reset_meta_cache()


def ew_road(name="Rue A", offset_north_m=0.0, bearing=90.0, oneway=False):
    origin = geo.destination(*KAMBO, 0.0, offset_north_m) if offset_north_m else KAMBO
    return Road(name, line(origin, bearing), oneway)


def east(d, base=KAMBO):
    return geo.destination(*base, 90.0 if d >= 0 else 270.0, abs(d))


def north(d, base=KAMBO):
    return geo.destination(*base, 0.0 if d >= 0 else 180.0, abs(d))


# ── layer 1 scenarios ────────────────────────────────────────────────────────

async def test_head_on_same_lane_without_accuracy_alerts(world):
    world.use(ew_road())
    await world.ping("A", east(-30), 10.0, 90.0)              # no accuracy_m
    await world.ping("B", east(30), 10.0, 270.0)
    alerts = await world.both("A", "B")
    assert len(alerts) == 1
    a = alerts[0]
    assert a.level == AlertLevel.CRITICAL and a.physical_level == AlertLevel.CRITICAL
    assert a.encounter == "head_on" and a.context_index == 0.0 and a.context_reasons == []


async def test_head_on_keeping_right_without_accuracy_is_passing(world):
    world.use(ew_road())
    # each 1.8 m to the right of its direction of travel (eastbound south, westbound north)
    await world.ping("A", north(-1.8, east(-30)), 10.0, 90.0)
    await world.ping("B", north(1.8, east(30)), 10.0, 270.0)
    assert await world.both("A", "B") == []


async def test_queue_at_a_red_light_does_not_alert(world):
    world.use(ew_road())
    await world.ping("B", KAMBO, 0.0, 90.0, accuracy=5.0)               # first car, stopped at the light
    await world.ping("C", east(-7), 0.0, 90.0, accuracy=5.0)            # queued behind it
    await world.ping("A", east(-27), 4.0, 90.0, accuracy=5.0)           # rolling up at 14 km/h
    for vid in ("B", "C", "A"):
        await world.chain(vid)
    for vid in ("A", "B", "C"):
        assert await alert_service.get_alerts(vid) == []


async def test_fast_approach_on_a_stopped_queue_alerts(world):
    world.use(ew_road())
    await world.ping("B", KAMBO, 0.0, 90.0, accuracy=5.0)
    await world.ping("A", east(-25), 15.0, 90.0, accuracy=5.0)          # 54 km/h, 25 m behind: 4.9 m/s² needed
    alerts = await world.both("A", "B")
    assert [a.level for a in alerts] == [AlertLevel.HIGH_WARNING]
    assert "stopped vehicle ahead" in alerts[0].reason


async def test_parallel_road_does_not_alert(world):
    world.use(ew_road("Rue A"), ew_road("Rue B", offset_north_m=30.0))
    await world.ping("A", east(-30), 10.0, 90.0, accuracy=5.0)
    await world.ping("B", north(30.0, east(30)), 10.0, 270.0, accuracy=5.0)
    assert await world.both("A", "B") == []


async def test_divided_carriageway_is_passing(world):
    eastbound = ew_road("N3", oneway=True)
    westbound = ew_road("N3", offset_north_m=12.0, bearing=270.0, oneway=True)
    world.use(eastbound, westbound)
    await world.ping("A", east(-30), 15.0, 90.0, accuracy=5.0)
    await world.ping("B", north(12.0, east(30)), 15.0, 270.0, accuracy=5.0)
    assert await world.both("A", "B") == []
    report = await collision_service.get_conflicts("A", world.db)
    assert report.conflicts == [] and "B" in report.cleared_vehicle_ids


async def test_crossing_at_an_intersection_alerts(world):
    world.use(ew_road("Rue A"), Road("Rue B", line(KAMBO, 0.0)))
    await world.ping("A", east(-30), 10.0, 90.0, accuracy=5.0)
    await world.ping("B", north(-30), 10.0, 0.0, accuracy=5.0)
    alerts = await world.both("A", "B")
    assert [a.level for a in alerts] == [AlertLevel.CRITICAL]
    assert alerts[0].encounter == "crossing" and alerts[0].road_relation == "connected"


# ── layer 2: context ─────────────────────────────────────────────────────────

def seed_risky_cell(fake, point):
    near = geo.destination(*point, 45.0, 40.0)
    cell = ctx.PlaceCell(
        cell_id=grid_cell_id(*point, context_service.cell_size_m()),
        history=1.0, history_events=25, history_counts={"HARD_BRAKING": 18, "ACCIDENT": 2},
        history_dominant="HARD_BRAKING",
        hazards=[("pothole", *near), ("pothole", *near)],
        reports=[("accident", "ACCIDENT_REPORTED", *near)],
    )
    fake.set(context_service.cell_key(cell.cell_id), cell.model_dump_json())


async def _head_on_warning(world):
    world.use(ew_road())
    await world.ping("A", east(-120), 10.0, 90.0, accuracy=5.0)        # meet in 12 s → WARNING physically
    await world.ping("B", east(120), 10.0, 270.0, accuracy=5.0)
    return await world.both("A", "B")


async def test_without_context_data_the_level_is_physical(world):
    (a,) = await _head_on_warning(world)
    assert a.level == AlertLevel.WARNING and a.physical_level == AlertLevel.WARNING
    assert a.context_level_boost == 0


async def test_risky_cell_raises_warning_to_high_warning(world):
    seed_risky_cell(world.redis, KAMBO)
    (a,) = await _head_on_warning(world)
    assert a.physical_level == AlertLevel.WARNING
    assert a.level == AlertLevel.HIGH_WARNING and a.context_level_boost == 1
    assert a.context_index >= 0.7
    codes = [r.code for r in a.context_reasons]
    assert codes[0] == "HISTORY_HARSH_BRAKING" and {"ACCIDENT_REPORTED", "POTHOLE_NEARBY"} <= set(codes)
    assert "risky context" in a.reason


async def test_risky_cell_never_creates_an_alert(world):
    seed_risky_cell(world.redis, KAMBO)
    world.use(ew_road())
    await world.ping("A", east(-25), 10.0, 90.0, accuracy=5.0)         # same speed, 50 m apart: not closing
    await world.ping("B", east(25), 10.0, 90.0, accuracy=5.0)
    assert await world.both("A", "B") == []


async def test_risky_driver_from_the_plate(world):
    world.redis.hset(context_service.DRIVERS_KEY, mapping={"plate:LTTR995AR": json.dumps({"score": 82.0, "high": True})})
    world.use(ew_road())
    await world.ping("A", east(-120), 10.0, 90.0, accuracy=5.0, plate="LTTR 995 AR")
    await world.ping("B", east(120), 10.0, 270.0, accuracy=5.0)
    (a,) = await world.both("A", "B")
    (r,) = [r for r in a.context_reasons if r.code == "RISKY_DRIVER"]
    assert r.params["vehicle_id"] == "A" and r.params["score"] == 82.0
    assert a.level == AlertLevel.WARNING                                # 0.25 × 0.82: widened, not boosted


# ── pair evaluated once per pair of fixes ────────────────────────────────────

async def test_pair_is_evaluated_once_per_pair_of_fixes(world, monkeypatch):
    calls = []
    real = detector.assess_conflict

    def spy(*a, **kw):
        calls.append((a[0], a[2]))
        return real(*a, **kw)
    monkeypatch.setattr(detector, "assess_conflict", spy)
    world.use(ew_road())
    t0 = datetime.now(timezone.utc)
    await world.ping("A", east(-30), 10.0, 90.0, accuracy=5.0, ts=t0)
    await world.ping("B", east(30), 10.0, 270.0, accuracy=5.0, ts=t0)
    await world.both("A", "B")
    assert len(calls) == 1                                             # B's pipeline reused A's result
    await world.ping("B", east(28), 10.0, 270.0, accuracy=5.0, ts=t0 + timedelta(seconds=1))
    await world.chain("B")
    assert len(calls) == 2                                             # a new fix → re-evaluated


# ── delivery & metrics ───────────────────────────────────────────────────────

async def test_transitions_are_published_for_both_vehicles(world):
    world.use(ew_road())
    await world.ping("A", east(-30), 10.0, 90.0, accuracy=5.0)
    await world.ping("B", east(30), 10.0, 270.0, accuracy=5.0)
    (a,) = await world.both("A", "B")
    by_channel = {}
    for channel, data in world.redis.published:
        by_channel.setdefault(channel, []).append(json.loads(data))
    for vid, other in (("A", "B"), ("B", "A")):
        msgs = by_channel[alert_service.alert_channel(vid)]
        assert msgs[0]["type"] == "alert" and msgs[0]["other_vehicle_id"] == other
        assert msgs[0]["alert"]["transition"] == AlertTransition.NEW.value
        assert msgs[0]["alert"]["conflict_id"] == a.conflict_id
    assert a.detection_latency_ms is not None and a.detection_latency_ms >= 0
    hour = world.redis.hgetall(metrics_service.hour_key(datetime.now(timezone.utc)))
    assert hour.get("raised:CRITICAL") == "1"


def test_metrics_summary():
    h0 = datetime(2026, 10, 29, 8, tzinfo=timezone.utc)
    buckets = {h0: {"raised:WARNING": "2", "raised:CRITICAL": "1", "boosted": "1"},
               h0 + timedelta(hours=1): {"raised:MONITOR": "3", "context_raised": "2"}}
    out = metrics_service.summarise(buckets, [120.0, 80.0, 300.0],
                                    [("false_alert", "WARNING", 1), ("useful", "CRITICAL", 3)])
    assert out["alerts_total"] == 6 and out["by_level"]["WARNING"] == 2
    assert [h["total"] for h in out["by_hour"]] == [3, 3]
    assert out["context_boosts"] == 1 and out["context_raised"] == 2
    assert out["detection_latency_ms"] == {"median": 120.0, "p90": 264.0, "samples": 3}
    assert out["feedback"]["false_alert_rate"] == 0.25
