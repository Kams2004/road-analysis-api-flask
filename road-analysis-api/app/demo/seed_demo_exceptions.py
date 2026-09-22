"""
Demo-only: seed synthetic historical "exception" events (Ymane's own term
for a driving incident — speeding, hard braking, accidents, ...) via the
manual source, so the mobile app's historical risk-zone overlay has
something to show without needing live Ymane credentials.

Why this exists: Ymane's real account is currently suspended (repeated
logins during manual testing tripped their abuse detection — see
HISTORICAL_YMANE_SYNC_ENABLED's docstring in app/core/config.py), so
POST /historical/import?source=ymane isn't usable for a demo right now.
source=manual has no such dependency; this script just builds a plausible
event set and posts it the same way a real manual/CSV import would.

Event points are real OSRM route geometry — not synthetic lat/lon jitter
around a center point — so every event sits exactly on the road trace.
Clusters are spread along BOTH the Douala-Bafoussam (N5) and
Douala-Yaounde (N3) corridors, not bunched into one area.

Severity is deliberately tiered per cluster (light/moderate/high/critical)
rather than left to pure randomness, and each cluster's events are kept
within a tight ~120m window (well inside one HISTORICAL_GRID_CELL_SIZE_M
150m cell) — see app/services/historical_service.py's _risk_score: it's a
weighted sum of event-type counts (ACCIDENT=5.0, HARD_BRAKING/
HARSH_CORNERING=1.5, HARD_ACCELERATION=1.2, SPEEDING=1.0, IDLE=0.3) divided
by HISTORICAL_RISK_SATURATION_WEIGHT (20.0). Spreading a cluster's events
across too wide a window dilutes them into separate grid cells, so no
single cell ever crosses the moderate/high/critical color thresholds —
that's what an earlier version of this script got wrong (it used a much
wider window and produced an almost uniformly green/low-risk map).

Usage:
    API_BASE_URL=http://185.182.184.188:8080 python -m app.demo.seed_demo_exceptions

Re-running is safe: each event has a unique source_event_id and the
(source, source_event_id) pair is a DB uniqueness constraint, so duplicate
runs are simply skipped (skipped_duplicates in the response), not doubled.

This replaces two earlier batches:
  - "demo-n5-*"    — random +/-100m jitter, Bafoussam only, could land off-road
  - "demo-route-*" — real road points but too wide a window, mostly green
Neither is auto-deleted (the import API is insert-only, no delete
endpoint) — remove them manually on the server if desired:
    psql "$DB_URL" -c "DELETE FROM historical_events WHERE source='manual' AND (source_event_id LIKE 'demo-n5-%' OR source_event_id LIKE 'demo-route-%')"
"""
import json
import os
import random
import urllib.request
from datetime import datetime, timedelta, timezone

API_BASE_URL = os.environ.get("API_BASE_URL", "http://api:8080")
OSRM_URL = os.environ.get("SEED_DEMO_OSRM_URL", "https://router.project-osrm.org")
SEED = int(os.environ.get("SEED_DEMO_EXCEPTIONS_SEED", "11"))

# (origin, destination, label) — real driving routes; events are placed
# using the actual OSRM-returned road geometry between them.
ROUTES = [
    ((9.700013, 4.045019), (10.239891, 5.270995), "Douala-Bafoussam (N5)"),
    ((9.700013, 4.045019), (11.502169, 3.848010), "Douala-Yaounde (N3)"),
]

# Half-width of the window (metres) each cluster's events are drawn from —
# small enough to stay within one 150m grid cell in the vast majority of
# cases regardless of exactly where the anchor lands relative to a cell
# boundary.
WINDOW_HALF_WIDTH_M = 60.0

# (fraction along the route, tier) — five clusters per route, deliberately
# spanning the full severity range so the map shows real color variation
# instead of one uniform tier.
ANCHORS = [
    (0.15, "light"),
    (0.35, "moderate"),
    (0.55, "high"),
    (0.75, "critical"),
    (0.92, "moderate"),
]

