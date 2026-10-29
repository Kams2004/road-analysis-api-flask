"""
End-to-end collision scenarios against a running VIASafe API (default: the online server):
simulated vehicles drive REAL road geometries (OSRM), ping once a second through the public API
(the whole pipeline: map matching → prediction → context → alert → WebSocket), while WebSocket
listeners record every alert with its receive time. See docs/COLLISION-PREVENTION.md.

    python tools/collision_scenarios.py <phone_vehicle_id> P1,P2,P3,P4,F1,F2,F3,F5

  P1  a vehicle arrives fast behind the (stopped) phone           → alert escalating on the phone
  P2  a vehicle approaches slowly and stops 8 m behind (queue)    → no alert
  P3  a stationary vehicle 30 m away with ±8 m GPS jitter         → no alert
  P4  a vehicle on another street ~150 m away                     → no alert
  F1  two vehicles head-on in the same lane                       → HIGH_WARNING / CRITICAL
  F2  two vehicles passing in opposite lanes 3.5 m apart          → at most MONITOR
  F3  two vehicles arriving at the same intersection              → WARNING or more
  F5  the same head-on in a risky place (Limbé) and a quiet one   → context index / reasons differ

The API key is read from mobile/.env.local (EXPO_PUBLIC_API_KEY) and never printed.
Env: VIASAFE_API (default https://api-roadrisk.camtrack.net), PHONE_LAT / PHONE_LON (the phone's
matched road point, default Avenue de l'indépendance, Douala).
"""
import asyncio
import json
import math
import os
import random
import sys
import time
import uuid
from pathlib import Path

import httpx
import websockets

API = os.getenv("VIASAFE_API", "https://api-roadrisk.camtrack.net")
OSRM = "https://router.project-osrm.org"
ENV = Path(__file__).resolve().parents[1] / "mobile" / ".env.local"
KEY = next(l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines() if l.startswith("EXPO_PUBLIC_API_KEY="))
H = {"X-API-Key": KEY, "Content-Type": "application/json"}
R = 6371000.0
ORDER = ["SAFE", "MONITOR", "WARNING", "HIGH_WARNING", "CRITICAL"]


