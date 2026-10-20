"""
Test-data seeder for the VIASafe WEB CONSOLE (hazard tracking, validation
screen, AI reasoning, clustering zones, cloud imports, community reports).

Everything is replayed through the real services, in chronological order,
with a virtual clock (see VirtualClock below):

  * jobs (dashcam videos) drive real OSRM routes in Douala, Yaoundé, Edéa (N3)
    and Bafoussam; their trajectory is stored as job_track_points;
  * detections get their initial status from ai_validation.decide /
    rule_reject (detection_workflow.record_creation history), accepted ones
    join hazards through hazard_service.attach (radius from the Cluster
    Configuration zones); human reviews go through detection_workflow
    (accept / correct / reject / location fix);
  * parked vehicles are judged by the real parked-vehicle pipeline
    (detection.parked_vehicle prepare → OSRM /nearest → finish) on synthetic
    tracks built with the pipeline's own camera model, so ai_details
    (verdict, score components, reasons, features, …) is the real output;
  * hazard lifecycle: video_pass.evaluate_job after each job (sightings and
    "not seen" passes), expiry.sweep at the virtual expiry times, driver
    votes (lifecycle.votes.cast_vote), manual resolutions (lifecycle.resolve);
  * community reports clustered/merged like POST /signalements, votes via
    signalement_votes.cast_vote; a demo cloud connection (disabled) with two
    finished import batches.

Images are synthetic road scenes (PIL) uploaded to MinIO with the
processor's bucket / key layout.

Tagging (what --purge removes):
  * every row id this script creates ends with "-5eed7e575eed" (jobs,
    detections, signalements, cloud connection / batches / items, driver
    users) — hazards, events, votes, reviews, labels, audit rows, track
    points and job events are found through them;
  * cluster zones: created_by = "seed-web-test";
  * MinIO: detections/<seeded job id>/…, signalements/seed-web-test/….
The console user testeur@viasafe.local is kept by --purge (use
--purge-users to drop it too). Its password is read from stdin
(--tester-password-stdin); it is never printed.

Run (demo stack, project `roaddemo`):
    docker exec -i -w /app -e PYTHONPATH=/app roaddemo_api_1 \\
        python -m app.demo.seed_web_test --tester-password-stdin < infra/local/web-test-credentials.txt
Options: --purge (remove only), --no-verify, --seed N.
Default = purge previous seed, then seed again, then verify.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import io
import math
import pkgutil
import random
import sys
import uuid
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont
from sqlalchemy import delete, func, insert, or_, select, update

warnings.filterwarnings("ignore", category=DeprecationWarning)

import app.models as _models_pkg  # noqa: E402
for _m in pkgutil.iter_modules(_models_pkg.__path__):        # register every mapper / FK target
    importlib.import_module(f"app.models.{_m.name}")

from app.api.deps import Principal  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.session import AsyncSessionLocal  # noqa: E402
from app.models.audit_log import AuditLog  # noqa: E402
from app.models.auth_token import AuthSession, SessionStage  # noqa: E402
from app.models.cloud_connection import CloudConnection  # noqa: E402
from app.models.cloud_import import CloudImportBatch, CloudImportItem  # noqa: E402
from app.models.cluster_zone import ClusterZone  # noqa: E402
from app.models.detection import Detection, LocationSource, LocationStatus, ReviewStatus  # noqa: E402
from app.models.detection_review import DetectionReview  # noqa: E402
from app.models.hazard import Hazard, HazardStatus  # noqa: E402
from app.models.hazard_lifecycle import HazardEvent, HazardVote  # noqa: E402
from app.models.job import Job, JobStatus  # noqa: E402
from app.models.job_event import JobEvent  # noqa: E402
from app.models.job_track_point import JobTrackPoint  # noqa: E402
from app.models.signalement import Signalement, SignalementStatus, SignalementType  # noqa: E402
from app.models.signalement_vote import SignalementPass, SignalementVote  # noqa: E402
from app.models.user import AccountType, Role, User, UserStatus  # noqa: E402
from app.models.validation_label import ValidationLabel  # noqa: E402
from app.services import (  # noqa: E402
    ai_validation, audit, detection_workflow as wf, hazard_service, model_registry, signalement_votes,
)
from app.services.clustering import boundaries, service as clustering_service  # noqa: E402
from app.services.detection import parked_vehicle as pv  # noqa: E402
from app.services.detection.parked_vehicle import geometry as pv_geo  # noqa: E402
from app.services.hazards.lifecycle import (  # noqa: E402
    events as lc_events, expiry as lc_expiry, recurring as lc_recurring, service as lc_service,
    video_pass, votes as lc_votes,
)
from app.services.minio_service import get_minio, upload_image, upload_signalement_image  # noqa: E402
from app.services.osrm.client import get_osrm_client  # noqa: E402
from app.services.osrm.geo import destination, haversine_m  # noqa: E402
from app.services.settings_service import get_section  # noqa: E402
from app.services.signalement_service import find_merge_target  # noqa: E402

TAG = "seed-web-test"
SUFFIX = "5eed7e575eed"
JOB_LABEL = "DEMO · web test"
TESTER_EMAIL = "testeur@viasafe.local"
DRIVERS = [("chauffeur1.webtest@viasafe.local", "Jean-Paul Mbarga"),
           ("chauffeur2.webtest@viasafe.local", "Aïcha Bello"),
           ("chauffeur3.webtest@viasafe.local", "Serge Ngono")]
TZ = ZoneInfo("Africa/Douala")
FPS = 30.0
STEP = 6                     # sampled every 6th frame (0.2 s)
W_PX, H_PX = 1280, 720
NOW = datetime.utcnow().replace(microsecond=0)


def sid() -> str:
    """uuid4 whose last group marks the row as seeded."""
    h = uuid.uuid4().hex
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{SUFFIX}"


def is_seeded(row_id: Optional[str]) -> bool:
    return bool(row_id) and row_id.endswith(SUFFIX)


def local_day(days_ago: int, hhmm: str) -> datetime:
    """UTC naive instant of HH:MM Douala time, `days_ago` days before today."""
    today = datetime.now(TZ).date() - timedelta(days=days_ago)
    h, m = map(int, hhmm.split(":"))
    return datetime(today.year, today.month, today.day, h, m, tzinfo=TZ).astimezone(timezone.utc).replace(tzinfo=None)


def aware(utc_naive: datetime) -> datetime:
    return utc_naive.replace(tzinfo=timezone.utc).astimezone(TZ)


def local_naive(utc_naive: datetime) -> datetime:
    return aware(utc_naive).replace(tzinfo=None)


# ── virtual clock ─────────────────────────────────────────────────────────────
# The lifecycle / workflow services read datetime.utcnow() for "now" (expiry
# checks, resolved_at, reviewed_at, event created_at). To replay ten days of
# history through them, their module-level `datetime` is swapped for a
# subclass whose utcnow() returns the virtual clock; rows whose timestamps
# come from model column defaults are back-dated by `settle()` afterwards.

class VirtualClock:
    now: Optional[datetime] = None


CLOCK = VirtualClock()


class _ClockDT(datetime):
    @classmethod
    def utcnow(cls):  # type: ignore[override]
        return CLOCK.now or datetime.utcnow()


for _mod in (hazard_service, wf, lc_service, lc_votes, lc_recurring, lc_events, lc_expiry, signalement_votes):
    _mod.datetime = _ClockDT  # type: ignore[attr-defined]


class Ctx:
    """Run-wide state."""

    def __init__(self, rng: random.Random):
        self.rng = rng
        self.tester: Optional[User] = None
        self.tester_actor: Optional[wf.Actor] = None
        self.drivers: list[User] = []
        self.labels: set[str] = {"system", TESTER_EMAIL}
        self.user_ids: set[str] = set()
        self.job_ids: list[str] = []
        self.routes: dict[str, "Route"] = {}
        self.jobs: dict[str, "JobRun"] = {}
        self.dets: dict[str, Detection] = {}       # spec name → detection
        self.spot_shift: dict[tuple, float] = {}
        self.existing_hazards: list[tuple[str, float, float]] = []
        self.policy: Optional[ai_validation.Policy] = None
        self.pvs = None
        self.pv_version = ""
        self.connection: Optional[CloudConnection] = None
        self.warnings: list[str] = []
        self.counts: dict[str, int] = {}

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)
        print(f"  ! {msg}")


async def settle(db, ctx: Ctx, marker: datetime, when: datetime) -> None:
    """Back-date the rows written since `marker` (wall clock) to `when`."""
    await db.flush()
    opts = {"synchronize_session": False}
    await db.execute(update(DetectionReview).where(DetectionReview.created_at >= marker)
                     .values(created_at=when).execution_options(**opts))
    await db.execute(update(ValidationLabel).where(ValidationLabel.labeled_at >= marker)
                     .values(labeled_at=when).execution_options(**opts))
    await db.execute(update(AuditLog).where(
        AuditLog.created_at >= marker,
        or_(AuditLog.actor_label.in_(ctx.labels), AuditLog.actor_id.in_(ctx.user_ids or {"-"})))
        .values(created_at=when).execution_options(**opts))
    await db.execute(update(HazardEvent).where(HazardEvent.created_at >= marker)
                     .values(created_at=when).execution_options(**opts))
    await db.execute(update(Hazard).where(Hazard.created_at >= marker)
                     .values(created_at=when).execution_options(**opts))
    await db.execute(update(Hazard).where(Hazard.updated_at >= marker)
                     .values(updated_at=when).execution_options(**opts))
    if ctx.job_ids:
        await db.execute(update(JobEvent).where(JobEvent.at >= marker, JobEvent.job_id.in_(ctx.job_ids))
                         .values(at=when).execution_options(**opts))


class at_time:
    """async with at_time(db, ctx, when): run real services at virtual time `when`."""

    def __init__(self, db, ctx: Ctx, when: datetime):
        self.db, self.ctx, self.when = db, ctx, when

    async def __aenter__(self):
        self.marker = datetime.utcnow()
        CLOCK.now = self.when
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is None:
            await settle(self.db, self.ctx, self.marker, self.when)
            await self.db.commit()
        CLOCK.now = None
        return False


async def advance(db, ctx: Ctx, until: datetime) -> None:
    """Run the periodic expiry sweep at each expiry instant before `until`."""
    for _ in range(200):
        e = (await db.execute(select(func.min(Hazard.expires_at)).where(
            Hazard.status == HazardStatus.active.value, Hazard.expires_at <= until))).scalar()
        if e is None:
            return
        when = min(e + timedelta(minutes=3), until)
        async with at_time(db, ctx, when):
            n = await lc_expiry.sweep(db, now=when)
        if n == 0:
            return


# ── geography ─────────────────────────────────────────────────────────────────

ROUTE_POINTS = {
    # Bonabéri → Pont du Wouri → Bonanjo → Akwa → Deido → Ndokoti
    "D": [(4.0745, 9.6620), (4.0420, 9.6930), (4.0505, 9.7015), (4.0620, 9.7110), (4.0470, 9.7386)],
    # Deido → Bépanda → Makepe
    "M": [(4.0620, 9.7110), (4.0760, 9.7470)],
    # Mvan → centre → Mokolo → Bastos
    "Y": [(3.8290, 11.5180), (3.8640, 11.5190), (3.8730, 11.4990), (3.8920, 11.5080)],
    # Edéa (D58 → N3 through the town)
    "E": [(3.8050, 10.0950), (3.8000, 10.1330), (3.7950, 10.1700)],
    # Bafoussam, Tamdja → Marché A → Djeleng
    "B": [(5.4600, 10.4100), (5.4780, 10.4180), (5.4950, 10.4250)],
}
ROUTE_CITY = {"D": "douala", "M": "douala", "Y": "yaounde", "E": "edea", "B": "bafoussam"}

# (quartier, arrondissement, lat, lon)
QUARTERS = {
    "douala": [("Bonabéri", "Douala IVe", 4.0745, 9.6625), ("Bonassama", "Douala IVe", 4.0700, 9.6760),
               ("Bonanjo", "Douala Ier", 4.0420, 9.6930), ("Bali", "Douala Ier", 4.0400, 9.7040),
               ("Akwa", "Douala Ier", 4.0505, 9.7015), ("Deido", "Douala Ier", 4.0620, 9.7110),
               ("Bessengue", "Douala Ier", 4.0540, 9.7140), ("New Bell", "Douala IIe", 4.0380, 9.7170),
               ("Bépanda", "Douala Ve", 4.0640, 9.7290), ("Ndokoti", "Douala IIIe", 4.0460, 9.7380),
               ("Makepe", "Douala Ve", 4.0760, 9.7470)],
    "yaounde": [("Mvan", "Yaoundé IVe", 3.8290, 11.5180), ("Nsam", "Yaoundé IIIe", 3.8420, 11.5170),
                ("Centre administratif", "Yaoundé Ier", 3.8640, 11.5190), ("Mokolo", "Yaoundé IIe", 3.8730, 11.4990),
                ("Briqueterie", "Yaoundé IIe", 3.8800, 11.5050), ("Bastos", "Yaoundé Ier", 3.8920, 11.5080)],
    "edea": [("Pongo", "Edéa Ier", 3.8040, 10.1100), ("Centre-ville", "Edéa Ier", 3.8000, 10.1330),
             ("Ekité", "Edéa IIe", 3.7950, 10.1650)],
    "bafoussam": [("Tamdja", "Bafoussam Ier", 5.4620, 10.4110), ("Marché A", "Bafoussam Ier", 5.4780, 10.4180),
                  ("Djeleng", "Bafoussam IIe", 5.4950, 10.4250)],
}
CITY_INFO = {"douala": ("Douala", "Littoral"), "yaounde": ("Yaoundé", "Centre"),
             "edea": ("Edéa", "Littoral"), "bafoussam": ("Bafoussam", "Ouest")}


def _clean_name(name: Optional[str], ref: Optional[str]) -> Optional[str]:
    if name:
        name = name.split("(")[0].strip()
        if name:
            return name
    if ref:
        return ref.replace(" ", "").replace("N", "N", 1)
    return None


class Route:
    def __init__(self, key: str, data: dict, steps_data: Optional[dict] = None, city: Optional[str] = None,
                 ref: Optional[str] = None):
        r = data["routes"][0]
        self.key = key
        self.city = city or ROUTE_CITY[key]
        self.ref = ref
        self.pts = [(lat, lon) for lon, lat in r["geometry"]["coordinates"]]
        self.cum = [0.0]
        for a, b in zip(self.pts, self.pts[1:]):
            self.cum.append(self.cum[-1] + haversine_m(a[0], a[1], b[0], b[1]))
        self.length = self.cum[-1]
        # road names along the route from the OSRM steps
        sr = (steps_data or data)["routes"][0]
        if abs(sr["distance"] - r["distance"]) > 0.02 * r["distance"]:
            sr = {"distance": r["distance"], "legs": []}         # other path: no step names
        scale = self.length / max(sr["distance"], 1.0)
        self.names: list[tuple[float, float, Optional[str]]] = []
        acc = 0.0
        for leg in sr.get("legs", []):
            for s in leg.get("steps", []):
                d = s["distance"] * scale
                self.names.append((acc, acc + d, _clean_name(s.get("name"), s.get("ref"))))
                acc += d

    def at(self, m: float) -> tuple[float, float, float]:
        """(lat, lon, bearing) at `m` metres from the start."""
        m = max(0.0, min(m, self.length - 0.01))
        lo, hi = 0, len(self.cum) - 1
        while lo < hi - 1:
            mid = (lo + hi) // 2
            if self.cum[mid] <= m:
                lo = mid
            else:
                hi = mid
        (la1, lo1), (la2, lo2) = self.pts[lo], self.pts[hi]
        seg = self.cum[hi] - self.cum[lo]
        t = 0.0 if seg <= 0 else (m - self.cum[lo]) / seg
        return la1 + t * (la2 - la1), lo1 + t * (lo2 - lo1), video_pass.bearing(la1, lo1, la2, lo2)

    def road_at(self, m: float) -> Optional[str]:
        best, best_d = None, None
        for a, b, name in self.names:
            if not name:
                continue
            d = 0.0 if a <= m <= b else min(abs(m - a), abs(m - b))
            if best_d is None or d < best_d:
                best, best_d = name, d
        return best if best_d is not None and best_d <= 400 else self.ref


def address(route: Route, m: Optional[float], lat: float, lon: float) -> dict:
    if route.city == "rural":
        return rural_address(route, m, lat, lon)
    town, region = CITY_INFO[route.city]
    q = min(QUARTERS[route.city], key=lambda x: haversine_m(lat, lon, x[2], x[3]))
    locality, district = q[0], q[1]
    road = route.road_at(m) if m is not None else None
    parts = [p for p in (road, locality, town) if p]
    return {"road": road, "locality": locality, "district": district, "town": town, "region": region,
            "country": "Cameroun", "display": ", ".join(parts),
            "location_name": ", ".join(p for p in (road, locality, region, "Cameroun") if p), "city": town}


def offset(lat: float, lon: float, bearing: float, ahead: float, right: float) -> tuple[float, float]:
    if ahead:
        lat, lon = destination(lat, lon, bearing % 360 if ahead > 0 else (bearing + 180) % 360, abs(ahead))
    if right:
        lat, lon = destination(lat, lon, (bearing + (90 if right > 0 else -90)) % 360, abs(right))
    return lat, lon


# ── synthetic images ─────────────────────────────────────────────────────────

HORIZON = int(0.45 * H_PX)
DET_COLOUR = {"pothole": (255, 0, 0), "signs": (255, 255, 255), "speedbump": (255, 0, 255), "vehicles": (255, 140, 0)}
DET_TAG = {"pothole": "pothole", "signs": "sign", "speedbump": "speedbump", "vehicles": "vehicle"}
MODEL_OF = {"pothole": "pothole", "traffic_sign": "signs", "speed_bump": "speedbump", "parked_vehicle": "vehicles"}
MODEL_VERSIONS = {"pothole": "model_pothole.pt@f73664cdaa85", "signs": "model_signs.pt@f3106984ae96",
                  "speedbump": "model_speedbump.pt@121a513bee77", "vehicles": "model_vehicles.pt@9c2e41d07a55"}
VERIFIER_VERSIONS = {"pothole": "pothole_classifier.pt@5b8d0e7f12aa", "traffic_sign": "signs_classifier.pt@e04a7c93b1d6"}


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:                       # Pillow < 10.1
        return ImageFont.load_default()


FONT_OSD, FONT_LABEL, FONT_SIGN, FONT_SIGN_S = _font(22), _font(18), _font(26), _font(15)


def road_halfwidth(y: float, vp_x: float) -> float:
    return 6 + (y - HORIZON) / (H_PX - HORIZON) * 585


class Scene:
    """A dashcam-like frame: sky, buildings, laterite shoulders, asphalt, lane marks, OSD strip."""

    def __init__(self, rng: random.Random, city: str, osd: str, night: bool = False):
        self.rng = rng
        self.night = night
        self.vp = W_PX / 2 + rng.uniform(-25, 25)
        img = Image.new("RGB", (W_PX, H_PX))
        d = ImageDraw.Draw(img)
        for y in range(HORIZON):                       # sky gradient
            t = y / HORIZON
            d.line([(0, y), (W_PX, y)], fill=(int(120 + 90 * t), int(165 + 60 * t), int(215 + 25 * t)))
        if city in ("bafoussam", "edea", "rural"):       # hills / forest on the horizon
            for _ in range(7):
                cx, r = rng.uniform(0, W_PX), rng.uniform(80, 220)
                d.ellipse([cx - r, HORIZON - r * 0.45, cx + r, HORIZON + r * 0.3],
                          fill=(rng.randint(40, 70), rng.randint(95, 125), rng.randint(45, 70)))
        for side in (-1, 1):                            # buildings
            x = self.vp + side * 60
            while 0 < x < W_PX:
                w, h = rng.uniform(50, 150), rng.uniform(40, 170 if city not in ("edea", "rural") else 60)
                col = rng.choice([(200, 190, 170), (225, 215, 190), (170, 160, 150), (215, 180, 140),
                                  (190, 200, 205), (235, 225, 210)])
                x0, x1 = (x, x + w) if side > 0 else (x - w, x)
                d.rectangle([x0, HORIZON - h, x1, HORIZON + 4], fill=col)
                for wy in range(int(HORIZON - h + 10), HORIZON - 8, 22):
                    for wx in range(int(x0 + 8), int(x1 - 10), 24):
                        d.rectangle([wx, wy, wx + 10, wy + 12], fill=(70, 80, 95))
                x += side * (w + rng.uniform(4, 30))
        d.rectangle([0, HORIZON, W_PX, H_PX], fill=(168, 98, 62))          # laterite
        for _ in range(60):                                                # grass tufts
            gx, gy = rng.uniform(0, W_PX), rng.uniform(HORIZON + 5, H_PX)
            d.ellipse([gx - 14, gy - 5, gx + 14, gy + 5], fill=(85, rng.randint(110, 140), 55))
        vp = self.vp
        d.polygon([(vp - 6, HORIZON), (vp + 6, HORIZON), (W_PX - 50, H_PX), (50, H_PX)], fill=(84, 86, 90))
        noise = Image.effect_noise((W_PX, H_PX), 22).convert("RGB")
        img = Image.blend(img, noise, 0.06)
        d = ImageDraw.Draw(img)
        for y0 in range(HORIZON + 6, H_PX, 1):                              # centre dashes + edge lines
            t = (y0 - HORIZON) / (H_PX - HORIZON)
            if int((t ** 0.5) * 18) % 2 == 0:
                d.line([(vp - 1 - 4 * t, y0), (vp + 1 + 4 * t, y0)], fill=(235, 235, 225))
            hw = road_halfwidth(y0, vp)
            d.line([(vp - hw * 0.97, y0), (vp - hw * 0.97 + 2 + 5 * t, y0)], fill=(230, 230, 220))
            d.line([(vp + hw * 0.97 - 2 - 5 * t, y0), (vp + hw * 0.97, y0)], fill=(230, 230, 220))
        self.img = img
        self.osd = osd

    def finish(self) -> Image.Image:
        if self.night:                      # dark scene, headlight cone on the road, lit signs keep some light
            dark = ImageEnhance.Brightness(self.img).enhance(0.28)
            dark = Image.blend(dark, Image.new("RGB", dark.size, (10, 14, 40)), 0.25)
            mask = Image.new("L", self.img.size, 0)
            md = ImageDraw.Draw(mask)
            md.polygon([(self.vp - 40, HORIZON + 20), (self.vp + 60, HORIZON + 20), (W_PX - 120, H_PX), (120, H_PX)],
                       fill=200)
            md.ellipse([W_PX * 0.62, 150, W_PX * 0.98, 520], fill=120)
            mask = mask.filter(ImageFilter.GaussianBlur(60))
            self.img = Image.composite(ImageEnhance.Brightness(self.img).enhance(0.9), dark, mask)
        d = ImageDraw.Draw(self.img)
        d.rectangle([0, 0, W_PX, 40], fill=(0, 0, 0))
        d.text((14, 8), self.osd, fill=(255, 255, 255), font=FONT_OSD)
        return self.img

    # objects — each returns its pixel box
    def pothole(self) -> tuple[int, int, int, int]:
        rng, d = self.rng, ImageDraw.Draw(self.img)
        y = rng.uniform(470, 640)
        hw = road_halfwidth(y, self.vp)
        cx = self.vp + rng.uniform(-0.55, 0.55) * hw
        w = hw * rng.uniform(0.22, 0.34)
        h = w * rng.uniform(0.28, 0.4)
        box = (cx - w / 2, y - h / 2, cx + w / 2, y + h / 2)
        d.ellipse([box[0] - 4, box[1] - 3, box[2] + 4, box[3] + 3], fill=(120, 112, 100))
        pts = [(cx + math.cos(a) * w / 2 * rng.uniform(0.75, 1.0), y + math.sin(a) * h / 2 * rng.uniform(0.7, 1.0))
               for a in [i * math.pi / 8 for i in range(16)]]
        d.polygon(pts, fill=(38, 36, 34))
        d.ellipse([cx - w * 0.2, y - h * 0.1, cx + w * 0.25, y + h * 0.3], fill=(60, 72, 82))   # water
        return tuple(int(v) for v in box)  # type: ignore[return-value]

    def speed_bump(self) -> tuple[int, int, int, int]:
        rng, d = self.rng, ImageDraw.Draw(self.img)
        y = rng.uniform(480, 600)
        hw = road_halfwidth(y, self.vp) * 0.96
        th = 14 + (y - HORIZON) * 0.07
        x0, x1 = self.vp - hw, self.vp + hw
        d.rectangle([x0, y - th / 2, x1, y + th / 2], fill=(110, 110, 105))
        n = 14
        for i in range(n):
            if i % 2 == 0:
                d.polygon([(x0 + (x1 - x0) * i / n, y + th / 2), (x0 + (x1 - x0) * (i + 0.7) / n, y + th / 2),
                           (x0 + (x1 - x0) * (i + 1) / n, y - th / 2), (x0 + (x1 - x0) * (i + 0.3) / n, y - th / 2)],
                          fill=(235, 200, 30))
        return (int(x0), int(y - th / 2 - 3), int(x1), int(y + th / 2 + 3))

    def sign(self, code: str) -> tuple[int, int, int, int]:
        rng, d = self.rng, ImageDraw.Draw(self.img)
        s = rng.uniform(70, 105)
        cx = self.vp + road_halfwidth(560, self.vp) * rng.uniform(0.62, 0.78)
        top = rng.uniform(170, 240)
        if code == "not_a_sign":                       # an advertising board
            w, h = s * 2.4, s * 1.3
            d.rectangle([cx - 4, top + h, cx + 4, top + h + 170], fill=(70, 70, 70))
            d.rectangle([cx - w / 2, top, cx + w / 2, top + h], fill=(250, 210, 40), outline=(40, 40, 40), width=3)
            d.text((cx - w / 2 + 12, top + h * 0.3), "PROMO -30%", fill=(180, 20, 30), font=FONT_SIGN)
            return (int(cx - w / 2), int(top), int(cx + w / 2), int(top + h))
        d.rectangle([cx - 4, top + s / 2, cx + 4, top + s + 190], fill=(120, 120, 125))
        box = (cx - s / 2, top, cx + s / 2, top + s)
        x0, y0, x1, y1 = box
        if code == "stop":
            pts = [(cx + s / 2 * math.cos(math.radians(22.5 + 45 * i)), top + s / 2 + s / 2 * math.sin(math.radians(22.5 + 45 * i)))
                   for i in range(8)]
            d.polygon(pts, fill=(200, 20, 30), outline=(255, 255, 255))
            d.text((x0 + s * 0.12, top + s * 0.36), "STOP", fill=(255, 255, 255), font=FONT_SIGN)
        elif code == "give_way":
            d.polygon([(x0, y0), (x1, y0), (cx, y1)], fill=(255, 255, 255), outline=(200, 20, 30), width=7)
        elif code.startswith("speed_limit_"):
            d.ellipse(box, fill=(255, 255, 255), outline=(200, 20, 30), width=8)
            d.text((cx - s * 0.22, top + s * 0.33), code.rsplit("_", 1)[1], fill=(10, 10, 10), font=FONT_SIGN)
        elif code in ("no_parking", "no_stopping"):
            d.ellipse(box, fill=(30, 70, 170), outline=(200, 20, 30), width=7)
            d.line([(x0 + s * 0.2, y0 + s * 0.2), (x1 - s * 0.2, y1 - s * 0.2)], fill=(200, 20, 30), width=7)
            if code == "no_stopping":
                d.line([(x1 - s * 0.2, y0 + s * 0.2), (x0 + s * 0.2, y1 - s * 0.2)], fill=(200, 20, 30), width=7)
        elif code == "no_entry":
            d.ellipse(box, fill=(200, 20, 30))
            d.rectangle([x0 + s * 0.18, top + s * 0.42, x1 - s * 0.18, top + s * 0.58], fill=(255, 255, 255))
        else:                                           # faded / unidentified plate
            d.polygon([(cx, y0), (x1, top + s / 2), (cx, y1), (x0, top + s / 2)], fill=(205, 185, 90),
                      outline=(90, 80, 40), width=4)
            d.text((cx - s * 0.12, top + s * 0.3), "?", fill=(90, 80, 40), font=FONT_SIGN)
        return tuple(int(v) for v in box)  # type: ignore[return-value]

    def vehicle(self, box, cls: str) -> None:
        rng, d = self.rng, ImageDraw.Draw(self.img)
        x0, y0, x1, y1 = box
        w, h = x1 - x0, y1 - y0
        d.ellipse([x0 - w * 0.05, y1 - h * 0.08, x1 + w * 0.05, y1 + h * 0.06], fill=(40, 30, 25))   # shadow
        if cls == "car":
            col = rng.choice([(235, 200, 20), (235, 200, 20), (230, 230, 230), (160, 165, 170), (150, 30, 35),
                              (30, 40, 70)])                 # yellow Douala taxis are frequent
            d.rounded_rectangle([x0, y0 + h * 0.38, x1, y1 - h * 0.12], radius=max(2, w * 0.08), fill=col)
            d.polygon([(x0 + w * 0.15, y0 + h * 0.4), (x0 + w * 0.25, y0), (x1 - w * 0.25, y0),
                       (x1 - w * 0.15, y0 + h * 0.4)], fill=col)
            d.polygon([(x0 + w * 0.22, y0 + h * 0.38), (x0 + w * 0.3, y0 + h * 0.08), (x1 - w * 0.3, y0 + h * 0.08),
                       (x1 - w * 0.22, y0 + h * 0.38)], fill=(60, 75, 95))
            d.rectangle([x0 + w * 0.06, y0 + h * 0.5, x0 + w * 0.2, y0 + h * 0.6], fill=(220, 40, 30))
            d.rectangle([x1 - w * 0.2, y0 + h * 0.5, x1 - w * 0.06, y0 + h * 0.6], fill=(220, 40, 30))
        else:
            col = (240, 240, 235) if cls == "bus" else rng.choice([(40, 90, 160), (190, 60, 40), (230, 230, 225)])
            d.rectangle([x0, y0, x1, y1 - h * 0.1], fill=col, outline=(50, 50, 50))
            if cls == "bus":
                d.rectangle([x0 + w * 0.08, y0 + h * 0.1, x1 - w * 0.08, y0 + h * 0.42], fill=(55, 70, 90))
                d.rectangle([x0, y0 + h * 0.55, x1, y0 + h * 0.62], fill=(30, 120, 60))
            else:
                for i in range(1, 4):
                    d.line([(x0 + w * i / 4, y0 + 3), (x0 + w * i / 4, y1 - h * 0.12)], fill=(60, 60, 60), width=2)
            d.rectangle([x0 + w * 0.05, y1 - h * 0.22, x0 + w * 0.18, y1 - h * 0.14], fill=(220, 40, 30))
            d.rectangle([x1 - w * 0.18, y1 - h * 0.22, x1 - w * 0.05, y1 - h * 0.14], fill=(220, 40, 30))
        for wx in (x0 + w * 0.06, x1 - w * 0.22):
            d.rectangle([wx, y1 - h * 0.14, wx + w * 0.16, y1], fill=(20, 20, 20))


def jpeg(img: Image.Image, q: int = 84) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=q)
    return buf.getvalue()


def annotate(img: Image.Image, box, colour, label: str) -> Image.Image:
    out = img.copy()
    d = ImageDraw.Draw(out)
    x0, y0, x1, y1 = box
    d.rectangle([x0, y0, x1, y1], outline=colour, width=2)
    d.text((x0, max(42, y0 - 22)), label, fill=colour, font=FONT_LABEL, stroke_width=2, stroke_fill=(0, 0, 0))
    return out


def crop(img: Image.Image, box, pad: int = 20) -> Image.Image:
    x0, y0, x1, y1 = box
    return img.crop((max(0, x0 - pad), max(0, y0 - pad), min(W_PX, x1 + pad), min(H_PX, y1 + pad)))


# ── jobs ──────────────────────────────────────────────────────────────────────

@dataclass
class JobSpec:
    key: str
    route: str
    km0: float
    km1: float
    rec_start: datetime
    speed: float
    models: str
    vehicle_id: str
    status: str = "done"
    source: str = "upload"
    stop_km: Optional[float] = None          # cancelled: processing stopped there
    fast: list = field(default_factory=list)  # [(km_from, km_to, kmh)]
    slow: list = field(default_factory=list)
    step_s: float = 1.0                       # one OCR fix every step_s seconds in the track
    proc_ratio: Optional[float] = None        # processing time / video duration
    upload_min: Optional[float] = None        # minutes between recording end and upload


@dataclass
class JobRun:
    spec: JobSpec
    route: Route
    job: Optional[Job] = None
    t: list = field(default_factory=list)
    s: list = field(default_factory=list)
    v: list = field(default_factory=list)
    duration: float = 0.0
    created: datetime = None  # type: ignore[assignment]
    started: datetime = None  # type: ignore[assignment]
    finished: datetime = None  # type: ignore[assignment]
    review_at: datetime = None  # type: ignore[assignment]
    verdicts: dict = field(default_factory=dict)
    by_model: dict = field(default_factory=dict)
    conf_sum: float = 0.0
    n: int = 0
    gaps: list = field(default_factory=list)   # video times without an OCR fix

    def build(self, rng: random.Random) -> None:
        sp = self.spec
        s, t = sp.km0 * 1000, 0.0
        end = (sp.stop_km or sp.km1) * 1000
        while s <= end:
            km = s / 1000
            v = sp.speed + 4 * math.sin(s / 350.0) + rng.gauss(0, 1.0)
            for a, b, kmh in sp.fast + sp.slow:
                if a <= km <= b:
                    v = kmh + rng.gauss(0, 0.8)
            v = max(8.0, min(v, 95.0))
            self.t.append(t)
            self.s.append(s)
            self.v.append(round(v, 1))
            s += v / 3.6 * sp.step_s
            t += sp.step_s
        self.duration = self.t[-1]

    def time_at(self, m: float) -> float:
        if m <= self.s[0]:
            return 0.0
        import bisect
        i = bisect.bisect_left(self.s, m)
        if i >= len(self.s):
            return self.t[-1]
        f = (m - self.s[i - 1]) / max(self.s[i] - self.s[i - 1], 1e-6)
        return self.t[i - 1] + f * (self.t[i] - self.t[i - 1])

    def speed_at(self, m: float) -> float:
        t = self.time_at(m)
        return self.v[min(int(t / self.spec.step_s), len(self.v) - 1)]

    def rec(self, t: float) -> datetime:
        return self.spec.rec_start + timedelta(seconds=t)

    def processed_at(self, t: float) -> datetime:
        return self.started + (self.finished - self.started) * (t / max(self.duration, 1.0))


def job_specs() -> list[JobSpec]:
    def ago(hours: float = 0, minutes: float = 0) -> datetime:
        return NOW - timedelta(hours=hours, minutes=minutes)
    D3 = "pothole,signs,speedbump"
    D4 = "pothole,signs,speedbump,vehicles"
    bridge = [(3.3, 5.5, 46)]
    return [
        JobSpec("J1", "D", 5.5, 13.5, local_day(9, "08:10"), 29, D3, "LT-482-AB"),
        JobSpec("J14", "Y", 0.0, 8.0, local_day(7, "10:05"), 27, D3, "CE-913-KX"),
        JobSpec("J6", "E", 0.0, 12.0, local_day(7, "14:30"), 52, D3, "LT-205-EF", source="google_drive"),
        JobSpec("J2", "D", 5.5, 13.5, local_day(6, "17:20"), 27, D4, "LT-482-AB"),
        JobSpec("J3", "D", 9.5, 17.5, local_day(5, "09:00"), 30, D4, "LT-482-AB"),
        JobSpec("J13", "M", 0.0, 6.4, local_day(4, "11:15"), 28, D4, "LT-482-AB"),
        JobSpec("J5", "Y", 0.0, 11.3, local_day(4, "15:40"), 28, D4, "CE-913-KX", source="google_drive"),
        JobSpec("J7", "B", 0.0, 6.5, local_day(3, "09:30"), 31, D4, "OU-207-BF"),
        JobSpec("J4", "D", 1.0, 12.0, local_day(2, "07:45"), 31, D4, "LT-482-AB", fast=bridge),
        JobSpec("J9", "Y", 0.0, 9.0, ago(29), 27, D4, "CE-913-KX", status="cancelled", stop_km=3.0,
                source="google_drive"),
        JobSpec("J6b", "E", 0.0, 12.0, ago(27.5), 50, D4, "LT-205-EF", source="google_drive"),
        JobSpec("J8", "D", 9.0, 14.0, ago(21), 30, D4, "LT-731-CD", status="failed", source="google_drive"),
        JobSpec("J10", "D", 12.5, 18.5, ago(5), 31, D4, "LT-731-CD"),
        JobSpec("J11", "D", 9.0, 16.0, ago(3), 30, D4, "LT-731-CD"),
        JobSpec("J12", "D", 9.5, 14.5, ago(1, 10), 31, D4, "LT-731-CD", fast=[(12.95, 13.3, 66)]),
    ]


def filename_for(spec: JobSpec) -> str:
    loc = local_naive(spec.rec_start)
    city = {"D": "DLA", "M": "DLA", "Y": "YDE", "E": "EDEA", "B": "BAF"}.get(spec.route) or \
        spec.route.split("-")[-1].upper()
    return f"{loc:%Y%m%d_%H%M%S}_{city}_F.MP4"


# ── detection specs ───────────────────────────────────────────────────────────

@dataclass
class DSpec:
    job: str
    km: float
    type: str
    intent: str = "review"         # auto | review | lowconf | verifier_reject | not_a_sign | unknown | audit
    act: Optional[str] = None      # accept | locfix | reject:<code> | correct:<type>:<subtype>
    code: Optional[str] = None     # traffic sign class predicted by the classifier
    scen: str = "parked"           # parked vehicles: parked | moving | oncoming | queue | not_grounded | off_road | fallback
    cls: str = "car"
    side: str = "roadside"
    loc: str = "ok"                # ok | approx | missing
    along: float = 0.0             # metres along the route from the spot (cluster spread)
    name: Optional[str] = None
    note: Optional[str] = None     # validator's note (accept)


def det_specs() -> list[DSpec]:
    P, S, T, V = "pothole", "speed_bump", "traffic_sign", "parked_vehicle"
    acc = "accept"
    L = [
        # ── Douala, potholes ──
        DSpec("J1", 10.35, P, act=acc, along=-2), DSpec("J2", 10.35, P, act=acc, along=1.5),
        DSpec("J3", 10.35, P, loc="approx", act="locfix", along=3), DSpec("J4", 10.35, P, "auto", along=-1),
        DSpec("J11", 10.35, P, act=acc, along=2.5), DSpec("J12", 10.35, P, along=0.5),
        DSpec("J1", 10.9, P, act="reject:shadow"), DSpec("J2", 10.9, P, "auto", along=2),
        DSpec("J3", 10.9, P, act=acc, along=-2), DSpec("J4", 10.9, P, act=acc, along=1),
        DSpec("J11", 10.9, P, act=acc, along=-1),
        DSpec("J1", 12.9, P, act=acc, along=1), DSpec("J2", 12.9, P, act=acc, along=-2.5),
        DSpec("J3", 12.9, P, "auto", along=2), DSpec("J10", 12.9, P, act=acc, along=-1),
        DSpec("J11", 12.9, P, "auto", along=0), DSpec("J12", 12.9, P, act=acc, along=3),
        DSpec("J4", 1.6, P, act=acc),
        DSpec("J1", 6.3, P, act=acc, along=0), DSpec("J4", 6.3, P, act=acc, along=4),
        DSpec("J2", 6.3, P, act=acc, along=16),
        DSpec("J3", 15.2, P, act=acc, along=1), DSpec("J10", 15.2, P, "auto", along=-2),
        DSpec("J11", 15.2, P, act=acc, along=2),
        DSpec("J1", 8.0, P, act=acc, name="P_RES"), DSpec("J2", 8.0, P, act=acc, along=2),
        DSpec("J4", 4.2, P), DSpec("J12", 14.0, P), DSpec("J10", 17.0, P),
        DSpec("J3", 16.4, P, loc="missing"),
        DSpec("J1", 9.7, P, act="reject:road_marking"), DSpec("J2", 11.6, P, act="reject:manhole"),
        DSpec("J4", 2.8, P, act="reject:water_reflection"), DSpec("J3", 14.8, P, "lowconf"),
        DSpec("J11", 13.9, P, "verifier_reject"),
        # ── Douala, speed bumps ──
        DSpec("J1", 12.2, S, act=acc), DSpec("J2", 12.2, S, act=acc, along=2),
        DSpec("J3", 12.2, P, act="correct:speed_bump:", along=-2), DSpec("J11", 12.2, S, "auto", along=1),
        DSpec("J12", 12.2, S, act=acc, along=-1),
        DSpec("J1", 7.4, S, "auto"), DSpec("J2", 7.4, S, act=acc, along=2), DSpec("J4", 7.4, S, act=acc, along=-1),
        DSpec("J4", 3.9, S, act="reject:road_edge"), DSpec("J10", 16.6, S),
        # ── Douala, traffic signs ──
        DSpec("J1", 10.6, T, code="stop", act=acc), DSpec("J2", 10.6, T, "auto", code="stop", along=1),
        DSpec("J3", 10.6, T, code="stop", act=acc, along=-1), DSpec("J4", 10.6, T, code="stop", along=2),
        DSpec("J11", 10.6, T, "auto", code="stop"), DSpec("J12", 10.6, T, code="stop", act=acc, along=1),
        DSpec("J1", 6.8, T, code="no_parking", act=acc), DSpec("J2", 6.8, T, code="no_stopping",
                                                               act="correct:traffic_sign:no_parking", along=1),
        DSpec("J4", 6.8, T, "auto", code="no_parking", along=-1),
        DSpec("J1", 13.1, T, code="speed_limit_40", act=acc, name="S_ABS"),
        DSpec("J2", 13.1, T, code="speed_limit_40", act=acc, along=1),
        DSpec("J3", 13.1, T, "auto", code="speed_limit_40", along=-1),
        DSpec("J3", 16.9, T, code="give_way", act=acc), DSpec("J10", 16.9, T, "auto", code="give_way", along=1),
        DSpec("J4", 2.2, T, "unknown", code="unknown_sign"), DSpec("J11", 15.5, T, "unknown", code="speed_limit_30"),
        DSpec("J2", 11.9, T, "not_a_sign", code="not_a_sign"), DSpec("J10", 18.0, T, "not_a_sign", code="not_a_sign"),
        DSpec("J3", 14.2, T, code="no_entry", act="reject:pole_as_sign"),
        DSpec("J12", 11.0, T, code="give_way", act="reject:street_light"),
        # ── Douala, parked vehicles ──
        DSpec("J2", 10.15, V, act=acc, name="RS1"), DSpec("J3", 10.15, V, "auto", along=2, name="RS2"),
        DSpec("J4", 10.15, V, act=acc, along=-2, name="RS3"), DSpec("J12", 10.15, V, "auto", along=1, name="RS4"),
        DSpec("J12", 13.6, V, "auto", side="blocking", name="BLOCKING"),
        DSpec("J10", 18.0, V, act=acc, cls="truck", name="TRUCK"),
        DSpec("J10", 14.0, V, "auto", cls="bus", name="VA_BUS"),
        DSpec("J11", 15.6, V, "auto", name="VOTED"),
        DSpec("J11", 12.4, V, act=acc, side="wrong_side", name="MANUAL_W"),
        DSpec("J10", 15.4, V, "auto"), DSpec("J12", 11.4, V, "auto"),
        DSpec("J10", 16.3, V, side="wrong_side"), DSpec("J11", 9.3, V, cls="bus"),
        DSpec("J12", 12.0, V, cls="truck", side="blocking"), DSpec("J4", 2.5, V, cls="truck"),
        DSpec("J4", 7.9, V, "audit", side="wrong_side"),
        DSpec("J4", 3.0, V, "auto", scen="off_road", cls="truck"),
        DSpec("J3", 15.9, V, "auto", scen="not_grounded"),
        DSpec("J11", 11.1, V, "auto", scen="queue"), DSpec("J11", 15.0, V, "auto", scen="queue"),
        DSpec("J10", 17.2, V, "auto", scen="oncoming"), DSpec("J4", 5.0, V, "auto", scen="moving", cls="truck"),
        DSpec("J2", 9.9, V, act="reject:not_road_side"), DSpec("J3", 13.8, V, act="reject:ai_moving_vehicle"),
        DSpec("J3", 14.9, V, act=acc, side="blocking"), DSpec("J4", 8.6, V, act=acc, cls="bus"),
        DSpec("J3", 16.8, V, "auto"), DSpec("J4", 6.0, V, "auto", scen="fallback", loc="missing"),
        # ── Makepe ──
        DSpec("J13", 1.9, P, act=acc), DSpec("J13", 4.5, P), DSpec("J13", 5.6, S, "auto"),
        DSpec("J13", 0.7, T, code="no_entry", act=acc), DSpec("J13", 3.2, V),
        DSpec("J13", 5.2, V, "auto", scen="queue"),
        # ── Yaoundé ──
        DSpec("J14", 0.6, P, act=acc), DSpec("J14", 2.0, T, code="speed_limit_60", act=acc),
        DSpec("J14", 5.3, T, "auto", code="give_way"), DSpec("J14", 7.9, P, act=acc),
        DSpec("J14", 3.5, T, code="stop", act="reject:person_as_sign"),
        DSpec("J5", 0.6, P, "auto", along=2), DSpec("J5", 2.0, T, code="speed_limit_80",
                                                    act="correct:traffic_sign:speed_limit_60", along=1),
        DSpec("J5", 5.3, T, code="give_way", act=acc, along=-1), DSpec("J5", 7.9, P, act=acc, along=2),
        DSpec("J5", 10.4, S, act=acc), DSpec("J5", 4.4, T, "unknown", code="unknown_sign"),
        DSpec("J5", 10.0, T, "not_a_sign", code="not_a_sign"), DSpec("J5", 9.0, P, "lowconf"),
        DSpec("J5", 3.1, V), DSpec("J5", 6.5, V, "auto", cls="bus", side="blocking"),
        DSpec("J5", 8.8, V, "auto", scen="off_road", cls="truck"), DSpec("J5", 9.5, V, "auto", scen="not_grounded"),
        DSpec("J9", 0.6, P, act=acc, along=-2), DSpec("J9", 1.4, V, side="wrong_side"),
        # ── Edéa / N3 ──
        DSpec("J6", 6.2, P, act=acc), DSpec("J6", 3.0, T, code="speed_limit_50", act=acc),
        DSpec("J6", 7.3, S, "auto"), DSpec("J6", 10.5, P, act="reject:dirt_patch"),
        DSpec("J6b", 6.2, P, act=acc, along=22), DSpec("J6b", 3.0, T, "auto", code="speed_limit_50", along=2),
        DSpec("J6b", 7.3, S, along=1), DSpec("J6b", 1.2, P),
        DSpec("J6b", 4.5, V, "auto", cls="truck"), DSpec("J6b", 9.0, V, "auto", scen="off_road", cls="truck"),
        # ── Bafoussam ──
        DSpec("J7", 2.9, P, act=acc), DSpec("J7", 4.6, P, "auto"), DSpec("J7", 2.4, T, code="stop", act=acc),
        DSpec("J7", 5.1, T, "unknown", code="unknown_sign"), DSpec("J7", 3.3, S),
        DSpec("J7", 3.6, V, side="wrong_side"), DSpec("J7", 1.0, V, "auto", scen="oncoming"),
        DSpec("J7", 5.8, V, act=acc, cls="truck"),
    ]
    return L


# ── parked-vehicle synthetic tracks ──────────────────────────────────────────

CLASS_WIDTH = {"car": 1.8, "bus": 2.5, "truck": 2.5, "motorcycle": 0.8}


def _scenario(d: DSpec, rng: random.Random) -> dict:
    lat = {"roadside": rng.uniform(3.4, 3.9), "wrong_side": -rng.uniform(3.9, 4.4),
           "blocking": rng.choice([-1, 1]) * rng.uniform(0.15, 0.5)}[d.side]
    high = d.intent in ("auto", "audit")
    if d.scen == "parked":
        if high:
            return dict(L=lat, ego=rng.uniform(28, 36), r0=rng.uniform(42, 48), r1=rng.uniform(6.6, 7.4),
                        conf=rng.uniform(0.8, 0.88), vT=0.0, hf=1.0, gps=True)
        r1 = rng.uniform(9.5, 11.0)
        return dict(L=lat, ego=rng.uniform(22, 28), r0=r1 + rng.uniform(35, 38), r1=r1,
                    conf=rng.uniform(0.535, 0.58), vT=0.0, hf=1.0, gps=True)
    if d.scen == "moving":
        return dict(L=3.6, ego=50, r0=40, r1=9, conf=0.8, vT=6.0, hf=1.0, gps=True)
    if d.scen == "oncoming":
        return dict(L=-3.8, ego=40, r0=75, r1=10, conf=0.78, vT=-9.0, hf=1.0, gps=True)
    if d.scen == "queue":
        return dict(L=rng.uniform(0.1, 0.4), ego=2.5, r0=8.0 + 0.69 * 8, r1=8.0, conf=0.82, vT=0.0, hf=1.0,
                    gps=True)
    if d.scen == "not_grounded":
        return dict(L=3.6, ego=30, r0=44, r1=7, conf=0.74, vT=0.0, hf=0.55, gps=True)
    if d.scen == "off_road":
        return dict(L=15.0, ego=35, r0=55, r1=17, conf=0.74, vT=0.0, hf=1.0, gps=True)
    if d.scen == "fallback":
        return dict(L=3.6, ego=10, r0=14, r1=6, conf=0.72, vT=0.0, hf=1.0, gps=False)
    raise ValueError(d.scen)


def project(r: float, L: float, H: float, W: float, hf: float, cam) -> tuple[float, float, float, float]:
    f = pv_geo.focal(cam)
    aspect = W_PX / H_PX
    y2 = cam.horizon_y + cam.camera_height_m * f / r
    h = H * f / r * hf
    cx = 0.5 + cam.center_offset + L * f / (r * aspect)
    w = W * f / (r * aspect)
    return cx - w / 2, y2 - h, cx + w / 2, y2


def vehicle_track(rng: random.Random, spot: tuple[float, float], heading: float, t_end: float, frame_end: int,
                  sc: dict, cls: str, pvs, rec_start: datetime, extra_far: bool = False, short: bool = False):
    """Sightings + ego samples of one vehicle track ending at the spot (closest sighting)."""
    cam = pvs.camera
    v_e = sc["ego"] / 3.6
    rel = v_e - sc["vT"]
    T = (sc["r0"] - sc["r1"]) / rel if rel > 0 else 8.0
    if short:
        T = 1.4
    H = pvs.class_heights_m.get(cls, 1.5)
    W = CLASS_WIDTH.get(cls, 1.8)
    n = int(T * FPS / STEP)
    sightings, best = [], None
    for k in range(n + 1):
        frame = frame_end - (n - k) * STEP
        t = frame / FPS
        dt = t_end - t
        r = sc["r1"] + rel * dt
        nb = project(r, sc["L"], H, W, sc["hf"], cam)
        if nb[0] < 0 or nb[2] > 1 or nb[1] < 0.0 or pv_geo.is_masked(nb, pvs.mask_regions, pvs.hood_mask_bottom_ratio):
            continue
        box = tuple(int(round(v * (W_PX if i % 2 == 0 else H_PX) + rng.gauss(0, 0.8))) for i, v in enumerate(nb))
        conf = round(max(0.36, min(0.97, sc["conf"] + rng.gauss(0, 0.035))), 4)
        lat = lon = None
        if sc["gps"]:
            fix_t = t_end - math.ceil(dt - 1e-9)       # 1 Hz fix aligned on t_end
            lat, lon = offset(spot[0], spot[1], heading, -(t_end - fix_t) * v_e, 0)
        s = pv.VehicleSighting(frame, box, conf, lat, lon, t_s=t, cls=cls,
                               speed_kmh=round(sc["ego"] + rng.gauss(0, 0.4), 1) if sc["gps"] else None,
                               frame_w=W_PX, frame_h=H_PX,
                               timestamp=local_naive(rec_start + timedelta(seconds=t)))
        sightings.append(s)
        best = s
    ego = []
    t = t_end - T - 3.0
    while t <= t_end + 3.0:
        if sc["gps"]:
            fix_t = t_end - math.ceil(t_end - t - 1e-9)
            lat, lon = offset(spot[0], spot[1], heading, -(t_end - fix_t) * v_e, 0)
            ego.append(pv.EgoSample(round(t, 3), lat, lon, round(sc["ego"] + rng.gauss(0, 0.4), 1)))
        else:
            ego.append(pv.EgoSample(round(t, 3), None, None, None))
        t += STEP / FPS
    return sightings, ego, best


async def road_distance(lat: float, lon: float) -> tuple[Optional[float], str]:
    try:
        res = await get_osrm_client().nearest((lat, lon))
    except Exception as exc:
        return None, f"road snap unavailable ({type(exc).__name__})"
    wps = res.data.get("waypoints") or []
    if res.data.get("code") != "Ok" or not wps or wps[0].get("distance") is None:
        return None, f"OSRM nearest: {res.data.get('code')}"
    return float(wps[0]["distance"]), ""


async def judge_track(sightings, ego, pvs, vehicle_id):
    f, _cam = pv.prepare(sightings, ego, pvs, vehicle_id)
    dist, note = None, ""
    if pv.needs_road_check(f, pvs):
        dist, note = await road_distance(f.target_lat, f.target_lon)  # type: ignore[arg-type]
    return pv.finish(f, pvs, road_distance_m=dist, road_note=note, queue_peers=0, vehicle_id=vehicle_id)


# ── intercity road sections (tronçons of settings section `routes`) ──────────

# (town, lat, lon, region) — addresses of the detections on the intercity roads
TOWNS = [
    ("Dibombari", 4.1830, 9.6560, "Littoral"), ("Souza", 4.3370, 9.6080, "Littoral"),
    ("Mbanga", 4.5016, 9.5671, "Littoral"), ("Njombé", 4.5833, 9.6667, "Littoral"),
    ("Penja", 4.6333, 9.6833, "Littoral"), ("Loum", 4.7182, 9.7351, "Littoral"),
    ("Manjo", 4.8436, 9.8228, "Littoral"), ("Nkongsamba", 4.9547, 9.9404, "Littoral"),
    ("Melong", 5.1214, 9.9614, "Littoral"), ("Kékem", 5.1497, 10.0258, "Ouest"),
    ("Bafang", 5.1592, 10.1808, "Ouest"), ("Bandja", 5.2580, 10.2490, "Ouest"),
    ("Bandjoun", 5.3500, 10.4167, "Ouest"), ("Baham", 5.3333, 10.3833, "Ouest"),
    ("Bangangté", 5.1440, 10.5240, "Ouest"), ("Foumbot", 5.5080, 10.6330, "Ouest"),
    ("Japoma", 4.0170, 9.8160, "Littoral"), ("Pouma", 3.8481, 10.5196, "Littoral"),
    ("Dizangué", 3.7670, 9.9830, "Littoral"), ("Boumnyebel", 3.8833, 10.8500, "Centre"),
    ("Ngoumou", 3.5833, 11.3000, "Centre"), ("Matomb", 3.8333, 11.0833, "Centre"),
    ("Mbankomo", 3.7833, 11.3833, "Centre"), ("Obala", 4.1667, 11.5333, "Centre"),
    ("Monatélé", 4.2667, 11.2000, "Centre"), ("Ombessa", 4.6000, 11.2500, "Centre"),
    ("Bafia", 4.7500, 11.2333, "Centre"), ("Makénéné", 4.8833, 10.8000, "Centre"),
    ("Tonga", 4.9667, 10.7000, "Ouest"), ("Bazou", 5.0667, 10.4667, "Ouest"),
    ("Ayos", 3.9000, 12.5167, "Centre"), ("Akonolinga", 3.7667, 12.2500, "Centre"),
    ("Abong-Mbang", 3.9833, 13.1833, "Est"), ("Doumé", 4.2333, 13.4500, "Est"),
    ("Bertoua", 4.5774, 13.6846, "Est"), ("Nsimalen", 3.7200, 11.5500, "Centre"),
    ("Kribi", 2.9404, 9.9101, "Sud"), ("Bipindi", 3.0833, 10.4167, "Sud"),
    ("Lolodorf", 3.2333, 10.7333, "Sud"), ("Lokoundjé", 3.1000, 9.9500, "Sud"),
    ("Tiko", 4.0750, 9.3600, "Sud-Ouest"), ("Mutengene", 4.0913, 9.3104, "Sud-Ouest"),
    ("Limbé", 4.0186, 9.2043, "Sud-Ouest"), ("Mouanko", 3.6390, 9.7750, "Littoral"),
    ("Mbalmayo", 3.5167, 11.5000, "Centre"), ("Soa", 3.9833, 11.6000, "Centre"),
    ("Ndikiniméki", 4.7667, 10.8333, "Centre"), ("Mbandjock", 4.4500, 11.9000, "Centre"),
]

# areas already holding the crafted city scenarios: intercity videos stop outside them
PROTECTED = [(4.050, 9.720, 15000.0), (3.866, 11.516, 12000.0), (3.800, 10.133, 7000.0), (5.478, 10.418, 6000.0)]
SECTION_ORDER = ["douala-yaounde-n3", "douala-bafoussam-n5", "douala-limbe-n3", "douala-kribi-n7",
                 "yaounde-bafoussam-n4", "yaounde-bertoua-n10"]
SIGN_CODES = ["speed_limit_60", "give_way", "speed_limit_80", "stop", "no_overtaking", "speed_limit_50"]


def rural_address(route: Route, m: Optional[float], lat: float, lon: float) -> dict:
    name, tla, tlo, region = min(TOWNS, key=lambda x: haversine_m(lat, lon, x[1], x[2]))
    d = haversine_m(lat, lon, tla, tlo)
    road = route.road_at(m) if m is not None else route.ref
    locality = name if d <= 3000 else None
    parts = [p for p in (road, locality or f"près de {name}") if p]
    return {"road": road, "locality": locality, "district": None, "town": name, "region": region,
            "country": "Cameroun", "display": ", ".join(parts),
            "location_name": ", ".join(p for p in (road, locality or name, region, "Cameroun") if p), "city": name}


def _cells(lat: float, lon: float) -> tuple[int, int]:
    return int(round(lat / 0.001)), int(round(lon / 0.001))


async def plan_intercity(db, ctx: Ctx) -> tuple[list[JobSpec], list[DSpec]]:
    """Two drives (a week ago-ish and finishing ~1 h ago) per predefined road
    section, each one job per free stretch, and 6–12 detections on the
    road geometry the Detection map uses (GET /routes/preview → routes[0])."""
    routes_cfg = await get_section(db, "routes")
    sections = sorted(routes_cfg.sections, key=lambda x: SECTION_ORDER.index(x.id) if x.id in SECTION_ORDER else 99)
    client = get_osrm_client()
    covered: set[tuple[int, int]] = set()
    jobs: list[JobSpec] = []
    dets: list[DSpec] = []
    rng = ctx.rng
    for si, sec in enumerate(sections):
        a, b = (sec.from_.lat, sec.from_.lon), (sec.to.lat, sec.to.lon)
        ov = await client.route_overview([a, b], alternatives=True)
        st = await client.route([a, b])
        if ov.data.get("code") != "Ok" or not ov.data.get("routes"):
            ctx.warn(f"tronçon {sec.id}: no OSRM route")
            continue
        ref = next((w for w in sec.name.replace("(", " ").replace(")", " ").split() if w[:1] == "N" and w[1:].isdigit()), None)
        route = Route(sec.id, ov.data, st.data, city="rural", ref=ref)
        ctx.routes[sec.id] = route
        # free stretches: outside the protected cities and not already driven by an earlier section
        step = 100.0
        free = []
        m = 0.0
        while m < route.length:
            lat, lon, _ = route.at(m)
            prot = any(haversine_m(lat, lon, pa, po) < r for pa, po, r in PROTECTED)
            ci, cj = _cells(lat, lon)
            seen = any((ci + di, cj + dj) in covered for di in (-1, 0, 1) for dj in (-1, 0, 1))
            free.append((m, not prot and not seen))
            m += step
        stretches, cur = [], None
        for m, ok in free:
            if ok and cur is None:
                cur = [m, m]
            elif ok:
                cur[1] = m
            elif cur is not None:
                stretches.append(cur)
                cur = None
        if cur is not None:
            stretches.append(cur)
        stretches = [(x0 + 200, x1 - 200) for x0, x1 in stretches if x1 - x0 >= 8000]
        m = 0.0
        while m < route.length:
            lat, lon, _ = route.at(m)
            covered.add(_cells(lat, lon))
            m += 50
        if not stretches:
            ctx.warn(f"tronçon {sec.id}: no free stretch")
            continue
        total = sum(x1 - x0 for x0, x1 in stretches)
        print(f"  tronçon {sec.name}: {route.length / 1000:.0f} km, video stretches "
              f"{[(round(x0 / 1000, 1), round(x1 / 1000, 1)) for x0, x1 in stretches]}")
        # spots: towns along the free stretches, then evenly spread rural points
        spots: list[float] = []
        pts = [(m, *route.at(m)[:2]) for m in range(0, int(route.length), 250)]
        for name, tla, tlo, _r in TOWNS:
            mm, la, lo = min(pts, key=lambda p: haversine_m(tla, tlo, p[1], p[2]))
            if haversine_m(tla, tlo, la, lo) <= 2500:
                cand = mm + 600
                if any(x0 + 500 <= cand <= x1 - 500 for x0, x1 in stretches) and all(abs(cand - o) > 3000 for o in spots):
                    spots.append(cand)
        k = 1
        while len(spots) < 9 and k < 40:
            frac = (k * 0.618) % 1.0
            cand = None
            acc = frac * total
            for x0, x1 in stretches:
                if acc <= x1 - x0:
                    cand = x0 + acc
                    break
                acc -= x1 - x0
            if cand is not None and all(abs(cand - o) > 4000 for o in spots):
                spots.append(cand)
            k += 1
        spots = sorted(spots)[:11]
        vid = f"{['LT', 'OU', 'SW', 'SU', 'CE', 'ES'][si % 6]}-{300 + si * 37}-{'ABCDEF'[si % 6]}{'KLMNPR'[si % 6]}"
        speed = 61.0
        dur_total = sum(x1 - x0 for x0, x1 in stretches) / (speed / 3.6) + 600
        drives = {
            "old": local_day(4 + si, "07:20"),
            "new": NOW - timedelta(minutes=72) - timedelta(seconds=(stretches[-1][1] - stretches[0][0]) / (speed / 3.6)),
        }
        for drv, start in drives.items():
            for k2, (x0, x1) in enumerate(stretches):
                key = f"{sec.id}:{drv}:{k2}"
                rec = start + timedelta(seconds=(x0 - stretches[0][0]) / (speed / 3.6))
                jobs.append(JobSpec(key, sec.id, x0 / 1000, x1 / 1000, rec, speed, "pothole,signs,speedbump,vehicles",
                                    vid, step_s=2.0, proc_ratio=0.1 if drv == "new" else 0.3,
                                    upload_min=8 if drv == "new" else 35))

        def job_of(drv: str, m: float) -> Optional[str]:
            for k2, (x0, x1) in enumerate(stretches):
                if x0 + 300 <= m <= x1 - 300:
                    return f"{sec.id}:{drv}:{k2}"
            return None

        # the blocking car sits on the last spot (recorded ~1 h 15 before now: blocking expires after 2 h)
        P, S, T, V = "pothole", "speed_bump", "traffic_sign", "parked_vehicle"
        order = list(range(len(spots)))
        for idx, n in enumerate(order):
            mm = spots[n]
            jo, jn = job_of("old", mm), job_of("new", mm)
            if not jo or not jn:
                continue
            km = mm / 1000
            code = SIGN_CODES[(si + idx) % len(SIGN_CODES)]
            kind = idx % 9
            if kind == 0:
                dets += [DSpec(jo, km, P, act="accept"), DSpec(jn, km, P, "auto", along=6)]
            elif kind == 1:
                dets += [DSpec(jo, km, T, code=code, act="accept"), DSpec(jn, km, T, "auto", code=code, along=4)]
            elif kind == 2:
                dets += [DSpec(jn, km, V, cls="truck", act="accept",
                               note="Camion en panne sur l'accotement, triangle de signalisation posé")]
            elif kind == 3:
                dets += [DSpec(jo, km, S, "auto"), DSpec(jn, km, S, act="accept", along=-5)]
            elif kind == 4:
                dets += [DSpec(jo, km, P, act="accept", along=-8), DSpec(jn, km, P, act="accept", along=10),
                         DSpec(jn, km + 0.9, P)]
            elif kind == 5:
                dets += [DSpec(jn, km, V, "auto", cls="bus")]
            elif kind == 6:
                dets += [DSpec(jo, km, T, code=SIGN_CODES[(si + idx + 1) % len(SIGN_CODES)], act="accept"),
                         DSpec(jn, km, T, code=SIGN_CODES[(si + idx + 1) % len(SIGN_CODES)], act="accept", along=3)]
            elif kind == 7:
                dets += [DSpec(jn, km, V, side="wrong_side"), DSpec(jo, km + 0.6, P)]
            else:
                dets += [DSpec(jo, km, P, "auto"), DSpec(jn, km, P, act="accept", along=7),
                         DSpec(jo, km + 1.2, S)]
        last = stretches[-1][1] - 1500
        jn = job_of("new", last)
        if jn:
            dets.append(DSpec(jn, last / 1000, V, "auto", side="blocking"))
        print(f"    {len(spots)} spots, {sum(1 for d in dets if d.job.startswith(sec.id))} detections")
    return jobs, dets


# ── seeding steps ─────────────────────────────────────────────────────────────

async def fetch_routes(ctx: Ctx) -> None:
    client = get_osrm_client()
    for key, pts in ROUTE_POINTS.items():
        res = await client.route(pts)
        if res.data.get("code") != "Ok":
            raise SystemExit(f"OSRM route {key} failed: {res.data.get('code')}")
        ctx.routes[key] = Route(key, res.data)
        print(f"  route {key} ({ROUTE_CITY[key]}): {ctx.routes[key].length / 1000:.1f} km")


async def ensure_users(db, ctx: Ctx, tester_password: Optional[str]) -> None:
    role = (await db.execute(select(Role).where(Role.is_system.is_(True)).limit(1))).scalar_one_or_none() \
        or (await db.execute(select(Role).where(Role.name == "Administrator"))).scalar_one_or_none()
    tester = (await db.execute(select(User).where(User.email == TESTER_EMAIL))).scalar_one_or_none()
    if tester is None:
        if not tester_password:
            raise SystemExit("testeur@viasafe.local does not exist yet: pass --tester-password-stdin")
        tester = User(id=str(uuid.uuid4()), email=TESTER_EMAIL, username="testeur", full_name="Testeur Web Console",
                      account_type=AccountType.staff, status=UserStatus.active)
        db.add(tester)
        print(f"  created console user {TESTER_EMAIL}")
    if tester_password:
        tester.password_hash = hash_password(tester_password)
        tester.password_changed_at = datetime.utcnow()
    tester.status = UserStatus.active
    tester.mfa_enabled = False
    tester.mfa_secret_enc = tester.mfa_pending_enc = None
    tester.failed_login_count = 0
    tester.locked_until = None
    if role is not None and role not in tester.roles:
        tester.roles = [*tester.roles, role]
    await db.flush()
    ctx.tester = tester
    ctx.user_ids.add(tester.id)
    principal = Principal(kind="user", id=tester.id, label=tester.email, user=tester)
    ctx.tester_actor = wf.Actor("user", tester.email, principal, None)
    for i, (email, name) in enumerate(DRIVERS):
        u = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if u is None:
            u = User(id=sid(), email=email, full_name=name, account_type=AccountType.mobile,
                     status=UserStatus.active, phone=f"+2376{77 + i}{random.randint(100000, 999999)}",
                     password_hash=None, created_at=NOW - timedelta(days=40 - i * 7))
            db.add(u)
        ctx.drivers.append(u)
        ctx.user_ids.add(u.id)
        ctx.labels.add(email)
    await db.commit()


def driver_principal(u: User) -> Principal:
    return Principal(kind="user", id=u.id, label=u.email, user=u)


async def ensure_zones(db, ctx: Ctx) -> None:
    existing = {z.name.lower(): z for z in (await db.execute(select(ClusterZone))).scalars().all()}
    when = local_day(10, "09:00")
    for q, name, city in (("Yaoundé", "Yaoundé", "Yaoundé"), ("Bafoussam", "Bafoussam", "Bafoussam")):
        if name.lower() in existing:
            print(f"  zone {name}: already exists — kept")
            continue
        try:
            cands = await boundaries.search(q, limit=5, countrycodes="cm")
        except Exception as exc:
            ctx.warn(f"boundary search '{q}' failed ({exc}) — zone not created")
            continue
        cand = next((c for c in cands if not c["approximate"] and q.lower()[:5] in c["name"].lower()), None) \
            or (cands[0] if cands else None)
        if cand is None:
            ctx.warn(f"boundary search '{q}' returned nothing — zone not created")
            continue
        z = ClusterZone(name=name, kind="agglomeration", environment="urban", geometry=cand["geometry"],
                        source="nominatim", source_ref=f"{cand['osm_type']}/{cand['osm_id']}",
                        source_meta={"display_name": cand["display_name"], "approximate": cand["approximate"],
                                     "seed": TAG},
                        priority=10, is_active=True, country="Cameroon", city=city, created_by=TAG,
                        created_at=when, updated_at=when,
                        **clustering_service.geometry_fields(cand["geometry"]))
        db.add(z)
        await db.flush()
        audit.record(db, None, "cluster_zone.create", principal=ctx.tester_actor.principal,
                     object_type="cluster_zone", object_id=str(z.id),
                     details={"name": z.name, "kind": z.kind, "source": z.source, "source_ref": z.source_ref})
        print(f"  zone {name}: {cand['display_name'][:60]} ({cand['area_km2']} km², {cand['vertices']} vertices)")
    if "port de douala" not in existing:
        route = ctx.routes["D"]
        pts = [route.at(m) for m in range(5900, 7400, 50)]
        lat0, lat1 = min(p[0] for p in pts), max(p[0] for p in pts)
        lon0, lon1 = min(p[1] for p in pts), max(p[1] for p in pts)
        pad = 0.0025
        ring = [[lon0 - pad, lat0 - pad], [lon1 + pad, lat0 - pad], [lon1 + pad, lat1 + pad],
                [lon0 - pad, lat1 + pad], [lon0 - pad, lat0 - pad]]
        geom = {"type": "Polygon", "coordinates": [ring]}
        z = ClusterZone(name="Port de Douala", kind="custom", environment="urban", geometry=geom, source="drawn",
                        source_meta={"note": "Zone portuaire / Bonanjo — rayon nids-de-poule réduit", "seed": TAG},
                        default_radius_m=None, type_radius={"pothole": 10.0, "parked_vehicle": 15.0},
                        priority=20, is_active=True, country="Cameroon", city="Douala", created_by=TAG,
                        created_at=when, updated_at=when, **clustering_service.geometry_fields(geom))
        db.add(z)
        await db.flush()
        audit.record(db, None, "cluster_zone.create", principal=ctx.tester_actor.principal,
                     object_type="cluster_zone", object_id=str(z.id),
                     details={"name": z.name, "kind": z.kind, "type_radius": z.type_radius})
        print("  zone Port de Douala (custom, pothole 10 m, parked_vehicle 15 m)")
    await db.flush()
    await db.execute(update(AuditLog).where(AuditLog.action == "cluster_zone.create",
                                            AuditLog.actor_id == ctx.tester.id)
                     .values(created_at=when).execution_options(synchronize_session=False))
    await db.commit()
    clustering_service.invalidate()


async def build_policy(db, ctx: Ctx) -> None:
    """The real decision policy (settings) + the model-card thresholds a
    complete installation would carry. This demo stack has no model cards /
    classifiers installed, so the cards below stand in for them; the
    decisions themselves are computed by ai_validation.decide / rule_reject."""
    ai = await get_section(db, "ai_validation")
    rules = await get_section(db, "validation")
    pvs = await get_section(db, "parked_vehicle")
    # the seeded jobs ran with "store rejected verdicts for review" on (moving included)
    ctx.pvs = pvs.model_copy(update={"store_rejected_for_review": True,
                                     "review_verdicts": ["off_road", "not_grounded", "traffic_queue", "moving"]})
    ctx.pv_version = pv.rules_version(ctx.pvs)
    cards = model_registry.card_thresholds()
    sim = {
        "pothole": {"detector_accept": 0.70, "detector_reject": 0.30, "auto_accept": 0.85, "auto_reject": 0.25},
        "speed_bump": {"detector_accept": 0.70, "detector_reject": 0.30},
        "traffic_sign": {"detector_accept": 0.65, "detector_reject": 0.30, "auto_accept": 0.85, "auto_reject": 0.45,
                         "reject_class": "not_a_sign"},
        "parked_vehicle": {"detector_accept": 0.55, "detector_reject": 0.30},
    }
    for t, vals in sim.items():
        cards.setdefault(t, {})
        for k, v in vals.items():
            cards[t].setdefault(k, v)
    cards = pv.merge_policy_cards(cards, ctx.pvs)
    ctx.policy = ai_validation.build_policy(ai, rules, cards, [t for t, _ in model_registry.SLOTS.values()])


def resolve_spot(ctx: Ctx, route: Route, km: float, det_type: str, key: tuple) -> float:
    """Metres of a spot, shifted away from hazards that existed before this seed."""
    if key in ctx.spot_shift:
        return ctx.spot_shift[key]
    m = km * 1000
    for _ in range(12):
        lat, lon, _b = route.at(m)
        if not any(t == det_type and haversine_m(lat, lon, la, lo) < 150 for t, la, lo in ctx.existing_hazards):
            break
        m += 120
    if abs(m - km * 1000) > 1:
        print(f"    spot {key} shifted by {m - km * 1000:.0f} m (pre-existing hazard nearby)")
    ctx.spot_shift[key] = m
    return m


async def find_off_road_spot(ctx: Ctx, route: Route, m0: float, lo: float, hi: float) -> float:
    """A position where a vehicle 15 m right of the road is > 13 m from any road (OSRM)."""
    cands = sorted([m0 + k * 60 for k in range(-25, 26)], key=lambda m: abs(m - m0))
    for m in cands:
        if not lo <= m <= hi:
            continue
        lat, lon, brg = route.at(m)
        tl, tn = offset(lat, lon, brg, 17, 15)
        d, _ = await road_distance(tl, tn)
        if d is not None and d > 13.5:
            return m
    ctx.warn(f"no off-road spot found near {route.key} {m0 / 1000:.2f} km")
    return m0


def sign_inputs(rng: random.Random, d: DSpec) -> tuple[float, Optional[str], Optional[float], list]:
    """(detector conf, predicted code, P(code), top-3 candidates)."""
    code = d.code or "stop"
    if d.intent == "auto":
        conf, p = rng.uniform(0.7, 0.92), rng.uniform(0.88, 0.98)
    elif d.intent == "not_a_sign":
        conf, p = rng.uniform(0.4, 0.6), rng.uniform(0.88, 0.96)
    elif d.intent == "unknown":
        conf = rng.uniform(0.5, 0.66)
        p = rng.uniform(0.5, 0.7) if code == "unknown_sign" else rng.uniform(0.3, 0.43)
    elif d.intent == "lowconf":
        conf, p = rng.uniform(0.2, 0.29), rng.uniform(0.4, 0.7)
    else:
        conf, p = rng.uniform(0.45, 0.64), rng.uniform(0.55, 0.84)
    alts = [c for c in ("speed_limit_50", "speed_limit_40", "give_way", "no_entry", "no_parking", "stop",
                        "speed_limit_30", "unknown_sign", "not_a_sign") if c != code]
    rng.shuffle(alts)
    rest = 1 - p
    a = round(rest * rng.uniform(0.45, 0.75), 4)
    cands = [{"code": code, "score": round(p, 4)}, {"code": alts[0], "score": a},
             {"code": alts[1], "score": round(max(0.0, rest - a) * rng.uniform(0.3, 0.8), 4)}]
    return round(conf, 4), code, round(p, 4), cands


async def create_detection(db, ctx: Ctx, run: JobRun, d: DSpec, phys_ctx) -> Optional[Detection]:
    """One detection as the processor stores it (status from the AI decision)."""
    rng = ctx.rng
    route = run.route
    key = (route.key, round(d.km, 3), "pv" if d.type == "parked_vehicle" else d.type)
    m_spot = resolve_spot(ctx, route, d.km, d.type, key)
    if d.scen == "off_road":
        lo, hi = run.spec.km0 * 1000 + 300, (run.spec.stop_km or run.spec.km1) * 1000 - 300
        okey = key + ("off_road",)
        if okey not in ctx.spot_shift:
            ctx.spot_shift[okey] = await find_off_road_spot(ctx, route, m_spot, lo, hi)
        m_spot = ctx.spot_shift[okey]
    m = m_spot + d.along
    lat, lon, brg = route.at(m)
    if d.type != "parked_vehicle":
        lat, lon = offset(lat, lon, brg, 0, rng.uniform(-1.2, 1.2))
    t = run.time_at(m)
    frame = int(round(t * FPS / STEP)) * STEP
    t = frame / FPS
    rec = run.rec(t)
    speed = run.speed_at(m)
    det_id = sid()
    model_key = MODEL_OF[d.type]
    vehicle_id = run.spec.vehicle_id
    loc_status, loc_source, interpolated = LocationStatus.confirmed.value, LocationSource.ocr.value, False
    raw_gps = f"N{lat:.6f} E{lon:.6f} {speed:.0f}km/h"
    true_pos = (lat, lon)
    if d.loc == "approx":
        lat, lon = offset(lat, lon, brg, rng.uniform(14, 20), 0)     # GPS-voter window estimate
        loc_status, loc_source, interpolated = LocationStatus.approximate.value, LocationSource.interpolated.value, True
        raw_gps = f"N{true_pos[0]:.3f}?? E{true_pos[1]:.2f}?.?"
    elif d.loc == "missing":
        loc_status, loc_source = LocationStatus.missing.value, LocationSource.none.value
        raw_gps = "N4.0?? E9.?4?1 ---km/h" if route.city == "douala" else "N?.???? E??.???"
        run.gaps.append(t)

    city = route.city
    osd = f"{local_naive(rec):%Y/%m/%d %H:%M:%S}   " + (
        f"N{true_pos[0]:.5f} E{true_pos[1]:.5f}" if d.loc == "ok" else "N --.----- E --.-----") + \
        f"   {speed:03.0f}KM/H   {vehicle_id}"
    loc_h = local_naive(rec).hour + local_naive(rec).minute / 60
    scene = Scene(rng, city, osd, night=not (6.2 <= loc_h < 18.6))

    verifier, predicted_class, candidates, ai_details, verifier_model, rule_reject = None, None, None, None, None, None
    blockers: tuple = ()
    subtype = None
    if d.type == "parked_vehicle":
        sc = _scenario(d, rng)
        sightings, ego, best = vehicle_track(rng, (lat, lon), brg, t, frame, sc, d.cls, ctx.pvs, run.spec.rec_start)
        ev = await judge_track(sightings, ego, ctx.pvs, vehicle_id)
        run.verdicts[ev.verdict] = run.verdicts.get(ev.verdict, 0) + 1
        if not ev.store:
            ctx.warn(f"{run.spec.key} parked {d.scen}@{d.km} → verdict {ev.verdict} (not stored): {ev.reason_text()}")
            return None
        expected = {"parked": "parked", "fallback": "parked", "moving": "moving", "oncoming": "moving",
                    "queue": "traffic_queue", "not_grounded": "not_grounded", "off_road": "off_road"}[d.scen]
        if ev.verdict != expected:
            ctx.warn(f"{run.spec.key} parked {d.scen}@{d.km}: verdict {ev.verdict} (expected {expected}) — "
                     f"{ev.reason_text()}")
        box = best.box
        conf = best.conf
        subtype = ev.subtype
        verifier = ev.score
        ai_details = ev.details()
        verifier_model = ctx.pv_version
        blockers = tuple(ev.blockers)
        if ev.rejection_code:
            rule_reject = (ev.rejection_code, ev.reason_text())
        scene.vehicle(box, ev.vehicle_class)
        if not sc["gps"]:
            lat = lon = None
            raw_gps = None
        label = (f"{ev.verdict if ev.rejection_code else 'Parked'} {ev.subtype or ''} {ev.vehicle_class} "
                 f"{conf:.0%} / score {ev.score:.0%}")
    else:
        if d.type == "pothole":
            box = scene.pothole()
            if d.intent == "auto":
                conf, verifier = rng.uniform(0.74, 0.93), rng.uniform(0.87, 0.98)
            elif d.intent == "lowconf":
                conf, verifier = rng.uniform(0.2, 0.29), rng.uniform(0.3, 0.6)
            elif d.intent == "verifier_reject":
                conf, verifier = rng.uniform(0.45, 0.6), rng.uniform(0.06, 0.2)
            else:
                conf, verifier = rng.uniform(0.48, 0.68), rng.uniform(0.4, 0.84)
            verifier_model = VERIFIER_VERSIONS["pothole"]
            label = f"Pothole {conf:.0%} / cls {verifier:.0%}"
        elif d.type == "speed_bump":
            box = scene.speed_bump()
            conf = rng.uniform(0.72, 0.92) if d.intent == "auto" else rng.uniform(0.45, 0.68)
            label = f"SpeedBump {conf:.0%}"
        else:
            conf, predicted_class, verifier, candidates = sign_inputs(rng, d)
            th = ctx.policy.for_type("traffic_sign")
            subtype = ai_validation.sign_subtype(th, predicted_class, verifier)
            box = scene.sign(predicted_class if predicted_class not in ("unknown_sign",) else "unknown")
            verifier_model = VERIFIER_VERSIONS["traffic_sign"]
            label = f"{subtype} {conf:.0%} / cls {verifier:.0%}"
        conf = round(conf, 4)
        verifier = round(verifier, 4) if verifier is not None else None

    if d.loc == "missing":
        lat = lon = None
    frame_img = scene.finish().filter(ImageFilter.GaussianBlur(0.6))
    annotated = annotate(frame_img, box, DET_COLOUR[model_key], label)
    tag = DET_TAG[model_key]
    job_id = run.job.id
    img_url = upload_image(jpeg(annotated), f"{job_id}/{model_key}/job-{job_id[:8]}_det-{det_id[:8]}_f{frame:06d}_{tag}.jpg")
    crop_url = upload_image(jpeg(crop(frame_img, box), 90),
                            f"{job_id}/{model_key}/crops/det-{det_id[:8]}_f{frame:06d}_crop.jpg")

    draw = 0.0 if d.intent == "audit" else None
    if rule_reject is not None:
        decision = ai_validation.rule_reject(ctx.policy, rule_reject[0], rule_reject[1], loc_status, None)
    else:
        decision = ai_validation.decide(ctx.policy, d.type, conf, verifier, loc_status, draw,
                                        predicted_class=predicted_class, accept_blockers=blockers)
    status, action, rejection_code = decision.status, decision.action, decision.rejection_code
    want = {"auto": None, "review": "review", "audit": "audit"}.get(d.intent)
    if want == "review" and decision.ai_decision != "review":
        ctx.warn(f"{run.spec.key} {d.type}@{d.km}: AI decided {decision.ai_decision} ({decision.reason}), expected review")
    if d.intent == "auto" and d.scen == "parked" and status != "validated":
        ctx.warn(f"{run.spec.key} {d.type}@{d.km}: AI decided {decision.ai_decision} ({decision.reason}), expected accept")

    geo = address(route, m, lat if lat is not None else true_pos[0], lon if lon is not None else true_pos[1])
    has_pos = lat is not None
    created = run.processed_at(t)
    det = Detection(
        id=det_id, job_id=job_id, type=d.type, subtype=subtype, confidence=conf,
        predicted_type=d.type, predicted_subtype=subtype,
        frame_number=frame, video_time_s=round(t, 3),
        bbox_x1=float(box[0]), bbox_y1=float(box[1]), bbox_x2=float(box[2]), bbox_y2=float(box[3]),
        model_name=model_key, model_version=MODEL_VERSIONS[model_key],
        verifier_score=verifier, verifier_model=verifier_model if verifier is not None else None,
        class_candidates=candidates, ai_details=ai_details,
        ai_decision=decision.ai_decision, ai_decision_reason=decision.reason[:250], audit_sample=decision.audit_sample,
        raw_gps_text=raw_gps, latitude=round(lat, 7) if has_pos else None,
        longitude=round(lon, 7) if has_pos else None, gps_interpolated=interpolated,
        location_status=loc_status, location_source=loc_source,
        country=geo["country"] if has_pos else None, city=geo["city"] if has_pos else None,
        speed_kmh=speed, vehicle_id=vehicle_id,
        captured_at=local_naive(rec), recorded_at=aware(rec), image_url=img_url, crop_url=crop_url,
        location_name=geo["location_name"] if has_pos else None,
        road=geo["road"] if has_pos else None, locality=geo["locality"] if has_pos else None,
        district=geo["district"] if has_pos else None, town=geo["town"] if has_pos else None,
        region=geo["region"] if has_pos else None, display_location=geo["display"] if has_pos else None,
        geocoded_at=created if has_pos else None,
        review_status=ReviewStatus(status), created_at=created,
    )
    if action != "create":
        det.reviewed_by, det.reviewed_at = "system", created
    if rejection_code:
        det.rejection_code, det.rejection_source = rejection_code, "auto"
    db.add(det)
    details = {"confidence": conf, "verifier_score": verifier, "ai_decision": decision.ai_decision,
               "reason": decision.reason, "mode": ctx.policy.mode}
    if predicted_class is not None:
        details["predicted_class"] = predicted_class
    if blockers:
        details["accept_blockers"] = [c for c, _ in blockers]
    if ai_details:
        details.update({k: ai_details.get(k) for k in ("verdict", "severity", "vehicle_class")})
    if decision.audit_sample:
        details["audit_sample"] = True
    wf.record_creation(db, det, action, details, audited=decision.audit_sample)
    if status in (ReviewStatus.validated.value, ReviewStatus.corrected.value):
        await db.flush()
        await hazard_service.attach(db, det, phys_ctx)
    run.by_model[model_key] = run.by_model.get(model_key, 0) + 1
    run.conf_sum += conf
    run.n += 1
    det._spec = d  # type: ignore[attr-defined]
    det._m = m  # type: ignore[attr-defined]
    det._true = true_pos  # type: ignore[attr-defined]
    if d.name:
        ctx.dets[d.name] = det
    ctx.counts[f"{d.type}:{status}"] = ctx.counts.get(f"{d.type}:{status}", 0) + 1
    return det


async def extra_vehicle_tracks(ctx: Ctx, run: JobRun) -> None:
    """Tracks the rules judged but did not store (job metrics only): far / short ones."""
    rng = ctx.rng
    for i in range(rng.randint(3, 6)):
        m = run.spec.km0 * 1000 + rng.uniform(200, (run.spec.km1 - run.spec.km0) * 1000 - 200)
        lat, lon, brg = run.route.at(m)
        t = run.time_at(m)
        frame = int(round(t * FPS / STEP)) * STEP
        far = i % 2 == 0
        sc = dict(L=3.6 if far else -3.8, ego=30, r0=75 if far else 30, r1=40 if far else 12, conf=0.6,
                  vT=0.0 if far else -8.0, hf=1.0, gps=True)
        sts, ego, best = vehicle_track(rng, (lat, lon), brg, frame / FPS, frame, sc, "car", ctx.pvs,
                                       run.spec.rec_start, short=not far)
        if len(sts) < 2:
            continue
        ev = await judge_track(sts, ego, ctx.pvs, run.spec.vehicle_id)
        run.verdicts[ev.verdict] = run.verdicts.get(ev.verdict, 0) + 1
    for _ in range(rng.randint(4, 9)):
        run.verdicts["moving"] = run.verdicts.get("moving", 0) + 1     # passing traffic (not stored)


async def insert_track(db, run: JobRun) -> None:
    rows = []
    for t, s, v in zip(run.t, run.s, run.v):
        if any(abs(t - g) <= 1.0 for g in run.gaps):
            continue
        lat, lon, _ = run.route.at(s)
        rows.append({"job_id": run.job.id, "frame_number": int(t * FPS), "video_time_s": t, "latitude": round(lat, 7),
                     "longitude": round(lon, 7), "speed_kmh": v, "source": "ocr", "recorded_at": aware(run.rec(t))})
    for i in range(0, len(rows), 500):
        await db.execute(insert(JobTrackPoint), rows[i:i + 500])


def job_event(db, job_id: str, event: str, at: datetime, actor: str = "system", details: Optional[dict] = None):
    db.add(JobEvent(job_id=job_id, event=event, actor=actor, details=details, at=at))


async def process_job(db, ctx: Ctx, run: JobRun, specs: list[DSpec]) -> list[Detection]:
    """The video processor's work for one job, at its processing time."""
    sp = run.spec
    job = run.job
    db.add(job)
    await db.flush()
    async with at_time(db, ctx, run.finished):
        if sp.status == "failed":
            job_event(db, job.id, "created", run.created, TESTER_EMAIL if sp.source == "upload" else "system",
                      {"source_type": sp.source, "frame_skip": STEP, "models": sp.models.split(",")})
            for attempt in (1, 2, 3):
                at = run.created + timedelta(minutes=2 + attempt * 3)
                job_event(db, job.id, "queued", at, details={"attempt": attempt})
                job_event(db, job.id, "started", at + timedelta(seconds=20), details={"attempt": attempt})
                if attempt < 3:
                    job_event(db, job.id, "progress_failed", at + timedelta(seconds=40),
                              details={"error": job.error, "attempt": attempt})
                    job_event(db, job.id, "retried", at + timedelta(minutes=1), details={"attempt": attempt + 1})
            job_event(db, job.id, "failed", run.finished, details={"error": job.error, "attempt": 3})
            return []
        phys_ctx = await hazard_service.load_context(db)
        dets = []
        for d in specs:
            det = await create_detection(db, ctx, run, d, phys_ctx)
            if det is not None:
                dets.append(det)
        await insert_track(db, run)
        if "vehicles" in sp.models:
            await extra_vehicle_tracks(ctx, run)
        job.detections = run.n
        job.detections_by_model = dict(run.by_model)
        job.avg_confidence = round(run.conf_sum / run.n, 4) if run.n else None
        frames = int(run.duration * FPS)
        planned = (sp.km1 - sp.km0) / ((sp.stop_km or sp.km1) - sp.km0)
        job.total_frames = int(frames * planned)
        job.processed = frames
        job.duration_s = round(job.total_frames / FPS, 2)
        skips = {mk: STEP for mk in sp.models.split(",")}
        actor = TESTER_EMAIL if sp.source == "upload" else "system"
        job_event(db, job.id, "created", run.created, actor,
                  {"source_type": sp.source, "frame_skip": STEP, "models": sp.models.split(",")})
        job_event(db, job.id, "queued", run.created + timedelta(seconds=5), actor, {"attempt": 1})
        job_event(db, job.id, "started", run.started, details={"attempt": 1, "total_frames": job.total_frames,
                                                               "fps": FPS, "frame_skip": skips})
        extra = {}
        if "vehicles" in sp.models:
            extra = {"vehicle_verdicts": dict(run.verdicts), "vehicle_boxes_masked": ctx.rng.randint(12, 64)}
        if sp.status == "cancelled":
            job.status = JobStatus.cancelled
            job.cancel_requested = True
            job.cancelled_at = run.finished
            job.cancelled_by = TESTER_EMAIL
            job_event(db, job.id, "cancel_requested", run.finished - timedelta(seconds=40), TESTER_EMAIL, {})
            job_event(db, job.id, "cancelled", run.finished, TESTER_EMAIL,
                      {"processed": frames, "detections": run.n, **extra})
        else:
            job.status = JobStatus.done
            job_event(db, job.id, "done", run.finished,
                      details={"processed": frames, "detections": run.n, "by_model": dict(run.by_model), **extra})
    if sp.status == "done":
        async with at_time(db, ctx, run.finished + timedelta(seconds=4)):
            summary = await video_pass.evaluate_job(db, job.id)
        if summary.get("not_seen") or summary.get("resolved") or summary.get("rejected"):
            print(f"    {sp.key} lifecycle: sightings={summary['sightings']} not_seen={summary['not_seen']} "
                  f"rejected={summary['rejected']} resolved={summary['resolved']}")
    return dets


