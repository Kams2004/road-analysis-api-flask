"""
Demo-only: seed a realistic road-risk scenario along ONE real driving route
so the viasafe-mobile app can be tested end to end with the phone's real GPS.

Along the OSRM route origin -> dest (defaults: Ndoghem -> Akwa, Douala) it
creates:
  * one finished upload Job ("demo_dashcam.mp4") with ~12 VALIDATED
    detections (pothole / speed_bump / traffic_sign stop|crosswalk|speedlimit)
    whose frame + crop images are real training crops uploaded to MinIO in the
    same object layout and URL format as app/modules/m07_ingestion/pipeline.py
    ("minio:9000/detections/<job>/<type>/..."), then attached to hazards with
    app.modules.m06_hazards.hazard_service exactly like the processor does;
  * 5-6 active community reports (signalements) next to the route;
  * two clusters of historical driving "exceptions" (manual source) at ~35 %
    and ~70 % of the route, each confined to one 150 m grid cell and heavy
    enough to score >= 0.7, spread over 2024-2025.

Everything it writes is tagged so --reset removes only demo rows:
  jobs.submitted_by = 'demo-seed', signalements.reported_by = 'demo-seed',
  historical_events.source_event_id LIKE 'demo-seed-mobile-%',
  MinIO objects under <demo job id>/ and signalements/demo-seed/.

Run (from the backend/ directory, demo stack up as project `roaddemo`):

    docker run --rm --network roaddemo_default \\
      -v "$PWD":/app -w /app \\
      -v /home/kamsu-perold/road-risk-analysis/ai/archive/detections_crops:/crops:ro \\
      -e DATABASE_URL=postgresql+asyncpg://road_api:demo_password@db:5432/road_analysis \\
      -e OSRM_BASE_URL=http://osrm:5000 \\
      -e MINIO_ENDPOINT=minio:9000 -e MINIO_ACCESS_KEY=demo_minio \\
      -e MINIO_SECRET_KEY=demo_minio_password -e MINIO_SECURE=false \\
      road-analysis-api_api:latest \\
      python -m app.demo.seed_mobile_demo --reset [--origin LAT,LON] [--dest LAT,LON]

Without --reset an existing demo seed is left untouched (the script exits).
--clean removes the demo data and seeds nothing.
"""
import argparse
import asyncio
import io
import json
import math
import os
import random
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import cv2
from sqlalchemy import delete, select

from app.core.config import settings
from app.core.db.session import AsyncSessionLocal
from app.modules.m07_ingestion.models import job as _job  # noqa: F401 — register every mapper / FK target
from app.modules.m11_community.models import signalement as _sig  # noqa: F401 — register every mapper / FK target
from app.modules.m08_historical.models import historical_event as _he  # noqa: F401 — register every mapper / FK target
from app.modules.m06_hazards.models import (  # noqa: F401 — register every mapper / FK target
    detection as _det, hazard as _hz, detection_review as _dr, validation_label as _vl,
)
from app.modules.m06_hazards.models.detection import Detection, LocationSource, LocationStatus, ReviewStatus
from app.modules.m13_admin.models.audit_log import AuditLog
from app.modules.m06_hazards.models.detection_review import DetectionReview
from app.modules.m06_hazards.models.hazard import Hazard
from app.modules.m06_hazards.models.hazard_lifecycle import HazardEvent
from app.modules.m08_historical.models.historical_event import HistoricalEventRecord
from app.modules.m07_ingestion.models.job import Job, JobStatus, SourceType
from app.modules.m11_community.models.signalement import Signalement, SignalementStatus, SignalementType
from app.modules.m06_hazards.models.validation_label import ValidationLabel
from app.modules.m06_hazards import hazard_service
from app.modules.m08_historical import historical_service
from app.modules.m08_historical.grid import grid_cell_id
from app.modules.m08_historical.domain import HistoricalEvent
from app.modules.m08_historical.sources.manual import ManualEventSource
from app.core.storage.minio_service import get_minio, upload_image, upload_signalement_image

