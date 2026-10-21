"""
Video/image processing worker.
Runs models frame by frame, saves detections to PostgreSQL + MinIO.

OSD: the job's OsdReader (osd_parser) reads the overlay of every processed frame
with the layout of the equipment (forced, or auto-detected on the first frames —
event `osd_layout`, job.osd_layout); an unreadable GPS / time is re-read on image
variants then on the frames ±1–2 s around (_NeighbourFrames).
  - raw_gps_text is always saved (exact OCR output).
  - latitude/longitude are saved when NMEA parse succeeds, else None.
  - Every valid (non-interpolated) fix is stored in job_track_points — the
    vehicle trajectory a validator uses to place a detection (RRAP-06-014).

Creation rules (RRAP-06-007/012/013, see app.modules.m06_hazards.detection_workflow):
  - location_status: confirmed (OCR fix) | approximate (GPS voter) | missing;
    anything not confirmed starts in `location_pending` and gets a ±N s
    context clip (validation.clip_seconds).
  - two-stage pothole pipeline (RRAP-05-010/011): every pothole box is
    cropped (padding as the trainer did, see model_registry.crop_box) and
    scored by the crop classifier (MODEL_POTHOLE_CLASSIFIER) → verifier_score
    = P(pothole). No classifier file (or an unloadable one) → single stage,
    verifier_score NULL, exactly as before.
  - two-stage traffic-sign pipeline: when MODEL_SIGNS is the 2-class
    detector (traffic_sign, traffic_light) and MODEL_SIGNS_CLASSIFIER loads,
    traffic_light boxes are stored as subtype `traffic_light`; traffic_sign
    boxes are cropped (card crop_pad_ratio) and classified → subtype = the
    VIASafe sign code, verifier_score = P(code), class_candidates = top-3;
    an unsure classifier gives subtype `unknown_sign` (review), its reject
    class (`not_a_sign`) an auto-reject `ai_not_a_sign`. The legacy
    multi-class sign model (or no classifier) keeps the single stage:
    subtype = the detector class, as before.
  - video consistency (app.modules.m05_ai_detection.static_check): boxes are tracked across
    sampled frames per model; one detection is stored per track (its best
    sighting) once the track ends. An object seen in fewer than
    ai_validation.min_frames_for_auto_accept frames, or whose estimated
    position drifts > max_static_drift_m while the vehicle moves, cannot be
    auto-accepted (review, `not_static`); nor can a pothole whose box centre
    is above pothole_min_y_ratio of the frame (`off_road_area`).
  - pothole false-positive rules (static_check.pothole_rule_check, settings
    ai_validation.pothole_*): a finished pothole track whose best box lies
    above the road-area line (pothole_off_road_action = reject → `ai_off_road`)
    or is too large / a wide box touching the image side (`ai_box_too_large`)
    is auto-rejected through ai_validation.rule_reject (manual mode: review
    hint only; audit sampling applies); the rules evaluated are stored in
    ai_details (pipeline `pothole_rules`). The classifier score is still stored.
  - detectors run at their card's `imgsz` (ultralytics default without a card).
  - the automatic decision comes from settings section `ai_validation`
    (mode manual | assisted | automatic, audit sampling — see
    app.modules.m05_ai_detection.ai_validation); thresholds from its overrides, else the
    model cards, else section `validation`. Automatic decisions are taken by
    actor "system" (history + audit) and reversible via reopen.
  - bbox, video time, model name, weights version (`file@sha256[:12]`) and
    model card version are stored for traceability (RRAP-05-003/05-010).
  - images per stored detection: image_url = annotated frame (box drawn, for
    validators), crop_url = clean padded crop, frame_clean_url = clean full
    frame (once per job frame, `{job}/frames/f{frame:06d}_clean.jpg`) + its
    frame_width / frame_height — the detector training image. Automatic
    decisions are recorded as training samples (app.modules.m06_hazards.training_data)
    and written to the `datasets` bucket at the end of the job.
  - recorded_at (every type, and every job_track_points fix): video
    recording time of the frame — its
    OSD date-time, else the nearest OSD time of the video ± the video-time
    difference, else NULL — timezone-aware (settings.OSD_TIMEZONE).
  - parked / roadside vehicles (model key `vehicles`, MODEL_VEHICLES = stock
    COCO-pretrained YOLOv8, opt-in per job; app.modules.m05_ai_detection.parked_vehicle):
    boxes of `parked_vehicle.vehicle_classes` ≥ detector_confidence, outside
    the masked regions (OSD overlay, own bonnet), are tracked with motion
    prediction while the ego path (OSD fix + speed of every sampled frame) is
    kept. A finished track is judged by rules (ego motion, world stability,
    ground contact, OSRM road distance, traffic queue, evidence, proximity)
    → verdict parked | moving | traffic_queue | off_road | not_grounded |
    insufficient_evidence | too_far. Only `parked` is stored (type
    `parked_vehicle`, subtype blocking | wrong_side | roadside, verifier_score
    = rule score, verifier_model = rule-set version, ai_details = verdict,
    severity, reasons, features); with store_rejected_for_review the
    review_verdicts are stored auto-rejected (ai_off_road, ai_not_grounded,
    ai_traffic_queue, ai_moving_vehicle). Every verdict is counted in the
    job events (`vehicle_verdicts`). OSRM unreachable → road rule skipped for
    the rest of the job.

Geo-dedup (applied to finished tracks): a detection within the clustering
radius of a prior detection of the same detector in the job is suppressed.
The radius is the global Cluster Configuration resolved at the new
detection's position for its type (app.modules.m06_hazards.clustering: zone →
urban/rural profile → per-type value). Falls back to pixel-distance when
GPS is None.
Across jobs (the same road driven again, the same video processed twice)
duplicates are prevented by the clusters: every stored detection that is
not rejected joins its cluster at once (hazard_service.attach, docs/CLUSTERS.md).
Repeated observations: a finished track whose best sighting is the same
observation as an already stored detection — same type (subtype for signs),
same camera (OSD vehicle id), same recording instant (OSD time ± 1 s, else the
same OSD GPS reading) and same frame region, whatever the job / file
(app.modules.m06_hazards.observations; e.g. a clip cut from a video already
processed) — is not stored: counted as `duplicates_skipped` in the job stats.
Object position: every stored detection gets an estimated ground position of
the object (object_latitude / object_longitude, app.modules.m06_hazards.object_position:
vehicle fix + flat-road range along the heading of the ego GPS track, parked-
vehicle camera calibration) — the clustering uses it; latitude / longitude
stay the vehicle's fix.

Job lifecycle (Module 7, app.modules.m07_ingestion.job_service):
  - frame sampling: zero-based frame_index % step == 0, step = job.frame_skip
    or processing.frame_skip_by_model[model] when configured;
  - cancel_requested is polled every CANCEL_CHECK_EVERY frames — the job
    stops, keeps what it found so far and ends `cancelled`;
  - metrics (fps, duration, avg confidence, per-model counts) are stored on
    the job; the source media is deleted only once the job is `done`.
"""
import cv2
import random
import uuid
import logging
import numpy as np
from bisect import bisect_left
from collections import deque
from pathlib import Path
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from ultralytics import YOLO
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.modules.m07_ingestion.models.job import Job, JobStatus
from app.modules.m06_hazards.models.detection import Detection, ReviewStatus
from app.modules.m07_ingestion.models.job_track_point import JobTrackPoint
from app.modules.m05_ai_detection import ai_validation, model_registry, static_check
from app.modules.m06_hazards import detection_workflow as wf, hazard_service, object_position, observations, training_data
from app.modules.m07_ingestion import job_service
from app.modules.m06_hazards.clustering.zones import haversine_m
from app.modules.m05_ai_detection.sign_taxonomy import TRAFFIC_LIGHT, UNKNOWN_SIGN, is_negative
from app.modules.m05_ai_detection import parked_vehicle as pv
from app.modules.m05_ai_detection.parked_vehicle import ParkedVehicleSettings
from app.modules.m05_ai_detection.parked_vehicle import geometry as pv_geo
from app.modules.m03_maps.osrm.client import OSRMUnavailableError, get_osrm_client
from app.modules.m07_ingestion.job_service import CANCEL_CHECK_EVERY, PermanentJobError
from app.core.storage.minio_service import upload_image
from app.modules.m05_ai_detection.osd_parser import OsdReader, reset_last, OSDData, GpsVoter
from app.modules.m05_ai_detection.osd_settings import OsdSettings, layout_for_job
from app.modules.m03_maps.geocoding import empty_address, has_address, reverse_geocode_details
from app.core.settings_service import (
    AiValidationSettings, ProcessingSettings, ValidationSettings, get_section,
)