async def review_job(db, ctx: Ctx, run: JobRun, dets: list[Detection]) -> None:
    """The validator's decisions on a job's detections, at its review time."""
    todo = [d for d in dets if d._spec.act]  # type: ignore[attr-defined]
    if not todo:
        return
    actor = ctx.tester_actor
    async with at_time(db, ctx, run.review_at):
        phys_ctx = await hazard_service.load_context(db)
        for det in todo:
            act: str = det._spec.act  # type: ignore[attr-defined]
            status = det.review_status.value if isinstance(det.review_status, ReviewStatus) else det.review_status
            if act == "locfix":
                if status != "location_pending":
                    ctx.warn(f"locfix on {det.id} in status {status}")
                    continue
                tl, tn = det._true  # type: ignore[attr-defined]
                pts = (await db.execute(select(JobTrackPoint).where(JobTrackPoint.job_id == det.job_id))).scalars().all()
                tp = min(pts, key=lambda p: haversine_m(tl, tn, p.latitude, p.longitude))
                previous = {"latitude": det.latitude, "longitude": det.longitude,
                            "location_status": det.location_status, "location_source": det.location_source}
                det.latitude, det.longitude, det.speed_kmh = tp.latitude, tp.longitude, tp.speed_kmh
                det.location_status = LocationStatus.confirmed.value
                det.location_source = LocationSource.trajectory.value
                geo = address(run.route, det._m, tp.latitude, tp.longitude)  # type: ignore[attr-defined]
                det.location_name, det.city, det.country = geo["location_name"], geo["city"], geo["country"]
                for k in ("road", "locality", "district", "town", "region"):
                    setattr(det, k, geo[k])
                det.display_location = geo["display"]
                det.geocoded_at = run.review_at
                await wf._enter(db, det, ReviewStatus.pending.value, "location_fix", actor,
                                note="Position choisie sur la trajectoire du véhicule", details={
                                    "method": "trajectory_point", "previous": previous,
                                    "new": {"latitude": tp.latitude, "longitude": tp.longitude,
                                            "location_source": det.location_source},
                                    "track_point_id": tp.id})
                await wf.accept(db, det, actor, severity_score=2, note="Nid-de-poule confirmé")
            elif status not in ("pending",):
                ctx.warn(f"{run.spec.key} {det.type}@{det._spec.km}: cannot '{act}' a detection in status {status}")  # type: ignore[attr-defined]
                continue
            elif act == "accept":
                note = {"pothole": "Nid-de-poule visible, à signaler à la CUD", "speed_bump": "Ralentisseur confirmé",
                        "traffic_sign": "Panneau lisible", "parked_vehicle": "Véhicule stationné confirmé"}[det.type]
                note = det._spec.note or note  # type: ignore[attr-defined]
                await wf.accept(db, det, actor, severity_score=ctx.rng.randint(1, 3), note=note)
            elif act.startswith("reject:"):
                code = act.split(":", 1)[1]
                await wf.reject(db, det, actor, reason_code=code, note=None)
            elif act.startswith("correct:"):
                _, typ, sub = act.split(":")
                await wf.correct(db, det, actor, type=typ, subtype=sub or None,
                                 note="Classe corrigée par le validateur")
            ctx.counts[f"human:{act.split(':')[0]}"] = ctx.counts.get(f"human:{act.split(':')[0]}", 0) + 1