# event_type weighted list, roughly targeted per tier: (event types, count range)
TIER_SPEC = {
    "light":    (["SPEEDING"] * 3 + ["IDLE_VIOLATION"] * 1, (3, 4)),
    "moderate": (["SPEEDING"] * 3 + ["HARD_BRAKING"] * 2 + ["HARSH_CORNERING"] * 1, (6, 8)),
    "high":     (["HARD_BRAKING"] * 3 + ["HARSH_CORNERING"] * 2 + ["SPEEDING"] * 2 + ["HARD_ACCELERATION"] * 1, (9, 11)),
    "critical": (["ACCIDENT"] * 2 + ["HARD_BRAKING"] * 3 + ["SPEEDING"] * 3 + ["HARSH_CORNERING"] * 2, (11, 13)),
}


def haversine_m(lat1, lon1, lat2, lon2):
    import math
    R = 6_371_000
    to_rad = math.radians
    d_lat = to_rad(lat2 - lat1)
    d_lon = to_rad(lon2 - lon1)
    a = math.sin(d_lat / 2) ** 2 + math.cos(to_rad(lat1)) * math.cos(to_rad(lat2)) * math.sin(d_lon / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def fetch_osrm_route_with_distances(origin, dest):
    lon1, lat1 = origin
    lon2, lat2 = dest
    url = f"{OSRM_URL}/route/v1/driving/{lon1},{lat1};{lon2},{lat2}?geometries=geojson&overview=full"
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.load(resp)
    coords = data["routes"][0]["geometry"]["coordinates"]  # [lon, lat]
    cumulative = [0.0]
    for i in range(1, len(coords)):
        lon_a, lat_a = coords[i - 1]
        lon_b, lat_b = coords[i]
        cumulative.append(cumulative[-1] + haversine_m(lat_a, lon_a, lat_b, lon_b))
    return coords, cumulative


def window_around(coords, cumulative, center_idx, half_width_m):
    center_dist = cumulative[center_idx]
    window = [
        coords[i] for i in range(len(coords))
        if abs(cumulative[i] - center_dist) <= half_width_m
    ]
    return window or [coords[center_idx]]


def build_events():
    random.seed(SEED)
    now = datetime.now(timezone.utc)
    events, eid = [], 0

    for origin, dest, label in ROUTES:
        coords, cumulative = fetch_osrm_route_with_distances(origin, dest)
        n = len(coords)
        for frac, tier in ANCHORS:
            center_idx = int(n * frac)
            window = window_around(coords, cumulative, center_idx, WINDOW_HALF_WIDTH_M)
            types, (lo, hi) = TIER_SPEC[tier]
            for _ in range(random.randint(lo, hi)):
                eid += 1
                lon, lat = random.choice(window)
                occurred = now - timedelta(days=random.uniform(0, 45), hours=random.uniform(0, 23))
                etype = random.choice(types)
                speed = random.uniform(14, 33) if etype in ("SPEEDING", "HARD_BRAKING", "HARSH_CORNERING") else None
                events.append({
                    "source": "manual",
                    "source_event_id": f"demo-tier-{eid:04d}",
                    "event_type": etype,
                    "occurred_at": occurred.isoformat(),
                    "latitude": round(lat, 6),
                    "longitude": round(lon, 6),
                    "vehicle_external_id": f"demo-veh-{random.randint(1, 6)}",
                    "speed_mps": round(speed, 1) if speed else None,
                    "speed_limit_mps": 22.2 if speed else None,  # ~80 km/h highway limit
                    "severity": {"light": "low", "moderate": "low", "high": "medium", "critical": "high"}[tier],
                    "raw": {"demo": True, "label": label, "tier": tier},
                })
    return events, now


def main():
    events, now = build_events()
    payload = {
        "source": "manual",
        "start": (now - timedelta(days=46)).date().isoformat(),
        "end": now.date().isoformat(),
        "events": events,
    }
    req = urllib.request.Request(
        f"{API_BASE_URL}/historical/import",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.load(resp)
    print(f"[seed-demo-exceptions] {API_BASE_URL}: {json.dumps(result, indent=2)}")


if __name__ == "__main__":
    main()
