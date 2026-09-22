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
around a center point — so every event sits exactly on the road trace,
never off to the side of it. Several clusters are spread along BOTH the
Douala-Bafoussam (N5) and Douala-Yaounde (N3) corridors rather than
bunched into one area.

Usage:
    API_BASE_URL=http://185.182.184.188:8080 python -m app.demo.seed_demo_exceptions

Re-running is safe: each event has a unique source_event_id and the
(source, source_event_id) pair is a DB uniqueness constraint, so duplicate
runs are simply skipped (skipped_duplicates in the response), not doubled.

This replaces an earlier batch (source_event_id prefix "demo-n5-") that
used random jitter and could land slightly off the actual road; that batch
is NOT auto-deleted by this script (the import API is insert-only, no
delete endpoint) — remove it manually on the server if desired:
    psql "$DB_URL" -c "DELETE FROM historical_events WHERE source='manual' AND source_event_id LIKE 'demo-n5-%'"
"""
import json
import os
import random
import urllib.request
from datetime import datetime, timedelta, timezone

API_BASE_URL = os.environ.get("API_BASE_URL", "http://api:8080")
OSRM_URL = os.environ.get("SEED_DEMO_OSRM_URL", "https://router.project-osrm.org")
SEED = int(os.environ.get("SEED_DEMO_EXCEPTIONS_SEED", "7"))

# (origin, destination, label) — real driving routes; events are placed
# using the actual OSRM-returned road geometry between them.
ROUTES = [
    ((9.700013, 4.045019), (10.239891, 5.270995), "Douala-Bafoussam (N5)"),
    ((9.700013, 4.045019), (11.502169, 3.848010), "Douala-Yaounde (N3)"),
]

# Where along each route (as a fraction of total points, 0=origin/Douala,
# 1=destination) to anchor a cluster. Skipping the first ~12% keeps events
# off dense in-city Douala streets and on the actual intercity highway.
ANCHOR_FRACTIONS = [0.18, 0.38, 0.58, 0.78, 0.93]

EVENT_TYPES_WEIGHTED = (
    ["SPEEDING"] * 5
    + ["HARD_BRAKING"] * 4
    + ["HARSH_CORNERING"] * 2
    + ["HARD_ACCELERATION"] * 2
    + ["ACCIDENT"] * 1
    + ["IDLE_VIOLATION"] * 1
)


def fetch_osrm_route(origin, dest):
    lon1, lat1 = origin
    lon2, lat2 = dest
    url = f"{OSRM_URL}/route/v1/driving/{lon1},{lat1};{lon2},{lat2}?geometries=geojson&overview=full"
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.load(resp)
    # OSRM coordinates are [lon, lat]
    return data["routes"][0]["geometry"]["coordinates"]


def build_events():
    random.seed(SEED)
    now = datetime.now(timezone.utc)
    events, eid = [], 0

    for origin, dest, label in ROUTES:
        coords = fetch_osrm_route(origin, dest)
        n = len(coords)
        for frac in ANCHOR_FRACTIONS:
            center_idx = int(n * frac)
            # window of real route points around the anchor — no synthetic
            # offset, so every event is exactly on the road trace
            window = coords[max(0, center_idx - 30):center_idx + 30]
            if not window:
                continue
            for _ in range(random.randint(8, 14)):
                eid += 1
                lon, lat = random.choice(window)
                occurred = now - timedelta(days=random.uniform(0, 45), hours=random.uniform(0, 23))
                etype = random.choice(EVENT_TYPES_WEIGHTED)
                speed = random.uniform(14, 33) if etype in ("SPEEDING", "HARD_BRAKING", "HARSH_CORNERING") else None
                events.append({
                    "source": "manual",
                    "source_event_id": f"demo-route-{eid:04d}",
                    "event_type": etype,
                    "occurred_at": occurred.isoformat(),
                    "latitude": round(lat, 6),
                    "longitude": round(lon, 6),
                    "vehicle_external_id": f"demo-veh-{random.randint(1, 6)}",
                    "speed_mps": round(speed, 1) if speed else None,
                    "speed_limit_mps": 22.2 if speed else None,  # ~80 km/h highway limit
                    "severity": random.choice(["low", "medium", "high"]),
                    "raw": {"demo": True, "label": label},
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