async def hazard_of(db, det: Detection) -> Optional[Hazard]:
    await db.refresh(det, ["hazard_id"])
    return await db.get(Hazard, det.hazard_id) if det.hazard_id else None


async def manual_resolve(db, ctx: Ctx, name: str, when: datetime, note: str) -> None:
    det = ctx.dets.get(name)
    hz = await hazard_of(db, det) if det is not None else None
    if hz is None or hz.status != HazardStatus.active.value:
        ctx.warn(f"manual resolve {name}: hazard missing or not active")
        return
    async with at_time(db, ctx, when):
        n = await lc_service.resolve(db, hz, "manual", actor=ctx.tester_actor, note=note)
        audit.record(db, None, "hazard.resolve", principal=ctx.tester_actor.principal, actor_label=TESTER_EMAIL,
                     object_type="hazard", object_id=hz.id,
                     details={"code": hz.code, "detections": n, "note": note, "source": "manual"})
    print(f"    {hz.code} ({hz.type}) resolved manually")


async def hazard_vote(db, ctx: Ctx, name: str, driver: int, vote: str, when: datetime) -> None:
    det = ctx.dets.get(name)
    hz = await hazard_of(db, det) if det is not None else None
    if hz is None:
        ctx.warn(f"vote on {name}: no hazard")
        return
    u = ctx.drivers[driver]
    lat, lon = offset(hz.latitude, hz.longitude, ctx.rng.uniform(0, 360), ctx.rng.uniform(25, 140), 0)
    async with at_time(db, ctx, when):
        cfg = await lc_service.load_settings(db, fresh=True)
        res = await lc_votes.cast_vote(db, hz, vote, lat, lon, cfg, principal=driver_principal(u),
                                       device_id=f"android-webtest-{driver + 1}", now=when)
    print(f"    vote {vote} on {hz.code} by driver{driver + 1}: counted={res['counted']} reason={res['reason']} "
          f"resolved={res['resolved']}")