TAG = "demo-seed"
HIST_PREFIX = "demo-seed-mobile-"
OSRM = os.environ.get("OSRM_BASE_URL", "http://osrm:5000").rstrip("/")
CROPS = Path(os.environ.get("DEMO_CROPS_DIR", "/crops"))
DEFAULT_ORIGIN = (4.0676, 9.7926)   # Ndoghem, Douala
DEFAULT_DEST = (4.0511, 9.7043)     # Akwa, Douala

MODEL_VERSIONS = {  # weights file @ sha256[:12] of models/*.pt — same format as processor._weights_version
    "pothole": "model_pothole.pt@f73664cdaa85",
    "signs": "model_signs.pt@f3106984ae96",
    "speedbump": "model_speedbump.pt@121a513bee77",
}

# (position, type, subtype, crop folder, confidence). position: metres from
# the origin when > 1, otherwise a fraction of the route length. The first
# one sits ~400 m out so the approach alert fires almost immediately.
DETECTIONS = [
    (400,  "pothole",      None,         "pothole",    0.91),
    (0.12, "speed_bump",   None,         "speed_bump", 0.84),
    (0.20, "pothole",      None,         "pothole",    0.67),
    (0.28, "traffic_sign", "stop",       "stop",       0.93),
    (0.38, "pothole",      None,         "pothole",    0.78),
    (0.381, "pothole",     None,         "pothole",    0.72),   # 2nd sighting -> same hazard
    (0.47, "traffic_sign", "crosswalk",  "crosswalk",  0.81),
    (0.55, "pothole",      None,         "pothole",    0.58),
    (0.63, "speed_bump",   None,         "speed_bump", 0.76),
    (0.72, "traffic_sign", "speedlimit", "speedlimit", 0.88),
    (0.82, "pothole",      None,         "pothole",    0.63),
    (0.92, "pothole",      None,         "pothole",    0.86),
]
MODEL_KEY = {"pothole": "pothole", "traffic_sign": "signs", "speed_bump": "speedbump"}
OBJ_DIR = {"pothole": "pothole", "traffic_sign": "signs", "speed_bump": "speedbump"}

# (fraction, type, lateral offset m, description, photo crop folder, route index)
REPORTS = [
    (0.15, "embouteillage", 12, "Bouchon important, circulation au pas", None, 0),
    (0.33, "accident",      18, "Collision entre un taxi et une moto, une voie bloquée", None, 0),
    (0.50, "travaux",       15, "Travaux sur la chaussée, rétrécissement à une voie", None, 0),
    (0.66, "nid_de_poule",   8, "Gros nid de poule au milieu de la voie de droite", "pothole", 0),
    (0.86, "embouteillage", 10, "Ralentissements à l'approche du carrefour", None, 0),
    (0.50, "accident",      14, "Accrochage, véhicules sur le bas-côté", None, 1),  # on the alternative, if any
]

# Historical clusters: weights ACCIDENT 5, HARD_BRAKING/HARSH_CORNERING 1.5,
# SPEEDING 1 — saturation 20, so ~16-21 weighted per cell -> 0.8-1.0.
HIST_CLUSTERS = [
    (0.35, {"ACCIDENT": 2, "HARD_BRAKING": 4, "SPEEDING": 3, "HARSH_CORNERING": 1}),   # 20.5 -> 1.0
    (0.70, {"ACCIDENT": 1, "HARD_BRAKING": 4, "SPEEDING": 5, "HARSH_CORNERING": 2}),   # 19.0 -> 0.95
]


# ── geometry ──────────────────────────────────────────────────────────────────

def haversine_m(lat1, lon1, lat2, lon2):
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


