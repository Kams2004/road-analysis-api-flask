"""
Video/image processing worker.
Runs models frame by frame, saves detections to PostgreSQL + MinIO.

OSD: extract_osd() runs on every processed frame.
  - raw_gps_text is always saved (exact OCR output).
  - latitude/longitude are saved when NMEA parse succeeds, else None.
  - Validator can correct missing coordinates via PATCH /{id}/location.

Geo-dedup: detections within ~55m of a prior detection of the same type
are suppressed (0.0005° ≈ 55m). Falls back to pixel-distance when GPS is None.
"""
import cv2
import uuid
import logging
import numpy as np
from pathlib import Path
from datetime import datetime
from typing import Optional

from ultralytics import YOLO
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.job import Job, JobStatus
from app.models.detection import Detection
from app.services.minio_service import upload_image
from app.services.osd_parser import extract_osd, reset_last, OSDData, GpsVoter
from app.services.geocoding import reverse_geocode

logger = logging.getLogger(__name__)

GEO_DEDUP_DEG = 0.0005  # ~55m radius at Cameroon latitudes

# ── model registry ────────────────────────────────────────────────────────────
# Keyed by model name. Only the models requested for the current job are loaded.
# All models are unloaded after each job to free memory.
_models: dict = {}

def _load_models(enabled: list):
    """Load exactly the models in `enabled`. Does not load anything else."""
    model_paths = {
        "pothole":   settings.MODEL_POTHOLE,
        "signs":     settings.MODEL_SIGNS,
        "speedbump": settings.MODEL_SPEEDBUMP,
    }
    for key in enabled:
        if key in _models:
            continue
        path = model_paths.get(key)
        if not path:
            continue
        p = Path(path)
        if p.exists():
            _models[key] = YOLO(str(p))
            logger.info(f"  ✓ {key} model loaded from {path}")
        else:
            logger.warning(f"  ✗ {key} model NOT found at {path}")


def _unload_models():
    """Release all loaded models from memory after a job completes."""
    _models.clear()
    logger.info("Models unloaded")


def _classify_severity(frame, x1, y1, x2, y2) -> tuple:
    # Classifier is not active yet — will be enabled once training dataset is ready
    return None, None


def _is_geo_duplicate(lat: Optional[float], lon: Optional[float], seen: list) -> bool:
    if lat is None or lon is None:
        return False
    for slat, slon in seen:
        if abs(lat - slat) < GEO_DEDUP_DEG and abs(lon - slon) < GEO_DEDUP_DEG:
            return True
    seen.append((lat, lon))
    return False


def _is_pixel_duplicate(box, seen: list, threshold: int) -> bool:
    cx = (box[0] + box[2]) / 2
    cy = (box[1] + box[3]) / 2
    for sx, sy in seen:
        if abs(cx - sx) < threshold and abs(cy - sy) < threshold:
            return True
    seen.append((cx, cy))
    return False


def _frame_to_jpeg(frame: np.ndarray) -> bytes:
    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return buf.tobytes()


def _save_context_clip(
    source_path: str,
    job_id: str,
    det_type: str,
    frame_num: int,
    fps: float,
    det_id: str,
    annotated_frame: np.ndarray,
) -> Optional[str]:
    """
    Extract a ±3s video clip around frame_num from source_path and upload to MinIO.
    The detection frame is replaced with the annotated version (bounding box drawn).
    Only called when GPS coordinates are missing (latitude is None).
    """
    window = int(fps * 3)
    start  = max(0, frame_num - window)
    end    = frame_num + window

    cap = cv2.VideoCapture(source_path)
    if not cap.isOpened():
        return None

    w   = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h   = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    out_path = f"/tmp/clip_{det_id[:8]}.mp4"

    writer = cv2.VideoWriter(
        out_path,
        cv2.VideoWriter_fourcc(*"avc1"),
        fps,
        (w, h),
    )

    cap.set(cv2.CAP_PROP_POS_FRAMES, start)
    for fn in range(start, end + 1):
        ret, f = cap.read()
        if not ret:
            break
        if fn == frame_num:
            # Use the annotated frame so the verificator sees the detection box
            f = annotated_frame.copy()
        cv2.putText(f, f"frame {fn}", (10, h - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 0), 1)
        writer.write(f)

    writer.release()
    cap.release()

    clip_name = (
        f"job-{job_id[:8]}_"
        f"det-{det_id[:8]}_"
        f"f{frame_num:06d}_pm3s.mp4"
    )
    minio_path = f"{job_id}/context_clips/{det_type}/{clip_name}"
    try:
        with open(out_path, "rb") as fh:
            clip_url = upload_image(fh.read(), minio_path)
        Path(out_path).unlink(missing_ok=True)
        return clip_url
    except Exception as e:
        logger.warning(f"Context clip upload failed: {e}")
        Path(out_path).unlink(missing_ok=True)
        return None