# ── signalements & cloud imports ─────────────────────────────────────────────

SIGNALEMENTS = [
    # (key, route, km, lateral m, type, description, minutes ago, driver, photo)
    ("ndokoti", "D", 15.4, 14, "embouteillage", "Bouchon au carrefour Ndokoti, circulation à l'arrêt", 35, 0, False),
    ("ndokoti_dup", "D", 15.46, 18, "embouteillage", "Gros embouteillage à Ndokoti, ça n'avance pas", 20, 2, False),
    ("akwa_acc", "D", 10.8, 9, "accident", "Accrochage entre un taxi et une moto, voie de droite bloquée", 50, 1, False),
    ("deido_trav", "D", 12.7, 12, "travaux", "Travaux de réhabilitation de la chaussée, circulation sur une voie",
     60 * 30, 0, False),
    ("bonaberi_ndp", "D", 1.9, 6, "nid_de_poule", "Gros nid-de-poule sur la voie de droite après le marché",
     60 * 70, 2, True),
    ("bonanjo_pol", "D", 6.5, 10, "police", "Contrôle de police, ralentir", 40, 1, False),
    ("makepe_inond", "M", 4.0, 10, "inondation", "Chaussée inondée après l'averse, passage difficile", 5 * 60, 0, False),
    ("mokolo_ferme", "Y", 7.6, 8, "route_fermee", "Route fermée devant le marché Mokolo (déviation par la droite)",
     6 * 60, 2, False),
    ("mvan_panne", "Y", 1.0, 9, "vehicule_panne", "Camion en panne sur la voie de droite", 60, 1, False),
    ("bastos_danger", "Y", 10.2, 7, "danger", "Câble électrique tombé sur la chaussée", 3 * 60, 0, False),
    ("edea_obst", "E", 5.5, 8, "obstacle", "Branches d'arbre sur la route après l'orage", 4 * 60, 2, False),
    ("yde_rejete", "Y", 4.8, 10, "accident", "Accident grave !!!", 26 * 60, 1, False),
]


