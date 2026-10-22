"""
Box review in the server's Label Studio, driven from the web console's
"Training data" page — docs/TRAINING-DATA.md ("Box review in Label Studio").

    send(db, type)    one Label Studio project per detection type ("VIASafe – <type>"),
                      one task per training frame still `needs_box_review`; the frame's
                      current boxes are the task's prediction (pre-annotation). A frame
                      whose review was reset by a later decision gets a fresh task.
    fetch(db, type)   every annotation submitted since the last fetch replaces the boxes
                      of its frame (`box_reviewed`, [] = reviewed background); skipped
                      tasks are ignored. Same write-back as PUT /datasets/frames/…/labels.
    status(db)        connection + per-type project counters for the page.

Configuration (infra/secrets/labelstudio.env, mounted read-only on /app/secrets — the
same file is the labelstudio container's env_file; never logged, never returned):

    LABEL_STUDIO_USER_TOKEN   API token of the Label Studio admin (legacy token)
    VIASAFE_LS_URL            where the API reaches Label Studio   (http://labelstudio:8080)
    VIASAFE_LS_PUBLIC_URL     what the browser opens               (http://<host>:18085)
    VIASAFE_PUBLIC_API_URL    public API address the task images are loaded from

Images: Label Studio (the annotator's browser) loads each frame from
GET /labelstudio/image/<type>/<frame_key>.jpg?s=<HMAC> — public, but only for a
signature made with SECRET_KEY, so nothing else in the `datasets` bucket is exposed.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.modules.m06_hazards.models.labelstudio_task import LabelStudioTask
from app.modules.m06_hazards.models.training_sample import TrainingFrame, TrainingSample
from app.modules.m06_hazards.training_data import card_info
from app.modules.m06_hazards.training_data import export as td_export
from app.modules.m06_hazards.training_data import sync as training_sync

logger = logging.getLogger(__name__)

SECRETS_FILE = "labelstudio.env"
MODEL_VERSION = "viasafe-platform"
IMPORT_CHUNK = 500
COLORS = ("#e53935", "#1e88e5", "#fdd835", "#43a047", "#8e24aa", "#fb8c00", "#00acc1", "#6d4c41")
TYPE_TITLES = {"pothole": "Potholes", "traffic_sign": "Traffic signs", "speed_bump": "Speed bumps",
               "parked_vehicle": "Parked vehicles"}


class LabelStudioError(Exception):
    """Label Studio not configured / unreachable / refused a call (message is safe to show)."""


# ── configuration ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Config:
    url: str
    public_url: str
    api_url: str
    token: str


def config() -> Optional[Config]:
    """From the environment, else <secrets dir>/labelstudio.env; None when no token."""
    from app.modules.m07_ingestion.sftp_core import parse_env, secrets_dir

    values: dict[str, str] = {}
    try:
        path = secrets_dir() / SECRETS_FILE
        if path.is_file():
            values = parse_env(path.read_text(encoding="utf-8"))
    except OSError as exc:
        logger.warning(f"label studio: {SECRETS_FILE} unreadable ({exc.__class__.__name__})")

    def get(name: str, default: str = "") -> str:
        return (os.getenv(name) or values.get(name) or default).strip()

    token = get("LABEL_STUDIO_USER_TOKEN")
    if not token:
        return None
    url = get("VIASAFE_LS_URL", "http://labelstudio:8080").rstrip("/")
    return Config(url=url, public_url=get("VIASAFE_LS_PUBLIC_URL", url).rstrip("/"),
                  api_url=get("VIASAFE_PUBLIC_API_URL").rstrip("/"), token=token)


def _require() -> Config:
    cfg = config()
    if cfg is None:
        raise LabelStudioError("Label Studio is not configured on this server "
                               "(infra/secrets/labelstudio.env — see docs/TRAINING-DATA.md).")
    if not cfg.api_url:
        raise LabelStudioError("VIASAFE_PUBLIC_API_URL is missing in infra/secrets/labelstudio.env "
                               "(Label Studio loads the images from it).")
    return cfg


# ── pure helpers (tests/test_labelstudio_review.py) ──────────────────────────

def image_signature(det_type: str, frame_key: str) -> str:
    msg = f"ls-image:{det_type}/{frame_key}".encode()
    return hmac.new(settings.SECRET_KEY.encode(), msg, hashlib.sha256).hexdigest()[:32]


def check_signature(det_type: str, frame_key: str, sig: str) -> bool:
    return hmac.compare_digest(image_signature(det_type, frame_key), sig or "")


def image_url(api_url: str, det_type: str, frame_key: str) -> str:
    return f"{api_url}/labelstudio/image/{det_type}/{frame_key}.jpg?s={image_signature(det_type, frame_key)}"


def project_title(det_type: str) -> str:
    return f"VIASafe – {TYPE_TITLES.get(det_type, det_type)}"


def label_config(classes: list[str]) -> str:
    labels = "\n".join(f'    <Label value="{c}" background="{COLORS[i % len(COLORS)]}"/>'
                       for i, c in enumerate(classes))
    return ("<View>\n  <Header value=\"$info\" size=\"6\"/>\n"
            "  <Image name=\"image\" value=\"$image\" zoom=\"true\" zoomControl=\"true\" rotateControl=\"false\" "
            "brightnessControl=\"true\" contrastControl=\"true\"/>\n"
            "  <RectangleLabels name=\"label\" toName=\"image\" strokeWidth=\"2\" opacity=\"0.25\">\n"
            f"{labels}\n  </RectangleLabels>\n</View>\n")


def yolo_to_ls(b: dict) -> dict:
    """Normalised YOLO box (cx, cy, w, h) → Label Studio value (percent, top-left)."""
    return {"x": round((b["cx"] - b["w"] / 2) * 100, 4), "y": round((b["cy"] - b["h"] / 2) * 100, 4),
            "width": round(b["w"] * 100, 4), "height": round(b["h"] * 100, 4), "rotation": 0}


def ls_result_boxes(result: list[dict]) -> list[dict]:
    """Rectangles of a Label Studio annotation → [{class_name, cx, cy, w, h}] (normalised)."""
    out = []
    for r in result or []:
        if r.get("type") != "rectanglelabels":
            continue
        v = r.get("value") or {}
        names = v.get("rectanglelabels") or []
        if not names:
            continue
        x, y, w, h = (float(v.get(k, 0)) / 100 for k in ("x", "y", "width", "height"))
        out.append({"class_name": names[0], "cx": x + w / 2, "cy": y + h / 2, "w": w, "h": h})
    return out


def latest_annotation(task: dict) -> Optional[dict]:
    """The newest submitted (not skipped) annotation of an exported task."""
    anns = [a for a in task.get("annotations") or [] if not a.get("was_cancelled")]
    return max(anns, key=lambda a: (a.get("updated_at") or "", a.get("id") or 0)) if anns else None


def annotation_key(ann: dict) -> str:
    return f"{ann.get('id')}:{ann.get('updated_at')}"


def task_info(fr: TrainingFrame, samples: list[TrainingSample]) -> str:
    decisions = ", ".join(
        f"{s.decision}{' (' + s.reason_code + ')' if s.reason_code else ''}{' by system' if s.decided_by == 'system' else ''}"
        for s in samples)
    return (f"{fr.frame_key}  ·  t={float(fr.video_time_s or 0):.1f}s  ·  current boxes: {fr.n_boxes or 0}"
            f"  ·  decisions: {decisions or '—'}")


def task_for(cfg: Config, fr: TrainingFrame, classes: list[str], samples: list[TrainingSample]) -> dict:
    W, H = int(fr.image_width or 0), int(fr.image_height or 0)
    res = []
    for i, b in enumerate(fr.labels or []):
        idx = int(b.get("class_index", 0))
        name = b.get("class_name") or (classes[idx] if idx < len(classes) else classes[0])
        res.append({"id": f"p{i}", "type": "rectanglelabels", "from_name": "label", "to_name": "image",
                    "original_width": W, "original_height": H, "image_rotation": 0,
                    "value": {**yolo_to_ls(b), "rectanglelabels": [name]}})
    return {"data": {"image": image_url(cfg.api_url, fr.detection_type, fr.frame_key), "frame_key": fr.frame_key,
                     "detection_type": fr.detection_type, "info": task_info(fr, samples)},
            "predictions": [{"model_version": MODEL_VERSION, "score": 1.0, "result": res}]}


# ── Label Studio REST client (blocking; called through asyncio.to_thread) ────

class Client:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def call(self, method: str, path: str, body: Any = None, timeout: int = 120) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.cfg.url + path, data=data, method=method, headers={
            "Authorization": f"Token {self.cfg.token}", "Accept": "application/json",
            "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - internal Label Studio
                txt = r.read()
        except urllib.error.HTTPError as e:
            detail = e.read()[:200].decode(errors="replace")
            if e.code in (401, 403):
                raise LabelStudioError("Label Studio refused the API token (LABEL_STUDIO_USER_TOKEN).") from e
            raise LabelStudioError(f"Label Studio {method} {path.split('?')[0]}: HTTP {e.code} {detail}") from e
        except (urllib.error.URLError, OSError) as e:
            raise LabelStudioError(f"Label Studio is not reachable at {self.cfg.url} "
                                   f"({getattr(e, 'reason', e)}).") from e
        return json.loads(txt) if txt else None

    def projects(self) -> list[dict]:
        r = self.call("GET", "/api/projects?page_size=1000")
        return r.get("results", []) if isinstance(r, dict) else (r or [])

    def find_project(self, title: str) -> Optional[dict]:
        return next((p for p in self.projects() if p.get("title") == title), None)

    def ensure_project(self, det_type: str, classes: list[str]) -> dict:
        title = project_title(det_type)
        proj = self.find_project(title)
        if proj is None:
            proj = self.call("POST", "/api/projects", {
                "title": title, "label_config": label_config(classes),
                "description": f"VIASafe {det_type} frames: correct the boxes (pre-annotations = the platform's "
                               "current label), Submit (empty = no object), Skip when you cannot judge.",
                "show_collab_predictions": True, "model_version": MODEL_VERSION})
        return proj

    def task_ids_by_frame(self, project_id: int) -> dict[str, int]:
        out, page = {}, 1
        while True:
            r = self.call("GET", f"/api/tasks?project={project_id}&page={page}&page_size=500")
            tasks = r.get("tasks", []) if isinstance(r, dict) else (r or [])
            for t in tasks:
                key = (t.get("data") or {}).get("frame_key")
                if key:
                    out[key] = t["id"]
            if len(tasks) < 500:
                return out
            page += 1

    def import_tasks(self, project_id: int, tasks: list[dict]) -> int:
        n = 0
        for i in range(0, len(tasks), IMPORT_CHUNK):
            r = self.call("POST", f"/api/projects/{project_id}/import", tasks[i:i + IMPORT_CHUNK], timeout=600)
            n += int((r or {}).get("task_count", 0))
        return n

    def delete_task(self, task_id: int) -> None:
        try:
            self.call("DELETE", f"/api/tasks/{task_id}")
        except LabelStudioError as exc:
            if "HTTP 404" not in str(exc):
                raise

    def annotated_tasks(self, project_id: int) -> list[dict]:
        return self.call("GET", f"/api/projects/{project_id}/export?exportType=JSON", timeout=600) or []


def _client() -> Client:
    return Client(_require())


# ── operations ───────────────────────────────────────────────────────────────

async def _samples_by_frame(db: AsyncSession, det_type: str, keys: list[str]) -> dict[str, list[TrainingSample]]:
    out: dict[str, list[TrainingSample]] = {}
    for i in range(0, len(keys), 1000):
        rows = (await db.execute(select(TrainingSample).where(
            TrainingSample.detection_type == det_type, TrainingSample.state == "active",
            TrainingSample.frame_key.in_(keys[i:i + 1000])))).scalars().all()
        for s in rows:
            out.setdefault(s.frame_key, []).append(s)
    return out


async def send(db: AsyncSession, det_type: str) -> dict:
    """Create / complete the type's project with every frame still to review."""
    client = _client()
    classes = card_info(det_type)[0]
    proj = await asyncio.to_thread(client.ensure_project, det_type, classes)
    pid = int(proj["id"])
    existing = await asyncio.to_thread(client.task_ids_by_frame, pid)

    frames = (await db.execute(select(TrainingFrame).where(
        TrainingFrame.detection_type == det_type, TrainingFrame.status == "active",
        TrainingFrame.review_status == "needs_box_review", TrainingFrame.image_key.is_not(None),
        TrainingFrame.dirty.is_(False)).order_by(TrainingFrame.id))).scalars().all()
    rows = {r.frame_key: r for r in (await db.execute(select(LabelStudioTask).where(
        LabelStudioTask.detection_type == det_type))).scalars().all()}

    to_send, renewed, waiting = [], 0, 0
    for fr in frames:
        row = rows.get(fr.frame_key)
        task_id = existing.get(fr.frame_key)
        if task_id is not None and row is not None and row.project_id == pid and row.applied_key:
            # reviewed once, then a decision reset the frame: a fresh task to review again
            await asyncio.to_thread(client.delete_task, task_id)
            renewed += 1
        elif task_id is not None:
            waiting += 1                                 # already in Label Studio, not reviewed yet
            continue
        to_send.append(fr)

    samples = await _samples_by_frame(db, det_type, [f.frame_key for f in to_send])
    tasks = [task_for(client.cfg, fr, classes, samples.get(fr.frame_key, [])) for fr in to_send]
    imported = await asyncio.to_thread(client.import_tasks, pid, tasks) if tasks else 0
    ids = await asyncio.to_thread(client.task_ids_by_frame, pid) if tasks else existing
    now = datetime.utcnow()
    for fr in to_send:
        row = rows.get(fr.frame_key)
        if row is None:
            row = LabelStudioTask(detection_type=det_type, frame_key=fr.frame_key, project_id=pid)
            db.add(row)
        row.project_id, row.task_id, row.sent_at = pid, ids.get(fr.frame_key), now
        row.applied_key, row.applied_at = None, None
    await db.commit()
    pending_sync = (await db.execute(select(TrainingFrame.id).where(
        TrainingFrame.detection_type == det_type, TrainingFrame.status == "active",
        TrainingFrame.review_status == "needs_box_review", TrainingFrame.dirty.is_(True)))).scalars().all()
    return {"detection_type": det_type, "project_id": pid, "project_url": f"{client.cfg.public_url}/projects/{pid}",
            "sent": imported, "renewed": renewed, "already_there": waiting, "not_ready": len(pending_sync)}