class Route:
    def __init__(self, coords_lonlat, distance_m, duration_s):
        self.pts = [(lat, lon) for lon, lat in coords_lonlat]
        self.cum = [0.0]
        for a, b in zip(self.pts, self.pts[1:]):
            self.cum.append(self.cum[-1] + haversine_m(*a, *b))
        self.length = self.cum[-1]
        self.distance_m, self.duration_s = distance_m, duration_s

    def at(self, position: float):
        """(lat, lon, bearing_deg) at `position` (metres if > 1 else fraction)."""
        d = position if position > 1 else position * self.length
        d = max(0.0, min(d, self.length))
        for i in range(1, len(self.cum)):
            if self.cum[i] >= d:
                (la1, lo1), (la2, lo2) = self.pts[i - 1], self.pts[i]
                seg = self.cum[i] - self.cum[i - 1]
                t = 0 if seg == 0 else (d - self.cum[i - 1]) / seg
                brg = math.degrees(math.atan2((lo2 - lo1) * math.cos(math.radians(la1)), la2 - la1)) % 360
                return la1 + t * (la2 - la1), lo1 + t * (lo2 - lo1), brg
        la, lo = self.pts[-1]
        return la, lo, 0.0


def offset(lat, lon, bearing_deg, metres):
    """Point `metres` to the right of the travel direction."""
    b = math.radians(bearing_deg + 90)
    return (lat + metres * math.cos(b) / 111_320.0,
            lon + metres * math.sin(b) / (111_320.0 * math.cos(math.radians(lat))))


def fetch_routes(origin, dest):
    url = (f"{OSRM}/route/v1/driving/{origin[1]},{origin[0]};{dest[1]},{dest[0]}"
           f"?geometries=geojson&overview=full&alternatives=true")
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.load(resp)
    if data.get("code") != "Ok" or not data.get("routes"):
        raise SystemExit(f"OSRM returned no route: {data.get('code')} {data.get('message')}")
    return [Route(r["geometry"]["coordinates"], r["distance"], r["duration"]) for r in data["routes"]]


# ── media ─────────────────────────────────────────────────────────────────────

def pick_crops(folder: str, n: int, rng: random.Random):
    files = sorted((CROPS / folder).glob("*.jpg"), key=lambda p: -p.stat().st_size)
    if not files:
        raise SystemExit(f"No crops in {CROPS / folder} — mount ai/archive/detections_crops at {CROPS}")
    pool = files[:max(n * 4, 20)]
    return rng.sample(pool, min(n, len(pool)))


def jpeg(img) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])
    return buf.tobytes()


def frame_and_crop(path: Path, label: str):
    """(annotated 'frame' bytes, clean crop bytes, bbox) from one crop file —
    the frame is the crop upscaled with the box/label drawn like the processor."""
    crop = cv2.imread(str(path))
    h, w = crop.shape[:2]
    scale = max(1.0, 480 / max(w, h))
    big = cv2.resize(crop, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_CUBIC)
    bh, bw = big.shape[:2]
    pad = int(20 * scale)
    x1, y1, x2, y2 = min(pad, bw // 4), min(pad, bh // 4), bw - min(pad, bw // 4), bh - min(pad, bh // 4)
    ann = big.copy()
    cv2.rectangle(ann, (x1, y1), (x2, y2), (0, 0, 255), 2)
    cv2.putText(ann, label, (x1 + 4, max(16, y1 - 6) if y1 > 20 else y1 + 18),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 255), 2)
    return jpeg(ann), jpeg(crop), (x1, y1, x2, y2)


def make_buckets_public():
    client = get_minio()  # creates the detections + signalements buckets
    for bucket in (settings.MINIO_BUCKET_DETECTIONS, settings.MINIO_BUCKET_SIGNALEMENTS):
        client.set_bucket_policy(bucket, json.dumps({
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Principal": {"AWS": ["*"]},
                           "Action": ["s3:GetObject"], "Resource": [f"arn:aws:s3:::{bucket}/*"]}],
        }))


def remove_objects(bucket: str, prefix: str):
    client = get_minio()
    for obj in client.list_objects(bucket, prefix=prefix, recursive=True):
        client.remove_object(bucket, obj.object_name)