async def seed_signalements(db, ctx: Ctx) -> dict:
    community = await get_section(db, "community")
    snapshot = await clustering_service.get_snapshot(db)
    out = {}
    for key, rk, km, lat_m, typ, desc, mins, drv, photo in SIGNALEMENTS:
        route = ctx.routes[rk]
        lat, lon, brg = route.at(km * 1000)
        lat, lon = offset(lat, lon, brg, 0, lat_m)
        when = NOW - timedelta(minutes=mins)
        s_id = sid()
        image_url = None
        if photo:
            sc = Scene(ctx.rng, route.city, f"{local_naive(when):%Y/%m/%d %H:%M}")
            box = sc.pothole()
            image_url = upload_signalement_image(jpeg(crop(sc.finish(), box, 120)), f"{TAG}/{s_id}.jpg")
        geo = address(route, km * 1000, lat, lon)
        sig = Signalement(id=s_id, type=SignalementType(typ), status=SignalementStatus.actif,
                          latitude=round(lat, 7), longitude=round(lon, 7), description=desc, image_url=image_url,
                          reported_by=ctx.drivers[drv].id, location_name=geo["location_name"],
                          blocked_bearing=round(brg, 1) if typ in ("accident", "route_fermee", "travaux") else None,
                          reported_at=when, last_reported_at=None)
        async with at_time(db, ctx, when):
            res = await clustering_service.resolve(db, lat, lon, "signalement", typ)
            sig.cluster_zone_id, sig.cluster_environment, sig.cluster_radius_m = res.zone_id, res.environment, res.radius_m
            target = None
            if snapshot.settings.merge_signalements:
                target = await find_merge_target(db, typ, lat, lon, res.radius_m, community)
            if target is not None and not is_seeded(target.id):
                ctx.warn(f"report {key} would merge into a pre-existing report {target.id} — not merged")
                target = None
            if target is not None:
                sig.merged_into_id = target.id
                target.report_count = (target.report_count or 1) + 1
                target.last_reported_at = when
            db.add(sig)
            if key == "yde_rejete":
                sig.status = SignalementStatus.rejete
                sig.moderated_by = TESTER_EMAIL
                sig.moderated_at = when + timedelta(hours=2)
                sig.moderation_note = "Signalement erroné — aucun accident constaté par la patrouille"
                audit.record(db, None, "signalement.reject", principal=ctx.tester_actor.principal,
                             object_type="signalement", object_id=s_id, details={"note": sig.moderation_note})
        out[key] = sig
    votes = [("ndokoti", 1, "present", 30, 60), ("ndokoti", 2, "present", 25, 90), ("akwa_acc", 0, "present", 42, 50),
             ("deido_trav", 1, "present", 60 * 20, 120), ("deido_trav", 2, "absent", 60 * 8, 2500),
             ("bonaberi_ndp", 0, "present", 60 * 50, 40), ("bonaberi_ndp", 1, "present", 60 * 30, 80),
             ("makepe_inond", 2, "absent", 3 * 60, 70), ("mokolo_ferme", 0, "present", 5 * 60, 150),
             ("mvan_panne", 0, "present", 45, 60), ("bastos_danger", 2, "present", 2 * 60, 30)]
    for key, drv, vote, mins, dist in votes:
        sig = out[key]
        when = NOW - timedelta(minutes=mins)
        vlat, vlon = offset(sig.latitude, sig.longitude, ctx.rng.uniform(0, 360), dist, 0)
        async with at_time(db, ctx, when):
            await signalement_votes.cast_vote(db, sig, vote, vlat, vlon, f"android-webtest-{drv + 1}", community,
                                              principal=driver_principal(ctx.drivers[drv]))
    return out


