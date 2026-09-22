"""
Demo-only vehicle simulator for testing Step 1 of the Predictive Cooperative
Collision Prevention Module (live vehicle telemetry).

It does NOT belong to the product — it exists purely so a single real phone
(running the viasafe-mobile app) has other "vehicles" to see moving around it
on the map without needing a second physical device. It:

  1. Registers a handful of fake vehicles (bus/taxi/truck/private/moto).
  2. Polls GET /vehicles/live to find the real phone's position (the one
     live vehicle that isn't one of its own fake ids).
  3. Picks a random nearby destination and asks OSRM (the same public
     routing server the app itself uses for navigation, see fetchRoute in
     viasafe-mobile/app/home/map.tsx) for a real driving route to it, then
     walks each vehicle along that route's road geometry at its own speed —
     so vehicles stay on streets instead of cutting through buildings —
     and picks a new nearby destination whenever it reaches the end.

Pure standard library — no extra dependency to install into the shared app
image just to run a demo script.
"""
import bisect
import json
import math
import os
import random
import time
import urllib.error
import urllib.request
import uuid

API_BASE_URL = os.environ.get("API_BASE_URL", "http://api:8080")
OSRM_BASE_URL = os.environ.get("SIM_OSRM_URL", "https://router.project-osrm.org")
NUM_VEHICLES = int(os.environ.get("SIM_NUM_VEHICLES", "5"))
TICK_SECONDS = float(os.environ.get("SIM_TICK_SECONDS", "2"))
DEST_RADIUS_MIN_M = float(os.environ.get("SIM_DEST_RADIUS_MIN_M", "150"))
DEST_RADIUS_MAX_M = float(os.environ.get("SIM_DEST_RADIUS_MAX_M", "600"))
ANCHOR_JUMP_M = float(os.environ.get("SIM_ANCHOR_JUMP_M", "2000"))  # re-route fleet if the phone moves this far

VEHICLE_TYPES = ["bus", "taxi", "truck", "private", "moto"]
SPEED_RANGE_MPS = {  # roughly realistic cruising speeds per type
    "bus": (4.0, 11.0),
    "taxi": (5.0, 15.0),
    "truck": (4.0, 12.0),
    "private": (5.0, 16.0),
    "moto": (5.0, 18.0),
}

EARTH_R = 6_371_000.0


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return 2 * EARTH_R * math.asin(math.sqrt(a))


def _bearing_to(lat1, lon1, lat2, lon2) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    y = math.sin(dlon) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _destination(lat, lon, bearing_deg, distance_m):
    """Great-circle destination point — used only to pick a random nearby
    target coordinate; OSRM then snaps the actual route to the road network."""
    ang_dist = distance_m / EARTH_R
    bearing = math.radians(bearing_deg)
    phi1, lam1 = math.radians(lat), math.radians(lon)
    phi2 = math.asin(
        math.sin(phi1) * math.cos(ang_dist) + math.cos(phi1) * math.sin(ang_dist) * math.cos(bearing)
    )
    lam2 = lam1 + math.atan2(
        math.sin(bearing) * math.sin(ang_dist) * math.cos(phi1),
        math.cos(ang_dist) - math.sin(phi1) * math.sin(phi2),
    )
    return math.degrees(phi2), (math.degrees(lam2) + 540) % 360 - 180


def _request(method: str, path: str, body: dict | None = None, base_url: str = API_BASE_URL) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{base_url}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def _osrm_route(lat1: float, lon1: float, lat2: float, lon2: float):
    """Real driving route between two points, as a list of (lat, lon) — or
    None if OSRM can't find one (e.g. offline, no nearby road, rate limit)."""
    path = (
        f"/route/v1/driving/{lon1:.6f},{lat1:.6f};{lon2:.6f},{lat2:.6f}"
        "?overview=full&geometries=geojson"
    )
    try:
        data = _request("GET", path, base_url=OSRM_BASE_URL)
    except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
        print(f"[simulator] OSRM request failed: {e}")
        return None
    if data.get("code") != "Ok" or not data.get("routes"):
        return None
    return [(lat, lon) for lon, lat in data["routes"][0]["geometry"]["coordinates"]]