def geocode(lat, lon, fallback):
    try:
        from app.modules.m03_maps.geocoding import reverse_geocode_details
        g = reverse_geocode_details(lat, lon)
    except Exception:
        g = {}
    return {"location_name": g.get("location_name") or fallback,
            "city": g.get("city") or "Douala", "country": g.get("country") or "Cameroun"}


# ── reset ─────────────────────────────────────────────────────────────────────

async def clean(db) -> bool:
    jobs = (await db.execute(select(Job).where(Job.submitted_by == TAG))).scalars().all()
    job_ids = [j.id for j in jobs]
    had = bool(job_ids)
    if job_ids:
        dets = (await db.execute(select(Detection).where(Detection.job_id.in_(job_ids)))).scalars().all()
        det_ids = [d.id for d in dets]
        hazard_ids = {d.hazard_id for d in dets if d.hazard_id}
        # lifecycle events written for these jobs / detections (no FK, would be orphaned)
        await db.execute(delete(HazardEvent).where(
            HazardEvent.job_id.in_(job_ids) | HazardEvent.detection_id.in_(det_ids or ["-"])))
        await db.execute(delete(AuditLog).where(AuditLog.object_id.in_([*job_ids, *det_ids])))
        if det_ids:
            await db.execute(delete(DetectionReview).where(DetectionReview.detection_id.in_(det_ids)))
            await db.execute(delete(ValidationLabel).where(ValidationLabel.detection_id.in_(det_ids)))
            await db.execute(delete(Detection).where(Detection.id.in_(det_ids)))
        await db.flush()
        for hid in hazard_ids:  # drops hazards left without members, refreshes shared ones
            hz = await db.get(Hazard, hid)
            if hz is not None:
                await hazard_service.recompute(db, hz)
        await db.execute(delete(Job).where(Job.id.in_(job_ids)))
        for jid in job_ids:
            try:
                remove_objects(settings.MINIO_BUCKET_DETECTIONS, f"{jid}/")
            except Exception as e:
                print(f"  ! could not remove MinIO objects for job {jid}: {e}")
    n_sig = (await db.execute(delete(Signalement).where(Signalement.reported_by == TAG))).rowcount
    n_hist = (await db.execute(delete(HistoricalEventRecord).where(
        HistoricalEventRecord.source_event_id.like(f"{HIST_PREFIX}%")))).rowcount
    await db.commit()
    try:
        remove_objects(settings.MINIO_BUCKET_SIGNALEMENTS, f"{TAG}/")
    except Exception:
        pass
    print(f"[reset] removed {len(job_ids)} job(s), {n_sig} report(s), {n_hist} historical event(s)")
    return had or bool(n_sig) or bool(n_hist)


# ── seed ──────────────────────────────────────────────────────────────────────

