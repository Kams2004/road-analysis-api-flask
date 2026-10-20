"""
Presentation mode of the demo vehicle simulator (SIM_MODE=presentation).

Instead of roaming around the phone, the fake units come at the phone in
WAVES (every SIM_WAVE_SECONDS, default 40 s) on the real road network, each
unit scripted to hold one collision-alert level for a while, then break off
(alert clears) — so within one wave the app shows MONITOR, WARNING,
HIGH_WARNING and CRITICAL, a grouped alarm (several units at once) and the
units leaving. Waves loop forever and are re-planned around the phone's
latest live position, so they follow it during a demo drive.

How a level is held (thresholds: Settings → Collision prevention; defaults
CRITICAL ≤15 m & TTC ≤4 s, HIGH ≤30 m & ≤8 s, WARNING ≤60 m & ≤15 s, MONITOR
≤120 m; prediction horizon 15 s): every tick the unit sets its speed so the
CLOSING speed c along the road toward the phone gives the wanted
closest-approach geometry under the constant-velocity prediction (the
simulator reports acceleration 0):
  MONITOR   c = (gap − 90) / 15   → closest approach ≈ 90 m at the horizon
  WARNING   c = gap / 11.5        → TTC ≈ 11.5 s, paths meet
  HIGH      c = gap / 6           → TTC ≈ 6 s
  CRITICAL  constant SIM_PASS_MPS → passes the phone (head-on / overtaking)
capped at SIM_CRUISE_MPS while far away (so levels escalate naturally).
After SIM_HOLD_SECONDS at its level a unit breaks off: it pulls away / falls
back / turns around (separating → the alert resolves at once), then roams
until the next wave reuses it.

Geometry, from the phone's last ping (GET /vehicles/live):
  * phone (nearly) stationary: each unit gets an OSRM route from a point
    ~600 m away (bearings spread around the phone) TO the phone, i.e. it
    arrives on the phone's own road from either side; the CRITICAL unit's
    route continues through the phone and beyond (passes it).
  * phone moving (≥ SIM_MOVING_MPS, e.g. the app's "Lent" demo drive at
    ~25 m/s): one OSRM route behind → phone → 4 km ahead along its heading;
    MONITOR / HIGH units drive ahead of it in the same direction (it closes
    in from behind), WARNING closes from behind, CRITICAL comes head-on on
    the same road (reversed route) and passes.
"""
import json
import math
import os
import random
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

from app.demo import vehicle_simulator as vs

WAVE_S = float(os.environ.get("SIM_WAVE_SECONDS", "40"))
WAVE_MOVING_S = float(os.environ.get("SIM_WAVE_SECONDS_MOVING", "25"))   # a driving phone outruns a wave sooner
UNITS_PER_WAVE = int(os.environ.get("SIM_UNITS_PER_WAVE", "4"))
POOL = int(os.environ.get("SIM_POOL", str(max(8, UNITS_PER_WAVE * 2))))
TICK_S = float(os.environ.get("SIM_TICK_SECONDS", "1"))
CRUISE = float(os.environ.get("SIM_CRUISE_MPS", "11"))       # approach speed while far (closing)
PASS = float(os.environ.get("SIM_PASS_MPS", "13"))           # CRITICAL unit's closing speed
HOLD_S = float(os.environ.get("SIM_HOLD_SECONDS", "13"))     # time spent at its level before breaking off
STAGGER_S = float(os.environ.get("SIM_STAGGER_SECONDS", "4"))
MOVING_MPS = float(os.environ.get("SIM_MOVING_MPS", "4"))
TARGET_ID = os.environ.get("SIM_TARGET_VEHICLE_ID") or None
MAX_UNIT_MPS = 38.0
SIM_PREFIX = "sim-"      # simulated unit ids — never taken for "the phone" by any simulator instance

# wave script: (level, delay index) — extra units (UNITS_PER_WAVE > 4) repeat WARNING / HIGH
LEVELS = ["MONITOR", "WARNING", "HIGH", "CRITICAL"]
TYPES = {"MONITOR": "bus", "WARNING": "taxi", "HIGH": "truck", "CRITICAL": "moto"}
GAP0 = {"MONITOR": 300.0, "WARNING": 260.0, "HIGH": 230.0, "CRITICAL": 230.0}


