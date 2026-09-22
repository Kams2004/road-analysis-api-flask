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

Points are deliberately the same Douala-Bafoussam (N5) coordinates already
used by the seeded pothole detections, so the risk-zone halos line up with
the detection clusters on the map instead of appearing in an unrelated spot.

Usage:
    API_BASE_URL=http://185.182.184.188:8080 python -m app.demo.seed_demo_exceptions

Re-running is safe: each event has a unique source_event_id and the
(source, source_event_id) pair is a DB uniqueness constraint, so duplicate
runs are simply skipped (skipped_duplicates in the response), not doubled.
"""
import json
import os
import random
import urllib.request
from datetime import datetime, timedelta, timezone

API_BASE_URL = os.environ.get("API_BASE_URL", "http://api:8080")
SEED = int(os.environ.get("SEED_DEMO_EXCEPTIONS_SEED", "42"))

# Real points along the Douala-Bafoussam (N5) corridor near Chepang/Bandja,
# taken from the already-seeded pothole detections — keeps detection
# clusters and historical-risk zones visually aligned on the same stretch.
CLUSTER_CENTERS = [
    (5.256852, 10.225625, "Chepang"),
    (5.260587, 10.225945, "Chepang"),
    (5.265423, 10.228842, "Chepang / Bandja approach"),
    (5.269182, 10.228443, "Bandja"),
    (5.271007, 10.239335, "Bandja outskirts"),
]

EVENT_TYPES_WEIGHTED = (
    ["SPEEDING"] * 5
    + ["HARD_BRAKING"] * 4
    + ["HARSH_CORNERING"] * 2
    + ["HARD_ACCELERATION"] * 2
    + ["ACCIDENT"] * 1
    + ["IDLE_VIOLATION"] * 1
)


def build_events():
    random.seed(SEED)
    now = datetime.now(timezone.utc)
    events, eid = [], 0
    for lat0, lon0, label in CLUSTER_CENTERS:
        for _ in range(random.randint(8, 14)):
            eid += 1
            dlat = random.uniform(-0.0009, 0.0009)
            dlon = random.uniform(-0.0009, 0.0009)
            occurred = now - timedelta(days=random.uniform(0, 45), hours=random.uniform(0, 23))
            etype = random.choice(EVENT_TYPES_WEIGHTED)
            speed = random.uniform(14, 33) if etype in ("SPEEDING", "HARD_BRAKING", "HARSH_CORNERING") else None
            events.append({
                "source": "manual",
                "source_event_id": f"demo-n5-{eid:04d}",
                "event_type": etype,
                "occurred_at": occurred.isoformat(),
                "latitude": round(lat0 + dlat, 6),
                "longitude": round(lon0 + dlon, 6),
                "vehicle_external_id": f"demo-veh-{random.randint(1, 6)}",
                "speed_mps": round(speed, 1) if speed else None,
                "speed_limit_mps": 22.2 if speed else None,  # ~80 km/h N5 limit
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
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.load(resp)
    print(f"[seed-demo-exceptions] {API_BASE_URL}: {json.dumps(result, indent=2)}")


if __name__ == "__main__":
    main()