async def fetch(db: AsyncSession, det_type: str, reviewer: str) -> dict:
    """Write back every annotation submitted since the last fetch."""
    client = _client()
    proj = await asyncio.to_thread(client.find_project, project_title(det_type))
    if proj is None:
        raise LabelStudioError(f"No Label Studio project for {det_type} yet — send the frames first.")
    pid = int(proj["id"])
    classes = card_info(det_type)[0]
    exported = await asyncio.to_thread(client.annotated_tasks, pid)

    rows = {r.frame_key: r for r in (await db.execute(select(LabelStudioTask).where(
        LabelStudioTask.detection_type == det_type))).scalars().all()}
    applied, unchanged, skipped, missing, errors = 0, 0, 0, 0, []
    frame_ids: list[int] = []
    now = datetime.utcnow()
    for task in exported:
        key = (task.get("data") or {}).get("frame_key")
        ann = latest_annotation(task)
        if not key:
            continue
        if ann is None:
            skipped += 1
            continue
        akey = annotation_key(ann)
        row = rows.get(key)
        if row is not None and row.applied_key == akey:
            unchanged += 1
            continue
        fr = (await db.execute(select(TrainingFrame).where(
            TrainingFrame.detection_type == det_type, TrainingFrame.frame_key == key))).scalar_one_or_none()
        if fr is None or fr.status != "active":
            missing += 1
            continue
        try:
            boxes = td_export.clean_review_boxes(ls_result_boxes(ann.get("result") or []), classes)
        except (ValueError, KeyError) as exc:
            errors.append(f"{key}: {exc}")
            continue
        td_export.apply_review(fr, boxes, f"{reviewer} (Label Studio)")
        if row is None:
            row = LabelStudioTask(detection_type=det_type, frame_key=key, project_id=pid, sent_at=now)
            db.add(row)
        row.project_id, row.task_id = pid, task.get("id")
        row.applied_key, row.applied_at = akey, now
        frame_ids.append(fr.id)
        applied += 1
    await db.commit()
    if frame_ids:
        await training_sync.sync_dirty(db, frame_ids=frame_ids, limit=len(frame_ids) + 10)
    return {"detection_type": det_type, "project_id": pid, "applied": applied, "unchanged": unchanged,
            "skipped": skipped, "missing": missing, "errors": errors[:20]}


async def status(db: AsyncSession) -> dict:
    """Connection state and, per type, the project's counters (no secret)."""
    cfg = config()
    if cfg is None:
        return {"configured": False, "reachable": False, "public_url": None, "error": None, "projects": []}
    client = Client(cfg)
    try:
        projects = await asyncio.to_thread(client.projects)
    except LabelStudioError as exc:
        return {"configured": True, "reachable": False, "public_url": cfg.public_url, "error": str(exc),
                "projects": []}
    by_title = {p.get("title"): p for p in projects}
    rows = (await db.execute(select(LabelStudioTask.detection_type, LabelStudioTask.applied_key))).all()
    out = []
    for t in TYPE_TITLES:
        p = by_title.get(project_title(t))
        if p is None:
            continue
        applied = sum(1 for dt, k in rows if dt == t and k)
        out.append({"detection_type": t, "project_id": p["id"], "url": f"{cfg.public_url}/projects/{p['id']}",
                    "tasks": p.get("task_number") or 0, "annotated": p.get("num_tasks_with_annotations") or 0,
                    "skipped": p.get("skipped_annotations_number") or 0, "applied": applied})
    return {"configured": True, "reachable": True, "public_url": cfg.public_url, "error": None, "projects": out}