def log(msg: str) -> None:
    print(f"[presentation {datetime.now():%H:%M:%S}] {msg}", flush=True)


# ── geometry ──────────────────────────────────────────────────────────────────

class Path:
    def __init__(self, pts):
        self.pts = pts
        self.cum = [0.0]
        for a, b in zip(pts, pts[1:]):
            self.cum.append(self.cum[-1] + vs._haversine_m(*a, *b))
        self.length = self.cum[-1]

    def at(self, m: float):
        m = max(0.0, min(m, self.length))
        lo, hi = 1, len(self.cum) - 1
        while lo < hi:                               # first index with cum >= m
            mid = (lo + hi) // 2
            if self.cum[mid] >= m:
                hi = mid
            else:
                lo = mid + 1
        i = lo
        a, b = self.pts[i - 1], self.pts[i]
        seg = self.cum[i] - self.cum[i - 1]
        t = 0.0 if seg <= 0 else (m - self.cum[i - 1]) / seg
        return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, vs._bearing_to(*a, *b)

    def project(self, lat: float, lon: float):
        """(along_m, offset_m) of the closest point of the polyline."""
        ref = math.cos(math.radians(lat))
        best = (0.0, float("inf"))
        px, py = lon * ref * 111_320.0, lat * 111_320.0
        for i in range(1, len(self.pts)):
            (la1, lo1), (la2, lo2) = self.pts[i - 1], self.pts[i]
            ax, ay = lo1 * ref * 111_320.0, la1 * 111_320.0
            bx, by = lo2 * ref * 111_320.0, la2 * 111_320.0
            dx, dy = bx - ax, by - ay
            l2 = dx * dx + dy * dy
            t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
            d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
            if d < best[1]:
                best = (self.cum[i - 1] + t * (self.cum[i] - self.cum[i - 1]), d)
        return best

    def reversed_from(self, m: float) -> "Path":
        """The way back from distance m to the start."""
        la, lo, _ = self.at(m)
        pts = [(la, lo)] + [p for p, c in zip(reversed(self.pts), reversed(self.cum)) if c < m]
        return Path(pts if len(pts) >= 2 else [(la, lo), self.pts[0]])


def route(points):
    coords = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in points)
    try:
        data = vs._request("GET", f"/route/v1/driving/{coords}?overview=full&geometries=geojson",
                           base_url=vs.OSRM_BASE_URL)
    except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
        log(f"OSRM request failed: {e}")
        return None, None
    if data.get("code") != "Ok" or not data.get("routes"):
        return None, None
    r = data["routes"][0]
    pts = [(lat, lon) for lon, lat in r["geometry"]["coordinates"]]
    return (Path(pts) if len(pts) >= 2 else None), [leg["distance"] for leg in r["legs"]]


# ── the phone ─────────────────────────────────────────────────────────────────

class Phone:
    def __init__(self, item: dict):
        self.id = item["vehicle_id"]
        self.lat, self.lon = item["latitude"], item["longitude"]
        self.speed = item.get("speed_mps") or 0.0
        self.heading = item.get("heading")
        self.matched = None          # map-matched position (backend trajectory), when known
        try:
            ts = datetime.fromisoformat(item["ts"].replace("Z", "+00:00"))
            self.age = max(0.0, (datetime.now(timezone.utc) - ts).total_seconds())
        except (KeyError, ValueError):
            self.age = 0.0

    @property
    def moving(self) -> bool:
        return self.speed >= MOVING_MPS and self.heading is not None

    def now_pos(self):
        """Dead-reckoned position now (pings arrive every ~3 s)."""
        if not self.moving:
            return self.lat, self.lon
        return vs._destination(self.lat, self.lon, self.heading, self.speed * min(self.age, 4.0))


_current_phone = None