def hav(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    x = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * R * math.asin(math.sqrt(x))


def bearing(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    y = math.sin(lo2 - lo1) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(lo2 - lo1)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def offset(p, dist_m, brg):
    d, b = dist_m / R, math.radians(brg)
    la1, lo1 = math.radians(p[0]), math.radians(p[1])
    la2 = math.asin(math.sin(la1) * math.cos(d) + math.cos(la1) * math.sin(d) * math.cos(b))
    lo2 = lo1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(la1), math.cos(d) - math.sin(la1) * math.sin(la2))
    return (math.degrees(la2), math.degrees(lo2))


async def route(c, pts):
    q = ";".join(f"{lon},{lat}" for lat, lon in pts)
    r = (await c.get(f"{OSRM}/route/v1/driving/{q}",
                     params={"overview": "full", "geometries": "geojson", "steps": "true"})).json()
    rt = r["routes"][0]
    return [(lat, lon) for lon, lat in rt["geometry"]["coordinates"]], rt


def cut(path, length_m, from_end=False):
    """The first (or last) `length_m` of a path."""
    p = list(reversed(path)) if from_end else list(path)
    out, acc = [p[0]], 0.0
    for a, b in zip(p, p[1:]):
        d = hav(a, b)
        if acc + d >= length_m:
            f = (length_m - acc) / d if d else 0
            out.append((a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f))
            break
        out.append(b)
        acc += d
    return list(reversed(out)) if from_end else out


def walk(path, speed_fn, dt=1.0, max_still=20):
    """One sample every dt along `path`: (lat, lon, speed, heading); speed_fn(t, remaining_m) → m/s."""
    out, seg, along, t, done, still = [], 0, 0.0, 0.0, 0.0, 0
    total = sum(hav(a, b) for a, b in zip(path, path[1:]))
    while seg < len(path) - 1 and len(out) < 300:     # hard cap: 5 minutes of pings per vehicle
        a, b = path[seg], path[seg + 1]
        d = hav(a, b)
        f = along / d if d else 1
        v = speed_fn(t, total - done)
        if v < 0.3:                                    # a crawl is a stop (no endless creeping samples)
            v = 0.0
        out.append((a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, v, bearing(a, b)))
        t += dt
        if v <= 0:
            still += 1
            if still >= max_still:
                break
            continue
        done += v * dt
        along += v * dt
        while seg < len(path) - 1 and along >= hav(path[seg], path[seg + 1]):
            along -= hav(path[seg], path[seg + 1])
            seg += 1
    return out


class Recorder:
    def __init__(self):
        self.events, self.tasks = [], []

    async def listen(self, vid):
        url = f"{API.replace('https', 'wss').replace('http', 'ws')}/alerts/ws?vehicle_id={vid}"
        async for ws in websockets.connect(url, additional_headers={"X-API-Key": KEY}):
            try:
                async for raw in ws:
                    m = json.loads(raw)
                    if m.get("type") == "alert":
                        self.events.append((time.time(), vid, m["alert"]))
                    elif m.get("type") == "snapshot":
                        self.events += [(time.time(), vid, a) for a in m.get("alerts", [])]
            except websockets.ConnectionClosed:
                continue

    def start(self, vids):
        self.tasks = [asyncio.create_task(self.listen(v)) for v in vids]

    def stop(self):
        for t in self.tasks:
            t.cancel()


async def register(c, vid, plate=None):
    body = {"vehicle_id": vid, "vehicle_type": "private"}
    if plate:
        body["plate"] = plate
    (await c.post(f"{API}/vehicles/register", headers=H, json=body)).raise_for_status()


async def drive(c, vid, samples, sent, jitter_m=0.0, accuracy=5.0):
    for lat, lon, v, hdg in samples:
        if jitter_m:
            lat, lon = offset((lat, lon), random.uniform(0, jitter_m), random.uniform(0, 360))
        t0 = time.time()
        try:
            r = await c.post(f"{API}/vehicles/{vid}/ping", headers=H, json={
                "latitude": lat, "longitude": lon, "speed_mps": v, "heading": hdg, "accuracy_m": accuracy, "ts": t0})
            sent.append((t0, vid, r.status_code))
        except Exception as e:
            sent.append((t0, vid, str(e)))
        await asyncio.sleep(max(0.0, 1.0 - (time.time() - t0)))


def report(name, rec, sent, involved, expect):
    evs = [(t, v, a) for t, v, a in rec.events if v in involved and a.get("status") != "RESOLVED"]
    top = max((a["level"] for _, _, a in evs), key=ORDER.index, default="NONE")
    best = max((a for _, _, a in evs), key=lambda a: ORDER.index(a["level"]), default=None)
    t0 = min((t for t, *_ in sent), default=time.time())
    first = min((t for t, _, a in evs if ORDER.index(a["level"]) >= 2), default=None)
    print(f"\n=== {name}\n  expected: {expect}")
    print(f"  pings: {len(sent)} (errors {sum(1 for *_, s in sent if s != 204)})  alert messages: {len(evs)}  "
          f"highest level: {top}")
    if best:
        print(f"  physical={best.get('physical_level')} context_index={best.get('context_index')} "
              f"boost={best.get('context_level_boost')} encounter={best.get('encounter')} "
              f"relation={best.get('road_relation')} TTC={best.get('time_to_conflict_s')} "
              f"min_dist={best.get('minimum_predicted_distance_m')} latency_ms={best.get('detection_latency_ms')}")
        print(f"  reasons: {[r.get('code') for r in best.get('context_reasons') or []]}")
    if first:
        print(f"  first WARNING+ {first - t0:.1f} s after the first ping")
    return top


async def main():
    phone, which = sys.argv[1], set(sys.argv[2].split(","))
    phone_pt = (float(os.getenv("PHONE_LAT", "4.022444")), float(os.getenv("PHONE_LON", "9.703328")))
    results = {}
    async with httpx.AsyncClient(timeout=20) as c:
        far, _ = await route(c, [phone_pt, (4.0480, 9.7600)])        # a road leaving the phone's position

        async def run(name, vehicles, involved, expect):
            rec, sent = Recorder(), []
            rec.start(involved)
            await asyncio.sleep(2)
            await asyncio.gather(*(drive(c, vid, smp, sent, **kw) for vid, smp, kw in vehicles))
            await asyncio.sleep(6)
            rec.stop()
            results[name.split()[0]] = report(name, rec, sent, involved, expect)

        async def one(prefix):
            vid = f"sim-{prefix}-{uuid.uuid4().hex[:6]}"
            await register(c, vid)
            return vid

        if "P1" in which:
            path = list(reversed(cut(far, 260)))
            vid = await one("p1")
            await run("P1 fast approach (16 m/s) to the stopped phone",
                      [(vid, walk(path, lambda t, rem: 16.0 if rem > 25 else 0.0), {})], [phone, vid],
                      "alert escalating to HIGH_WARNING/CRITICAL before the vehicle reaches the phone")
        if "P2" in which:
            path = list(reversed(cut(far, 120)))
            vid = await one("p2")
            await run("P2 slow approach, stops 8 m behind (queue)",
                      [(vid, walk(path, lambda t, rem: max(0.0, min(4.0, (rem - 8) / 3))), {})], [phone, vid],
                      "no alert (at most MONITOR)")
        if "P3" in which:
            p = cut(far, 30)[-1]
            vid = await one("p3")
            await run("P3 stationary vehicle 30 m away, GPS jitter ±8 m",
                      [(vid, [(p[0], p[1], 0.0, 0.0)] * 20, {"jitter_m": 8.0, "accuracy": 12.0})], [phone, vid],
                      "no alert")
        if "P4" in which:
            path, _ = await route(c, [offset(phone_pt, 220, 200), offset(phone_pt, 260, 290)])
            dmin = min(hav(q, phone_pt) for q in path)
            vid = await one("p4")
            await run(f"P4 other street (closest point {dmin:.0f} m from the phone), 14 m/s",
                      [(vid, walk(path, lambda t, rem: 14.0), {})], [phone, vid],
                      "no alert (unrelated road)" if dmin > 40 else "the street passes close: MONITOR at most")

        async def head_on(name, path, expect, lane=0.0, speed=13.0):
            a, b = await one("ha"), await one("hb")
            sa, sb = walk(path, lambda t, r: speed), walk(list(reversed(path)), lambda t, r: speed)
            if lane:                                           # each one keeps right of the centre line
                sa = [(*offset((la, lo), lane / 2, (h + 90) % 360), v, h) for la, lo, v, h in sa]
                sb = [(*offset((la, lo), lane / 2, (h + 90) % 360), v, h) for la, lo, v, h in sb]
            n = min(len(sa), len(sb))
            await run(name, [(a, sa[:n], {}), (b, sb[:n], {})], [a, b], expect)

        if "F1" in which:
            await head_on("F1 head-on in the same lane (Douala), 13 m/s each", cut(far, 500),
                          "HIGH_WARNING / CRITICAL")
        if "F2" in which:
            await head_on("F2 passing in opposite lanes 3.5 m apart (Douala)", cut(far, 500),
                          "at most MONITOR (normal passing)", lane=3.5)
        if "F3" in which:
            path, rt = await route(c, [(4.0480, 9.7050), (4.0535, 9.7120)])
            turns = [s for s in rt["legs"][0]["steps"] if s["maneuver"]["type"] in ("turn", "end of road")]
            if turns:
                x = (turns[0]["maneuver"]["location"][1], turns[0]["maneuver"]["location"][0])
                k = min(range(len(path)), key=lambda i: hav(path[i], x))
                in_a = cut(path[:k + 1], 220, from_end=True)
                in_b = list(reversed(cut(path[k:], 220)))
                a, b = await one("xa"), await one("xb")
                sa = walk(in_a + [offset(x, 60, bearing(in_a[-2], x))], lambda t, r: 12.0)
                sb = walk(in_b + [offset(x, 60, bearing(in_b[-2], x))], lambda t, r: 12.0)
                n = min(len(sa), len(sb))
                await run("F3 crossing at an intersection, 12 m/s each", [(a, sa[:n], {}), (b, sb[:n], {})],
                          [a, b], "WARNING or more")
        if "F5" in which:
            limbe, _ = await route(c, [(4.0120, 9.1480), (4.0175, 9.1300)])
            await head_on("F5a head-on in a RISKY place (Limbé, exception history)", cut(limbe, 600),
                          "context_index high, history reasons, level possibly raised one step", speed=10.0)
            quiet, _ = await route(c, [(3.8480, 11.5021), (3.8560, 11.5100)])
            await head_on("F5b same head-on in a QUIET place (Yaoundé)", cut(quiet, 600),
                          "context_index low, no history reasons", speed=10.0)
    print("\nSUMMARY", json.dumps(results))


asyncio.run(main())
