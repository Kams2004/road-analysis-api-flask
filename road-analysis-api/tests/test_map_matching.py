"""
Map-matching scenarios (Step 2 acceptance criteria / spec section "TESTING").
All OSRM calls are faked — no network access, fully deterministic.
"""
from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.services.osrm import geo
from app.services.osrm.client import OSRMUnavailableError
from app.services.osrm.map_matching import match_vehicle_to_road
from app.services.osrm.models import MapMatchQuality, RoadAwareVehicleState, RoadDirection, TelemetrySnapshot
from tests.fakes import FakeOSRMClient, match_response, nearest_response, route_window_response

NOW = datetime.now(timezone.utc)
KAMBO = (4.0412, 9.8107)  # real coordinate used elsewhere in this project's demo/testing


def telemetry(vehicle_id="BUS-001", lat=KAMBO[0], lon=KAMBO[1], heading=90.0, ts=NOW, **kw):
    return TelemetrySnapshot(
        vehicle_id=vehicle_id, latitude=lat, longitude=lon, heading=heading, timestamp=ts, **kw
    )


_PREV_DEFAULT_LAT, _PREV_DEFAULT_LON = geo.destination(*KAMBO, 270.0, 20.0)  # ~20m west of KAMBO


def a_previous_state(vehicle_id="BUS-001", lat=_PREV_DEFAULT_LAT, lon=_PREV_DEFAULT_LON, ts=None) -> RoadAwareVehicleState:
    """A vehicle's prior computed state, old enough that _should_reuse_cache
    won't kick in. /match needs at least two points (real OSRM behaviour —
    a duplicated single point gives a degenerate, always-zero confidence
    score), so most scenarios below simulate an already-tracked vehicle by
    supplying one of these as `previous` rather than a from-cold-start ping.
    Defaults to a point a realistic ~20m from `telemetry()`'s own default,
    not the identical coordinate — real consecutive telemetry is never
    bit-for-bit identical, and match_vehicle_to_road itself now rejects a
    near-zero-distance previous point for exactly the reason above (see
    OSRM_MATCH_MIN_TRACE_DISTANCE_M), so an identical-point default here
    would take the same single-point /nearest path as no previous at all."""
    ts = ts or (NOW - timedelta(seconds=5))
    return RoadAwareVehicleState(
        vehicle_id=vehicle_id,
        telemetry=telemetry(vehicle_id=vehicle_id, lat=lat, lon=lon, ts=ts),
        road_context=None,
        match_quality=MapMatchQuality.GOOD,
        computed_at=ts,
    )


def a_route_window(matched, bearing=90.0, current_name="Route de Kambo", next_name="Avenue de la Paix"):
    """Two-step window: matched point sits inside the first step, a
    differently-named step follows — used by most of the tests below."""
    p_behind = geo.destination(*matched, (bearing + 180) % 360, 150.0)
    p_mid = geo.destination(*matched, bearing, 150.0)
    p_ahead = geo.destination(*matched, bearing, 300.0)

    d0 = geo.haversine_m(*p_behind, *matched) + geo.haversine_m(*matched, *p_mid)
    d1 = geo.haversine_m(*p_mid, *p_ahead)

    return route_window_response(
        [
            {"name": current_name, "distance": d0, "coords": [p_behind, matched, p_mid], "bearing": bearing},
            {"name": next_name, "distance": d1, "coords": [p_mid, p_ahead], "bearing": bearing},
        ]
    ), p_behind, p_mid, p_ahead


# ── Test 1 — valid road position ────────────────────────────────────────────

async def test_valid_road_position_is_good_quality():
    route_resp, p_behind, p_mid, _ = a_route_window(KAMBO)
    client = FakeOSRMClient(
        match_responses=[match_response(KAMBO, name="Route de Kambo", confidence=0.92, bearing_after=90.0)],
        route_responses=[route_resp],
    )

    state = await match_vehicle_to_road(telemetry(), previous=a_previous_state(), client=client)

    assert state.match_quality == MapMatchQuality.GOOD
    assert state.road_context is not None
    assert state.road_context.road_name == "Route de Kambo"
    assert state.road_context.matched_latitude == KAMBO[0]
    assert state.road_context.matched_longitude == KAMBO[1]
    assert 0 <= state.road_context.distance_along_segment_m <= state.road_context.segment_length_m


# ── Test 2 — position near road (GPS error) does not fail outright ─────────

async def test_noisy_gps_near_road_degrades_but_does_not_fail():
    slightly_off = geo.destination(*KAMBO, 45.0, 12.0)  # 12m of GPS error
    route_resp, *_ = a_route_window(KAMBO)
    client = FakeOSRMClient(
        match_responses=[match_response(KAMBO, confidence=0.25, bearing_after=90.0)],
        route_responses=[route_resp],
    )

    state = await match_vehicle_to_road(
        telemetry(lat=slightly_off[0], lon=slightly_off[1]), previous=a_previous_state(), client=client
    )

    assert state.match_quality in (MapMatchQuality.DEGRADED, MapMatchQuality.GOOD)
    assert state.road_context is not None