def find_phone(own_ids: set):
    """The phone to play against: SIM_TARGET_VEHICLE_ID, else the phone already
    followed while it stays live, else the most recently pinging live vehicle
    that is not a simulated unit (ids starting with "sim-")."""
    global _current_phone
    try:
        live = vs._request("GET", "/vehicles/live")
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        log(f"/vehicles/live unreachable: {e}")
        return None
    items = [v for v in live.get("items", [])
             if v["vehicle_id"] not in own_ids and not v["vehicle_id"].startswith(SIM_PREFIX)]
    if TARGET_ID:
        items = [v for v in items if v["vehicle_id"] == TARGET_ID]
    if not items:
        return None
    current = [v for v in items if v["vehicle_id"] == _current_phone]
    if current:
        return Phone(current[0])
    items.sort(key=lambda v: v["ts"], reverse=True)
    _current_phone = items[0]["vehicle_id"]
    log(f"following phone {_current_phone}")
    return Phone(items[0])


# ── units ─────────────────────────────────────────────────────────────────────

class Unit:
    def __init__(self, vtype: str):
        self.id = f"{SIM_PREFIX}{uuid.uuid4()}"
        self.vtype = vtype
        self.path = None
        self.along = 0.0
        self.speed = 0.0
        self.heading = 0.0
        self.lat = self.lon = None
        self.state = "idle"          # idle | approach | leaving | roam
        self.level = None
        self.level_since = None
        self.state_since = 0.0
        self.passed = False
        self.last_sign = None
        self.rel = None              # leaving: speed kept at phone speed + rel (moving phone)

    def register(self):
        vs._request("POST", "/vehicles/register", {"vehicle_id": self.id, "vehicle_type": self.vtype})

    def ping(self):
        body = {"latitude": self.lat, "longitude": self.lon, "speed_mps": round(self.speed, 2),
                "heading": round(self.heading, 1), "acceleration_mps2": 0.0}
        try:
            vs._request("POST", f"/vehicles/{self.id}/ping", body)
        except urllib.error.HTTPError as e:
            if e.code == 404:              # DB reset since we registered — register again
                self.register()
                vs._request("POST", f"/vehicles/{self.id}/ping", body)
            else:
                raise

    def place(self):
        self.lat, self.lon, self.heading = self.path.at(self.along)

    def assign(self, level: str, path: Path, along: float):
        self.state, self.level, self.path, self.along = "approach", level, path, along
        self.level_since, self.passed, self.last_sign, self.rel = None, False, None, None
        self.speed = CRUISE
        self.state_since = time.monotonic()
        self.place()

    def leave(self, v_ph: float, direction: float, why: str):
        """Break off: pull away / fall back on the same road (speed kept relative
        to the phone's), or turn around when that would mean reversing."""
        if v_ph - direction * 8.0 < 3.0:
            self.path = self.path.reversed_from(self.along)
            self.along = 0.0
            self.speed = 9.0
            self.rel = None
        else:
            self.rel = -direction * 8.0
            self.speed = min(MAX_UNIT_MPS, v_ph + self.rel)
        self.state = "leaving"
        self.state_since = time.monotonic()
        log(f"unit {self.id[:8]} {self.level:<8} breaks off ({why})")

    def roam(self, phone):
        """Drive away from the phone to a random point 1–1.5 km off, at cruise speed."""
        base = (self.lat, self.lon)
        brg = random.uniform(0, 360)
        if phone is not None:
            brg = (vs._bearing_to(phone.lat, phone.lon, *base) + random.uniform(-50, 50)) % 360
        dest = vs._destination(*base, brg, random.uniform(1000, 1500))
        p, _ = route([base, dest])
        if p is not None and p.length > 100:
            self.path, self.along, self.speed = p, 0.0, random.uniform(8, 12)
        self.state = "roam"
        self.rel = None
        self.state_since = time.monotonic()

    def advance(self, dt: float):
        if self.path is None:
            return
        self.along = min(self.path.length, self.along + self.speed * dt)
        self.place()

    def _phone_on_path(self, phone):
        """(signed gap phone−unit along the path, lateral offset, phone speed along the path)."""
        plat, plon = phone.now_pos()
        p_along, p_off = self.path.project(plat, plon)
        v_ph = 0.0
        if phone.moving:
            brg = self.path.at(p_along)[2]
            v_ph = phone.speed * math.cos(math.radians((phone.heading - brg + 540) % 360 - 180))
        return p_along - self.along, p_off, v_ph

    def tick(self, dt: float, phone, now: float):
        if self.state == "approach":
            if phone is None:
                self.roam(None)
                return
            s, p_off, v_ph = self._phone_on_path(phone)
            direction = 1.0 if s >= 0 else -1.0
            gap = abs(s)
            if p_off > 80:
                self.leave(v_ph, direction, f"phone left this road ({p_off:.0f} m off)")
            elif self.level == "CRITICAL":
                if self.last_sign is not None and direction != self.last_sign:
                    self.passed = True
                self.last_sign = direction
                if not self.passed:
                    self.speed = max(4.0, min(MAX_UNIT_MPS, v_ph + direction * PASS))
                elif gap > 25:
                    self.state, self.state_since = "leaving", now
                    self.rel = None if self.speed < 6 else self.speed - v_ph
                    log(f"unit {self.id[:8]} CRITICAL  passed the phone — leaving")
            else:
                if self.level == "MONITOR":
                    c_level = (gap - 90.0) / 15.0
                elif self.level == "WARNING":
                    c_level = gap / 11.5
                else:
                    c_level = gap / 6.0
                if c_level <= CRUISE and self.level_since is None:
                    self.level_since = now
                c = max(0.9, min(CRUISE, c_level))
                self.speed = max(0.0, min(MAX_UNIT_MPS, v_ph + direction * c))
                too_close = gap < {"HIGH": 22.0, "WARNING": 30.0}.get(self.level, 0.0) and self.level_since is not None
                if too_close or (self.level_since is not None and now - self.level_since >= HOLD_S):
                    self.leave(v_ph, direction, f"held {HOLD_S:.0f} s, gap {gap:.0f} m")
                elif self.along >= self.path.length - 1:
                    self.leave(v_ph, direction, "end of its road")
        elif self.state == "leaving":
            if self.rel is not None and phone is not None and phone.moving:
                _, _, v_ph = self._phone_on_path(phone)
                self.speed = max(3.0, min(MAX_UNIT_MPS, v_ph + self.rel))
            limit = 25 if phone is None or not phone.moving else 10 ** 9
            if self.along >= self.path.length - 1 or now - self.state_since > limit:
                self.roam(phone)
        elif self.state in ("roam", "idle"):
            if self.path is None or self.along >= self.path.length - 1:
                self.roam(phone)
        self.advance(dt)