logger = logging.getLogger(__name__)


# ── loaded models ─────────────────────────────────────────────────────────────
# Keyed by slot (model_registry.SLOTS). Only the models requested for the current
# job are loaded (+ the crop classifier of an enabled detector). All models are
# unloaded after each job to free memory.
_models: dict = {}
_model_versions: dict = {}           # key → "weights.pt@<sha256[:12]>"
_model_cards: dict = {}              # key → model_registry.ModelCard (when the .json card exists)
_sign_two_stage: dict = {"on": False}   # signs detector is traffic_sign/traffic_light AND its classifier loaded


def _weights_version(p: Path) -> str:
    """Weights file name + first 12 hex chars of its SHA-256 (hashed once per file version)."""
    return model_registry.weights_version(p)


def _card_label(key: str) -> Optional[str]:
    card = _model_cards.get(key)
    return card.label if card is not None else None


def _load_one(key: str, path: Optional[str]) -> None:
    if not path:
        return
    p = Path(path)
    if not p.exists():
        logger.warning(f"  ✗ {key} model NOT found at {path}")
        return
    _models[key] = YOLO(str(p))
    _model_versions[key] = _weights_version(p)
    card, error = model_registry.load_card(p)
    if card is not None:
        _model_cards[key] = card
    logger.info(f"  ✓ {key} model loaded from {path}"
                + (f" — card {card.label}" if card else f" — no model card ({error})" if error else " — no model card"))


def _class_names(model) -> list[str]:
    names = getattr(model, "names", None) or {}
    return [str(v) for v in (names.values() if isinstance(names, dict) else names)]


def _load_verifier(det_key: str) -> None:
    """Crop classifier of an enabled detector (two-stage pipeline). Anything
    unusable — missing file, not a classification model, no positive class —
    leaves the pipeline single-stage. The sign classifier is only used behind
    the 2-class sign detector (traffic_sign / traffic_light): the legacy
    multi-class sign model keeps the single stage."""
    det_type = model_registry.SLOTS[det_key][0]
    key = model_registry.VERIFIER_FOR_TYPE.get(det_type)
    if key is None or key in _models:
        return
    if det_type == "traffic_sign" and det_key in _models and \
            not model_registry.is_two_stage_sign_detector(_class_names(_models[det_key])):
        logger.info(f"  · {det_key} is a legacy multi-class sign detector — single-stage sign pipeline")
        return
    path = model_registry.slot_path(key)
    if not path or not Path(path).exists():
        logger.info(f"  · no {key} at {path or '(unset)'} — single-stage {det_type} pipeline")
        return
    try:
        _load_one(key, path)
    except Exception as exc:   # e.g. a stub / incompatible checkpoint
        logger.warning(f"  ✗ {key} could not be loaded from {path} ({exc}) — single-stage {det_type} pipeline")
        return
    model = _models[key]
    usable = (_class_names(model) if det_type == "traffic_sign" else _positive_index(model, det_type) is not None)
    if getattr(model, "task", None) != "classify" or not usable:
        logger.warning(f"  ✗ {key} at {path} is not a {det_type} classifier "
                       f"(task={getattr(model, 'task', None)}, classes={getattr(model, 'names', None)}) — ignored")
        for d in (_models, _model_versions, _model_cards):
            d.pop(key, None)
        return
    if det_type == "traffic_sign":
        _sign_two_stage["on"] = True


def _positive_index(model, det_type: str) -> Optional[int]:
    """Index of the positive class ("pothole") in a classifier's names."""
    names = getattr(model, "names", None) or {}
    items = names.items() if isinstance(names, dict) else enumerate(names)
    for idx, name in items:
        if str(name).lower() == det_type:
            return int(idx)
    return None


def _load_models(enabled: list):
    """Load exactly the models in `enabled` (and their crop classifiers)."""
    for key in enabled:
        if key in _models or key not in model_registry.SLOTS:
            continue
        _load_one(key, model_registry.slot_path(key))
        if key in _models:
            _load_verifier(key)


def _unload_models():
    """Release all loaded models from memory after a job completes."""
    _models.clear()
    _model_versions.clear()
    _model_cards.clear()
    _sign_two_stage["on"] = False
    logger.info("Models unloaded")


def _predict_kwargs(key: str) -> dict:
    """Extra detector inference arguments from its model card (imgsz)."""
    card = _model_cards.get(key)
    return {"imgsz": card.imgsz} if card is not None and card.imgsz else {}


def _verify(det_type: str, frame: np.ndarray, x1: int, y1: int, x2: int, y2: int) -> Optional[float]:
    """Second stage: P(det_type) of the crop classifier, None when there is none."""
    key = model_registry.VERIFIER_FOR_TYPE.get(det_type)
    model = _models.get(key) if key else None
    if model is None:
        return None
    card = _model_cards.get(key)
    cx1, cy1, cx2, cy2 = model_registry.crop_box(x1, y1, x2, y2, frame.shape[1], frame.shape[0], card)
    crop = frame[cy1:cy2, cx1:cx2]
    if crop.size == 0:
        return None
    try:
        imgsz = (card.imgsz if card and card.imgsz else model_registry.DEFAULT_CLASSIFIER_IMGSZ)
        result = model(crop, imgsz=imgsz, verbose=False)[0]
        idx = _positive_index(model, det_type)
        return round(float(result.probs.data[idx].item()), 4) if idx is not None else None
    except Exception as exc:
        logger.warning(f"Classifier {key} failed on a crop: {exc}")
        return None


def _classify_sign(frame: np.ndarray, x1: int, y1: int, x2: int, y2: int, reject_class: Optional[str] = None,
                   ) -> Optional[tuple[str, float, list[dict]]]:
    """Second stage of the sign pipeline: (top code, P(top code), top-3
    candidates without the non-sign classes); None without a classifier."""
    key = model_registry.VERIFIER_FOR_TYPE["traffic_sign"]
    model = _models.get(key)
    if model is None:
        return None
    card = _model_cards.get(key)
    cx1, cy1, cx2, cy2 = model_registry.crop_box(x1, y1, x2, y2, frame.shape[1], frame.shape[0], card)
    crop = frame[cy1:cy2, cx1:cx2]
    if crop.size == 0:
        return None
    try:
        imgsz = (card.imgsz if card and card.imgsz else model_registry.DEFAULT_CLASSIFIER_IMGSZ)
        result = model(crop, imgsz=imgsz, verbose=False)[0]
        probs = [float(v) for v in result.probs.data.tolist()]
    except Exception as exc:
        logger.warning(f"Classifier {key} failed on a crop: {exc}")
        return None
    names = getattr(model, "names", None) or {}
    ranked = sorted(((names[i] if isinstance(names, dict) else names[i], p) for i, p in enumerate(probs)),
                    key=lambda cp: -cp[1])
    if not ranked:
        return None
    if reject_class is None and card is not None:
        reject_class = card.thresholds.reject_class
    candidates = [{"code": str(c), "score": round(p, 4)} for c, p in ranked
                  if not is_negative(str(c), reject_class)][:3]
    return str(ranked[0][0]), round(ranked[0][1], 4), candidates