# ── Test 3 — heading vs. road bearing ───────────────────────────────────────

async def test_heading_aligned_with_road_is_forward():
    route_resp, *_ = a_route_window(KAMBO, bearing=90.0)
    client = FakeOSRMClient(
        match_responses=[match_response(KAMBO, confidence=0.9, bearing_after=90.0)],
        route_responses=[route_resp],
    )
    state = await match_vehicle_to_road(
        telemetry(heading=95.0), previous=a_previous_state(), client=client
    )  # within tolerance of 90°
    assert state.road_context.direction == RoadDirection.FORWARD


async def test_heading_opposite_road_is_reverse():
    route_resp, *_ = a_route_window(KAMBO, bearing=90.0)
    client = FakeOSRMClient(
        match_responses=[match_response(KAMBO, confidence=0.9, bearing_after=90.0)],
        route_responses=[route_resp],
    )
    state = await match_vehicle_to_road(
        telemetry(heading=270.0), previous=a_previous_state(), client=client
    )  # 180° from road bearing
    assert state.road_context.direction == RoadDirection.REVERSE


async def test_heading_far_off_road_bearing_is_unknown():
    route_resp, *_ = a_route_window(KAMBO, bearing=90.0)
    client = FakeOSRMClient(
        match_responses=[match_response(KAMBO, confidence=0.9, bearing_after=90.0)],
        route_responses=[route_resp],
    )
    # Perpendicular to the road (90°) — 90° off forward, 90° off reverse,
    # outside tolerance (45°) both ways.
    state = await match_vehicle_to_road(telemetry(heading=180.0), previous=a_previous_state(), client=client)
    assert state.road_context.direction == RoadDirection.UNKNOWN


# ── Test 4 — unknown/unmatchable road handled gracefully ───────────────────

async def test_no_match_and_no_nearest_result_is_failed_not_a_crash():
    client = FakeOSRMClient(
        match_responses=[match_response(KAMBO, code="NoMatch")],
        nearest_responses=[nearest_response(KAMBO, code="NoSegment")],
    )
    state = await match_vehicle_to_road(telemetry(), client=client)

    assert state.match_quality == MapMatchQuality.FAILED
    assert state.road_context is None
    assert state.telemetry.latitude == KAMBO[0]  # raw telemetry preserved


async def test_no_match_falls_back_to_nearest():
    # /match found nothing, so there's no road-bearing hint yet — the
    # subsequent route-window request defaults to bearing 0.
    route_resp, *_ = a_route_window(KAMBO, bearing=0.0)
    client = FakeOSRMClient(
        match_responses=[match_response(KAMBO, code="NoMatch")],
        nearest_responses=[nearest_response(KAMBO, name="Unnamed Track")],
        route_responses=[route_resp],
    )
    state = await match_vehicle_to_road(telemetry(), client=client)

    # /nearest-only fallback is always low-confidence but still usable.
    assert state.match_quality in (MapMatchQuality.DEGRADED, MapMatchQuality.FAILED)
    assert len(client.nearest_calls) == 1


async def test_first_ping_with_no_history_uses_nearest_not_match():
    """A vehicle's very first ping has no previous point. /match needs at
    least two points, and duplicating the single point to satisfy that
    produces a zero-distance trace whose confidence is always 0 (confirmed
    against the live public OSRM server) — worse than just using /nearest,
    so this path must skip /match entirely rather than send a degenerate
    request."""
    route_resp, *_ = a_route_window(KAMBO, bearing=0.0)
    client = FakeOSRMClient(
        nearest_responses=[nearest_response(KAMBO, name="Route de Kambo")],
        route_responses=[route_resp],
    )

    state = await match_vehicle_to_road(telemetry(), previous=None, client=client)

    assert len(client.match_calls) == 0
    assert len(client.nearest_calls) == 1
    assert state.road_context is not None
    assert state.road_context.road_name == "Route de Kambo"
    assert state.match_quality == MapMatchQuality.DEGRADED  # /nearest alone is always low-confidence


# ── Test 5 — multiple vehicles are matched independently ───────────────────