# ── wave planning ─────────────────────────────────────────────────────────────

def phone_road_bearing(phone: Phone):
    """Bearing of the phone's road at its position (its predicted trajectory /
    road state from the API), or its heading when moving."""
    if phone.moving:
        return phone.heading
    for path, key in ((f"/trajectory/{phone.id}", "travel_bearing_deg"),
                      (f"/road-state/{phone.id}", None)):
        try:
            data = vs._request("GET", path)
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError):
            continue
        if key and data.get("matched_latitude") is not None:
            phone.matched = (data["matched_latitude"], data["matched_longitude"])
        val = data.get(key) if key else (data.get("road_context") or {}).get("road_bearing_deg")
        if val is not None:
            return float(val)
    return phone.heading


def road_through_phone(phone: Phone, bearing):
    """OSRM route  behind → phone → ahead  along `bearing` (the phone's own
    road), with the phone's distance along it — or (None, None)."""
    U = (phone.lat, phone.lon)
    tries = [bearing] if bearing is not None else []
    tries += [random.uniform(0, 360) for _ in range(3)]
    for b in tries:
        ahead_m = 4000 if phone.moving else 900
        p, legs = route([vs._destination(*U, (b + 180) % 360, 1500 if phone.moving else 900), U,
                         vs._destination(*U, b, ahead_m)])
        if p is None or len(legs) < 2:
            continue
        u_along = legs[0]
        if u_along < 250 or p.length - u_along < 250:
            continue
        x, y = p.at(u_along - 80), p.at(u_along + 80)
        if vs._haversine_m(x[0], x[1], y[0], y[1]) < 100:      # U-turn at the phone: not a through road
            continue
        return p, u_along
    return None, None