async def seed(db, routes, rng):
    main = routes[0]
    now = datetime.utcnow()

    # Job + detections
    job = Job(filename="demo_dashcam.mp4", status=JobStatus.done, enabled_models="pothole,signs,speedbump",
              source_type=SourceType.upload.value, source_name=TAG, submitted_by=TAG,
              total_frames=5400, processed=5400, detections=len(DETECTIONS), fps=30.0, duration_s=180.0,
              frame_skip=3, created_at=now - timedelta(hours=6), started_at=now - timedelta(hours=6),
              finished_at=now - timedelta(hours=5, minutes=50))
    db.add(job)
    await db.flush()

    crops_by_folder = {}
    for folder in {d[3] for d in DETECTIONS}:
        crops_by_folder[folder] = pick_crops(folder, sum(1 for d in DETECTIONS if d[3] == folder), rng)

    ctx = await hazard_service.load_context(db)
    by_model, conf_sum, created = {}, 0.0, []
    for i, (pos, dtype, subtype, folder, conf) in enumerate(DETECTIONS):
        lat, lon, _ = main.at(pos)
        frame_num = 150 + i * 420
        det_id = str(uuid.uuid4())
        model_key = MODEL_KEY[dtype]
        label = {"pothole": "Pothole", "speed_bump": "Speed bump"}.get(dtype, subtype or dtype)
        frame_b, crop_b, (x1, y1, x2, y2) = frame_and_crop(crops_by_folder[folder].pop(), f"{label} {conf:.0%}")
        d = OBJ_DIR[dtype]
        img_url = upload_image(frame_b, f"{job.id}/{d}/job-{job.id[:8]}_det-{det_id[:8]}_f{frame_num:06d}_{d}.jpg")
        crop_url = upload_image(crop_b, f"{job.id}/{d}/crops/det-{det_id[:8]}_f{frame_num:06d}_crop.jpg")
        geo = geocode(lat, lon, "Douala")
        captured = now - timedelta(hours=rng.uniform(5, 70))
        det = Detection(
            id=det_id, job_id=job.id, type=dtype, subtype=subtype, confidence=conf,
            predicted_type=dtype, predicted_subtype=subtype,
            frame_number=frame_num, video_time_s=round((frame_num - 1) / 30.0, 3),
            bbox_x1=float(x1), bbox_y1=float(y1), bbox_x2=float(x2), bbox_y2=float(y2),
            model_name=model_key, model_version=MODEL_VERSIONS[model_key],
            raw_gps_text=f"N{lat:.6f} E{lon:.6f}", latitude=round(lat, 6), longitude=round(lon, 6),
            gps_interpolated=False,
            location_status=LocationStatus.confirmed.value, location_source=LocationSource.ocr.value,
            country=geo["country"], city=geo["city"], location_name=geo["location_name"],
            speed_kmh=round(rng.uniform(22, 48), 1), vehicle_id="DEMO-CAM-01",
            captured_at=captured, image_url=img_url, crop_url=crop_url,
            review_status=ReviewStatus.validated, reviewed_by="system", reviewed_at=captured + timedelta(minutes=12),
            created_at=captured,
        )
        db.add(det)
        db.add(DetectionReview(detection_id=det_id, from_status=None, to_status="validated",
                               action="auto_accept", actor_type="system", actor=TAG,
                               details={"confidence": conf}))
        await db.flush()
        await hazard_service.attach(db, det, ctx)
        by_model[model_key] = by_model.get(model_key, 0) + 1
        conf_sum += conf
        created.append(det)
    job.detections_by_model = by_model
    job.avg_confidence = round(conf_sum / len(DETECTIONS), 4)

    # Community reports
    reports = []
    for frac, stype, off_m, desc, photo, ridx in REPORTS:
        route = routes[ridx] if ridx < len(routes) else None
        if route is None:
            continue
        lat, lon, brg = route.at(frac)
        lat, lon = offset(lat, lon, brg, off_m)
        sid = str(uuid.uuid4())
        image_url = None
        if photo:
            _, crop_b, _ = frame_and_crop(pick_crops(photo, 1, rng)[0], "")
            image_url = upload_signalement_image(crop_b, f"{TAG}/{sid}.jpg")
        geo = geocode(lat, lon, "Douala")
        sig = Signalement(
            id=sid, type=SignalementType(stype), status=SignalementStatus.actif,
            latitude=round(lat, 6), longitude=round(lon, 6), description=desc, image_url=image_url,
            reported_by=TAG, location_name=geo["location_name"],
            blocked_bearing=round(brg, 1) if stype in ("accident", "travaux") else None,
            confirmations=rng.randint(1, 6), not_there_votes=0,
            reported_at=now - timedelta(minutes=rng.randint(8, 150)),
        )
        db.add(sig)
        reports.append(sig)
    await db.commit()

    # Historical exceptions (through the historical service, manual source)
    events, eid = [], 0
    start, end = datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2025, 12, 31, tzinfo=timezone.utc)
    cells = []
    for frac, mix in HIST_CLUSTERS:
        alat, alon, brg = main.at(frac)
        cell = grid_cell_id(alat, alon, settings.HISTORICAL_GRID_CELL_SIZE_M)
        cells.append((frac, cell, alat, alon))
        # candidate points on the road within +-50 m that stay inside the anchor's cell
        d0 = frac * main.length
        cands = [main.at(d0 + k) for k in range(-50, 51, 5) if d0 + k > 1]
        cands = [(la, lo) for la, lo, _ in cands
                 if grid_cell_id(la, lo, settings.HISTORICAL_GRID_CELL_SIZE_M) == cell] or [(alat, alon)]
        for etype, count in mix.items():
            for _ in range(count):
                eid += 1
                la, lo = rng.choice(cands)
                when = start + timedelta(seconds=rng.uniform(0, (end - start).total_seconds()))
                speed = rng.uniform(15, 24) if etype in ("SPEEDING", "HARD_BRAKING", "HARSH_CORNERING") else None
                events.append(HistoricalEvent(
                    source="manual", source_event_id=f"{HIST_PREFIX}{eid:04d}", event_type=etype,
                    occurred_at=when, latitude=round(la, 6), longitude=round(lo, 6),
                    speed_mps=round(speed, 1) if speed else None,
                    speed_limit_mps=13.9 if speed else None,  # 50 km/h urban limit
                    severity="high" if etype == "ACCIDENT" else "medium",
                    raw={"demo": True, "seed": TAG, "cluster": frac},
                ))
    summary = await historical_service.import_events(ManualEventSource(events), start.date(), end.date(), db)
    return job, created, reports, summary, cells