def _is_geo_duplicate(lat: Optional[float], lon: Optional[float], seen: list, radius_m: float) -> bool:
    """True when (lat, lon) lies within `radius_m` (the clustering radius
    resolved at this position for this type) of a position already kept in
    the job; otherwise the position is remembered."""
    if lat is None or lon is None:
        return False
    for slat, slon in seen:
        if haversine_m(lat, lon, slat, slon) <= radius_m:
            return True
    seen.append((lat, lon))
    return False


def _dedup_radius(ctx: "_JobContext", lat: float, lon: float, det_type: str) -> float:
    return ctx.clusters.radius_for(lat, lon, det_type)


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


def _clip_writer(out_base: str, fps: float, w: int, h: int):
    """(write(frame), close() → (path, content type) | None) of a browser-playable clip:
    H.264 MP4 through the ffmpeg binary when the image has it, else VP8 WebM through
    OpenCV (its pip wheel has no H.264 encoder — 'avc1' fails with codec_id=27)."""
    import shutil
    import subprocess
    fps = fps if fps and 1 <= fps <= 120 else 25.0
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        path = out_base + ".mp4"
        proc = subprocess.Popen(
            [ffmpeg, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}",
             "-r", f"{fps:.3f}", "-i", "-", "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
             "-pix_fmt", "yuv420p", "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-movflags", "+faststart", path],
            stdin=subprocess.PIPE, stderr=subprocess.PIPE)

        def write(frame):
            proc.stdin.write(np.ascontiguousarray(frame).tobytes())

        def close():
            proc.stdin.close()
            err = proc.stderr.read().decode(errors="replace")
            ok = proc.wait(timeout=120) == 0 and Path(path).exists() and Path(path).stat().st_size > 0
            if not ok:
                logger.warning(f"Context clip: ffmpeg failed ({err.strip()[:200]})")
            return (path, "video/mp4") if ok else None
        return write, close
    path = out_base + ".webm"
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"VP80"), fps, (w, h))
    if not writer.isOpened():
        logger.warning("Context clip: no video encoder available (ffmpeg missing, VP8 unavailable)")
        return None, None

    def close():
        writer.release()
        ok = Path(path).exists() and Path(path).stat().st_size > 0
        return (path, "video/webm") if ok else None
    return writer.write, close


def _save_context_clip(
    source_path: str,
    job_id: str,
    det_type: str,
    frame_num: int,
    fps: float,
    det_id: str,
    annotated_frame: np.ndarray,
    seconds: int = 5,
) -> Optional[str]:
    """
    Extract a ±`seconds` video clip around frame_num from source_path and upload it
    (H.264 MP4, else VP8 WebM — both play in the browser). The detection frame is
    replaced with the annotated version (bounding box drawn). Called for detections
    whose location is not confirmed (location_pending): the validator sees the
    overlay before / after the detection to correct the position.
    """
    import tempfile
    window = int((fps or 25.0) * seconds)
    start  = max(0, frame_num - 1 - window)        # frame_num is 1-based, positions 0-based
    end    = frame_num - 1 + window

    cap = cv2.VideoCapture(source_path)
    if not cap.isOpened():
        return None
    w   = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h   = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    out_base = str(Path(tempfile.gettempdir()) / f"clip_{det_id[:8]}")
    result = None
    try:
        write, close = _clip_writer(out_base, fps, w, h)
        if write is None:
            return None
        cap.set(cv2.CAP_PROP_POS_FRAMES, start)
        try:
            for fn in range(start, end + 1):
                ret, f = cap.read()
                if not ret:
                    break
                if fn == frame_num - 1 and annotated_frame is not None \
                        and annotated_frame.shape[:2] == f.shape[:2]:
                    # Use the annotated frame so the validator sees the detection box
                    f = annotated_frame.copy()
                cv2.putText(f, f"frame {fn + 1}", (10, h - 10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 0), 1)
                write(f)
        finally:
            result = close()
    except Exception as e:
        logger.warning(f"Context clip encoding failed: {e}")
    finally:
        cap.release()
    if result is None:
        for ext in (".mp4", ".webm"):
            Path(out_base + ext).unlink(missing_ok=True)
        return None
    out_path, content_type = result
    clip_name = (
        f"job-{job_id[:8]}_"
        f"det-{det_id[:8]}_"
        f"f{frame_num:06d}_pm{seconds}s{Path(out_path).suffix}"
    )
    minio_path = f"{job_id}/context_clips/{det_type}/{clip_name}"
    try:
        with open(out_path, "rb") as fh:
            return upload_image(fh.read(), minio_path, content_type=content_type)
    except Exception as e:
        logger.warning(f"Context clip upload failed: {e}")
        return None
    finally:
        Path(out_path).unlink(missing_ok=True)


def _save_clean_frame(ctx: "_JobContext", frame: np.ndarray, frame_num: int) -> Optional[str]:
    """Upload the clean full frame (no box drawn) once per job frame — the
    detector training image (docs/TRAINING-DATA.md). Returns the URL or None."""
    if frame_num in ctx.clean_frames:
        return ctx.clean_frames[frame_num]
    url = None
    try:
        _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
        url = upload_image(buf.tobytes(), f"{ctx.job.id}/frames/f{frame_num:06d}_clean.jpg")
    except Exception as e:
        logger.warning(f"Clean frame upload failed: {e}")
    ctx.clean_frames[frame_num] = url
    return url


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


class _NeighbourFrames:
    """Frames around a processed frame (±1–2 s), read on demand from a second capture of
    the media — the overlay of a frame can be unreadable during a colour transition."""

    def __init__(self, path: str, fps: float, total: int):
        self.path, self.fps, self.total = path, fps, total
        self.cap = None
        self.pos = -1                     # index (0-based) of the next frame the capture returns
        self.cache: dict[int, np.ndarray] = {}

    def at(self, frame_num: int, offset_s: float) -> Optional[np.ndarray]:
        idx = frame_num - 1 + int(round(offset_s * self.fps))
        if idx < 0 or (self.total and idx >= self.total):
            return None
        if idx in self.cache:
            return self.cache[idx]
        try:
            if self.cap is None:
                self.cap = cv2.VideoCapture(self.path)
                self.pos = 0
            if not 0 <= idx - self.pos <= 2 * self.fps:
                self.cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                self.pos = idx
            while self.pos < idx:
                self.cap.grab()
                self.pos += 1
            ok, frame = self.cap.read()
            self.pos += 1
        except Exception as exc:                 # never fail the job for a retry read
            logger.debug(f"neighbour frame {idx}: {exc}")
            return None
        if not ok:
            return None
        if len(self.cache) > 8:
            self.cache.clear()
        self.cache[idx] = frame
        return frame

    def close(self) -> None:
        if self.cap is not None:
            self.cap.release()
            self.cap = None


