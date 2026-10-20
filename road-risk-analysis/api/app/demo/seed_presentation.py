"""
Presentation seeder: the Bafoussam demo (N5 via Mbanga, Njombé, Loum,
Nkongsamba, Melong, Kékem, Bafang, Bandjoun).

Along the real OSRM routes  Kambo (north of Bonabéri) → Bafoussam  and
Douala (Akwa) → Bafoussam  it creates:

  * community reports (signalements) every ~4–8 km — police, embouteillage,
    accident, travaux, nid_de_poule, danger, obstacle, vehicule_panne,
    route_glissante, animaux, inondation, dos_d_ane, eboulement… — snapped
    on the road (OSRM /nearest), status `actif`, fresh (reported a few
    minutes ago, well inside each type's community TTL), most with a demo
    photo and a few confirmations; all far more than any merge radius apart;
  * AI detections (one finished dashcam job per route) — traffic signs with
    real sign codes where they are plausible (speed_limit_50 entering towns,
    speed_limit_80 leaving them, pedestrian crossings, children/schools,
    stop / give way at junctions, curves, double bends, no overtaking and
    speed_limit_60 on the Nkongsamba–Bafang mountain section), potholes,
    speed bumps in towns, parked vehicles (cars on the roadside, trucks
    blocking a lane). All `validated`, attached to hazards through
    hazard_service.attach (hazard lifecycle runs as for real detections), so
    they are what /detections/along-route returns to the app;
  * historical risk zones (manual source, imported through
    historical_service.import_events) — tight clusters (one 150 m grid cell)
    of different dominant causes and levels, ~15–25 km apart, recent dates.

Tagging (what --purge removes, and nothing else):
  * jobs / detections / signalements: id ends with "-9e5e47a7104e";
    their hazards (and hazard events / votes), reviews, labels, audit rows;
  * historical_events: source_event_id LIKE 'seed-presentation-%';
  * MinIO: detections/<seeded job id>/…, signalements/seed-presentation/….

Run (demo stack, project `roaddemo`):
    docker exec -w /app -e PYTHONPATH=/app roaddemo_api_1 python -m app.demo.seed_presentation
        (default: purge previous presentation seed, seed again, verify)
    … --refresh   re-date the time-sensitive rows (reports, parked vehicles)
                  so they are fresh right before the talk, then verify
    … --verify    only print what the app sees along the routes
    … --purge     remove everything this script created
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from PIL import ImageDraw, ImageFilter
from sqlalchemy import delete, func, or_, select, update

from app.demo import seed_web_test as wt   # registers every mapper; synthetic dashcam scenes
from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.audit_log import AuditLog
from app.models.detection import Detection, LocationSource, LocationStatus, ReviewStatus
from app.models.detection_review import DetectionReview
from app.models.hazard import Hazard, HazardStatus
from app.models.hazard_lifecycle import HazardEvent, HazardVote
from app.models.historical_event import HistoricalEventRecord
from app.models.job import Job, JobStatus, SourceType
from app.models.job_event import JobEvent
from app.models.job_track_point import JobTrackPoint
from app.models.signalement import Signalement, SignalementStatus, SignalementType
from app.models.signalement_vote import SignalementPass, SignalementVote
from app.models.validation_label import ValidationLabel
from app.services import hazard_service, historical_service
from app.services.hazards.lifecycle import service as lc_service
from app.services.hazards.lifecycle.expiry import compute_expires_at
from app.services.hazards.lifecycle.policy import policy_for_hazard
from app.services.historical.grid import grid_cell_id
from app.services.historical.models import HistoricalEvent
from app.services.historical.sources.manual import ManualEventSource
from app.services.minio_service import get_minio, upload_image, upload_signalement_image

TAG = "seed-presentation"
SUFFIX = "9e5e47a7104e"
HIST_PREFIX = "seed-presentation-"
OSRM = settings.OSRM_BASE_URL.rstrip("/")
API = "http://127.0.0.1:8080"           # the API, from inside roaddemo_api_1

KAMBO = (4.2414, 9.6118)
AKWA = (4.0511, 9.7043)
DOUALA_EST = (4.0413, 9.8105)   # the presenter's phone position (Douala-Est); its route crosses the city
BAFOUSSAM = (5.4778, 10.4176)

# Towns on/near the N5 (approximate centres). Only those the route passes
# within 2.5 km of are used; their position along the route is computed.
TOWNS = [
    ("Ndokoti", 4.0459, 9.7353, "big"), ("Deïdo", 4.0640, 9.7080, "small"),
    ("Bonabéri", 4.0760, 9.6720, "small"), ("Mbanga", 4.5017, 9.5672, "big"),
    ("Njombé", 4.5860, 9.6560, "small"), ("Penja", 4.6380, 9.6790, "small"),
    ("Loum", 4.7180, 9.7350, "big"), ("Manjo", 4.8430, 9.8220, "small"),
    ("Nkongsamba", 4.9540, 9.9400, "big"), ("Melong", 5.1210, 9.9580, "small"),
    ("Kékem", 5.1580, 10.0250, "small"), ("Bafang", 5.1570, 10.1800, "big"),
    ("Banka", 5.1450, 10.2100, "small"), ("Baham", 5.3310, 10.3900, "small"),
    ("Bandjoun", 5.3600, 10.4100, "big"), ("Bafoussam", 5.4778, 10.4176, "big"),
]
MOUNTAIN = ("Nkongsamba", "Bafang")   # bends / cliffs between these towns


def sid() -> str:
    h = uuid.uuid4().hex
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{SUFFIX}"


# ── geometry ──────────────────────────────────────────────────────────────────

def hav(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(a))


def osrm_get(path: str) -> dict:
    with urllib.request.urlopen(f"{OSRM}{path}", timeout=60) as r:
        return json.load(r)


class Route:
    def __init__(self, name: str, origin, dest):
        d = osrm_get(f"/route/v1/driving/{origin[1]},{origin[0]};{dest[1]},{dest[0]}"
                     "?overview=full&geometries=geojson&steps=true")
        if d.get("code") != "Ok":
            raise SystemExit(f"OSRM: no route {name}: {d.get('code')}")
        r = d["routes"][0]
        self.name = name
        self.pts = [(la, lo) for lo, la in r["geometry"]["coordinates"]]
        self.cum = [0.0]
        for a, b in zip(self.pts, self.pts[1:]):
            self.cum.append(self.cum[-1] + hav(*a, *b))
        self.length = self.cum[-1]
        self.steps = []          # (start_m, name/ref)
        m = 0.0
        for leg in r["legs"]:
            for s in leg["steps"]:
                label = " ".join(x for x in (s.get("ref"), s.get("name")) if x)
                self.steps.append((m, label))
                m += s["distance"]
        self.towns = []
        for tname, tla, tlo, size in TOWNS:
            best = min(range(0, len(self.pts), 3), key=lambda i: hav(tla, tlo, *self.pts[i]))
            if hav(tla, tlo, *self.pts[best]) <= 2500:
                self.towns.append((self.cum[best], tname, size))
        self.towns.sort()

    def at(self, m: float):
        m = max(0.0, min(m, self.length))
        i = max(1, next((k for k in range(1, len(self.cum)) if self.cum[k] >= m), len(self.cum) - 1))
        (la1, lo1), (la2, lo2) = self.pts[i - 1], self.pts[i]
        seg = self.cum[i] - self.cum[i - 1]
        t = 0 if seg == 0 else (m - self.cum[i - 1]) / seg
        brg = math.degrees(math.atan2((lo2 - lo1) * math.cos(math.radians(la1)), la2 - la1)) % 360
        return la1 + t * (la2 - la1), lo1 + t * (lo2 - lo1), brg

    def project(self, lat, lon):
        """(along_m, offset_m) of the closest route vertex (dense geometry)."""
        best = min(range(len(self.pts)), key=lambda i: hav(lat, lon, *self.pts[i]))
        return self.cum[best], hav(lat, lon, *self.pts[best])

    def road_at(self, m: float) -> str:
        label = ""
        for start, name in self.steps:
            if start <= m:
                label = name or label
            else:
                break
        return label or "N5"

    def town_near(self, m: float):
        if not self.towns:
            return None, 1e9
        d, t = min(((abs(tm - m), (tm, name, size)) for tm, name, size in self.towns), key=lambda x: x[0])
        return t, d

    def place(self, m: float) -> dict:
        t, d = self.town_near(m)
        road = self.road_at(m)
        if road.startswith("N5 ") or road == "N5":
            road_disp = "N5 Douala–Bafoussam"
        else:
            road_disp = road
        if t and d < 1800:
            locality, near = t[1], f"{t[1]}"
        elif t:
            locality, near = None, f"{'avant' if m < t[0] else 'après'} {t[1]}"
        else:
            locality, near = None, ""
        region = "Ouest" if m > 0 and t and t[1] in ("Kékem", "Bafang", "Banka", "Baham", "Bandjoun", "Bafoussam") \
            else "Littoral"
        name = f"{road_disp}, {near}".strip(", ")
        return {"road": road_disp, "locality": locality, "town": t[1] if t else None, "region": region,
                "location_name": name, "display": f"{name} · {region}"}

    def in_mountains(self, m: float) -> bool:
        a = next((tm for tm, n, _ in self.towns if n == MOUNTAIN[0]), None)
        b = next((tm for tm, n, _ in self.towns if n == MOUNTAIN[1]), None)
        return a is not None and b is not None and a + 1500 < m < b - 1500


def snap(lat, lon):
    try:
        d = osrm_get(f"/nearest/v1/driving/{lon:.6f},{lat:.6f}?number=1")
        if d.get("code") == "Ok" and d["waypoints"] and d["waypoints"][0]["distance"] < 40:
            lo, la = d["waypoints"][0]["location"]
            return la, lo
    except Exception:
        pass
    return lat, lon


def offset(lat, lon, bearing, right_m):
    b = math.radians(bearing + 90)
    return (lat + right_m * math.cos(b) / 111_320.0,
            lon + right_m * math.sin(b) / (111_320.0 * math.cos(math.radians(lat))))


def simplify(points, tol=12.0):
    """Douglas–Peucker like the app's simplifyRoute (lib/geo.ts)."""
    if len(points) < 3:
        return points

    def perp(p, a, b):
        ref = math.cos(math.radians(a[0]))
        ax, ay = a[1] * ref * 111_320, a[0] * 111_320
        bx, by = b[1] * ref * 111_320, b[0] * 111_320
        px, py = p[1] * ref * 111_320, p[0] * 111_320
        dx, dy = bx - ax, by - ay
        l2 = dx * dx + dy * dy
        if l2 == 0:
            return math.hypot(px - ax, py - ay)
        return abs(dy * px - dx * py + bx * ay - by * ax) / math.sqrt(l2)

    out, stack = [], [(0, len(points) - 1)]
    keep = {0, len(points) - 1}
    while stack:
        i, j = stack.pop()
        best, idx = 0.0, None
        for k in range(i + 1, j):
            d = perp(points[k], points[i], points[j])
            if d > best:
                best, idx = d, k
        if idx is not None and best > tol:
            keep.add(idx)
            stack += [(i, idx), (idx, j)]
    return [points[k] for k in sorted(keep)]