async def seed_cloud(db, ctx: Ctx) -> None:
    """Demo connection (disabled, no token) — created before the jobs that reference it."""
    created = local_day(12, "10:00")
    conn = CloudConnection(id=sid(), provider="google_drive", name="DEMO · Drive dashcam (test, désactivé)",
                           account_email="demo@example.com", folder_id=None, folder_name="Dashcam/Exports",
                           status="disabled", refresh_token_enc=None, created_by=TESTER_EMAIL, created_at=created,
                           last_sync_at=None, last_sync_status=None, enabled=False, auto_import=False,
                           files_imported_total=0, last_sync_files=0)
    db.add(conn)
    await db.commit()
    ctx.connection = conn


async def seed_imports(db, ctx: Ctx) -> None:
    conn = ctx.connection
    j = ctx.jobs

    def item(batch, name, status, *, run=None, error=None, size=None, dup=None, started=None, finished=None,
             path="/Dashcam/Exports"):
        fid = "1" + uuid.uuid4().hex[:27]
        return CloudImportItem(
            id=sid(), batch_id=batch.id, connection_id=conn.id, file_id=fid, version=uuid.uuid4().hex[:16],
            name=name, path=path, mime_type="video/mp4" if name.lower().endswith(".mp4") else "video/quicktime",
            checksum=uuid.uuid4().hex, modified_at=(started or batch.created_at).isoformat() + "Z", status=status,
            bytes_total=size, bytes_done=size if status in ("done", "skipped_duplicate") or run else 0,
            speed_bps=round(size / 95.0, 1) if size and run else None, attempts=3 if status == "failed" and not run else 1,
            error=error, dest_path=None, job_id=run.job.id if run else None, duplicate_of_job_id=dup,
            created_at=batch.created_at, started_at=started or batch.created_at,
            finished_at=finished or batch.finished_at, updated_at=finished or batch.finished_at)

    b1t = j["J5"].created - timedelta(minutes=6)
    b1 = CloudImportBatch(id=sid(), connection_id=conn.id, provider="google_drive", connection_name=conn.name,
                          origin="manual", models=j["J5"].spec.models, frame_skip=STEP, options={"reimport": False},
                          item_count=5, created_by=TESTER_EMAIL, created_at=b1t, finished_at=j["J5"].finished)
    db.add(b1)
    await db.flush()
    items = [
        item(b1, filename_for(j["J5"].spec), "done", run=j["J5"], size=1_843_200_512, path="/Dashcam/Exports/Yaoundé",
             finished=j["J5"].finished),
        item(b1, filename_for(j["J6"].spec), "done", run=j["J6"], size=2_097_152_000, path="/Dashcam/Exports/Edéa",
             finished=j["J6"].finished),
        item(b1, filename_for(j["J5"].spec).replace(".MP4", " (1).MP4"), "skipped_duplicate", size=1_843_200_512,
             dup=j["J5"].job.id, path="/Dashcam/Exports/Yaoundé", finished=b1t + timedelta(seconds=20)),
        item(b1, f"{local_naive(b1t - timedelta(days=1)):%Y%m%d}_172201_R.MP4", "failed", size=1_201_345_678,
             error="Download failed after 3 attempts: HTTP 403 insufficientFilePermissions — the file's owner "
                   "removed sharing.", finished=b1t + timedelta(minutes=4)),
        item(b1, "trajet_nuit_edea.MOV", "failed", size=3_456_789_012,
             error="Unsupported video: HEVC 10-bit (hvc1) cannot be decoded by the processor — export it as "
                   "H.264 MP4.", finished=b1t + timedelta(minutes=9)),
    ]
    b2t = j["J9"].created - timedelta(minutes=5)
    b2_end = max(j["J6b"].finished, j["J8"].finished, j["J9"].finished)
    b2 = CloudImportBatch(id=sid(), connection_id=conn.id, provider="google_drive", connection_name=conn.name,
                          origin="manual", models=j["J6b"].spec.models, frame_skip=STEP, options={"reimport": False},
                          item_count=3, created_by=TESTER_EMAIL, created_at=b2t, finished_at=b2_end)
    db.add(b2)
    await db.flush()
    items += [
        item(b2, filename_for(j["J9"].spec), "cancelled", run=j["J9"], size=1_572_864_000,
             path="/Dashcam/Exports/Yaoundé", finished=j["J9"].finished),
        item(b2, filename_for(j["J6b"].spec), "done", run=j["J6b"], size=2_202_009_600, path="/Dashcam/Exports/Edéa",
             finished=j["J6b"].finished),
        item(b2, filename_for(j["J8"].spec), "failed", run=j["J8"], size=734_003_200, error=j["J8"].job.error,
             path="/Dashcam/Exports/Douala", finished=j["J8"].finished),
    ]
    for it in items:
        db.add(it)
    conn.files_imported_total = 3
    conn.last_sync_files = 0
    await db.commit()