class _JobContext:
    """Per-job state shared by every stored detection."""

    def __init__(self, job: Job, source_path: str, fps: float, rules: ValidationSettings,
                 clusters: hazard_service.ClusterContext, policy: Optional[ai_validation.Policy] = None,
                 rng: Optional[random.Random] = None, ai: Optional[AiValidationSettings] = None,
                 pvs: Optional[ParkedVehicleSettings] = None):
        self.job, self.source_path, self.fps = job, source_path, fps
        self.rules, self.clusters = rules, clusters
        self.policy = policy or ai_validation.legacy_policy(rules)
        self.rng = rng or random.Random()
        self.ai = ai or AiValidationSettings()
        # recording time: (video time, OSD date-time) of each frame where the OSD time changed
        self.osd_times: list[tuple[float, datetime]] = []
        try:
            self.osd_tz = ZoneInfo(settings.OSD_TIMEZONE)
        except Exception:                        # unknown zone name → keep the OSD time as UTC
            self.osd_tz = timezone.utc
        # parked vehicles: settings, ego path, recently judged tracks (queue peers), OSRM state
        self.pv = pvs or ParkedVehicleSettings()
        self.pv_version = pv.rules_version(self.pv)
        self.ego = pv.EgoPath()
        self.vehicle_peers: deque = deque(maxlen=200)
        self.horizon = pv.HorizonEstimator(min_boxes=self.pv.auto_calibrate_min_boxes)   # camera self-calibration
        self.vehicle_pending: list = []      # finished vehicle tracks waiting for the self-calibration
        self.vehicle_class_ids: Optional[list[int]] = None
        self.osrm_down: Optional[str] = None
        self.clean_frames: dict[int, Optional[str]] = {}    # frame number → clean frame URL
        self.osd_reader: Optional[OsdReader] = None
        self.neighbours: Optional["_NeighbourFrames"] = None

    def read_osd(self, frame: np.ndarray, frame_num: int, voter: GpsVoter) -> OSDData:
        """OSD of a processed frame (job reader, neighbour frames for unreadable overlays)."""
        if self.osd_reader is None:
            self.osd_reader = OsdReader(voter=voter)
        t = self.video_time(frame_num)
        nb = (lambda off: self.neighbours.at(frame_num, off)) if self.neighbours is not None else None
        return self.osd_reader.read(frame, float(frame_num) if t is None else t, nb)

    def osd_events(self, db) -> None:
        """Record the layout decisions of the reader (event `osd_layout`, job.osd_layout)."""
        if self.osd_reader is None:
            return
        for ev in self.osd_reader.pop_events():
            self.job.osd_layout = ev["layout"]
            if ev.get("timezone"):
                try:
                    self.osd_tz = ZoneInfo(ev["timezone"])
                except Exception:
                    pass
            job_service.record_event(db, self.job.id, "osd_layout", details=ev)

    def video_time(self, frame_num: int) -> Optional[float]:
        return round((frame_num - 1) / self.fps, 3) if self.fps else None

    def observe_osd(self, frame_num: int, osd: OSDData) -> None:
        """Remember the frame's OSD time (recorded_at) and ego fix / speed (parked vehicles)."""
        t = self.video_time(frame_num)
        t = float(frame_num) if t is None else t
        if osd.timestamp is not None and (not self.osd_times or self.osd_times[-1][1] != osd.timestamp):
            self.osd_times.append((t, osd.timestamp))
        self.ego.add(t, osd.latitude, osd.longitude, osd.speed_kmh, osd.gps_interpolated)

    def heading_at(self, frame_num: int) -> Optional[float]:
        """Ego heading (° from north) at a frame from the OCR GPS fixes around it."""
        t = self.video_time(frame_num)
        t = float(frame_num) if t is None else t
        w = object_position.HEADING_WINDOW_S
        return object_position.heading_from_fixes(
            [(s.t_s, s.lat, s.lon) for s in self.ego.window(t - w, t + w, pad=0.0)], t)

    def recorded_at(self, frame_num: int, osd: OSDData) -> Optional[datetime]:
        """Recording time of a frame (see module docstring), timezone-aware."""
        when = osd.timestamp
        if when is None and self.osd_times:
            t = self.video_time(frame_num)
            t = float(frame_num) if t is None else t
            i = bisect_left([a for a, _ in self.osd_times], t)
            near = min(self.osd_times[max(0, i - 1):i + 1], key=lambda a: abs(a[0] - t))
            when = near[1] + timedelta(seconds=t - near[0])
        if when is None:
            return None
        return when if when.tzinfo is not None else when.replace(tzinfo=self.osd_tz)


def _policy_for_job(ai: AiValidationSettings, rules: ValidationSettings,
                    pvs: Optional[ParkedVehicleSettings] = None) -> ai_validation.Policy:
    """Decision policy of a job: settings + the thresholds of the loaded models'
    cards (+ the parked-vehicle score thresholds, its rule set's "card")."""
    cards = pv.merge_policy_cards(model_registry.thresholds_from_cards(_model_cards),
                                  pvs or ParkedVehicleSettings())
    return ai_validation.build_policy(ai, rules, cards, [t for t, _ in model_registry.SLOTS.values()])


async def _store_detection(db, ctx: _JobContext, det_id: str, model_key: str, det_type: str, subtype,
                           conf: float, frame_num: int, box, osd: OSDData, img_url, crop_url,
                           annotated: np.ndarray, verifier_score: Optional[float] = None, *,
                           predicted_class: Optional[str] = None, class_candidates: Optional[list] = None,
                           accept_blockers=(), verifier_model: Optional[str] = None,
                           ai_details: Optional[dict] = None,
                           rule_reject: Optional[tuple[str, str]] = None,
                           frame: Optional[np.ndarray] = None) -> Detection:
    """Create the detection with its initial workflow status (see module docstring).
    `rule_reject` (code, reason): a rule set already rejected it (parked
    vehicles stored for review) — the mode / audit sampling still apply."""
    job = ctx.job
    loc_status, loc_source = wf.location_status_for(osd.latitude, osd.longitude, osd.gps_interpolated,
                                                    osd.gps_source)
    if rule_reject is not None:
        decision = ai_validation.rule_reject(ctx.policy, rule_reject[0], rule_reject[1], loc_status,
                                             ctx.rng.random())
    else:
        decision = ai_validation.decide(ctx.policy, det_type, conf, verifier_score, loc_status, ctx.rng.random(),
                                        predicted_class=predicted_class, accept_blockers=accept_blockers)
    status, action, rejection_code = decision.status, decision.action, decision.rejection_code
    verifier_key = model_registry.VERIFIER_FOR_TYPE.get(det_type) if verifier_score is not None else None

    clip_url = None
    if status == ReviewStatus.location_pending.value:
        clip_url = _save_context_clip(ctx.source_path, job.id, model_key, frame_num, ctx.fps, det_id,
                                      annotated, ctx.rules.clip_seconds)

    geo = empty_address()
    if osd.latitude is not None and osd.longitude is not None:
        geo = reverse_geocode_details(osd.latitude, osd.longitude)
    x1, y1, x2, y2 = box
    det = Detection(
        id=det_id, job_id=job.id, type=det_type,
        subtype=subtype, confidence=conf,
        predicted_type=det_type, predicted_subtype=subtype,
        frame_number=frame_num, video_time_s=ctx.video_time(frame_num),
        bbox_x1=float(x1), bbox_y1=float(y1), bbox_x2=float(x2), bbox_y2=float(y2),
        model_name=model_key, model_version=_model_versions.get(model_key),
        model_card_version=_card_label(model_key),
        verifier_score=verifier_score,
        verifier_model=verifier_model or (_model_versions.get(verifier_key) if verifier_key else None),
        verifier_card_version=_card_label(verifier_key) if verifier_key else None,
        class_candidates=class_candidates, ai_details=ai_details,
        ai_decision=decision.ai_decision, ai_decision_reason=decision.reason[:250],
        audit_sample=decision.audit_sample,
        raw_gps_text=osd.raw_gps_text,
        latitude=osd.latitude, longitude=osd.longitude,
        gps_interpolated=osd.gps_interpolated,
        location_status=loc_status, location_source=loc_source,
        country=geo["country"], city=geo["city"],
        speed_kmh=osd.speed_kmh, vehicle_id=osd.vehicle_id,
        captured_at=osd.timestamp, recorded_at=ctx.recorded_at(frame_num, osd), image_url=img_url,
        crop_url=crop_url,
        context_clip_url=clip_url,
        frame_clean_url=_save_clean_frame(ctx, frame, frame_num) if frame is not None else None,
        frame_width=int(frame.shape[1]) if frame is not None else None,
        frame_height=int(frame.shape[0]) if frame is not None else None,
        location_name=osd.location_name or geo["location_name"], rpm=osd.rpm,
        road=geo["road"], locality=geo["locality"], district=geo["district"], town=geo["town"],
        region=geo["region"], display_location=geo["display"],
        geocoded_at=datetime.utcnow() if has_address(geo) else None,
        review_status=ReviewStatus(status),
    )
    if frame is not None:
        h, w = frame.shape[:2]
        aid = ai_details or {}
        vclass = aid.get("vehicle_class")
        object_position.apply(det, object_position.estimate(
            det_type, (x1, y1, x2, y2), w, h, osd.latitude, osd.longitude, ctx.heading_at(frame_num),
            pv.camera(ctx.pv, osd.vehicle_id, getattr(osd, "layout", None), ctx.horizon.fit()),
            vehicle_height_m=ctx.pv.class_heights_m.get(vclass) if vclass else None,
            target=aid.get("target_position")))
    if action != "create":
        det.reviewed_by, det.reviewed_at = "system", datetime.utcnow()
    if rejection_code:
        det.rejection_code, det.rejection_source = rejection_code, "auto"
    db.add(det)
    details = {"confidence": conf, "verifier_score": verifier_score, "ai_decision": decision.ai_decision,
               "reason": decision.reason, "mode": ctx.policy.mode}
    if predicted_class is not None:
        details["predicted_class"] = predicted_class
    if accept_blockers:
        details["accept_blockers"] = [c for c, _ in accept_blockers]
    if ai_details:
        details.update({k: ai_details.get(k) for k in ("verdict", "severity", "vehicle_class")})
    if rule_reject is not None:
        details["rule_reject"] = rule_reject[0]
    if decision.audit_sample:
        details["audit_sample"] = True     # the AI decision is only a suggestion here
    wf.record_creation(db, det, action, details, audited=decision.audit_sample)
    if status in (ReviewStatus.validated.value, ReviewStatus.corrected.value, ReviewStatus.rejected.value):
        await training_data.record_decision(db, det, job=job, actor_type="system")
    if status != ReviewStatus.rejected.value:
        # Cluster-first (docs/CLUSTERS.md): every located detection joins its cluster now —
        # an accepted one confirms it; an unreviewed one joining a confirmed hazard is a
        # re-sighting (auto-validated), else it waits in a candidate cluster for review.
        await db.flush()
        await hazard_service.attach(db, det, ctx.clusters)
    return det


