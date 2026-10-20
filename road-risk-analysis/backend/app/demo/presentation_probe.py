"""
Rehearsal / verification probe for the presentation simulator: plays the
PHONE (a registered vehicle pinging like viasafe-mobile does: every 3 s,
accuracy 5 m, speed capped at 40 m/s) and polls GET /alerts/{id} like the
app (every 2 s here), printing the collision alerts it gets and a summary
(levels seen, largest group, resolutions).

    # phone standing still (default: Kambo, on the N5)
    docker exec -w /app -e PYTHONPATH=/app roaddemo_api_1 python -m app.demo.presentation_probe
    # phone driving the N5 towards Bafoussam at 25 m/s (the app's "Lent" demo speed)
    … python -m app.demo.presentation_probe --drive 25 --seconds 150

Run the simulator against it with SIM_TARGET_VEHICLE_ID=<the id printed>,
or stop the phone first so the simulator picks the probe (latest live ping).
"""
import argparse
import json
import time
import urllib.request
import uuid

from app.demo import vehicle_simulator as vs
from app.demo.sim_presentation import Path, route

LEVEL_RANK = {"SAFE": 0, "MONITOR": 1, "WARNING": 2, "HIGH_WARNING": 3, "CRITICAL": 4}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", default="4.241399,9.611796", help="lat,lon (stationary) or start of the drive")
    ap.add_argument("--to", default="5.4778,10.4176", help="drive destination lat,lon")
    ap.add_argument("--drive", type=float, default=0.0, help="drive speed m/s (0 = stand still)")
    ap.add_argument("--seconds", type=float, default=150)
    ap.add_argument("--id", default=None)
    args = ap.parse_args()
    vid = args.id or str(uuid.uuid4())
    start = tuple(float(v) for v in args.at.split(","))
    vs._request("POST", "/vehicles/register", {"vehicle_id": vid, "vehicle_type": "private"})
    print(f"probe vehicle {vid} ({'driving %.0f m/s' % args.drive if args.drive else 'stationary'})", flush=True)
    path = None
    if args.drive:
        path, _ = route([start, tuple(float(v) for v in args.to.split(","))])
    t0 = time.monotonic()
    last_ping = -10.0
    seen_levels, max_group, resolved, conflicts = {}, 0, set(), {}
    while (t := time.monotonic() - t0) < args.seconds:
        if t - last_ping >= 3.0:
            if path:
                lat, lon, brg = path.at(args.drive * t)
                spd, hdg = min(args.drive, 40.0), brg
            else:
                (lat, lon), spd, hdg = start, 0.0, 0.0
            vs._request("POST", f"/vehicles/{vid}/ping", {"latitude": lat, "longitude": lon, "speed_mps": spd,
                                                          "heading": hdg, "accuracy_m": 5.0})
            last_ping = t
        alerts = vs._request("GET", f"/alerts/{vid}")
        live = [a for a in alerts if a["status"] != "RESOLVED" and a["level"] != "SAFE"]
        for a in alerts:
            conflicts.setdefault(a["conflict_id"], set()).add(a["level"])
            if a["status"] == "RESOLVED":
                resolved.add(a["conflict_id"])
        for a in live:
            seen_levels.setdefault(a["level"], round(t, 1))
        max_group = max(max_group, len(live))
        desc = ", ".join(
            f"{a['level']}[{(a['vehicle_b_id'] if a['vehicle_a_id'] == vid else a['vehicle_a_id'])[:8]}](ttc {a['time_to_conflict_s'] or 0:.1f}s, cpa {a['minimum_predicted_distance_m'] or 0:.0f}m,"
            f" {a.get('encounter') or '?'})" for a in sorted(live, key=lambda a: -LEVEL_RANK[a["level"]]))
        print(f"t={t:6.1f}s live={len(live)} {desc}", flush=True)
        time.sleep(2.0)
    print("\nSUMMARY")
    print("  first time each level was live:", json.dumps(seen_levels))
    print(f"  largest group (simultaneous live alerts): {max_group}")
    print(f"  conflicts seen: {len(conflicts)}, resolved (units leaving): {len(resolved)}")


if __name__ == "__main__":
    main()