def _predicted_through(path: Path, a: float, target):
    """Mimic the backend's prediction for a unit at `a` on `path` (OSRM route
    150 m behind → 750 m ahead along its heading, app/modules/m03_maps/osrm/
    map_matching.py): the predicted road Path if it passes the target, else None."""
    lat, lon, h = path.at(a)
    win, _ = route([vs._destination(lat, lon, (h + 180) % 360, 150), vs._destination(lat, lon, h, 750)])
    if win is None:
        return None
    s_along, s_off = win.project(lat, lon)
    p_along, p_off = win.project(*target)
    if s_off < 15 and p_off < 12 and p_along - s_along > 30:
        return win
    return None


def predictable_start(path: Path, along: float, phone: Phone, u: float):
    """The backend predicts a unit along that window route; in a street grid it
    can leave the road the unit is on, and then the prediction misses the
    phone. Prefer a start whose prediction passes the phone (the unit then
    drives exactly that road); else accept `path` if its last ~70 m before
    the phone are predicted through it (the unit escalates late, e.g. after
    turning into the phone's side street). Returns (Path, along) or (None, 0)."""
    target = phone.matched or (phone.lat, phone.lon)
    for shift in (0.0, -40.0, 40.0):
        a = max(0.0, min(path.length, along + shift))
        win = _predicted_through(path, a, target)
        if win is not None:
            lat, lon, _ = path.at(a)
            return win, win.project(lat, lon)[0]
    for late in (60.0, 45.0, 75.0):
        if u - late > along and _predicted_through(path, u - late, target) is not None:
            return path, along
    return None, 0.0


def arrival_routes(phone: Phone) -> list:
    """Candidate roads to the phone, [(Path, phone along)]: through-routes
    (in from one side, out the other) for 8 bearings, then plain arrivals."""
    out, U = [], (phone.lat, phone.lon)
    b0 = random.uniform(0, 45)
    for k in range(8):
        b = b0 + k * 45
        p, legs = route([vs._destination(*U, (b + 180) % 360, 600), U, vs._destination(*U, b, 600)])
        if p is not None and legs and len(legs) == 2 and legs[0] > 150 and legs[1] > 150:
            x, y = p.at(legs[0] - 50), p.at(legs[0] + 50)
            if vs._haversine_m(x[0], x[1], y[0], y[1]) > 50:
                out.append((p, legs[0]))
    for k in range(8):
        p, _ = route([vs._destination(*U, b0 + k * 45, 600), U])
        if p is not None and p.length > 300:
            out.append((p, p.length))
    return out