TRACK_BATCH = 200


async def _flush_track(db, points: list) -> None:
    """Insert buffered trajectory points in one statement."""
    if points:
        await db.execute(insert(JobTrackPoint), points)
        points.clear()


class _Stats:
    """Running detection metrics of the job (RRAP-07-006)."""

    def __init__(self):
        self.count, self.conf_sum, self.by_model = 0, 0.0, {}
        self.duplicates_skipped = 0                      # repeated observations not stored
        self.vehicle_verdicts: dict[str, int] = {}     # parked-vehicle tracks per verdict (stored or not)
        self.vehicle_masked = 0                         # vehicle boxes ignored by the masks

    def verdict(self, verdict: str) -> None:
        self.vehicle_verdicts[verdict] = self.vehicle_verdicts.get(verdict, 0) + 1

    def event_details(self) -> dict:
        """Extra job-event details: repeated observations skipped, parked vehicles (when the model ran)."""
        out: dict = {"duplicates_skipped": self.duplicates_skipped} if self.duplicates_skipped else {}
        if self.vehicle_verdicts or self.vehicle_masked:
            out.update(vehicle_verdicts=dict(self.vehicle_verdicts), vehicle_boxes_masked=self.vehicle_masked)
        return out

    def add(self, model_key: str, conf: float) -> None:
        self.count += 1
        self.conf_sum += conf
        self.by_model[model_key] = self.by_model.get(model_key, 0) + 1

    def apply(self, job: Job, processed: int) -> None:
        job.processed = processed
        job.detections = self.count
        job.detections_by_model = dict(self.by_model)
        job.avg_confidence = round(self.conf_sum / self.count, 4) if self.count else None


async def _cancel_requested(db, job_id: str) -> bool:
    return bool((await db.execute(select(Job.cancel_requested).where(Job.id == job_id))).scalar())


async def process_source(
    job: Job,
    source_path: str,
    db: AsyncSession,
    enabled_models: list = None,
    heartbeat: Optional[Callable[[], None]] = None,
) -> str:
    """Main processing loop. Call from the Celery task. Returns "done" or
    "cancelled"; raises PermanentJobError when the media cannot be read."""
    logger.info(f"[Job {job.id}] Starting — file: {job.filename}, models: {enabled_models}")
    enabled = enabled_models or list(job_service.DEFAULT_MODELS)

    cap = cv2.VideoCapture(source_path)
    if not cap.isOpened():
        logger.error(f"[Job {job.id}] Cannot open source: {source_path}")
        raise PermanentJobError(f"Cannot open source media '{job.source_name or job.filename}' "
                                f"(unreadable or unsupported format)")

    _load_models(enabled)
    reset_last()
    voter = GpsVoter()

    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    fps   = cap.get(cv2.CAP_PROP_FPS) or 0
    proc: ProcessingSettings = await get_section(db, "processing")  # type: ignore[assignment]
    job_skip = job.frame_skip or proc.frame_skip_default
    skips = job_service.model_skips(enabled, job_skip, proc.frame_skip_by_model)
    job.frame_skip   = job_skip
    job.total_frames = total
    job.fps          = round(fps, 3) if fps else None
    job.duration_s   = round(total / fps, 2) if fps and total else None
    job.processed = job.detections = 0
    job.status       = JobStatus.processing
    job.started_at   = datetime.utcnow()
    job_service.record_event(db, job.id, "started", details={
        "attempt": job.attempts, "total_frames": total, "fps": job.fps, "frame_skip": skips})
    await db.commit()
    rules: ValidationSettings = await get_section(db, "validation")  # type: ignore[assignment]
    ai: AiValidationSettings = await get_section(db, "ai_validation")  # type: ignore[assignment]
    pvs: ParkedVehicleSettings = await get_section(db, "parked_vehicle")  # type: ignore[assignment]
    ctx = _JobContext(job, source_path, fps, rules, await hazard_service.load_context(db),
                      _policy_for_job(ai, rules, pvs), ai=ai, pvs=pvs)
    if pv.MODEL_KEY in _models:
        ctx.vehicle_class_ids = _vehicle_class_ids(_models[pv.MODEL_KEY], pvs.vehicle_classes)
        if not ctx.vehicle_class_ids:
            logger.warning(f"[Job {job.id}] vehicles model has none of the classes {pvs.vehicle_classes} "
                           f"({_class_names(_models[pv.MODEL_KEY])[:10]}…) — parked-vehicle detection skipped")
    osd_cfg: OsdSettings = await get_section(db, "osd")  # type: ignore[assignment]
    layout, layout_method = layout_for_job(osd_cfg, job.osd_layout_requested, job.source_type,
                                           (job.stream_id, job.connection_id))
    ctx.osd_reader = OsdReader(
        osd_cfg.available(), forced=layout, method=layout_method, by_vehicle=osd_cfg.by_vehicle,
        box=osd_cfg.gps_box, voter=voter, probe_frames=osd_cfg.probe_frames,
        full_frame_fallback=osd_cfg.full_frame_fallback,
        neighbour_offsets=(-1.0, 1.0, -2.0, 2.0) if osd_cfg.neighbour_retry else ())
    if osd_cfg.neighbour_retry and total > 1 and fps:
        ctx.neighbours = _NeighbourFrames(source_path, fps, total)
    track: list = []
    stats = _Stats()
    logger.info(f"[Job {job.id}] Video opened — {total} frames @ {fps:.1f}fps, sampling {skips}")

    geo_seen: dict = {key: [] for key in _DETECTORS}
    px_seen:  dict = {key: [] for key in _DETECTORS}
    try:
        outcome = await _frame_loop(db, job, cap, ctx, voter, enabled, skips, track, stats,
                                    geo_seen, px_seen, total, heartbeat)
    finally:
        cap.release()
        if ctx.neighbours is not None:
            ctx.neighbours.close()
        _unload_models()
    frame_num, cancelled = outcome
    ctx.osd_reader.finish()
    ctx.osd_events(db)
    await _flush_track(db, track)
    stats.apply(job, frame_num)

    if cancelled:
        await db.refresh(job, ["cancelled_by"])   # set by the API request that asked for it
        job_service.mark_cancelled(db, job, None, {"processed": frame_num, "detections": stats.count,
                                                   **stats.event_details()})
        await db.commit()
        await _sync_training_data(db, job.id)
        logger.info(f"[Job {job.id}] CANCELLED at frame {frame_num}/{total} — {stats.count} detections kept")
        return "cancelled"

    job_service.delete_media(job)
    if Path(source_path).exists():
        Path(source_path).unlink(missing_ok=True)
    job.status      = JobStatus.done
    job.finished_at = datetime.utcnow()
    job_service.record_event(db, job.id, "done", details={
        "processed": frame_num, "detections": stats.count, "by_model": stats.by_model, **stats.event_details(),
        "osd": ctx.osd_reader.summary()})
    await db.commit()
    await _sync_training_data(db, job.id)
    logger.info(f"[Job {job.id}] DONE — {stats.count} detections in {frame_num} frames"
                + (f" — vehicle verdicts {stats.vehicle_verdicts}" if stats.vehicle_verdicts else ""))
    return "done"