# ── purge ─────────────────────────────────────────────────────────────────────

async def purge(db, drop_tester: bool = False) -> dict:
    like = f"%-{SUFFIX}"
    job_ids = list((await db.execute(select(Job.id).where(Job.id.like(like)))).scalars().all())
    det_ids = list((await db.execute(select(Detection.id).where(Detection.job_id.in_(job_ids or ["-"])))).scalars().all())
    driver_ids = list((await db.execute(select(User.id).where(User.id.like(like)))).scalars().all())
    hz_ids = set((await db.execute(select(Detection.hazard_id).where(
        Detection.id.in_(det_ids or ["-"]), Detection.hazard_id.isnot(None)))).scalars().all())
    hz_ids |= set((await db.execute(select(HazardEvent.hazard_id).where(or_(
        HazardEvent.job_id.in_(job_ids or ["-"]), HazardEvent.detection_id.in_(det_ids or ["-"]))))).scalars().all())
    hz_ids |= set((await db.execute(select(HazardVote.hazard_id).where(
        HazardVote.user_id.in_(driver_ids or ["-"])))).scalars().all())
    full, mixed = [], []
    for hid in hz_ids:
        others = (await db.execute(select(func.count(Detection.id)).where(
            Detection.hazard_id == hid, Detection.id.notin_(det_ids or ["-"])))).scalar()
        (mixed if others else full).append(hid)
    sig_ids = list((await db.execute(select(Signalement.id).where(Signalement.id.like(like)))).scalars().all())
    conn_ids = list((await db.execute(select(CloudConnection.id).where(CloudConnection.id.like(like)))).scalars().all())
    batch_ids = list((await db.execute(select(CloudImportBatch.id).where(CloudImportBatch.id.like(like)))).scalars().all())
    zone_ids = [str(z) for z in (await db.execute(select(ClusterZone.id).where(ClusterZone.created_by == TAG))).scalars().all()]
    opts = {"synchronize_session": False}

    def ex(stmt):
        return db.execute(stmt.execution_options(**opts))

    await ex(delete(HazardEvent).where(or_(HazardEvent.hazard_id.in_(full or ["-"]),
                                           HazardEvent.job_id.in_(job_ids or ["-"]),
                                           HazardEvent.detection_id.in_(det_ids or ["-"]),
                                           HazardEvent.user_id.in_(driver_ids or ["-"]))))
    await ex(delete(HazardVote).where(or_(HazardVote.hazard_id.in_(full or ["-"]),
                                          HazardVote.user_id.in_(driver_ids or ["-"]))))
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
    obj_ids = set(det_ids) | set(full) | set(sig_ids) | set(job_ids) | set(conn_ids) | set(batch_ids)
    await ex(delete(AuditLog).where(or_(AuditLog.object_id.in_(obj_ids or {"-"}),
                                        AuditLog.actor_id.in_(driver_ids or ["-"]),
                                        (AuditLog.object_type == "cluster_zone") & AuditLog.object_id.in_(zone_ids or ["-"]))))
    await ex(delete(CloudImportItem).where(or_(CloudImportItem.id.like(like), CloudImportItem.batch_id.in_(batch_ids or ["-"]))))
    await ex(delete(CloudImportBatch).where(CloudImportBatch.id.in_(batch_ids or ["-"])))
    await ex(delete(JobEvent).where(JobEvent.job_id.in_(job_ids or ["-"])))
    await ex(delete(JobTrackPoint).where(JobTrackPoint.job_id.in_(job_ids or ["-"])))
    await ex(delete(Job).where(Job.id.in_(job_ids or ["-"])))
    await ex(delete(CloudConnection).where(CloudConnection.id.in_(conn_ids or ["-"])))
    await ex(delete(SignalementVote).where(or_(SignalementVote.signalement_id.in_(sig_ids or ["-"]),
                                               SignalementVote.voter_user_id.in_(driver_ids or ["-"]))))
    await ex(delete(SignalementPass).where(SignalementPass.signalement_id.in_(sig_ids or ["-"])))
    await ex(delete(Signalement).where(Signalement.id.in_(sig_ids or ["-"])))
    await ex(delete(ClusterZone).where(ClusterZone.created_by == TAG))
    users_removed = list(driver_ids)
    if drop_tester:
        t = (await db.execute(select(User.id).where(User.email == TESTER_EMAIL))).scalar_one_or_none()
        if t:
            users_removed.append(t)
    await ex(delete(AuthSession).where(AuthSession.user_id.in_(users_removed or ["-"])))
    await ex(delete(User).where(User.id.in_(users_removed or ["-"])))
    await db.commit()
    clustering_service.invalidate()
    removed_objects = 0
    try:
        client = get_minio()
        for jid in job_ids:
            for obj in client.list_objects(settings.MINIO_BUCKET_DETECTIONS, prefix=f"{jid}/", recursive=True):
                client.remove_object(settings.MINIO_BUCKET_DETECTIONS, obj.object_name)
                removed_objects += 1
        for obj in client.list_objects(settings.MINIO_BUCKET_SIGNALEMENTS, prefix=f"{TAG}/", recursive=True):
            client.remove_object(settings.MINIO_BUCKET_SIGNALEMENTS, obj.object_name)
            removed_objects += 1
    except Exception as exc:
        print(f"  ! MinIO cleanup failed: {exc}")
    out = {"jobs": len(job_ids), "detections": len(det_ids), "hazards_deleted": len(full),
           "hazards_recomputed": len(mixed), "signalements": len(sig_ids), "zones": len(zone_ids),
           "users": len(users_removed), "minio_objects": removed_objects}
    print(f"[purge] {out}")
    return out


# ── main seed ─────────────────────────────────────────────────────────────────