PAD = 20

def _save_crop(frame, x1, y1, x2, y2, job_id: str, det_type: str, det_id: str, frame_num: int) -> Optional[str]:
    """Upload a clean (unannotated) padded crop to MinIO. Returns the URL or None."""
    x1p = max(0, x1 - PAD)
    y1p = max(0, y1 - PAD)
    x2p = min(frame.shape[1], x2 + PAD)
    y2p = min(frame.shape[0], y2 + PAD)
    crop = frame[y1p:y2p, x1p:x2p]
    if crop.size == 0:
        return None
    try:
        return upload_image(
            _frame_to_jpeg(crop),
            f"{job_id}/{det_type}/crops/det-{det_id[:8]}_f{frame_num:06d}_crop.jpg",
        )
    except Exception as e:
        logger.warning(f"Crop upload failed: {e}")
        return None


def _add_detection(db, job, det_type, subtype, conf, frame_num, osd: OSDData, img_url, crop_url=None, clip_url=None):
    location_name = osd.location_name
    if location_name is None and osd.latitude is not None and osd.longitude is not None:
        location_name = reverse_geocode(osd.latitude, osd.longitude)
    db.add(Detection(
        job_id=job.id, type=det_type,
        subtype=subtype, confidence=conf,
        frame_number=frame_num,
        raw_gps_text=osd.raw_gps_text,
        latitude=osd.latitude, longitude=osd.longitude,
        gps_interpolated=osd.gps_interpolated,
        speed_kmh=osd.speed_kmh, vehicle_id=osd.vehicle_id,
        captured_at=osd.timestamp, image_url=img_url,
        crop_url=crop_url,
        context_clip_url=clip_url,
        location_name=location_name, rpm=osd.rpm,
    ))


