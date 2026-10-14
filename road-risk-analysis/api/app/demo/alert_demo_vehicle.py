"""
Demo-only, single-vehicle "alert showcase" simulator.

Purpose: let one real phone (running viasafe-mobile, staying put — the
person testing/presenting doesn't need to walk or drive anywhere) see the
*entire* collision-alert spectrum play out live: SAFE/MONITOR -> WARNING ->
HIGH_WARNING -> CRITICAL, then de-escalating back down as the threat
retreats, on a continuous loop.

Deliberately NOT the general-purpose fleet simulator (vehicle_simulator.py)
— that one spawns several vehicles that wander to random nearby
destinations, which is great for "there's live traffic around me" but has
no way to reliably walk through every alert tier on demand, and running it
alongside this script would mean two independent things generating traffic
markers near the same phone ("vehicle multiplication"). Run this ONE
instead of that one for a presentation; they're not meant to run together.

Mechanism: one fake vehicle alternates between two phases —
  - closing: a real OSRM route from wherever it currently is, straight to
    the phone's live position.
  - retreating: a real OSRM route from the phone's position back out to a
    point in the direction/distance of the NEXT scenario in SCENARIOS below
    — which is also what makes each cycle a genuinely different approach
    (direction and speed both vary), not the same pattern replayed.
Walking along one coherent OSRM-computed route per phase (recomputed only
at phase transitions) — not an independently road-snapped point every
tick — is what keeps the vehicle's predicted heading/direction consistent
tick-to-tick. An earlier version snapped each tick's straight-line-
projected point to the nearest road independently; consecutive snaps could
land on different nearby streets with unrelated bearings, which made
Step 3's road-aware trajectory prediction flicker between a real conflict
and no conflict at all rather than smoothly escalating. This is the same
"walk a real route" approach vehicle_simulator.py already uses, just aimed
at a scripted target (the phone) instead of a random nearby destination.

Reuses vehicle_simulator.py's request/routing/geometry helpers rather than
duplicating them — same conventions, same stdlib-only footprint.
"""
import bisect
import os
import time
import uuid

from app.demo.vehicle_simulator import _bearing_to, _destination, _haversine_m, _osrm_route, _request

API_BASE_URL = os.environ.get("API_BASE_URL", "http://api:8080")
TICK_SECONDS = float(os.environ.get("ALERT_DEMO_TICK_SECONDS", "2"))
VEHICLE_TYPE = os.environ.get("ALERT_DEMO_VEHICLE_TYPE", "truck")  # visually distinct from the phone's own icon

# Rotates through a different direction/speed/distance each cycle so
# repeated testing sees genuinely different approach scenarios, not the
# same single pattern replayed — advanced once per retreat (see
# _build_route_for_phase), so it loops back to scenario 0 after every 4th
# close+retreat, comfortably giving "each scenario at least twice" within
# a few minutes of the demo running unattended.
SCENARIOS = [
    {"label": "from the north", "bearing": 0.0, "speed": 15.0, "distance": 320.0},
    {"label": "from the east", "bearing": 90.0, "speed": 15.0, "distance": 320.0},
    {"label": "from the south, fast", "bearing": 180.0, "speed": 20.0, "distance": 380.0},
    {"label": "from the west, slow", "bearing": 270.0, "speed": 10.0, "distance": 250.0},
]
# Re-anchor to the phone's latest position if it has moved more than this
# since the current phase's route was computed (handles the phone drifting
# a little without needing a full stop/restart).
REANCHOR_JUMP_M = float(os.environ.get("ALERT_DEMO_REANCHOR_JUMP_M", "40"))
# How long to sit at the closest point of approach before retreating. Kept
# brief on purpose — a long dwell here means the vehicle spends a large
# fraction of every cycle camped right at the alert-triggering position, so
# reopening the app after any time away tends to land mid-alert instead of
# catching the escalation. The vehicle still passes through CRITICAL on its
# way in and out either way.
DWELL_AT_CLOSEST_S = float(os.environ.get("ALERT_DEMO_DWELL_AT_CLOSEST_S", "1"))