async def _sync_training_data(db, job_id: str) -> None:
    """Write the automatic decisions of the job to the training dataset (the
    beat task retries whatever fails here)."""
    try:
        from app.modules.m06_hazards.training_data.sync import sync_dirty
        await sync_dirty(db, job_id=job_id, limit=5000)
    except Exception as exc:
        logger.warning(f"[Job {job_id}] training-data sync failed ({exc}) — left for the beat task")
        await db.rollback()


# model key → (detection type, box colour BGR, image file tag)
_DETECTORS = {
    "pothole":   ("pothole", (0, 0, 255), "pothole"),
    "signs":     ("traffic_sign", (255, 255, 255), "sign"),
    "speedbump": ("speed_bump", (255, 0, 255), "speedbump"),
    pv.MODEL_KEY: (pv.DETECTION_TYPE, (0, 140, 255), "vehicle"),
}


def _vehicle_class_ids(model, wanted: list[str]) -> list[int]:
    """COCO class ids of the wanted vehicle class names (filter passed to the detector)."""
    names = getattr(model, "names", None) or {}
    items = names.items() if isinstance(names, dict) else enumerate(names)
    want = {w.lower() for w in wanted}
    return sorted(int(i) for i, n in items if str(n).lower() in want)


MAX_PENDING_VEHICLE_TRACKS = 80   # finished tracks held back while the camera self-calibrates


def _vehicle_batch(ctx: "_JobContext", done: list, final: bool = False) -> list:
    """Finished vehicle tracks to judge now. While the per-job camera
    self-calibration (pv.HorizonEstimator) has not converged, tracks are held
    back (their ranges would use the default horizon, off by a large factor on
    most dashcams) — up to MAX_PENDING_VEHICLE_TRACKS, and all at the end of
    the media."""
    if (not final and ctx.pv.auto_calibrate and ctx.horizon.fit() is None
            and len(ctx.vehicle_pending) + len(done) <= MAX_PENDING_VEHICLE_TRACKS):
        ctx.vehicle_pending.extend(done)
        return []
    batch, ctx.vehicle_pending = [*ctx.vehicle_pending, *done], []
    return batch


def _vehicle_tracker(pvs: ParkedVehicleSettings) -> static_check.Tracker:
    """Vehicles sweep across the frame as we pass them: motion-predicted matching."""
    return static_check.Tracker(max_gap=pvs.tracker_max_gap, match_iou=pvs.tracker_match_iou, predict=True,
                                max_scale_change=pvs.tracker_max_scale_change)


class _Best:
    """Payload kept for the best sighting of a track (frame copy for the images)."""
    __slots__ = ("frame", "frame_num", "box", "conf", "osd")

    def __init__(self, frame, frame_num, box, conf, osd):
        self.frame, self.frame_num, self.box, self.conf, self.osd = frame, frame_num, box, conf, osd


async def _repeated_observation(db, ctx: _JobContext, det_type: str, subtype, box, frame: np.ndarray,
                                frame_num: int, osd: OSDData, stats: "_Stats") -> bool:
    """True (and counted) when the track repeats an observation already stored —
    the same footage processed before under another job / file (module docstring)."""
    h, w = frame.shape[:2]
    obs = SimpleNamespace(id=None, type=det_type, subtype=subtype, predicted_type=det_type,
                          predicted_subtype=subtype, vehicle_id=osd.vehicle_id,
                          recorded_at=ctx.recorded_at(frame_num, osd), raw_gps_text=osd.raw_gps_text,
                          bbox_x1=box[0], bbox_y1=box[1], bbox_x2=box[2], bbox_y2=box[3],
                          frame_width=w, frame_height=h)
    existing = await observations.find_existing(db, obs, ctx.clusters.distinct_subtype_types)
    if existing is None:
        return False
    stats.duplicates_skipped += 1
    logger.info(f"[Job {ctx.job.id}] {det_type} @ frame={frame_num}: same observation as detection "
                f"{existing.id} (job {existing.job_id}) — not stored")
    return True


def _sign_stage(ctx: _JobContext, label: str, frame: np.ndarray, box) -> dict:
    """Subtype / classifier outputs of a finished sign track (see module docstring)."""
    if label == TRAFFIC_LIGHT:
        return {"subtype": TRAFFIC_LIGHT}
    if label != "traffic_sign":                       # legacy multi-class detector: class = subtype
        return {"subtype": label}
    res = _classify_sign(frame, *box)
    if res is None:                                   # 2-class detector without a usable classifier
        return {"subtype": UNKNOWN_SIGN, "predicted_class": UNKNOWN_SIGN}
    code, score, candidates = res
    th = ctx.policy.for_type("traffic_sign")
    return {"subtype": ai_validation.sign_subtype(th, code, score), "verifier_score": score,
            "predicted_class": code, "class_candidates": candidates}