async def process_source(
    job: Job,
    source_path: str,
    db: AsyncSession,
    enabled_models: list = None,
):
    """Main processing loop. Call from background task."""
    logger.info(f"[Job {job.id}] Starting — file: {job.filename}, models: {enabled_models}")
    enabled = enabled_models or ["pothole", "signs", "speedbump"]
    _load_models(enabled)
    reset_last()
    voter = GpsVoter()

    cap = cv2.VideoCapture(source_path)
    if not cap.isOpened():
        logger.error(f"[Job {job.id}] Cannot open source: {source_path}")
        job.status = JobStatus.failed
        job.error  = f"Cannot open source: {source_path}"
        _unload_models()
        await db.commit()
        return

    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    fps   = cap.get(cv2.CAP_PROP_FPS) or 0
    job.total_frames = total
    job.status       = JobStatus.processing
    await db.commit()
    logger.info(f"[Job {job.id}] Video opened — {total} frames @ {fps:.1f}fps")

    geo_seen: dict = {"pothole": [], "signs": [], "speedbump": []}
    px_seen:  dict = {"pothole": [], "signs": [], "speedbump": []}
    frame_num = 0
    det_count = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        frame_num += 1
        if frame_num % settings.FRAME_SKIP != 0:
            continue

        # Extract OSD from clean frame before any annotation
        osd = extract_osd(frame, voter)
        logger.debug(
            f"[Job {job.id}] frame={frame_num} raw={osd.raw_gps_text!r} "
            f"lat={osd.latitude} lon={osd.longitude}"
        )

        fh = frame.shape[0]

        # ── pothole detection ─────────────────────────────────────────────
        if "pothole" in enabled and "pothole" in _models:
            results = _models["pothole"](frame, conf=settings.CONF_THRESHOLD,
                                         iou=settings.IOU_THRESHOLD, verbose=False)
            for box in results[0].boxes:
                x1,y1,x2,y2 = map(int, box.xyxy[0].tolist())
                conf = round(box.conf[0].item(), 4)
                if (y1+y2)/2 < fh * 0.5:
                    continue
                if osd.latitude is not None:
                    if _is_geo_duplicate(osd.latitude, osd.longitude, geo_seen["pothole"]):
                        continue
                else:
                    if _is_pixel_duplicate([x1,y1,x2,y2], px_seen["pothole"],
                                           settings.DEDUP_DISTANCE_PX):
                        continue
                severity, _ = _classify_severity(frame, x1, y1, x2, y2)
                annotated = frame.copy()
                cv2.rectangle(annotated, (x1,y1), (x2,y2), (0,0,255), 2)
                label = f"Pothole {conf:.0%}"
                cv2.putText(annotated, label, (x1, y1-8),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0,0,255), 2)
                det_id  = str(uuid.uuid4())
                img_url = upload_image(
                    _frame_to_jpeg(annotated),
                    f"{job.id}/pothole/job-{job.id[:8]}_det-{det_id[:8]}_f{frame_num:06d}_pothole.jpg"
                )
                crop_url = _save_crop(frame, x1, y1, x2, y2, job.id, "pothole", det_id, frame_num)
                clip_url = None
                if osd.latitude is None:
                    clip_url = _save_context_clip(source_path, job.id, "pothole", frame_num, fps, det_id, annotated)
                _add_detection(db, job, "pothole", severity, conf, frame_num, osd, img_url, crop_url, clip_url)
                det_count += 1
                logger.info(
                    f"[Job {job.id}] Pothole @ frame={frame_num} conf={conf} "
                    f"severity={severity} lat={osd.latitude} lon={osd.longitude}"
                    + (" [interp]" if osd.gps_interpolated else "")
                )

        # ── traffic signs ─────────────────────────────────────────────────
        if "signs" in enabled and "signs" in _models:
            results = _models["signs"](frame, conf=settings.CONF_THRESHOLD,
                                       iou=settings.IOU_THRESHOLD, verbose=False)
            names = results[0].names
            for box in results[0].boxes:
                x1,y1,x2,y2 = map(int, box.xyxy[0].tolist())
                conf     = round(box.conf[0].item(), 4)
                cls_name = names[int(box.cls[0].item())]
                if osd.latitude is not None:
                    if _is_geo_duplicate(osd.latitude, osd.longitude, geo_seen["signs"]):
                        continue
                else:
                    if _is_pixel_duplicate([x1,y1,x2,y2], px_seen["signs"],
                                           settings.DEDUP_DISTANCE_PX):
                        continue
                annotated = frame.copy()
                cv2.rectangle(annotated, (x1,y1), (x2,y2), (255,255,255), 2)
                cv2.putText(annotated, f"{cls_name} {conf:.0%}", (x1, y1-8),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255,255,255), 2)
                det_id  = str(uuid.uuid4())
                img_url = upload_image(
                    _frame_to_jpeg(annotated),
                    f"{job.id}/signs/job-{job.id[:8]}_det-{det_id[:8]}_f{frame_num:06d}_sign.jpg"
                )
                crop_url = _save_crop(frame, x1, y1, x2, y2, job.id, "signs", det_id, frame_num)
                clip_url = None
                if osd.latitude is None:
                    clip_url = _save_context_clip(source_path, job.id, "signs", frame_num, fps, det_id, annotated)
                _add_detection(db, job, "traffic_sign", cls_name, conf, frame_num, osd, img_url, crop_url, clip_url)
                det_count += 1
                logger.info(f"[Job {job.id}] Sign @ frame={frame_num} {cls_name} conf={conf}")

        # ── speed bumps ───────────────────────────────────────────────────
        if "speedbump" in enabled and "speedbump" in _models:
            results = _models["speedbump"](frame, conf=settings.CONF_THRESHOLD,
                                           iou=settings.IOU_THRESHOLD, verbose=False)
            for box in results[0].boxes:
                x1,y1,x2,y2 = map(int, box.xyxy[0].tolist())
                conf = round(box.conf[0].item(), 4)
                if osd.latitude is not None:
                    if _is_geo_duplicate(osd.latitude, osd.longitude, geo_seen["speedbump"]):
                        continue
                else:
                    if _is_pixel_duplicate([x1,y1,x2,y2], px_seen["speedbump"],
                                           settings.DEDUP_DISTANCE_PX):
                        continue
                annotated = frame.copy()
                cv2.rectangle(annotated, (x1,y1), (x2,y2), (255,0,255), 2)
                cv2.putText(annotated, f"SpeedBump {conf:.0%}", (x1, y1-8),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255,0,255), 2)
                det_id  = str(uuid.uuid4())
                img_url = upload_image(
                    _frame_to_jpeg(annotated),
                    f"{job.id}/speedbump/job-{job.id[:8]}_det-{det_id[:8]}_f{frame_num:06d}_speedbump.jpg"
                )
                crop_url = _save_crop(frame, x1, y1, x2, y2, job.id, "speedbump", det_id, frame_num)
                clip_url = None
                if osd.latitude is None:
                    clip_url = _save_context_clip(source_path, job.id, "speedbump", frame_num, fps, det_id, annotated)
                _add_detection(db, job, "speed_bump", None, conf, frame_num, osd, img_url, crop_url, clip_url)
                det_count += 1
                logger.info(f"[Job {job.id}] SpeedBump @ frame={frame_num} conf={conf}")

        if frame_num % 50 == 0:
            job.processed  = frame_num
            job.detections = det_count
            await db.commit()
            logger.info(f"[Job {job.id}] Progress: frame {frame_num}/{total} ({100*frame_num//total}%) — {det_count} detections")

    cap.release()
    Path(source_path).unlink(missing_ok=True)
    _unload_models()

    job.status      = JobStatus.done
    job.processed   = frame_num
    job.detections  = det_count
    job.finished_at = datetime.utcnow()
    await db.commit()
    logger.info(f"[Job {job.id}] DONE — {det_count} detections in {frame_num} frames")