class AlertDemoVehicle:
    def __init__(self):
        self.id = str(uuid.uuid4())
        self.vehicle_type = VEHICLE_TYPE
        self.scenario_index = 0
        self.speed = SCENARIOS[0]["speed"]
        self.lat = 0.0
        self.lon = 0.0
        self.heading = 0.0

        self.phase = "closing"  # or "retreating"
        self.path: list[tuple[float, float]] = []
        self.cum_dist: list[float] = []
        self.dist_along = 0.0
        self.route_anchor: tuple[float, float] | None = None  # phone position the current route was built against
        self.dwelling = False       # sitting at the closest point of approach, about to retreat
        self.dwell_elapsed_s = 0.0

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

    def _build_route_for_phase(self, anchor_lat: float, anchor_lon: float) -> bool:
        if self.phase == "closing":
            first_spawn = SCENARIOS[0]
            start = (
                (self.lat, self.lon)
                if self.path
                else _destination(anchor_lat, anchor_lon, first_spawn["bearing"], first_spawn["distance"])
            )
            path = _osrm_route(start[0], start[1], anchor_lat, anchor_lon)
        else:
            # Retreat toward the NEXT scenario's direction/distance (and
            # adopt its speed) — the following closing phase starts from
            # wherever this retreat ends, so this is what actually varies
            # the approach direction/speed cycle to cycle.
            scenario = SCENARIOS[self.scenario_index % len(SCENARIOS)]
            self.scenario_index += 1
            self.speed = scenario["speed"]
            print(f"[alert-demo] next approach scenario: {scenario['label']} at {scenario['speed']:.0f} m/s")
            far_lat, far_lon = _destination(anchor_lat, anchor_lon, scenario["bearing"], scenario["distance"])
            path = _osrm_route(anchor_lat, anchor_lon, far_lat, far_lon)
        if not path or len(path) < 2:
            return False
        self._set_path(path)
        self.route_anchor = (anchor_lat, anchor_lon)
        return True

    def tick(self, dt: float, anchor_lat: float, anchor_lon: float):
        if self.dwelling:
            # Sitting right at the closest point of approach on purpose —
            # long enough for CRITICAL to actually be visible/presentable,
            # not just a single flash as the vehicle passes through.
            self.dwell_elapsed_s += dt
            if self.dwell_elapsed_s < DWELL_AT_CLOSEST_S:
                return
            self.dwelling = False
            self.phase = "retreating"
            self._build_route_for_phase(anchor_lat, anchor_lon)
            return

        needs_new_route = (
            len(self.path) < 2
            or self.route_anchor is None
            or _haversine_m(*self.route_anchor, anchor_lat, anchor_lon) > REANCHOR_JUMP_M
        )
        if needs_new_route:
            if not self._build_route_for_phase(anchor_lat, anchor_lon):
                return  # OSRM couldn't route this leg — hold position, retry next tick

        self.dist_along += self.speed * dt
        total = self.cum_dist[-1]
        if self.dist_along >= total:
            self.dist_along = total
            self.lat, self.lon = self.path[-1]
            if self.phase == "closing":
                self.dwelling = True
                self.dwell_elapsed_s = 0.0
            else:
                self.phase = "closing"
                self._build_route_for_phase(anchor_lat, anchor_lon)
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


def main():
    scenario_list = ", ".join(f"{s['label']} @ {s['speed']:.0f}m/s" for s in SCENARIOS)
    print(
        f"[alert-demo] starting — ONE vehicle ({VEHICLE_TYPE}) against {API_BASE_URL}, "
        f"cycling scenarios: {scenario_list}"
    )
    vehicle = AlertDemoVehicle()
    known_ids = {vehicle.id}

    for attempt in range(30):
        try:
            vehicle.register()
            break
        except (OSError, TimeoutError) as e:
            print(f"[alert-demo] waiting for backend ({e}) — attempt {attempt + 1}")
            time.sleep(2)
    print(f"[alert-demo] registered demo vehicle {vehicle.id[:8]} ({vehicle.vehicle_type})")

    while True:
        try:
            live = _request("GET", "/vehicles/live")
        except (OSError, TimeoutError) as e:
            print(f"[alert-demo] /vehicles/live unreachable: {e}")
            time.sleep(TICK_SECONDS)
            continue

        real = [v for v in live.get("items", []) if v["vehicle_id"] not in known_ids]
        if not real:
            print("[alert-demo] no real phone live yet — waiting for the app to connect...")
            time.sleep(TICK_SECONDS)
            continue
        real.sort(key=lambda v: v["ts"], reverse=True)
        phone = real[0]

        vehicle.tick(TICK_SECONDS, phone["latitude"], phone["longitude"])
        try:
            vehicle.ping()
        except (OSError, TimeoutError) as e:
            print(f"[alert-demo] ping failed: {e}")

        remaining = (vehicle.cum_dist[-1] - vehicle.dist_along) if vehicle.cum_dist else -1
        print(f"[alert-demo] {vehicle.phase} — ~{remaining:.0f}m left on this leg")

        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    main()