async def test_multiple_vehicles_match_independently():
    other_point = geo.destination(*KAMBO, 0.0, 5000.0)
    route_resp_a, *_ = a_route_window(KAMBO, current_name="Road A")
    route_resp_b, *_ = a_route_window(other_point, current_name="Road B")

    client_a = FakeOSRMClient(
        match_responses=[match_response(KAMBO, name="Road A", confidence=0.9)],
        route_responses=[route_resp_a],
    )
    client_b = FakeOSRMClient(
        match_responses=[match_response(other_point, name="Road B", confidence=0.9)],
        route_responses=[route_resp_b],
    )

    other_point_prev = geo.destination(*other_point, 270.0, 20.0)  # ~20m west, same idea as the module default

    state_a = await match_vehicle_to_road(
        telemetry(vehicle_id="BUS-001"), previous=a_previous_state(vehicle_id="BUS-001"), client=client_a
    )
    state_b = await match_vehicle_to_road(
        telemetry(vehicle_id="BUS-002", lat=other_point[0], lon=other_point[1]),
        previous=a_previous_state(vehicle_id="BUS-002", lat=other_point_prev[0], lon=other_point_prev[1]),
        client=client_b,
    )

    assert state_a.vehicle_id == "BUS-001"
    assert state_b.vehicle_id == "BUS-002"
    assert state_a.road_context.road_name == "Road A"
    assert state_b.road_context.road_name == "Road B"


# ── Test 6 — OSRM unavailable must not crash the pipeline ──────────────────

async def test_osrm_unavailable_returns_unavailable_status_not_an_exception():
    client = FakeOSRMClient(match_responses=[OSRMUnavailableError("connection refused")])

    state = await match_vehicle_to_road(telemetry(), previous=a_previous_state(), client=client)  # must not raise

    assert state.match_quality == MapMatchQuality.UNAVAILABLE
    assert state.road_context is None
    assert state.telemetry.latitude == KAMBO[0]


# ── Test 7 — stale telemetry is not treated as current ──────────────────────

async def test_stale_telemetry_is_flagged_without_calling_osrm():
    old_ts = NOW - timedelta(seconds=settings.OSRM_STALE_TELEMETRY_S + 10)
    client = FakeOSRMClient()  # no canned responses — a call would raise AssertionError

    state = await match_vehicle_to_road(telemetry(ts=old_ts), client=client)

    assert state.match_quality == MapMatchQuality.STALE
    assert len(client.match_calls) == 0


# ── Test 8 — curved road geometry is preserved ──────────────────────────────

async def test_curved_road_geometry_is_preserved_not_flattened():
    bearing = 90.0
    p0 = KAMBO
    p1 = geo.destination(*p0, bearing, 50.0)
    p2 = geo.destination(*p1, bearing + 30, 50.0)   # the road curves here
    p3 = geo.destination(*p2, bearing + 60, 50.0)   # and curves further

    route_resp = route_window_response(
        [{"name": "Falaise Road", "distance": 150.0, "coords": [p0, p1, p2, p3], "bearing": bearing}]
    )
    client = FakeOSRMClient(
        match_responses=[match_response(p0, name="Falaise Road", confidence=0.9, bearing_after=bearing)],
        route_responses=[route_resp],
    )

    p0_prev = geo.destination(*p0, (bearing + 180) % 360, 20.0)  # ~20m behind p0
    state = await match_vehicle_to_road(
        telemetry(lat=p0[0], lon=p0[1]), previous=a_previous_state(lat=p0_prev[0], lon=p0_prev[1]), client=client
    )

    coords = state.road_context.geometry.coordinates
    assert len(coords) == 4  # all intermediate curve points kept, not simplified to a straight line


# ── Test 9 — junction: distinct upcoming road is not collapsed away ────────

async def test_junction_keeps_upcoming_road_distinct():
    route_resp, p_behind, p_mid, p_ahead = a_route_window(
        KAMBO, current_name="Route de Kambo", next_name="Route de la Falaise"
    )
    client = FakeOSRMClient(
        match_responses=[match_response(KAMBO, name="Route de Kambo", confidence=0.9, bearing_after=90.0)],
        route_responses=[route_resp],
    )

    state = await match_vehicle_to_road(telemetry(), previous=a_previous_state(), client=client)

    assert state.road_context.road_name == "Route de Kambo"
    names = {seg.road_name for seg in state.road_context.upcoming_segments}
    assert "Route de la Falaise" in names
    assert "Route de Kambo" not in names or len(state.road_context.upcoming_segments) >= 1


# ── Extra: don't hammer OSRM for a near-stationary vehicle ─────────────────

async def test_stable_position_reuses_cached_match_without_calling_osrm_again():
    route_resp, *_ = a_route_window(KAMBO)
    client = FakeOSRMClient(
        match_responses=[match_response(KAMBO, confidence=0.9, bearing_after=90.0)],
        route_responses=[route_resp],
    )
    first = await match_vehicle_to_road(telemetry(ts=NOW), previous=a_previous_state(), client=client)
    assert len(client.match_calls) == 1

    nudged = geo.destination(*KAMBO, 10.0, 2.0)  # 2m away, 1s later
    second = await match_vehicle_to_road(
        telemetry(lat=nudged[0], lon=nudged[1], ts=NOW + timedelta(seconds=1)),
        previous=first,
        client=client,
    )

    assert len(client.match_calls) == 1  # no new OSRM call
    assert second.road_context.road_name == first.road_context.road_name