def plan_wave(phone: Phone, units: list, now: float) -> int:
    """Every unit drives on the phone's own road (so the paths are 'connected'
    for the conflict detector), placed so they arrive one after another:
      MONITOR  from ahead (head-on), stops short ~90 m out
      WARNING  from behind (closing in on the phone)
      HIGH     from ahead, holds a ~6 s time-to-conflict
      CRITICAL from behind (stationary phone) / head-on (moving phone), passes
    A moving phone is caught up from behind by WARNING and closes in on the
    MONITOR / HIGH units driving ahead of it in its direction."""
    levels = [LEVELS[i] if i < 4 else ("WARNING", "HIGH")[i % 2] for i in range(UNITS_PER_WAVE)]
    free = sorted([u for u in units if u.state != "approach"], key=lambda u: u.state != "roam")
    if len(free) < len(levels):
        log(f"only {len(free)} free units for {len(levels)} — wave reduced")
        levels = levels[:len(free)]
    path, u_along = road_through_phone(phone, phone_road_bearing(phone))
    if path is None:
        log("no through road at the phone — skipping this wave")
        return 0
    rev = Path(list(reversed(path.pts)))
    u_rev = path.length - u_along
    k = max(1.0, phone.speed / 12.0) if phone.moving else 1.0
    arrivals = None
    for i, level in enumerate(levels):
        extra = i * STAGGER_S * (PASS if level == "CRITICAL" else CRUISE)   # staggered arrivals, nobody waits
        if phone.moving:
            if level == "CRITICAL":                                  # head-on, passes
                p, along = rev, max(0.0, u_rev - min(650.0, (phone.speed + PASS) * 16 + i * 10))
            elif level == "WARNING":                                 # closes in from behind
                p, along = path, max(0.0, u_along - min(450.0, 200.0 * k + extra))
            else:                                                    # ahead, same direction
                p, along = path, min(path.length, u_along + min(650.0, GAP0[level] * k + extra))
        else:
            side_rev = level in ("MONITOR", "HIGH")
            sides = [(rev, u_rev), (path, u_along)] if side_rev else [(path, u_along), (rev, u_rev)]
            if arrivals is None:
                arrivals = arrival_routes(phone)
            p, along = None, 0.0
            for cand, u in sides + arrivals[i % max(1, len(arrivals)):] + arrivals[:i % max(1, len(arrivals))]:
                p, along = predictable_start(cand, max(0.0, u - GAP0[level] - extra), phone, u)
                if p is not None:
                    break
            if p is None:                        # no start whose prediction passes the phone: plain road
                p, along = sides[0][0], max(0.0, sides[0][1] - GAP0[level] - extra)
                log(f"{level}: no start predicted through the phone — plain road")
        free[i].assign(level, p, along)
    log(f"wave (phone {'moving %.0f m/s, heading %.0f°' % (phone.speed, phone.heading) if phone.moving else 'stationary'}"
        f" at {phone.lat:.5f},{phone.lon:.5f}): " + ", ".join(f"{u.level}:{u.id[:8]}" for u in free[:len(levels)]))
    return len(levels)


def main():
    log(f"starting — pool {POOL}, {UNITS_PER_WAVE} units/wave every {WAVE_S:.0f} s, tick {TICK_S} s, "
        f"cruise {CRUISE} m/s, pass {PASS} m/s, hold {HOLD_S} s; API {vs.API_BASE_URL}, OSRM {vs.OSRM_BASE_URL}"
        + (f"; target {TARGET_ID}" if TARGET_ID else ""))
    types = ["bus", "taxi", "truck", "moto", "private", "taxi", "moto", "private"]
    units = [Unit(types[i % len(types)]) for i in range(POOL)]
    for u in units:
        for attempt in range(60):
            try:
                u.register()
                break
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                log(f"waiting for backend ({e})")
                time.sleep(2)
    own = {u.id for u in units}
    wave_at = 0.0
    wave_anchor = None
    last = time.monotonic()
    while True:
        now = time.monotonic()
        dt = now - last
        last = now
        phone = find_phone(own)
        if phone is None:
            if int(now) % 15 == 0:
                log("no phone live — waiting for a phone to ping")
            time.sleep(TICK_S)
            continue
        jumped = wave_anchor is not None and vs._haversine_m(*wave_anchor, phone.lat, phone.lon) > 3000
        if now >= wave_at or jumped or all(u.lat is None for u in units):
            for u in units:
                if u.state == "approach" and jumped:
                    u.state = "roam"
            if plan_wave(phone, units, now):
                wave_at = now + (WAVE_MOVING_S if phone.moving else WAVE_S)
                wave_anchor = (phone.lat, phone.lon)
            else:
                wave_at = now + 5
        for u in units:
            if u.lat is None:            # never placed yet: start it roaming ~1.2 km from the phone
                u.lat, u.lon = vs._destination(phone.lat, phone.lon, random.uniform(0, 360), 1200)
                u.roam(phone)
            try:
                u.tick(dt, phone, now)
                u.ping()
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                log(f"unit {u.id[:8]} ping failed: {e}")
        time.sleep(max(0.05, TICK_S - (time.monotonic() - now)))