class FakeVehicle:
    def __init__(self, vehicle_type: str):
        self.id = str(uuid.uuid4())
        self.vehicle_type = vehicle_type
        self.lat = 0.0
        self.lon = 0.0
        self.heading = 0.0
        lo, hi = SPEED_RANGE_MPS[vehicle_type]
        self.speed = random.uniform(lo, hi)
        self.spawned = False

        # Current road-following path: list of (lat, lon) plus the cumulative
        # distance (metres) to each point, so we can place the vehicle by
        # arc-length instead of just teleporting between waypoints.
        self.path: list[tuple[float, float]] = []
        self.cum_dist: list[float] = []
        self.dist_along = 0.0

    def register(self):
        _request("POST", "/vehicles/register", {"vehicle_id": self.id, "vehicle_type": self.vehicle_type})

    def _set_path(self, path):
        self.path = path
        cum = [0.0]
        for i in range(1, len(path)):
            cum.append(cum[-1] + _haversine_m(*path[i - 1], *path[i]))
        self.cum_dist = cum
        self.dist_along = 0.0
        self.lat, self.lon = path[0]
        if len(path) > 1:
            self.heading = _bearing_to(*path[0], *path[1])

    def _route_towards(self, anchor_lat: float, anchor_lon: float) -> bool:
        """Ask OSRM for a real route from the vehicle's current position to a
        random point near the phone. Returns False (and leaves the vehicle
        parked) if no route could be found."""
        bearing = random.uniform(0, 360)
        dist = random.uniform(DEST_RADIUS_MIN_M, DEST_RADIUS_MAX_M)
        dest_lat, dest_lon = _destination(anchor_lat, anchor_lon, bearing, dist)
        path = _osrm_route(self.lat, self.lon, dest_lat, dest_lon)
        if not path or len(path) < 2:
            return False
        self._set_path(path)
        return True

    def spawn_around(self, anchor_lat: float, anchor_lon: float):
        # Route FROM the phone's own position so the first leg starts right
        # on a real road near the user, then heads to a random nearby point.
        self.lat, self.lon = anchor_lat, anchor_lon
        if self._route_towards(anchor_lat, anchor_lon):
            self.spawned = True
        # else: stay parked at the anchor and retry on the next tick.

    def tick(self, dt: float, anchor_lat: float, anchor_lon: float):
        if len(self.path) < 2:
            self._route_towards(anchor_lat, anchor_lon)
            return

        self.dist_along += self.speed * dt
        total = self.cum_dist[-1]
        if self.dist_along >= total:
            # Reached the destination — head somewhere new near the phone.
            if not self._route_towards(anchor_lat, anchor_lon):
                self.dist_along = total  # hold at the route's end until OSRM answers again
            return

        idx = bisect.bisect_right(self.cum_dist, self.dist_along) - 1
        idx = max(0, min(idx, len(self.path) - 2))
        seg_start, seg_end = self.cum_dist[idx], self.cum_dist[idx + 1]
        t = 0.0 if seg_end <= seg_start else (self.dist_along - seg_start) / (seg_end - seg_start)
        lat1, lon1 = self.path[idx]
        lat2, lon2 = self.path[idx + 1]
        self.lat = lat1 + (lat2 - lat1) * t
        self.lon = lon1 + (lon2 - lon1) * t
        self.heading = _bearing_to(lat1, lon1, lat2, lon2)

    def ping(self):
        _request(
            "POST",
            f"/vehicles/{self.id}/ping",
            {
                "latitude": self.lat,
                "longitude": self.lon,
                "speed_mps": round(self.speed, 2),
                "heading": round(self.heading, 1),
            },
        )


def find_anchor(known_ids: set[str]):
    """Return (lat, lon) of the most recently seen non-simulated vehicle, or None."""
    try:
        live = _request("GET", "/vehicles/live")
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        print(f"[simulator] /vehicles/live unreachable: {e}")
        return None

    real = [v for v in live.get("items", []) if v["vehicle_id"] not in known_ids]
    if not real:
        return None
    real.sort(key=lambda v: v["ts"], reverse=True)
    top = real[0]
    return top["latitude"], top["longitude"]


def main():
    print(f"[simulator] starting — {NUM_VEHICLES} fake vehicles against {API_BASE_URL}, "
          f"routing via {OSRM_BASE_URL}")
    fleet = [FakeVehicle(VEHICLE_TYPES[i % len(VEHICLE_TYPES)]) for i in range(NUM_VEHICLES)]
    known_ids = {v.id for v in fleet}

    for v in fleet:
        for attempt in range(30):
            try:
                v.register()
                break
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                print(f"[simulator] waiting for backend ({e}) — attempt {attempt + 1}")
                time.sleep(2)
    print(f"[simulator] registered {len(fleet)} fake vehicles: "
          f"{[(v.id[:8], v.vehicle_type) for v in fleet]}")

    last_anchor = None
    while True:
        anchor = find_anchor(known_ids)
        if anchor is None:
            print("[simulator] no real vehicle live yet — waiting for a phone to connect...")
            time.sleep(TICK_SECONDS)
            continue

        needs_spawn = not any(v.spawned for v in fleet)
        if last_anchor is not None and not needs_spawn:
            if _haversine_m(*last_anchor, *anchor) > ANCHOR_JUMP_M:
                print("[simulator] phone jumped far — re-routing fleet around new position")
                for v in fleet:
                    v.spawned = False
                needs_spawn = True
        last_anchor = anchor

        for v in fleet:
            if needs_spawn:
                v.spawn_around(*anchor)
            else:
                v.tick(TICK_SECONDS, *anchor)
            try:
                v.ping()
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                print(f"[simulator] ping failed for {v.id[:8]} ({v.vehicle_type}): {e}")

        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    main()