async def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--origin", default=f"{DEFAULT_ORIGIN[0]},{DEFAULT_ORIGIN[1]}", help="lat,lon")
    ap.add_argument("--dest", default=f"{DEFAULT_DEST[0]},{DEFAULT_DEST[1]}", help="lat,lon")
    ap.add_argument("--reset", action="store_true", help="delete previous demo rows, then seed")
    ap.add_argument("--clean", action="store_true", help="delete previous demo rows and exit")
    ap.add_argument("--seed", type=int, default=7, help="RNG seed")
    args = ap.parse_args()
    origin = tuple(float(v) for v in args.origin.split(","))
    dest = tuple(float(v) for v in args.dest.split(","))
    rng = random.Random(args.seed)

    make_buckets_public()
    async with AsyncSessionLocal() as db:
        if args.reset or args.clean:
            await clean(db)
            if args.clean:
                return
        elif (await db.execute(select(Job.id).where(Job.submitted_by == TAG))).first():
            print("Demo data already present — re-run with --reset to recreate it.")
            return

        routes = fetch_routes(origin, dest)
        for i, r in enumerate(routes):
            print(f"route[{i}] {r.distance_m/1000:.2f} km, {r.duration_s/60:.1f} min, {len(r.pts)} pts")
        job, dets, reports, summary, cells = await seed(db, routes, rng)

    main_route = routes[0]
    print(f"\nOrigin {origin} -> dest {dest}; main route {main_route.length/1000:.2f} km")
    print(f"Job {job.id} ({job.filename}) — {len(dets)} validated detections:")
    for d in dets:
        dist = haversine_m(origin[0], origin[1], d.latitude, d.longitude)
        print(f"  {d.type:<13}{(d.subtype or ''):<11} conf={d.confidence:.2f} "
              f"({d.latitude:.6f},{d.longitude:.6f}) {dist:6.0f} m from origin  hazard={d.hazard_id}")
    print(f"{len(reports)} reports:")
    for s in reports:
        print(f"  {s.type.value:<14} ({s.latitude:.6f},{s.longitude:.6f}) {s.description}")
    print(f"historical import: inserted={summary.inserted} skipped={summary.skipped_duplicates} "
          f"errors={len(summary.errors)}")
    for frac, cell, la, lo in cells:
        print(f"  cluster @ {frac:.0%}: cell {cell} ({la:.6f},{lo:.6f})")
    print(f"sample image: http://localhost:9200/{dets[0].image_url.split('/', 1)[1]}")


if __name__ == "__main__":
    asyncio.run(main())