async def _finalize(db, ctx: _JobContext, key: str, track: static_check.Track, geo_seen: dict, px_seen: dict,
                    stats: _Stats, video: bool, others: tuple = ()) -> None:
    """Store one detection for a finished track (its best sighting)."""
    if key == pv.MODEL_KEY:
        await _finalize_vehicle(db, ctx, track, geo_seen, px_seen, stats, others)
        return
    best: _Best = track.best
    job, osd, frame, frame_num = ctx.job, best.osd, best.frame, best.frame_num
    x1, y1, x2, y2 = best.box
    conf = best.conf
    det_type, colour, tag = _DETECTORS[key]
    if osd.latitude is not None:
        if _is_geo_duplicate(osd.latitude, osd.longitude, geo_seen[key],
                             _dedup_radius(ctx, osd.latitude, osd.longitude, det_type)):
            return
    elif _is_pixel_duplicate([x1, y1, x2, y2], px_seen[key], settings.DEDUP_DISTANCE_PX):
        return
    out: dict = {"subtype": None}
    if key == "pothole":
        out["verifier_score"] = _verify("pothole", frame, x1, y1, x2, y2)
    elif key == "signs":
        out = _sign_stage(ctx, track.label, frame, best.box)
    if await _repeated_observation(db, ctx, det_type, out["subtype"], best.box, frame, frame_num, osd, stats):
        return
    ai = ctx.ai
    blockers = static_check.assess(det_type, track, frame.shape[0], min_frames=ai.min_frames_for_auto_accept,
                                   max_drift_m=ai.max_static_drift_m, video=video,
                                   min_y_ratio=ai.pothole_min_y_ratio if key == "pothole" else None)
    rule_reject, ai_details = (_pothole_rules(ai, best.box, frame) if key == "pothole" else (None, None))
    score = out.get("verifier_score")
    annotated = frame.copy()
    cv2.rectangle(annotated, (x1, y1), (x2, y2), colour, 2)
    name = {"pothole": "Pothole", "speedbump": "SpeedBump"}.get(key) or str(out["subtype"])
    label = f"{name} {conf:.0%}" + (f" / cls {score:.0%}" if score is not None else "")
    cv2.putText(annotated, label, (x1, y1 - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.55, colour, 2)
    det_id = str(uuid.uuid4())
    img_url = upload_image(_frame_to_jpeg(annotated),
                           f"{job.id}/{key}/job-{job.id[:8]}_det-{det_id[:8]}_f{frame_num:06d}_{tag}.jpg")
    crop_url = _save_crop(frame, x1, y1, x2, y2, job.id, key, det_id, frame_num)
    await _store_detection(db, ctx, det_id, key, det_type, out["subtype"], conf, frame_num,
                           (x1, y1, x2, y2), osd, img_url, crop_url, annotated, score,
                           predicted_class=out.get("predicted_class"),
                           class_candidates=out.get("class_candidates"), accept_blockers=blockers,
                           ai_details=ai_details, rule_reject=rule_reject, frame=frame)
    stats.add(key, conf)
    logger.info(f"[Job {job.id}] {det_type} @ frame={frame_num} subtype={out['subtype']} conf={conf} "
                f"verifier={score} frames={len(track.sightings)} lat={osd.latitude} lon={osd.longitude}"
                + (" [interp]" if osd.gps_interpolated else "")
                + (f" blockers={[c for c, _ in blockers]}" if blockers else "")
                + (f" rule_reject={rule_reject[0]}" if rule_reject else ""))


def _pothole_rules(ai: AiValidationSettings, box, frame: np.ndarray,
                   ) -> tuple[Optional[tuple[str, str]], Optional[dict]]:
    """(rule_reject (code, reason), ai_details) of a pothole box — (None, None)
    when no false-positive rule fires (see module docstring)."""
    h, w = frame.shape[:2]
    rejects, reasons = static_check.pothole_rule_check(box, w, h, **static_check.pothole_rule_kwargs(ai))
    if not rejects:
        return None, None
    reason = "; ".join(f"{c}: {d}" for c, d in rejects)
    details = {"pipeline": "pothole_rules", "verdict": rejects[0][0], "reasons": reasons,
               "features": static_check.box_ratios(box, w, h)}
    return (rejects[0][0], reason), details


async def _road_distance(ctx: _JobContext, lat: float, lon: float) -> tuple[Optional[float], str]:
    """OSRM /nearest snap distance (m) of an estimated vehicle position; (None,
    why) when unknown. The first failure disables the check for the job."""
    if ctx.osrm_down:
        return None, ctx.osrm_down
    try:
        res = await get_osrm_client().nearest((lat, lon))
    except OSRMUnavailableError as exc:
        ctx.osrm_down = "OSRM unreachable during this job"
        logger.warning(f"[Job {ctx.job.id}] OSRM unavailable ({exc}) — parked-vehicle road check skipped")
        return None, ctx.osrm_down
    wps = res.data.get("waypoints") or []
    if res.data.get("code") != "Ok" or not wps or wps[0].get("distance") is None:
        return None, f"OSRM nearest: {res.data.get('code')}"
    return float(wps[0]["distance"]), ""


def _vehicle_track_input(track: static_check.Track) -> list:
    return [s for s in track.sightings if isinstance(s, pv.VehicleSighting)]


def _queue_peers(ctx: _JobContext, track: static_check.Track, features: pv.TrackFeatures, others) -> int:
    """Stationary in-corridor vehicles overlapping this track: already judged
    tracks + the ones still open / finished in the same batch."""
    peers = list(ctx.vehicle_peers)
    for other in others:
        if other is track or len(other.sightings) < 2:
            continue
        sts = _vehicle_track_input(other)
        if sts:
            o = other.best.osd if other.best is not None else None
            f, _ = pv.prepare(sts, ctx.ego.window(sts[0].t_s, sts[-1].t_s), ctx.pv, getattr(o, "vehicle_id", None),
                              getattr(o, "layout", None), ctx.horizon.fit())
            peers.append(pv.peer_summary(f, ctx.pv))
    return pv.count_queue_peers(features, peers)


async def _judge_vehicle_track(ctx: _JobContext, track: static_check.Track, others=()):
    """(Evaluation, sightings) of a finished vehicle track, None when empty — the
    part of _finalize_vehicle without storage (tools/parked_eval replays it)."""
    s = ctx.pv
    sightings = _vehicle_track_input(track)
    best: _Best = track.best
    if not sightings or best is None:
        return None
    osd = best.osd
    features, _cam = pv.prepare(sightings, ctx.ego.window(sightings[0].t_s, sightings[-1].t_s), s, osd.vehicle_id,
                                getattr(osd, "layout", None), ctx.horizon.fit())
    distance, note = None, ""
    if pv.needs_road_check(features, s):
        distance, note = await _road_distance(ctx, features.target_lat, features.target_lon)  # type: ignore[arg-type]
    ev = pv.finish(features, s, road_distance_m=distance, road_note=note,
                   queue_peers=_queue_peers(ctx, track, features, others), vehicle_id=osd.vehicle_id)
    ctx.vehicle_peers.append(pv.peer_summary(features, s))
    return ev, sightings


async def _finalize_vehicle(db, ctx: _JobContext, track: static_check.Track, geo_seen: dict, px_seen: dict,
                            stats: _Stats, others=()) -> None:
    """Judge a finished vehicle track (parked?) and store it when the rules say so."""
    key = pv.MODEL_KEY
    judged = await _judge_vehicle_track(ctx, track, others)
    if judged is None:
        return
    ev, sightings = judged
    best: _Best = track.best
    osd = best.osd
    stats.verdict(ev.verdict)
    logger.debug(f"[Job {ctx.job.id}] vehicle track {len(sightings)} sightings → {ev.verdict} "
                 f"({ev.reason_text()})")
    if not ev.store:
        return
    frame, frame_num = best.frame, best.frame_num
    x1, y1, x2, y2 = best.box
    if osd.latitude is not None:
        if _is_geo_duplicate(osd.latitude, osd.longitude, geo_seen[key],
                             _dedup_radius(ctx, osd.latitude, osd.longitude, pv.DETECTION_TYPE)):
            return
    elif _is_pixel_duplicate([x1, y1, x2, y2], px_seen[key], settings.DEDUP_DISTANCE_PX):
        return
    if await _repeated_observation(db, ctx, pv.DETECTION_TYPE, ev.subtype, best.box, frame, frame_num, osd, stats):
        return
    _det_type, colour, tag = _DETECTORS[key]
    annotated = frame.copy()
    cv2.rectangle(annotated, (x1, y1), (x2, y2), colour, 2)
    label = (f"{ev.verdict if ev.rejection_code else 'Parked'} {ev.subtype or ''} {ev.vehicle_class} "
             f"{best.conf:.0%} / score {ev.score:.0%}")
    cv2.putText(annotated, label, (x1, max(12, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, colour, 2)
    det_id = str(uuid.uuid4())
    img_url = upload_image(_frame_to_jpeg(annotated),
                           f"{ctx.job.id}/{key}/job-{ctx.job.id[:8]}_det-{det_id[:8]}_f{frame_num:06d}_{tag}.jpg")
    crop_url = _save_crop(frame, x1, y1, x2, y2, ctx.job.id, key, det_id, frame_num)
    await _store_detection(db, ctx, det_id, key, pv.DETECTION_TYPE, ev.subtype, best.conf, frame_num,
                           (x1, y1, x2, y2), osd, img_url, crop_url, annotated, ev.score,
                           accept_blockers=ev.blockers, verifier_model=ctx.pv_version, ai_details=ev.details(),
                           rule_reject=(ev.rejection_code, ev.reason_text()) if ev.rejection_code else None,
                           frame=frame)
    stats.add(key, best.conf)
    logger.info(f"[Job {ctx.job.id}] parked_vehicle @ frame={frame_num} subtype={ev.subtype} "
                f"class={ev.vehicle_class} verdict={ev.verdict} score={ev.score} severity={ev.severity} "
                f"lat={osd.latitude} lon={osd.longitude}")


def _observe_vehicles(ctx: _JobContext, tracker: static_check.Tracker, results, frame: np.ndarray, frame_num: int,
                      osd: OSDData, step: int, stats: _Stats) -> None:
    """Feed the vehicle boxes of a frame to the vehicle tracker (masks applied)."""
    h, w = frame.shape[:2]
    names = results[0].names
    t = ctx.video_time(frame_num)
    t = float(frame_num) if t is None else t
    items = []
    for box in results[0].boxes:
        x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
        conf = round(box.conf[0].item(), 4)
        cls = str(names[int(box.cls[0].item())]).lower()
        if conf < ctx.pv.detector_confidence or cls not in ctx.pv.vehicle_classes:
            continue
        nbox = pv_geo.normalise_box((x1, y1, x2, y2), w, h)
        if pv_geo.is_masked(nbox, ctx.pv.masks_for(getattr(osd, "layout", None)), ctx.pv.hood_mask_bottom_ratio):
            stats.vehicle_masked += 1
            continue
        ctx.horizon.add(nbox, conf, cls)
        sighting = pv.VehicleSighting(frame_num, (x1, y1, x2, y2), conf, osd.latitude, osd.longitude,
                                      t_s=t, cls=cls, speed_kmh=osd.speed_kmh, frame_w=w, frame_h=h,
                                      timestamp=osd.timestamp)
        items.append((sighting, "vehicle",
                      lambda f=frame, n=frame_num, b=(x1, y1, x2, y2), c=conf, o=osd: _Best(f.copy(), n, b, c, o)))
    tracker.observe_frame(items, step)      # global assignment of the frame's boxes


PROGRESS_EVERY = 50   # frames read between two progress commits


async def _commit_progress(db, job: Job, track: list, stats: "_Stats", frame_num: int, total: int,
                           heartbeat: Optional[Callable[[], None]]) -> None:
    await _flush_track(db, track)
    stats.apply(job, frame_num)
    await db.commit()
    if heartbeat is not None:
        heartbeat()
    pct = 100 * frame_num // total if total else 0
    logger.info(f"[Job {job.id}] Progress: frame {frame_num}/{total} ({pct}%) — {stats.count} detections")


async def _frame_loop(db, job: Job, cap, ctx: _JobContext, voter, enabled: list, skips: dict, track: list,
                      stats: _Stats, geo_seen: dict, px_seen: dict, total: int,
                      heartbeat: Optional[Callable[[], None]]) -> tuple[int, bool]:
    """Read every frame, run the sampled models, track their boxes and store
    one detection per finished track. Returns (frames read, cancelled)."""
    trackers = {key: static_check.Tracker() for key in _DETECTORS}
    trackers[pv.MODEL_KEY] = _vehicle_tracker(ctx.pv)
    video = total != 1
    frame_num, cancelled = 0, False
    while True:
        ret, frame = cap.read()
        if not ret:
            break

        frame_num += 1
        if frame_num % CANCEL_CHECK_EVERY == 0 and await _cancel_requested(db, job.id):
            cancelled = True
            break
        # Progress + detections found so far are committed on EVERY 50th frame read —
        # before the sampling skip below, which would otherwise hide it whenever no
        # model samples that frame (e.g. frame_skip 5 never samples frame 50, 100 …).
        if frame_num % PROGRESS_EVERY == 0:
            await _commit_progress(db, job, track, stats, frame_num, total, heartbeat)
        active = job_service.models_for_frame(frame_num - 1, skips)
        if not active:
            continue

        # Extract OSD from clean frame before any annotation
        osd = ctx.read_osd(frame, frame_num, voter)
        ctx.osd_events(db)
        logger.debug(
            f"[Job {job.id}] frame={frame_num} raw={osd.raw_gps_text!r} "
            f"lat={osd.latitude} lon={osd.longitude}"
        )
        ctx.observe_osd(frame_num, osd)
        if osd.latitude is not None and osd.longitude is not None and not osd.gps_interpolated:
            track.append({"job_id": job.id, "frame_number": frame_num, "video_time_s": ctx.video_time(frame_num),
                          "latitude": osd.latitude, "longitude": osd.longitude,
                          "speed_kmh": osd.speed_kmh, "source": osd.gps_source or "ocr",
                          "recorded_at": ctx.recorded_at(frame_num, osd)})
            if len(track) >= TRACK_BATCH:
                await _flush_track(db, track)

        for key in _DETECTORS:
            if key not in active or key not in _models:
                continue
            step = skips.get(key, 1)
            if key == pv.MODEL_KEY:
                if ctx.vehicle_class_ids:
                    results = _models[key](frame, conf=ctx.pv.detector_confidence, iou=settings.IOU_THRESHOLD,
                                           classes=ctx.vehicle_class_ids, verbose=False, **_predict_kwargs(key))
                    _observe_vehicles(ctx, trackers[key], results, frame, frame_num, osd, step, stats)
                done_batch = _vehicle_batch(ctx, trackers[key].expire(frame_num, step))
                for done in done_batch:
                    await _finalize(db, ctx, key, done, geo_seen, px_seen, stats, video,
                                    others=(*trackers[key].open, *done_batch))
                continue
            results = _models[key](frame, conf=settings.CONF_THRESHOLD,
                                   iou=settings.IOU_THRESHOLD, verbose=False, **_predict_kwargs(key))
            names = results[0].names
            for box in results[0].boxes:
                x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
                conf = round(box.conf[0].item(), 4)
                label = str(names[int(box.cls[0].item())]) if key == "signs" else key
                sighting = static_check.Sighting(frame_num, (x1, y1, x2, y2), conf, osd.latitude, osd.longitude)
                trackers[key].observe(sighting, label, step,
                                      lambda f=frame, n=frame_num, b=(x1, y1, x2, y2), c=conf, o=osd:
                                      _Best(f.copy(), n, b, c, o))
            for done in trackers[key].expire(frame_num, step):
                await _finalize(db, ctx, key, done, geo_seen, px_seen, stats, video)
    # end of media or cancel: what is still tracked becomes detections too
    for key, tracker in trackers.items():
        done_batch = tracker.flush()
        if key == pv.MODEL_KEY:
            done_batch = _vehicle_batch(ctx, done_batch, final=True)
        for done in done_batch:
            await _finalize(db, ctx, key, done, geo_seen, px_seen, stats, video, others=done_batch)
    return frame_num, cancelled