# ── what to place ─────────────────────────────────────────────────────────────

SIGN_INFO = {  # code → (scene drawing code, P(class))
    "speed_limit_50": "speed_limit_50", "speed_limit_60": "speed_limit_60", "speed_limit_80": "speed_limit_80",
    "stop": "stop", "give_way": "give_way", "no_overtaking": "unknown", "pedestrian_crossing": "unknown",
    "children": "unknown", "curve_left": "unknown", "curve_right": "unknown", "double_bend": "unknown",
    "speed_bump": "unknown", "slippery_road": "unknown", "falling_rocks": "unknown",
}


def detection_plan(route: Route, lo_m: float, hi_m: float, rng: random.Random) -> list:
    """[(m, type, subtype, extra)] for the stretch [lo_m, hi_m] of the route."""
    plan = []
    for n, (tm, name, size) in enumerate(route.towns):
        if not lo_m + 1500 <= tm <= hi_m - 300:
            continue
        plan.append((tm - 1300, "traffic_sign", "speed_limit_50", None))     # entering the town
        if size == "big" or n % 2:
            plan.append((tm - 550, "speed_bump", None, None))
        if size == "big":
            plan += [(tm - 250, "traffic_sign", "pedestrian_crossing", None),
                     (tm + 150, "pothole", None, None),
                     (tm + 60, "traffic_sign", "stop" if n % 2 else "give_way", {"side": 16}),
                     (tm + 800, "parked_vehicle", "roadside", {"cls": "car"})]
            if n % 2 == 0:
                plan.append((tm + 450, "traffic_sign", "children", None))
            if tm + 1500 < hi_m:
                plan.append((tm + 1500, "traffic_sign", "speed_limit_80", None))   # leaving it
    # Open road: potholes, bends, parked trucks
    m = lo_m + 3000
    k = 0
    while m < hi_m - 1500:
        t, d = route.town_near(m)
        if d > 2200:
            if route.in_mountains(m):
                plan.append((m, "traffic_sign", ["curve_right", "double_bend", "no_overtaking", "curve_left",
                                                 "speed_limit_60", "falling_rocks"][k % 6], None))
            elif k % 3 == 0:
                plan.append((m, "pothole", None, None))
            elif k % 3 == 1:
                plan.append((m, "traffic_sign", ["no_overtaking", "curve_left", "slippery_road",
                                                 "curve_right"][(k // 3) % 4], None))
            else:
                plan.append((m, "parked_vehicle", "blocking" if (k // 3) % 2 == 0 else "roadside",
                             {"cls": "truck"}))
            k += 1
            m += rng.uniform(6500, 8500) if route.in_mountains(m) else rng.uniform(10000, 14000)
        else:
            m += 1500
    return [p for p in plan if lo_m <= p[0] <= hi_m]


REPORT_TEXT = {
    "police": ["Contrôle de police, pièces du véhicule vérifiées", "Barrage de gendarmerie, ralentir",
               "Contrôle routier à l'entrée de la ville"],
    "embouteillage": ["Bouchon au marché, circulation au pas", "Ralentissements importants, camions en file"],
    "accident": ["Accident entre un camion et un taxi, une voie bloquée", "Moto renversée sur la chaussée, prudence"],
    "travaux": ["Travaux de réfection, circulation alternée", "Chantier sur la chaussée, rétrécissement à une voie"],
    "nid_de_poule": ["Gros nid de poule dans la voie de droite", "Plusieurs nids de poule profonds"],
    "danger": ["Virage dangereux mal signalé", "Accotement effondré, danger"],
    "obstacle": ["Branches d'arbre sur la chaussée", "Pierres tombées d'un camion sur la route"],
    "vehicule_panne": ["Camion en panne sur la voie, sans triangle", "Bus en panne sur le bas-côté"],
    "route_glissante": ["Chaussée glissante après la pluie", "Boue sur la route dans la descente"],
    "animaux": ["Troupeau de chèvres sur la route", "Bœufs qui traversent la route"],
    "inondation": ["Route partiellement inondée après l'orage", "Eau stagnante sur la chaussée"],
    "dos_d_ane": ["Dos d'âne non signalé", "Ralentisseur très haut"],
    "eboulement": ["Éboulement partiel, une voie dégagée", "Pierres tombées de la falaise"],
    "vehicule_dangereux": ["Camion surchargé roulant au milieu de la route", "Véhicule roulant sans feux"],
}
TOWN_TYPES = ["police", "embouteillage", "dos_d_ane", "accident", "nid_de_poule", "obstacle", "vehicule_panne"]
RURAL_TYPES = ["accident", "travaux", "nid_de_poule", "danger", "animaux", "vehicule_panne", "inondation",
               "police", "obstacle", "vehicule_dangereux"]
MOUNTAIN_TYPES = ["route_glissante", "eboulement", "danger", "accident", "travaux", "vehicule_panne"]


def report_plan(route: Route, lo_m: float, hi_m: float, rng: random.Random) -> list:
    out, m, counters = [], lo_m + rng.uniform(1200, 2500), {}
    while m < hi_m - 800:
        t, d = route.town_near(m)
        pool = TOWN_TYPES if d < 1500 else MOUNTAIN_TYPES if route.in_mountains(m) else RURAL_TYPES
        key = id(pool)
        i = counters.get(key, rng.randrange(len(pool)))
        counters[key] = i + 1
        out.append((m, pool[i % len(pool)]))
        m += rng.uniform(4200, 7800)
    return out


# Historical risk zones: (km along the Kambo route, label, event mix).
# Score = Σ weight × count / 20 (ACCIDENT 5, HARD_BRAKING / HARSH_CORNERING
# 1.5, HARD_ACCELERATION 1.2, SPEEDING 1). App levels: ≥0.7 red, ≥0.5
# orange (entry banner), ≥0.3 moderate (line colour only).
ZONES_KAMBO = [   # (town, offset km from its centre along the route, label, mix)
    ("Mbanga", -14.0, "freinages brusques (avant Mbanga)", {"HARD_BRAKING": 8, "SPEEDING": 3}),   # 15.0 → 0.75
    ("Mbanga", 8.0, "excès de vitesse (Mbanga–Njombé)", {"SPEEDING": 11, "HARD_BRAKING": 1}),     # 12.5 → 0.62
    ("Penja", 4.0, "accidents (Penja–Loum)", {"ACCIDENT": 3, "HARD_BRAKING": 2, "SPEEDING": 1}),  # 19.0 → 0.95
    ("Manjo", -6.0, "freinages + accident (Loum–Manjo)", {"HARD_BRAKING": 5, "ACCIDENT": 1}),     # 12.5 → 0.62
    ("Nkongsamba", 8.0, "virages serrés (après Nkongsamba)", {"HARSH_CORNERING": 5, "SPEEDING": 1}),  # 8.5 → 0.42
    ("Kékem", 6.0, "virages + accident (falaise de Kékem)", {"HARSH_CORNERING": 6, "ACCIDENT": 1,
                                                             "HARD_BRAKING": 1}),                 # 15.5 → 0.78
    ("Bafang", -1.0, "accidents (Bafang)", {"ACCIDENT": 2, "HARD_BRAKING": 4}),                    # 16.0 → 0.80
    ("Baham", -18.0, "excès de vitesse (plateau)", {"SPEEDING": 9, "HARD_ACCELERATION": 2}),      # 11.4 → 0.57
    ("Bandjoun", -2.0, "accidents (entrée de Bandjoun)", {"ACCIDENT": 2, "SPEEDING": 3,
                                                          "HARD_BRAKING": 2}),                    # 16.0 → 0.80
]
# Douala-only stretch (before the routes join): km along the Akwa route.
ZONES_DOUALA = [
    (0.45, "freinages brusques (pont / Bonabéri)", {"HARD_BRAKING": 6, "ACCIDENT": 1}),        # 14.0 → 0.70
]


# ── images ────────────────────────────────────────────────────────────────────

def det_images(rng, dtype, subtype, extra, osd, model_key, label):
    scene = wt.Scene(rng, "rural", osd)
    if dtype == "pothole":
        box = scene.pothole()
    elif dtype == "speed_bump":
        box = scene.speed_bump()
    elif dtype == "traffic_sign":
        box = scene.sign(SIGN_INFO.get(subtype, "unknown"))
    else:
        cls = (extra or {}).get("cls", "car")
        if subtype == "blocking":
            box = (int(scene.vp + 10), 330, int(scene.vp + 260), 520)
        else:
            box = (int(wt.W_PX * 0.72), 360, int(wt.W_PX * 0.93), 520)
        scene.vehicle(box, cls)
    frame = scene.finish().filter(ImageFilter.GaussianBlur(0.6))
    ann = wt.annotate(frame, box, wt.DET_COLOUR[model_key], label)
    return wt.jpeg(ann), wt.jpeg(wt.crop(frame, box), 90), box


def report_image(rng, stype, osd):
    scene = wt.Scene(rng, "rural", osd)
    d = ImageDraw.Draw(scene.img)
    vp = scene.vp
    if stype in ("nid_de_poule",):
        box = scene.pothole()
    elif stype == "dos_d_ane":
        box = scene.speed_bump()
    elif stype in ("accident", "vehicule_panne", "embouteillage", "police", "vehicule_dangereux"):
        boxes = {"embouteillage": [(vp - 200, 330, vp - 20, 460), (vp - 260, 400, vp - 30, 560),
                                   (vp + 20, 340, vp + 190, 470)],
                 "accident": [(vp - 120, 380, vp + 120, 560), (vp + 90, 400, vp + 300, 560)]}
        for b in boxes.get(stype, [(vp + 20, 360, vp + 280, 540)]):
            scene.vehicle(tuple(int(v) for v in b), "truck" if stype in ("vehicule_panne", "vehicule_dangereux")
                          else "car")
        if stype == "police":
            d.rectangle([vp - 200, 380, vp - 170, 520], fill=(40, 60, 110))           # officer
            d.ellipse([vp - 202, 352, vp - 168, 386], fill=(80, 55, 40))
        box = (int(vp - 260), 330, int(vp + 300), 560)
    elif stype in ("travaux", "obstacle", "danger", "eboulement"):
        for i in range(5):
            x = vp - 180 + i * 80
            if stype == "eboulement":
                d.ellipse([x, 470 + (i % 2) * 30, x + 70, 530 + (i % 2) * 30], fill=(120, 110, 100))
            else:
                d.polygon([(x, 560), (x + 50, 560), (x + 25, 470)], fill=(245, 120, 20))
                d.rectangle([x + 12, 515, x + 38, 525], fill=(255, 255, 255))
        box = (int(vp - 200), 450, int(vp + 260), 580)
    elif stype in ("inondation", "route_glissante"):
        d.ellipse([vp - 400, 480, vp + 380, 700], fill=(90, 110, 125))
        box = (int(vp - 400), 480, int(vp + 380), 700)
    elif stype == "animaux":
        for i in range(4):
            x = vp - 160 + i * 90
            d.ellipse([x, 470, x + 70, 520], fill=(120 - i * 10, 90, 60))
            d.ellipse([x + 55, 450, x + 85, 480], fill=(110, 80, 50))
            for lx in (x + 10, x + 50):
                d.line([(lx, 515), (lx, 550)], fill=(60, 40, 30), width=5)
        box = (int(vp - 170), 440, int(vp + 280), 560)
    else:
        box = (int(vp - 200), 300, int(vp + 200), 600)
    return wt.jpeg(wt.crop(scene.finish(), box, 160), 86)


def make_buckets_public():
    client = get_minio()
    for bucket in (settings.MINIO_BUCKET_DETECTIONS, settings.MINIO_BUCKET_SIGNALEMENTS):
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
        client.set_bucket_policy(bucket, json.dumps({
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Principal": {"AWS": ["*"]},
                           "Action": ["s3:GetObject"], "Resource": [f"arn:aws:s3:::{bucket}/*"]}],
        }))


# ── purge ─────────────────────────────────────────────────────────────────────

async def purge(db) -> dict:
    like = f"%-{SUFFIX}"
    job_ids = list((await db.execute(select(Job.id).where(Job.id.like(like)))).scalars().all())
    det_ids = list((await db.execute(select(Detection.id).where(or_(
        Detection.id.like(like), Detection.job_id.in_(job_ids or ["-"]))))).scalars().all())
    hz_ids = set((await db.execute(select(Detection.hazard_id).where(
        Detection.id.in_(det_ids or ["-"]), Detection.hazard_id.isnot(None)))).scalars().all())
    hz_ids |= set((await db.execute(select(HazardEvent.hazard_id).where(or_(
        HazardEvent.job_id.in_(job_ids or ["-"]), HazardEvent.detection_id.in_(det_ids or ["-"]))))).scalars().all())
    full, mixed = [], []
    for hid in hz_ids:
        others = (await db.execute(select(func.count(Detection.id)).where(
            Detection.hazard_id == hid, Detection.id.notin_(det_ids or ["-"])))).scalar()
        (mixed if others else full).append(hid)
    sig_ids = list((await db.execute(select(Signalement.id).where(Signalement.id.like(like)))).scalars().all())
    opts = {"synchronize_session": False}

    def ex(stmt):
        return db.execute(stmt.execution_options(**opts))

    await ex(delete(HazardEvent).where(or_(HazardEvent.hazard_id.in_(full or ["-"]),
                                           HazardEvent.job_id.in_(job_ids or ["-"]),
                                           HazardEvent.detection_id.in_(det_ids or ["-"]))))
    await ex(delete(HazardVote).where(HazardVote.hazard_id.in_(full or ["-"])))
    await ex(update(Detection).where(Detection.id.in_(det_ids or ["-"])).values(hazard_id=None))
    await ex(delete(Hazard).where(Hazard.id.in_(full or ["-"])))
    await ex(delete(DetectionReview).where(DetectionReview.detection_id.in_(det_ids or ["-"])))
    await ex(delete(ValidationLabel).where(ValidationLabel.detection_id.in_(det_ids or ["-"])))
    await ex(delete(Detection).where(Detection.id.in_(det_ids or ["-"])))
    await db.flush()
    for hid in mixed:
        hz = await db.get(Hazard, hid)
        if hz is not None:
            await hazard_service.recompute(db, hz)
    obj_ids = set(det_ids) | set(full) | set(sig_ids) | set(job_ids)
    await ex(delete(AuditLog).where(AuditLog.object_id.in_(obj_ids or {"-"})))
    await ex(delete(JobEvent).where(JobEvent.job_id.in_(job_ids or ["-"])))
    await ex(delete(JobTrackPoint).where(JobTrackPoint.job_id.in_(job_ids or ["-"])))
    await ex(delete(Job).where(Job.id.in_(job_ids or ["-"])))
    await ex(delete(SignalementVote).where(SignalementVote.signalement_id.in_(sig_ids or ["-"])))
    await ex(delete(SignalementPass).where(SignalementPass.signalement_id.in_(sig_ids or ["-"])))
    await ex(delete(Signalement).where(Signalement.id.in_(sig_ids or ["-"])))
    n_hist = (await ex(delete(HistoricalEventRecord).where(
        HistoricalEventRecord.source_event_id.like(f"{HIST_PREFIX}%")))).rowcount
    await db.commit()
    removed = 0
    try:
        client = get_minio()
        for jid in job_ids:
            for obj in client.list_objects(settings.MINIO_BUCKET_DETECTIONS, prefix=f"{jid}/", recursive=True):
                client.remove_object(settings.MINIO_BUCKET_DETECTIONS, obj.object_name)
                removed += 1
        for obj in client.list_objects(settings.MINIO_BUCKET_SIGNALEMENTS, prefix=f"{TAG}/", recursive=True):
            client.remove_object(settings.MINIO_BUCKET_SIGNALEMENTS, obj.object_name)
            removed += 1
    except Exception as exc:
        print(f"  ! MinIO cleanup failed: {exc}")
    out = {"jobs": len(job_ids), "detections": len(det_ids), "hazards_deleted": len(full),
           "hazards_recomputed": len(mixed), "signalements": len(sig_ids), "historical_events": n_hist,
           "minio_objects": removed}
    print(f"[purge] {out}")
    return out


# ── seed ──────────────────────────────────────────────────────────────────────

MODEL_OF = wt.MODEL_OF
LABEL = {"pothole": "Pothole", "speed_bump": "SpeedBump", "parked_vehicle": "Parked"}


async def seed_detections(db, route: Route, lo_m: float, hi_m: float, rng, now: datetime) -> list:
    plan = detection_plan(route, lo_m, hi_m, rng)
    plan.sort(key=lambda p: p[0])
    speed = 22.0                                     # m/s along the recording
    rec_start = now - timedelta(hours=3, minutes=10) - timedelta(seconds=(hi_m - lo_m) / speed)
    job_id = sid()
    duration = (hi_m - lo_m) / speed
    job = Job(id=job_id, filename=f"DEMO_presentation_{route.name}.mp4", status=JobStatus.done,
              enabled_models="pothole,signs,speedbump,vehicles", source_type=SourceType.upload.value,
              source_name=TAG, submitted_by=TAG, total_frames=int(duration * 30), processed=int(duration * 30),
              detections=len(plan), fps=30.0, duration_s=round(duration, 1), frame_skip=6,
              created_at=now - timedelta(hours=2, minutes=40), started_at=now - timedelta(hours=2, minutes=38),
              finished_at=now - timedelta(hours=2, minutes=5))
    db.add(job)
    await db.flush()
    ctx = await hazard_service.load_context(db)
    by_model, conf_sum, made = {}, 0.0, []
    for m, dtype, subtype, extra in plan:
        lat, lon, brg = route.at(m)
        if extra and extra.get("side"):
            lat, lon = offset(lat, lon, brg, extra["side"])
        elif dtype == "parked_vehicle":
            lat, lon = offset(lat, lon, brg, 3.5 if subtype == "roadside" else 1.0)
        else:
            lat, lon = offset(lat, lon, brg, rng.uniform(-1.0, 1.0))
        t = (m - lo_m) / speed
        frame = int(t * 30 / 6) * 6
        if dtype == "parked_vehicle":               # time-sensitive: seen minutes ago
            rec = now - timedelta(minutes=rng.uniform(3, 12))
        else:
            rec = rec_start + timedelta(seconds=t)
        model_key = MODEL_OF[dtype]
        det_id = sid()
        conf = round(rng.uniform(0.78, 0.95), 4)
        verifier = round(rng.uniform(0.88, 0.98), 4) if dtype in ("pothole", "traffic_sign", "parked_vehicle") else None
        cands = None
        ai_details = None
        if dtype == "traffic_sign":
            cands = [{"code": subtype, "score": verifier},
                     {"code": "unknown_sign", "score": round((1 - verifier) * 0.6, 4)},
                     {"code": "not_a_sign", "score": round((1 - verifier) * 0.3, 4)}]
            label = f"{subtype} {conf:.0%} / cls {verifier:.0%}"
        elif dtype == "parked_vehicle":
            cls = (extra or {}).get("cls", "car")
            ai_details = {"verdict": "parked", "vehicle_class": cls, "subtype": subtype,
                          "severity": "high" if subtype == "blocking" else "low",
                          "reasons": ["stationary across several frames while the camera moved",
                                      "in the driving lane" if subtype == "blocking" else "on the shoulder"],
                          "seed": TAG}
            label = f"Parked {subtype} {cls} {conf:.0%} / score {verifier:.0%}"
        else:
            label = f"{LABEL[dtype]} {conf:.0%}" + (f" / cls {verifier:.0%}" if verifier else "")
        osd = f"{wt.local_naive(rec):%Y/%m/%d %H:%M:%S}   N{lat:.5f} E{lon:.5f}   {speed * 3.6:03.0f}KM/H   DEMO-N5"
        img_b, crop_b, box = det_images(rng, dtype, subtype, extra, osd, model_key, label)
        tag = wt.DET_TAG[model_key]
        img_url = upload_image(img_b, f"{job_id}/{model_key}/job-{job_id[:8]}_det-{det_id[:8]}_f{frame:06d}_{tag}.jpg")
        crop_url = upload_image(crop_b, f"{job_id}/{model_key}/crops/det-{det_id[:8]}_f{frame:06d}_crop.jpg")
        geo = route.place(m)
        det = Detection(
            id=det_id, job_id=job_id, type=dtype, subtype=subtype, confidence=conf,
            predicted_type=dtype, predicted_subtype=subtype, frame_number=frame, video_time_s=round(frame / 30, 3),
            bbox_x1=float(box[0]), bbox_y1=float(box[1]), bbox_x2=float(box[2]), bbox_y2=float(box[3]),
            model_name=model_key, model_version=wt.MODEL_VERSIONS[model_key],
            verifier_score=verifier, verifier_model=wt.VERIFIER_VERSIONS.get(dtype) if verifier else None,
            class_candidates=cands, ai_details=ai_details,
            ai_decision="accept", ai_decision_reason="high confidence (presentation seed)",
            raw_gps_text=f"N{lat:.6f} E{lon:.6f} {speed * 3.6:.0f}km/h",
            latitude=round(lat, 7), longitude=round(lon, 7), gps_interpolated=False,
            location_status=LocationStatus.confirmed.value, location_source=LocationSource.ocr.value,
            country="Cameroun", city=geo["town"], location_name=geo["location_name"], road=geo["road"],
            locality=geo["locality"], town=geo["town"], region=geo["region"], display_location=geo["display"],
            geocoded_at=now, speed_kmh=round(speed * 3.6, 1), vehicle_id="DEMO-N5",
            captured_at=wt.local_naive(rec), recorded_at=wt.aware(rec), image_url=img_url, crop_url=crop_url,
            review_status=ReviewStatus.validated, reviewed_by="system", reviewed_at=now - timedelta(hours=2),
            created_at=now - timedelta(hours=2, minutes=20),
        )
        db.add(det)
        db.add(DetectionReview(detection_id=det_id, from_status=None, to_status="validated", action="auto_accept",
                               actor_type="system", actor=TAG, details={"confidence": conf, "seed": TAG}))
        await db.flush()
        await hazard_service.attach(db, det, ctx)
        by_model[model_key] = by_model.get(model_key, 0) + 1
        conf_sum += conf
        made.append(det)
    job.detections_by_model = by_model
    job.avg_confidence = round(conf_sum / max(1, len(made)), 4)
    await db.commit()
    return made


async def seed_reports(db, route: Route, lo_m: float, hi_m: float, rng, now: datetime) -> list:
    made = []
    for i, (m, stype) in enumerate(report_plan(route, lo_m, hi_m, rng)):
        lat, lon, brg = route.at(m)
        lat, lon = snap(*offset(lat, lon, brg, rng.uniform(1.5, 3.5)))
        s_id = sid()
        reported = now - timedelta(minutes=rng.uniform(2, 25))
        image_url = None
        if i % 4 != 3:                          # three reports out of four come with a photo
            osd = f"{wt.local_naive(reported):%Y/%m/%d %H:%M}"
            image_url = upload_signalement_image(report_image(rng, stype, osd), f"{TAG}/{s_id}.jpg")
        conf_n = rng.choice([0, 1, 2, 2, 3, 4, 6])
        geo = route.place(m)
        sig = Signalement(
            id=s_id, type=SignalementType(stype), status=SignalementStatus.actif,
            latitude=round(lat, 7), longitude=round(lon, 7),
            description=rng.choice(REPORT_TEXT[stype]), image_url=image_url, reported_by=TAG,
            location_name=geo["location_name"],
            blocked_bearing=round(brg, 1) if stype in ("accident", "travaux", "eboulement") else None,
            confirmations=conf_n, present_count=conf_n, not_there_votes=0,
            last_confirmed_at=(now - timedelta(minutes=rng.uniform(0.5, 2))) if conf_n else None,
            reported_at=reported, last_reported_at=reported, report_count=1,
        )
        db.add(sig)
        made.append(sig)
    await db.commit()
    return made


async def seed_zones(db, kambo: Route, douala: Route, rng, now: datetime) -> list:
    """Historical risk clusters, each confined to the grid cell of its anchor."""
    cell_size = settings.HISTORICAL_GRID_CELL_SIZE_M
    # cells already carrying events near the route (other seeders) — keep away from them
    existing = await historical_service.get_zone_risk_profiles_along_route(
        simplify(kambo.pts), 60, db)
    busy = [(z.center_latitude, z.center_longitude) for z in existing]
    town_m = {n: m for m, n, _ in kambo.towns}
    anchors = [(kambo, town_m[t] + km * 1000, label, mix) for t, km, label, mix in ZONES_KAMBO if t in town_m]
    join_m = douala_join_m(douala, kambo)
    anchors += [(douala, frac * join_m, label, mix) for frac, label, mix in ZONES_DOUALA]
    events, out, eid = [], [], 0
    for route, m0, label, mix in anchors:
        m = m0
        for _ in range(40):                       # slide forward until clear of other seeders' cells
            la, lo, _ = route.at(m)
            if all(hav(la, lo, *b) > 600 for b in busy):
                break
            m += 300
        alat, alon, _ = route.at(m)
        cell = grid_cell_id(alat, alon, cell_size)
        cands = [route.at(m + k)[:2] for k in range(-50, 51, 5)]
        cands = [(la, lo) for la, lo in cands if grid_cell_id(la, lo, cell_size) == cell] or [(alat, alon)]
        for etype, count in mix.items():
            for _ in range(count):
                eid += 1
                la, lo = rng.choice(cands)
                when = now - timedelta(days=rng.uniform(1, 75), hours=rng.uniform(0, 23))
                spd = rng.uniform(17, 27) if etype in ("SPEEDING", "HARD_BRAKING", "HARSH_CORNERING") else None
                events.append(HistoricalEvent(
                    source="manual", source_event_id=f"{HIST_PREFIX}{eid:04d}", event_type=etype,
                    occurred_at=when.replace(tzinfo=timezone.utc), latitude=round(la, 6), longitude=round(lo, 6),
                    vehicle_external_id=f"demo-fleet-{rng.randint(1, 14)}",
                    speed_mps=round(spd, 1) if spd else None, speed_limit_mps=16.7 if spd else None,
                    severity="high" if etype == "ACCIDENT" else "medium",
                    raw={"demo": True, "seed": TAG, "zone": label},
                ))
        out.append((route.name, m, label, cell))
        busy.append((alat, alon))
    start = (now - timedelta(days=80)).date()
    summary = await historical_service.import_events(ManualEventSource(events), start, now.date(), db)
    print(f"[zones] historical import: inserted={summary.inserted} skipped={summary.skipped_duplicates} "
          f"errors={len(summary.errors)}")
    return out


def douala_join_m(douala: Route, *others: Route) -> float:
    """Distance along `douala` where it joins any of `others` (60 m corridor)."""
    grid = {}
    for o in others:
        for q in o.pts:
            grid.setdefault((round(q[0], 3), round(q[1], 3)), []).append(q)
    for i in range(0, len(douala.pts), 3):
        p = douala.pts[i]
        k = (round(p[0], 3), round(p[1], 3))
        near = [q for dl in (-0.001, 0, 0.001) for dn in (-0.001, 0, 0.001)
                for q in grid.get((round(k[0] + dl, 3), round(k[1] + dn, 3)), [])]
        if any(hav(*p, *q) < 60 for q in near):
            return douala.cum[i]
    return douala.length


# ── refresh ───────────────────────────────────────────────────────────────────

async def refresh(db, rng) -> None:
    """Re-date what expires: every presentation report (community TTL), and the
    parked vehicles (2 h blocking / 6 h roadside / 24 h trucks) — their hazards
    are re-opened if the expiry sweep resolved them."""
    now = datetime.utcnow()
    like = f"%-{SUFFIX}"
    sigs = (await db.execute(select(Signalement).where(Signalement.id.like(like)))).scalars().all()
    for s in sigs:
        rep = now - timedelta(minutes=rng.uniform(2, 20))
        s.reported_at = rep
        s.last_reported_at = rep
        s.status = SignalementStatus.actif
        s.absent_count = 0
        s.not_there_votes = 0
        s.uncertain = False
        s.ignore_count = 0
        if s.confirmations:
            s.last_confirmed_at = now - timedelta(minutes=rng.uniform(0.5, 2))
    cfg = await lc_service.load_settings(db, fresh=True)
    dets = (await db.execute(select(Detection).where(Detection.id.like(like),
                                                     Detection.type == "parked_vehicle"))).scalars().all()
    reopened = 0
    for d in dets:
        rec = now - timedelta(minutes=rng.uniform(1, 8))
        d.recorded_at = wt.aware(rec)
        d.captured_at = wt.local_naive(rec)
        if d.review_status == ReviewStatus.resolved:
            d.review_status = ReviewStatus.validated
            d.resolved_at = None
    await db.flush()
    hz_ids = {d.hazard_id for d in dets if d.hazard_id}
    for hid in hz_ids:
        hz = await db.get(Hazard, hid)
        if hz is None:
            continue
        members = await hazard_service.members_of(db, hz.id)
        if hz.status != HazardStatus.active.value:
            hz.status = HazardStatus.active.value
            hz.resolved_at = hz.resolved_by = hz.resolution_note = hz.resolution_source = None
            reopened += 1
        latest = max((m.recorded_at for m in members if m.recorded_at), default=None)
        hz.last_seen_at = latest.astimezone(timezone.utc).replace(tzinfo=None) if latest else now
        hz.last_detected_at = hz.last_seen_at
        hz.absence_count = 0
        hz.expires_at = compute_expires_at(hz.last_seen_at, policy_for_hazard(cfg, hz, members).expiry_hours)
    await db.commit()
    print(f"[refresh] {len(sigs)} reports re-dated, {len(dets)} parked vehicles re-seen "
          f"({len(hz_ids)} hazards, {reopened} re-opened)")


# ── verify (what the app sees) ────────────────────────────────────────────────

def api_post(path: str, body: dict):
    headers = {"Content-Type": "application/json"}
    if os.environ.get("API_KEY"):            # servers with AUTH_ENFORCED: same key as the mobile app / simulator
        headers["X-API-Key"] = os.environ["API_KEY"]
    req = urllib.request.Request(f"{API}{path}", data=json.dumps(body).encode(), method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def verify(routes: list) -> None:
    for route in routes:
        wps = simplify(route.pts)
        body = {"waypoints": [{"latitude": la, "longitude": lo} for la, lo in wps]}
        dets = api_post("/detections/along-route", body)["items"]
        sigs = api_post("/signalements/along-route", body)["items"]
        zones = api_post("/historical/risk/route", {"waypoints": [[la, lo] for la, lo in wps], "corridor_m": 60})
        mine_d = [d for d in dets if d["id"].endswith(SUFFIX)]
        mine_s = [s for s in sigs if s["id"].endswith(SUFFIX)]
        print(f"\n=== {route.name}: {route.length / 1000:.1f} km, {len(wps)} waypoints (simplified like the app) ===")
        print(f"detections along route: {len(dets)} (presentation seed: {len(mine_d)})")
        by = {}
        for d in dets:
            k = f"{d['type']}:{d.get('subtype') or '-'}"
            by[k] = by.get(k, 0) + 1
        for k in sorted(by):
            print(f"   {k:<34}{by[k]:>3}")
        print(f"reports along route: {len(sigs)} (presentation seed: {len(mine_s)})")
        by = {}
        for s in sigs:
            by[s["type"]] = by.get(s["type"], 0) + 1
        for k in sorted(by):
            print(f"   {k:<34}{by[k]:>3}")
        if mine_s:
            exp = min(s.get("expires_at") or "" for s in mine_s)
            print(f"   earliest expiry among presentation reports: {exp} UTC")
        print(f"risk zones along route (score >= 0.3): ")
        for z in sorted(zones, key=lambda z: route.project(z["center_latitude"], z["center_longitude"])[0]):
            if z["historical_risk_score"] < 0.3:
                continue
            km = route.project(z["center_latitude"], z["center_longitude"])[0] / 1000
            s = z["historical_risk_score"]
            lvl = "HIGH(3)" if s >= 0.7 else "MEDIUM(2)" if s >= 0.5 else "MODERATE(1)"
            causes = ", ".join(f"{e['event_type']}×{e['count']}" for e in
                               sorted(z["events_by_type"], key=lambda e: -e["count"]))
            print(f"   km {km:6.1f}  score {s:.2f} {lvl:<12} {z['total_events']:>2} ev  {causes}")


# ── main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    ap = argparse.ArgumentParser(description="Bafoussam presentation seed")
    ap.add_argument("--purge", action="store_true", help="remove the presentation seed and exit")
    ap.add_argument("--refresh", action="store_true", help="re-date reports / parked vehicles, then verify")
    ap.add_argument("--verify", action="store_true", help="only verify")
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--seed", type=int, default=2026)
    args = ap.parse_args()
    rng = random.Random(args.seed)

    async with AsyncSessionLocal() as db:
        if args.purge:
            await purge(db)
            return
        if args.refresh:
            await refresh(db, rng)
    if args.refresh or args.verify:
        verify([Route("Kambo→Bafoussam", KAMBO, BAFOUSSAM), Route("Douala(Akwa)→Bafoussam", AKWA, BAFOUSSAM),
                Route("Douala-Est→Bafoussam", DOUALA_EST, BAFOUSSAM)])
        return

    kambo = Route("Kambo→Bafoussam", KAMBO, BAFOUSSAM)
    douala = Route("Douala(Akwa)→Bafoussam", AKWA, BAFOUSSAM)
    est = Route("Douala-Est→Bafoussam", DOUALA_EST, BAFOUSSAM)
    join = douala_join_m(douala, kambo)
    join_est = douala_join_m(est, kambo, douala)
    print(f"{kambo.name}: {kambo.length / 1000:.1f} km; towns: "
          + ", ".join(f"{n}@{m / 1000:.0f}km" for m, n, _ in kambo.towns))
    print(f"{douala.name}: {douala.length / 1000:.1f} km; joins the Kambo route at km {join / 1000:.1f}")
    print(f"{est.name}: {est.length / 1000:.1f} km; joins the Akwa/Kambo routes at km {join_est / 1000:.1f}")
    make_buckets_public()
    now = datetime.utcnow().replace(microsecond=0)
    async with AsyncSessionLocal() as db:
        await purge(db)
        dk = await seed_detections(db, kambo, 0, kambo.length, rng, now)
        dd = await seed_detections(db, douala, 300, max(300, join - 200), rng, now) if join > 2500 else []
        rk = await seed_reports(db, kambo, 0, kambo.length, rng, now)
        rd = await seed_reports(db, douala, 0, max(0, join - 1500), rng, now) if join > 3000 else []
        if join_est > 2500:
            dd += await seed_detections(db, est, 300, join_est - 200, rng, now)
            rd += await seed_reports(db, est, 0, max(0, join_est - 1500), rng, now)
        zones = await seed_zones(db, kambo, douala, rng, now)
    print(f"[seed] detections: {len(dk)} on {kambo.name}, {len(dd)} on the Douala stretch; "
          f"reports: {len(rk)} + {len(rd)}; risk zones: {len(zones)}")
    if not args.no_verify:
        verify([kambo, douala, est])


if __name__ == "__main__":
    asyncio.run(main())