async def seed(db, ctx: Ctx, tester_password: Optional[str]) -> None:
    print("[seed] routes")
    await fetch_routes(ctx)
    print("[seed] users")
    await ensure_users(db, ctx, tester_password)
    print("[seed] cluster zones")
    await ensure_zones(db, ctx)
    await build_policy(db, ctx)
    ctx.existing_hazards = [(h.type, h.latitude, h.longitude) for h in (await db.execute(select(Hazard))).scalars().all()]
    await seed_cloud(db, ctx)

    rng = ctx.rng
    specs_by_job: dict[str, list[DSpec]] = {}
    for d in det_specs():
        specs_by_job.setdefault(d.job, []).append(d)

    inter_specs, inter_dets = await plan_intercity(db, ctx)
    for d in inter_dets:
        specs_by_job.setdefault(d.job, []).append(d)
    for sp in job_specs() + inter_specs:
        run = JobRun(sp, ctx.routes[sp.route])
        run.build(rng)
        rec_end = sp.rec_start + timedelta(seconds=run.duration)
        if sp.upload_min is not None:
            run.created = rec_end + timedelta(minutes=sp.upload_min)
        elif sp.source == "upload":
            run.created = rec_end + timedelta(minutes=12 if sp.key == "J12" else rng.randint(20, 45))
        elif sp.key in ("J5", "J6"):
            run.created = local_day(4, "18:05") + timedelta(minutes=2 if sp.key == "J6" else 4)
        else:
            run.created = NOW - timedelta(hours=20) + timedelta(minutes={"J9": 1, "J6b": 3, "J8": 5}[sp.key])
        run.started = run.created + timedelta(minutes=1, seconds=rng.randint(5, 50))
        proc = timedelta(seconds=max(120, run.duration * (sp.proc_ratio or (0.35 if sp.key == "J12" else 0.55))))
        if sp.status == "failed":
            proc = timedelta(minutes=14)
        run.finished = run.started + proc
        if sp.key in ("J5",):
            run.started = max(run.started, ctx.jobs["J6"].finished + timedelta(seconds=10))
            run.finished = run.started + proc
        delay = {"J10": timedelta(minutes=25), "J11": timedelta(minutes=25), "J12": timedelta(minutes=12)}.get(
            sp.key, timedelta(minutes=6) if sp.upload_min is not None and sp.upload_min < 15
            else timedelta(hours=rng.uniform(2.0, 4.5)))
        run.review_at = min(run.finished + delay, NOW - timedelta(minutes=8))
        cloud = sp.source != "upload"
        job = Job(id=sid(), filename=filename_for(sp), status=JobStatus.processing, enabled_models=sp.models,
                  total_frames=int(run.duration * FPS), processed=0, detections=0,
                  created_at=run.created, started_at=run.started, finished_at=run.finished,
                  source_type=sp.source, source_ref=("1" + uuid.uuid4().hex[:27]) if cloud else None,
                  source_name=JOB_LABEL, connection_id=ctx.connection.id if cloud else None,
                  submitted_by=TESTER_EMAIL, attempts=3 if sp.status == "failed" else 1, max_attempts=3,
                  frame_skip=STEP, fps=FPS, duration_s=round(run.duration, 2))
        if sp.status == "failed":
            job.status = JobStatus.failed
            job.error = "Video could not be decoded: moov atom not found (truncated recording — power loss?)"
            job.processed = 0
        run.job = job
        ctx.jobs[sp.key] = run
        ctx.job_ids.append(job.id)

    # timeline
    timeline: list[tuple[datetime, int, str, Any]] = []
    for key, run in ctx.jobs.items():
        timeline.append((run.finished, 0, "process", key))
        if run.spec.status != "failed" and any(d.act for d in specs_by_job.get(key, [])):
            timeline.append((run.review_at, 1, "review", key))
    timeline.append((local_day(3, "16:00"), 2, "resolve",
                     ("P_RES", "Nid-de-poule rebouché par la Communauté urbaine de Douala (constat sur place).")))
    timeline.append((NOW - timedelta(hours=2), 2, "vote", ("TRUCK", 0, "still_there")))
    timeline.append((NOW - timedelta(minutes=105), 2, "vote", ("VOTED", 0, "still_there")))
    timeline.append((NOW - timedelta(minutes=40), 2, "vote", ("VOTED", 1, "not_there")))
    timeline.append((NOW - timedelta(minutes=22), 2, "vote", ("VOTED", 2, "not_there")))
    timeline.append((NOW - timedelta(minutes=15), 2, "resolve",
                     ("MANUAL_W", "Véhicule enlevé par la fourrière municipale — vérifié sur place.")))
    timeline.sort(key=lambda x: (x[0], x[1]))

    dets_by_job: dict[str, list[Detection]] = {}
    for when, _o, kind, payload in timeline:
        await advance(db, ctx, when)
        if kind == "process":
            run = ctx.jobs[payload]
            print(f"[job] {payload} {run.spec.status:<9} {run.route.city:<9} {local_naive(run.spec.rec_start):%d/%m %H:%M} "
                  f"{run.spec.km0:.1f}→{(run.spec.stop_km or run.spec.km1):.1f} km")
            dets_by_job[payload] = await process_job(db, ctx, run, specs_by_job.get(payload, []))
        elif kind == "review":
            await review_job(db, ctx, ctx.jobs[payload], dets_by_job.get(payload, []))
        elif kind == "resolve":
            await manual_resolve(db, ctx, payload[0], when, payload[1])
        elif kind == "vote":
            await hazard_vote(db, ctx, payload[0], payload[1], payload[2], when)
    await advance(db, ctx, datetime.utcnow())

    print("[seed] community reports")
    await seed_signalements(db, ctx)
    print("[seed] cloud imports")
    await seed_imports(db, ctx)
    print(f"[seed] detections by type:status → {dict(sorted(ctx.counts.items()))}")


# ── verification ──────────────────────────────────────────────────────────────

async def verify(db) -> None:
    import httpx
    from app.services.auth_service import create_session
    like = f"%-{SUFFIX}"
    print("\n[verify] rows")
    jobs = (await db.execute(select(Job.status, func.count()).where(Job.id.like(like)).group_by(Job.status))).all()
    print("  jobs:", {s.value if hasattr(s, "value") else s: n for s, n in jobs})
    rows = (await db.execute(select(Detection.type, Detection.review_status, func.count())
                             .join(Job, Job.id == Detection.job_id).where(Job.id.like(like))
                             .group_by(Detection.type, Detection.review_status))).all()
    by_type: dict = {}
    for t, s, n in rows:
        by_type.setdefault(t, {})[s.value if hasattr(s, "value") else s] = n
    for t, v in sorted(by_type.items()):
        print(f"  detections {t:<15} {sum(v.values()):>3}  {v}")
    seeded_hz = select(Detection.hazard_id).join(Job, Job.id == Detection.job_id).where(
        Job.id.like(like), Detection.hazard_id.isnot(None))
    hz = (await db.execute(select(Hazard.type, Hazard.status, Hazard.resolution_source, func.count())
                           .where(Hazard.id.in_(seeded_hz))
                           .group_by(Hazard.type, Hazard.status, Hazard.resolution_source))).all()
    for t, s, rs, n in sorted(hz, key=lambda r: (r[0], r[1], r[2] or "")):
        print(f"  hazards {t:<15} {s:<9} {rs or '':<14} {n}")
    big = (await db.execute(select(Hazard.code, Hazard.type, Hazard.detection_count, Hazard.cluster_zone_name,
                                   Hazard.radius_m, Hazard.display_location)
                            .where(Hazard.id.in_(seeded_hz), Hazard.detection_count >= 3)
                            .order_by(Hazard.detection_count.desc()))).all()
    for r in big:
        print(f"    cluster {r[0]} {r[1]:<14} members={r[2]} zone={r[3]} r={r[4]:.0f} m  {r[5]}")
    special = (await db.execute(select(Hazard.code, Hazard.type, Hazard.subtype, Hazard.status, Hazard.resolution_source,
                                       Hazard.absence_count, Hazard.recurring_spot, Hazard.recurrence_count,
                                       Hazard.expires_at).where(Hazard.id.in_(seeded_hz), or_(
        Hazard.type == "parked_vehicle", Hazard.absence_count > 0, Hazard.resolution_source == "manual"))
        .order_by(Hazard.status, Hazard.expires_at))).all()
    for r in special:
        exp = f"expires in {(r[8] - datetime.utcnow()).total_seconds() / 3600:+.1f} h" if r[8] and r[3] == "active" else ""
        print(f"    {r[0]} {r[1]:<14} {r[2] or '':<10} {r[3]:<8} {r[4] or '':<13} absences={r[5]} "
              f"recurring={r[6]}({r[7]}) {exp}")
    ev = (await db.execute(select(HazardEvent.kind, func.count()).where(HazardEvent.hazard_id.in_(seeded_hz))
                           .group_by(HazardEvent.kind))).all()
    print("  hazard events:", dict(ev))
    votes = (await db.execute(select(HazardVote.vote, HazardVote.counted, func.count())
                              .where(HazardVote.user_id.like(like)).group_by(HazardVote.vote, HazardVote.counted))).all()
    print("  hazard votes:", [(v, c, n) for v, c, n in votes])
    tp = (await db.execute(select(func.count()).select_from(JobTrackPoint).where(JobTrackPoint.job_id.like(like)))).scalar()
    print("  track points:", tp)
    sig = (await db.execute(select(Signalement.type, Signalement.status, Signalement.merged_into_id.isnot(None),
                                   Signalement.report_count).where(Signalement.id.like(like)))).all()
    print("  signalements:", len(sig), "merged:", sum(1 for s in sig if s[2]),
          "with report_count>1:", sum(1 for s in sig if (s[3] or 1) > 1))
    sv = (await db.execute(select(func.count()).select_from(SignalementVote).where(SignalementVote.voter_user_id.like(like)))).scalar()
    print("  signalement votes:", sv)
    items = (await db.execute(select(CloudImportItem.status, func.count()).where(CloudImportItem.id.like(like))
                              .group_by(CloudImportItem.status))).all()
    print("  cloud import items:", dict(items))
    zones = (await db.execute(select(ClusterZone.id, ClusterZone.name, ClusterZone.kind, ClusterZone.created_by))).all()
    print("  cluster zones:", [(z[0], z[1], z[2], "seeded" if z[3] == TAG else "kept") for z in zones])

    tester = (await db.execute(select(User).where(User.email == TESTER_EMAIL))).scalar_one()
    token, sess = await create_session(db, tester, SessionStage.full, None, timedelta(minutes=30))
    await db.commit()
    print("\n[verify] API (session token of testeur@viasafe.local)")
    base = "http://127.0.0.1:8080"
    try:
        async with httpx.AsyncClient(base_url=base, headers={"Authorization": f"Bearer {token}"}, timeout=30) as c:
            async def get(path, **params):
                r = await c.get(path, params=params)
                body = r.json() if r.headers.get("content-type", "").startswith("application/json") else None
                return r.status_code, body

            st, me = await get("/auth/me")
            print(f"  /auth/me → {st} roles={[x.get('name') for x in (me or {}).get('roles', [])]}")
            st, body = await get("/hazards/", limit=200)
            items_ = (body or {}).get("items", [])
            with_addr = sum(1 for h in items_ if h.get("display_location"))
            with_cover = sum(1 for h in items_ if h.get("cover_image_url") or h.get("cover_crop_url"))
            with_zone = sum(1 for h in items_ if h.get("cluster_zone_name"))
            print(f"  /hazards/ → {st} total={(body or {}).get('total')} page={len(items_)} with address={with_addr} "
                  f"cover={with_cover} zone={with_zone}")
            sample = next((h for h in items_ if h.get("type") == "parked_vehicle" and h.get("status") == "resolved"
                           and h.get("resolution_source") == "votes"), items_[0] if items_ else None)
            if sample:
                print(f"    e.g. {sample.get('code')} {sample.get('type')} {sample.get('display_location')} "
                      f"zone={sample.get('cluster_zone_name')} cover={str(sample.get('cover_crop_url'))[:70]}")
                st, evs = await get(f"/hazards/{sample['id']}/events")
                print(f"  /hazards/{{id}}/events → {st} kinds={[e.get('kind') for e in (evs or {}).get('items', [])]}")
            st, spots = await get("/hazards/recurring-spots")
            print(f"  /hazards/recurring-spots → {st} {str(spots)[:200]}")
            st, dets = await get("/detections/", type="parked_vehicle", limit=100)
            d_items = (dets or {}).get("items", dets if isinstance(dets, list) else [])
            print(f"  /detections/?type=parked_vehicle → {st} total={(dets or {}).get('total') if isinstance(dets, dict) else len(d_items)} "
                  f"with ai_details={sum(1 for x in d_items if x.get('ai_details'))}")
            st, imps = await get("/imports/")
            print(f"  /imports/ → {st} {str(imps)[:160]}")
            st, zl = await get("/cluster-zones/")
            print(f"  /cluster-zones/ → {st} {[z.get('name') for z in (zl or [])]}")
            st, cfg = await get("/clustering/config")
            print(f"  /clustering/config → {st} zone_count={(cfg or {}).get('zone_count')}")
            st, ps = await get("/platform-settings/")
            keys = list(ps.keys()) if isinstance(ps, dict) else [s.get("section") for s in (ps or [])]
            print(f"  /platform-settings/ → {st} sections={keys}")
            if isinstance(ps, dict):
                print(f"    validation.distinct_subtype_types={(ps.get('validation') or {}).get('distinct_subtype_types')} "
                      f"captcha.provider={(ps.get('captcha') or {}).get('provider')} "
                      f"using_test_keys={(ps.get('captcha') or {}).get('using_test_keys')} "
                      f"parked_vehicle present={'parked_vehicle' in ps}")
            # Detection map: clusters along each predefined road section (same rule as the web:
            # centroid or any member within corridor_m of the main OSRM route, lib/routes-api.ts)
            det_types = dict((await db.execute(select(Detection.id, Detection.type))).all())
            st, secs = await get("/routes/sections")
            corridor = (secs or {}).get("corridor_m") or 60
            r = await c.post("/detections/clusters", json={})
            clusters = r.json().get("clusters", []) if r.status_code == 200 else []
            print(f"  POST /detections/clusters → {r.status_code} {len(clusters)} clusters; corridor {corridor} m")
            for sec in (secs or {}).get("sections", []):
                st, prev = await get("/routes/preview", **{"from": f"{sec['from']['lat']},{sec['from']['lon']}",
                                                           "to": f"{sec['to']['lat']},{sec['to']['lon']}"})
                geom = ((prev or {}).get("routes") or [{}])[0].get("geometry") or []
                along = [cl for cl in clusters if near_polyline((cl["centroid_lat"], cl["centroid_lon"]), geom, corridor)
                         or any(near_polyline(tuple(pt), geom, corridor) for pt in cl.get("detection_coords") or [])]
                seeded = [cl for cl in along if any(is_seeded(i) for i in cl.get("detection_ids", []))]
                types: dict = {}
                for cl in along:
                    t_ = det_types.get((cl.get("detection_ids") or ["-"])[0], "?")
                    types[t_] = types.get(t_, 0) + 1
                multi = sum(1 for cl in along if cl.get("count", 0) >= 2)
                print(f"    {sec['name']:<28} route {len(geom)} pts → {len(along)} clusters along "
                      f"({len(seeded)} seeded, {multi} with ≥2 members) {types}")
            img = (await db.execute(select(Detection.image_url).where(Detection.job_id.like(like),
                                                                      Detection.image_url.isnot(None)).limit(1))).scalar()
            async with httpx.AsyncClient(timeout=15) as raw:
                r = await raw.get(f"http://{img}")
                print(f"  MinIO GET {img[:80]}… → {r.status_code} {r.headers.get('content-type')} {len(r.content)} B")
    finally:
        await db.delete(sess)
        await db.commit()


def near_polyline(p: tuple, line: list, max_m: float) -> bool:
    """Distance of p to the polyline ≤ max_m (equirectangular, like distanceToPolylineM)."""
    if len(line) < 2:
        return False
    lat0 = math.radians(p[0])
    kx, ky = 6_371_000 * math.pi / 180 * math.cos(lat0), 6_371_000 * math.pi / 180
    pad = max_m / 111_000.0 * 1.5
    for (a0, a1), (b0, b1) in zip(line, line[1:]):
        if p[0] < min(a0, b0) - pad or p[0] > max(a0, b0) + pad or p[1] < min(a1, b1) - pad * 1.1 \
                or p[1] > max(a1, b1) + pad * 1.1:
            continue
        ax, ay = (a1 - p[1]) * kx, (a0 - p[0]) * ky
        bx, by = (b1 - p[1]) * kx, (b0 - p[0]) * ky
        dx, dy = bx - ax, by - ay
        L = dx * dx + dy * dy
        t = 0.0 if L == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / L))
        if math.hypot(ax + t * dx, ay + t * dy) <= max_m:
            return True
    return False


async def main() -> None:
    ap = argparse.ArgumentParser(description="Seed web-console test data (see module docstring).")
    ap.add_argument("--purge", action="store_true", help="only remove the previous seed")
    ap.add_argument("--purge-users", action="store_true", help="with --purge: also remove testeur@viasafe.local")
    ap.add_argument("--tester-password-stdin", action="store_true", help="read the tester password from stdin")
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--verify-only", action="store_true")
    ap.add_argument("--seed", type=int, default=20261015)
    args = ap.parse_args()
    pw = sys.stdin.readline().strip() if args.tester_password_stdin else None
    async with AsyncSessionLocal() as db:
        if args.verify_only:
            await verify(db)
            return
        await purge(db, drop_tester=args.purge and args.purge_users)
        if args.purge:
            return
        ctx = Ctx(random.Random(args.seed))
        await seed(db, ctx, pw)
        if ctx.warnings:
            print(f"\n[seed] {len(ctx.warnings)} warning(s):")
            for w in ctx.warnings:
                print("   -", w)
        if not args.no_verify:
            await verify(db)


if __name__ == "__main__":
    asyncio.run(main())
