"""
Cloud imports (RRAP-07-002/004) — pulling files picked in a connected drive
(or found in a watched folder) into the processing pipeline.

    POST /cloud-connections/{id}/imports ─▶ batch + one item per file (queued)
        └─▶ Celery `cloud_import_item` per item (at most
            processing.cloud_import_concurrency at once, own Redis slot pool)
              downloading ─▶ uploading_to_storage ─▶ queued_for_processing
                 (chunked,         (verify size / md5,       (job created and
                  resumable)        move into TEMP_DIR)       enqueued)
        └─▶ then the item follows its job: processing ─▶ done | failed | cancelled

Progress: the worker keeps live counters (bytes, speed, ETA) in a Redis hash
per item, refreshed every ~0.5 s, and writes the row at phase changes and
every 10 % / 15 s. Readers merge both (`live_view`). Processing progress is
the job's own (frames processed / total).

Pure rules (progress maths, effective status, batch roll-up, dedupe) are
plain functions, unit-tested without a database or a provider.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from app.core.config import settings
from app.models.cloud_import import CloudImportBatch, CloudImportItem, ImportItemStatus as S

logger = logging.getLogger(__name__)

TEMP_DIR = Path(settings.TEMP_DIR)
SLOTS_KEY = "cloud:import:slots"
LIVE_TTL_S = 24 * 3600
LIVE_EVERY_S = 0.5              # Redis refresh period while downloading
DB_EVERY_S = 15.0               # row refresh period while downloading …
DB_EVERY_FRACTION = 0.10        # … or every 10 % of the file
MAX_TRANSIENT_RETRIES = 4       # per attempt: network errors / 429 / 5xx / expired token
ACCESS_TOKEN_TTL_S = 45 * 60

# Item states owned by the import itself; the last three are read from the job.
ACTIVE = (S.queued.value, S.downloading.value, S.uploading_to_storage.value,
          S.queued_for_processing.value, S.processing.value)
TRANSFER = (S.queued.value, S.downloading.value, S.uploading_to_storage.value)
FINAL = (S.done.value, S.failed.value, S.skipped_duplicate.value, S.cancelled.value)
COUNTS_AS_IMPORTED = (S.queued.value, S.downloading.value, S.uploading_to_storage.value,
                      S.queued_for_processing.value, S.processing.value, S.done.value)


class ImportCancelled(Exception):
    """Raised from the progress callback when a cancel was requested."""


# ── keys ─────────────────────────────────────────────────────────────────────

def live_key(item_id: str) -> str:
    return f"cloud:import:live:{item_id}"


def cancel_key(item_id: str) -> str:
    return f"cloud:import:cancel:{item_id}"


def token_key(connection_id: str) -> str:
    return f"cloud:at:{connection_id}"


# ── progress maths ───────────────────────────────────────────────────────────

def percent(done: Optional[int], total: Optional[int]) -> Optional[float]:
    """0..100 (one decimal); None when the size is unknown (indeterminate bar)."""
    if not total or total <= 0 or done is None:
        return None
    return round(min(100.0, max(0.0, done * 100.0 / total)), 1)


def eta_seconds(done: Optional[int], total: Optional[int], speed_bps: Optional[float]) -> Optional[float]:
    if not total or done is None or not speed_bps or speed_bps <= 0:
        return None
    remaining = max(0, total - done)
    return round(remaining / speed_bps, 1)


class ProgressTracker:
    """Transfer speed as an exponential moving average of the instantaneous
    rate, sampled at most every `min_interval` seconds (bursty chunks would
    otherwise make it jump). `clock` is injectable for tests."""

    def __init__(self, total: Optional[int], start_bytes: int = 0, *, alpha: float = 0.3,
                 min_interval: float = 0.5, clock: Callable[[], float] = time.monotonic):
        self.total = total
        self.alpha = alpha
        self.min_interval = min_interval
        self.clock = clock
        self.done = start_bytes
        self.speed: Optional[float] = None
        self._t = clock()
        self._b = start_bytes

    def update(self, done: int) -> bool:
        """Record `done` bytes; True when a new speed sample was taken."""
        self.done = done
        now = self.clock()
        dt = now - self._t
        if dt < self.min_interval:
            return False
        rate = max(0.0, (done - self._b) / dt)
        self.speed = rate if self.speed is None else self.alpha * rate + (1 - self.alpha) * self.speed
        self._t, self._b = now, done
        return True

    @property
    def eta(self) -> Optional[float]:
        return eta_seconds(self.done, self.total, self.speed)

    @property
    def percent(self) -> Optional[float]:
        return percent(self.done, self.total)


def backoff_s(retry: int) -> float:
    """1, 2, 4, 8 … s (capped at 30 s) between transient-failure retries."""
    return float(min(30, 2 ** retry))


def resume_retry_count(retries: int, offset_now: int, offset_at_last_failure: Optional[int]) -> int:
    """Failures in a row: back to 0 when the last attempt moved the partial
    file forward (a server that drops every few minutes still completes a
    long download, each retry resuming where it stopped)."""
    if offset_at_last_failure is not None and offset_now > offset_at_last_failure:
        return 0
    return retries


# ── status rules ─────────────────────────────────────────────────────────────

def effective_status(item_status: str, job_status: Optional[str], has_job: bool = True) -> str:
    """Item status as shown: after hand-over, the job decides."""
    if item_status not in (S.queued_for_processing.value, S.processing.value, S.done.value):
        return item_status
    if not has_job:
        # Job deleted after hand-over: the import itself did complete.
        return S.done.value
    if job_status is None:
        return S.queued_for_processing.value
    return {
        "pending": S.queued_for_processing.value,
        "processing": S.processing.value,
        "done": S.done.value,
        "failed": S.failed.value,
        "cancelled": S.cancelled.value,
    }.get(job_status, item_status)


def item_fraction(status: str, download_pct: Optional[float], job_progress: Optional[float]) -> float:
    """Share (0..1) of an item's whole journey: download = first half,
    processing = second half; a finished item counts as complete."""
    if status in FINAL:
        return 1.0
    if status == S.queued.value:
        return 0.0
    if status == S.downloading.value:
        return 0.5 * ((download_pct or 0.0) / 100.0)
    if status in (S.uploading_to_storage.value, S.queued_for_processing.value):
        return 0.5
    if status == S.processing.value:
        return 0.5 + 0.5 * max(0.0, min(1.0, job_progress or 0.0))
    return 0.0


def batch_status(statuses: Iterable[str], cancel_requested: bool = False) -> str:
    """queued | running | done | completed_with_errors | failed | cancelled."""
    st = list(statuses)
    if not st:
        return "done"
    if any(s in ACTIVE for s in st):
        started = any(s in FINAL and s != S.skipped_duplicate.value for s in st)
        if all(s == S.queued.value for s in st if s in ACTIVE) and not started:
            return "queued"
        return "running"
    ok = sum(1 for s in st if s in (S.done.value, S.skipped_duplicate.value))
    failed = sum(1 for s in st if s == S.failed.value)
    cancelled = sum(1 for s in st if s == S.cancelled.value)
    if ok == len(st):
        return "done"
    if cancelled and not failed and not ok:
        return "cancelled"
    if failed and not ok:
        return "failed" if not cancelled or not cancel_requested else "cancelled"
    return "completed_with_errors"


def summarize(items: list[dict], cancel_requested: bool = False) -> dict:
    """Roll-up of item views (see item_view) for a batch."""
    counts: dict[str, int] = {}
    for it in items:
        counts[it["status"]] = counts.get(it["status"], 0) + 1
    considered = [it for it in items if it["status"] != S.skipped_duplicate.value]
    bytes_total = sum(it.get("bytes_total") or 0 for it in considered)
    bytes_done = sum(min(it.get("bytes_done") or 0, it.get("bytes_total") or it.get("bytes_done") or 0)
                     for it in considered)
    speed = sum(it.get("speed_bps") or 0 for it in considered if it["status"] == S.downloading.value)
    downloading_left = sum(max(0, (it.get("bytes_total") or 0) - (it.get("bytes_done") or 0))
                           for it in considered if it["status"] in (S.queued.value, S.downloading.value))
    progress = (sum(item_fraction(it["status"], it.get("percent"), it.get("job_progress")) for it in considered)
                / len(considered)) if considered else 1.0
    return {
        "status": batch_status([it["status"] for it in items], cancel_requested),
        "counts": counts,
        "bytes_total": bytes_total,
        "bytes_done": bytes_done,
        "speed_bps": round(speed, 1) if speed else None,
        "download_eta_s": round(downloading_left / speed, 1) if speed and downloading_left else None,
        "progress": round(progress * 100, 1),
        "active": any(it["status"] in ACTIVE for it in items),
    }


# ── dedupe ───────────────────────────────────────────────────────────────────

@dataclass
class Prior:
    """An earlier import of a file: an import item, or a job from the legacy
    folder sync (no version known)."""
    version: Optional[str]
    status: str
    job_id: Optional[str] = None
    item_id: Optional[str] = None
    batch_id: Optional[str] = None
    at: Optional[datetime] = None
    legacy: bool = False


@dataclass
class ImportState:
    state: str                         # none | importing | imported | changed
    job_id: Optional[str] = None
    item_id: Optional[str] = None
    batch_id: Optional[str] = None


def import_state(version: Optional[str], priors: list[Prior]) -> ImportState:
    """Has this version of the file been imported (or is it being imported)?"""
    live = [p for p in priors if p.status in COUNTS_AS_IMPORTED]
    live.sort(key=lambda p: p.at or datetime.min, reverse=True)
    same = [p for p in live if p.legacy or version is None or p.version is None or p.version == version]
    if same:
        p = same[0]
        importing = p.status in TRANSFER or p.status == S.queued_for_processing.value \
            or p.status == S.processing.value
        return ImportState("importing" if importing else "imported", p.job_id, p.item_id, p.batch_id)
    if live:
        p = live[0]
        return ImportState("changed", p.job_id, p.item_id, p.batch_id)
    return ImportState("none")


@dataclass
class PlannedItem:
    entry: Any                          # cloud_providers.DriveEntry
    status: str
    duplicate_of_job_id: Optional[str] = None


def plan_items(entries: Iterable[Any], priors: dict[str, list[Prior]], reimport: bool = False) -> list[PlannedItem]:
    """One item per distinct file: `skipped_duplicate` when this version was
    already imported (unless `reimport`), else `queued`."""
    out: list[PlannedItem] = []
    seen: set[str] = set()
    for e in entries:
        if e.id in seen:
            continue
        seen.add(e.id)
        st = import_state(e.version, priors.get(e.id, []))
        if st.state in ("imported", "importing") and not reimport:
            out.append(PlannedItem(e, S.skipped_duplicate.value, st.job_id))
        else:
            out.append(PlannedItem(e, S.queued.value))
    return out


# ── item transitions (pure) ──────────────────────────────────────────────────

def cancel_action(effective: str) -> Optional[str]:
    """What cancelling an item means in its current state:
    "now" (not started), "signal" (worker stops at the next chunk),
    "job" (cancel the processing job), None (already final)."""
    if effective == S.queued.value:
        return "now"
    if effective in (S.downloading.value, S.uploading_to_storage.value):
        return "signal"
    if effective in (S.queued_for_processing.value, S.processing.value):
        return "job"
    return None


def retry_action(effective: str, has_job: bool) -> Optional[str]:
    """"download" (pull again, resuming the partial file), "job" (re-run the
    processing job), or None (not retryable)."""
    if effective not in (S.failed.value, S.cancelled.value):
        return None
    return "job" if has_job else "download"


def reset_for_retry(item: CloudImportItem) -> None:
    item.status = S.queued.value
    item.error = None
    item.finished_at = None
    item.speed_bps = None
    item.updated_at = datetime.utcnow()


def mark_item(item: CloudImportItem, status: str, error: Optional[str] = None) -> None:
    now = datetime.utcnow()
    item.status = status
    item.updated_at = now
    if error is not None:
        item.error = error[:500]
    if status in FINAL:
        item.finished_at = now
        item.speed_bps = None


# ── views ────────────────────────────────────────────────────────────────────

def _f(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def item_view(item: CloudImportItem, job=None, live: Optional[dict] = None) -> dict:
    """API view of an item: DB row + live Redis counters + its job."""
    live = live or {}
    job_status = getattr(getattr(job, "status", None), "value", getattr(job, "status", None))
    status = effective_status(item.status, job_status, has_job=job is not None or item.job_id is not None)
    bytes_total = item.bytes_total
    bytes_done = item.bytes_done or 0
    speed = item.speed_bps
    if item.status == S.downloading.value and live:
        bytes_done = int(_f(live.get("bytes_done")) or bytes_done)
        bytes_total = int(_f(live.get("bytes_total"))) if _f(live.get("bytes_total")) else bytes_total
        speed = _f(live.get("speed_bps"))
    job_progress = None
    if job is not None:
        from app.services import job_service
        job_progress = job_service.progress(job)
    error = item.error
    if status == S.failed.value and job is not None and getattr(job, "error", None):
        error = job.error
    return {
        "id": item.id, "batch_id": item.batch_id, "file_id": item.file_id, "name": item.name, "path": item.path,
        "mime_type": item.mime_type, "status": status, "phase_status": item.status,
        "bytes_total": bytes_total, "bytes_done": bytes_done if status != S.done.value else (bytes_total or bytes_done),
        "percent": percent(bytes_done, bytes_total) if item.status != S.queued.value else (0.0 if bytes_total else None),
        "speed_bps": round(speed, 1) if speed and item.status == S.downloading.value else None,
        "eta_s": eta_seconds(bytes_done, bytes_total, speed) if item.status == S.downloading.value else None,
        "attempts": item.attempts or 0, "error": error, "job_id": item.job_id,
        "job_status": job_status, "job_progress": job_progress,
        "job_detections": getattr(job, "detections", None) if job is not None else None,
        "duplicate_of_job_id": item.duplicate_of_job_id,
        "created_at": item.created_at, "started_at": item.started_at, "finished_at": item.finished_at,
        "updated_at": item.updated_at,
    }


# ── database helpers ─────────────────────────────────────────────────────────

async def prior_imports(db, connection_id: str, file_ids: list[str]) -> dict[str, list[Prior]]:
    """Earlier imports of these files through this connection."""
    from sqlalchemy import select
    from app.models.job import Job
    out: dict[str, list[Prior]] = {}
    if not file_ids:
        return out
    ids = list(set(file_ids))
    rows = (await db.execute(
        select(CloudImportItem, Job.status).outerjoin(Job, Job.id == CloudImportItem.job_id)
        .where(CloudImportItem.connection_id == connection_id, CloudImportItem.file_id.in_(ids))
    )).all()
    with_item: set[str] = set()
    for it, job_status in rows:
        jst = getattr(job_status, "value", job_status)
        st = effective_status(it.status, jst, has_job=it.job_id is not None)
        out.setdefault(it.file_id, []).append(Prior(it.version, st, it.job_id, it.id, it.batch_id, it.created_at))
        if it.job_id:
            with_item.add(it.job_id)
    jobs = (await db.execute(
        select(Job.id, Job.source_ref, Job.status, Job.created_at)
        .where(Job.connection_id == connection_id, Job.source_ref.in_(ids))
    )).all()
    for job_id, ref, st, created in jobs:
        if job_id in with_item:
            continue
        jst = getattr(st, "value", st)
        status = {"pending": S.queued_for_processing.value, "processing": S.processing.value}.get(jst, jst)
        out.setdefault(ref, []).append(Prior(None, status, job_id, at=created, legacy=True))
    return out


async def live_counters(item_ids: list[str]) -> dict[str, dict]:
    """Redis hashes of the items being downloaded ({} when Redis is down)."""
    if not item_ids:
        return {}
    try:
        from app.services.redis_client import get_redis
        r = get_redis()
        pipe = r.pipeline()
        for i in item_ids:
            pipe.hgetall(live_key(i))
        res = await pipe.execute()
        return {i: v for i, v in zip(item_ids, res) if v}
    except Exception as exc:
        logger.debug(f"live import counters unavailable: {exc}")
        return {}


async def batch_views(db, batches: list[CloudImportBatch], with_items: bool = True) -> list[dict]:
    """Batches with their items (effective status, live progress) and roll-up.
    After hand-over an item row stays `queued_for_processing`: its shown
    status is always derived from its job (so a job retried from the Jobs
    page shows up here too). The batch end time is written back."""
    from sqlalchemy import select
    from app.models.job import Job
    if not batches:
        return []
    ids = [b.id for b in batches]
    rows = (await db.execute(
        select(CloudImportItem, Job).outerjoin(Job, Job.id == CloudImportItem.job_id)
        .where(CloudImportItem.batch_id.in_(ids)).order_by(CloudImportItem.created_at, CloudImportItem.name)
    )).all()
    live = await live_counters([it.id for it, _ in rows if it.status == S.downloading.value])
    by_batch: dict[str, list[dict]] = {b: [] for b in ids}
    dirty = False
    for it, job in rows:
        by_batch[it.batch_id].append(item_view(it, job, live.get(it.id)))
    out = []
    for b in batches:
        items = by_batch[b.id]
        summary = summarize(items, b.cancel_requested)
        if not summary["active"] and b.finished_at is None and items:
            b.finished_at = datetime.utcnow()
            dirty = True
        elif summary["active"] and b.finished_at is not None:
            b.finished_at = None                   # an item or its job was retried
            dirty = True
        view = {
            "id": b.id, "connection_id": b.connection_id, "provider": b.provider,
            "connection_name": b.connection_name, "origin": b.origin,
            "models": [m for m in (b.models or "").split(",") if m], "frame_skip": b.frame_skip,
            "options": b.options or {}, "item_count": len(items) or b.item_count,
            "cancel_requested": bool(b.cancel_requested), "created_by": b.created_by,
            "created_at": b.created_at, "finished_at": b.finished_at, **summary,
        }
        if with_items:
            view["items"] = items
        out.append(view)
    if dirty:
        await db.commit()
    return out


async def access_token(db, conn, client=None) -> str:
    """Access token of a connection, cached ~45 min in Redis (encrypted) so
    browsing and thumbnails don't refresh the OAuth token on every call.
    SFTP servers: their profile (no secret, nothing cached)."""
    from app.core.security import decrypt_secret, encrypt_secret
    from app.services import cloud_storage
    if conn.provider == "sftp":
        return await cloud_storage.connection_token(db, conn, client=client)
    r = None
    try:
        from app.services.redis_client import get_redis
        r = get_redis()
        cached = await r.get(token_key(conn.id))
        if cached:
            tok = decrypt_secret(cached)
            if tok:
                return tok
    except Exception:
        r = None
    tok = await cloud_storage.connection_token(db, conn, client=client)
    if r is not None:
        try:
            await r.set(token_key(conn.id), encrypt_secret(tok), ex=ACCESS_TOKEN_TTL_S)
        except Exception:
            pass
    return tok


async def forget_access_token(connection_id: str) -> None:
    try:
        from app.services.redis_client import get_redis
        await get_redis().delete(token_key(connection_id))
    except Exception:
        pass


def new_batch(conn, *, origin: str, models: list[str], frame_skip: Optional[int], options: dict,
              created_by: Optional[str]) -> CloudImportBatch:
    import uuid
    return CloudImportBatch(
        id=str(uuid.uuid4()), connection_id=conn.id, provider=conn.provider, connection_name=conn.name,
        origin=origin, models=",".join(models), frame_skip=frame_skip, options=options, item_count=0,
        cancel_requested=False, created_by=created_by, created_at=datetime.utcnow(),
    )


def new_item(batch: CloudImportBatch, conn, planned: PlannedItem) -> CloudImportItem:
    import uuid
    e = planned.entry
    item = CloudImportItem(
        id=str(uuid.uuid4()), batch_id=batch.id, connection_id=conn.id, file_id=e.id, version=e.version,
        name=e.name, path=getattr(e, "path", None), mime_type=e.mime_type, checksum=e.checksum,
        modified_at=e.modified, status=planned.status, bytes_total=e.size, bytes_done=0, attempts=0,
        duplicate_of_job_id=planned.duplicate_of_job_id, created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
    )
    if planned.status in FINAL:
        item.finished_at = item.created_at
    return item


def dispatch(item: CloudImportItem) -> bool:
    """Queue the worker task of a queued item (caller commits). False when
    the broker is unreachable — the item is then marked failed."""
    import uuid
    try:
        from app.workers.import_tasks import cloud_import_item
        task_id = str(uuid.uuid4())
        cloud_import_item.apply_async(args=[item.id], queue="processing", task_id=task_id)
        item.celery_task_id = task_id
        return True
    except Exception as exc:
        mark_item(item, S.failed.value, f"Could not queue the import: {exc}")
        return False


async def import_entries(db, conn, entries: list, *, origin: str, models: list[str], frame_skip: Optional[int],
                         options: dict, actor: Optional[str], reimport: bool = False,
                         dispatch_fn: Optional[Callable[[CloudImportItem], bool]] = None
                         ) -> tuple[CloudImportBatch, list[CloudImportItem]]:
    """Create a batch for `entries` (DriveEntry), skip already-imported
    versions, commit, then queue one worker task per queued item."""
    priors = await prior_imports(db, conn.id, [e.id for e in entries])
    planned = plan_items(entries, priors, reimport)
    batch = new_batch(conn, origin=origin, models=models, frame_skip=frame_skip, options=options, created_by=actor)
    batch.item_count = len(planned)
    db.add(batch)
    items = [new_item(batch, conn, p) for p in planned]
    for it in items:
        db.add(it)
    if not any(it.status == S.queued.value for it in items):
        batch.finished_at = datetime.utcnow()
    await db.commit()
    send = dispatch_fn or dispatch
    failed_dispatch = False
    for it in items:
        if it.status == S.queued.value and not send(it):
            failed_dispatch = True
    await db.commit()
    if failed_dispatch:
        logger.error(f"[Import batch {batch.id}] some items could not be queued (broker unreachable)")
    return batch, items


# ── worker side ──────────────────────────────────────────────────────────────

def _md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _readable(exc: BaseException) -> str:
    from app.services import cloud_storage
    if isinstance(exc, cloud_storage.CloudError):
        return str(exc)[:500]
    return f"{type(exc).__name__}: {exc}"[:500]


@dataclass
class WorkerDeps:
    """Injection points of run_item (tests replace them)."""
    redis: Any = None                                    # sync redis client (live counters, cancel flags)
    client: Any = None                                   # httpx.AsyncClient
    token: Optional[Callable[..., Any]] = None           # async (db, conn, client) -> access token
    sleep: Callable[[float], Any] = asyncio.sleep
    clock: Callable[[], float] = time.monotonic
    enqueue: Optional[Callable[..., Any]] = None         # async (db, job, actor) — job_service.enqueue_and_commit
    heartbeat: Optional[Callable[[], None]] = None
    temp_dir: Path = field(default_factory=lambda: TEMP_DIR)


def _rset(r, item_id: str, mapping: dict) -> None:
    if r is None:
        return
    try:
        key = live_key(item_id)
        r.hset(key, mapping={k: ("" if v is None else v) for k, v in mapping.items()})
        r.expire(key, LIVE_TTL_S)
    except Exception:
        pass


def _cancel_flag(r, item_id: str) -> bool:
    if r is None:
        return False
    try:
        return bool(r.get(cancel_key(item_id)))
    except Exception:
        return False


async def run_item(db, item_id: str, deps: Optional[WorkerDeps] = None) -> str:
    """Download one item, verify it, hand it to the job pipeline. Returns the
    item status reached. Never raises for provider / network failures: they
    end as `failed` with a readable error (retry resumes the partial file)."""
    from app.models.cloud_connection import CloudConnection
    from app.services import cloud_storage, job_service
    from app.services.cloud_providers import AuthExpiredError, TransientCloudError, get_provider, local_ext

    deps = deps or WorkerDeps()
    r = deps.redis
    item = await db.get(CloudImportItem, item_id)
    if item is None:
        return "missing"
    if item.status not in TRANSFER:
        return item.status                       # cancelled / already handed over / final
    batch = await db.get(CloudImportBatch, item.batch_id)
    if (batch is not None and batch.cancel_requested) or _cancel_flag(r, item.id):
        mark_item(item, S.cancelled.value)
        await db.commit()
        return item.status

    conn = await db.get(CloudConnection, item.connection_id) if item.connection_id else None
    if not job_service.connection_usable(conn):
        mark_item(item, S.failed.value, "The cloud connection was removed, disabled or needs to be reconnected.")
        await db.commit()
        return item.status

    provider = get_provider(conn.provider)
    get_token = deps.token or (lambda d, c, cl: cloud_storage.connection_token(d, c, client=cl))
    deps.temp_dir.mkdir(parents=True, exist_ok=True)
    dest = item.dest_path or str(deps.temp_dir / f"import_{item.id}{local_ext(item.name)}")
    part = f"{dest}.part"
    item.dest_path = dest
    item.status = S.downloading.value
    item.attempts = (item.attempts or 0) + 1
    item.started_at = item.started_at or datetime.utcnow()
    item.error = None
    item.updated_at = datetime.utcnow()
    await db.commit()

    start = os.path.getsize(part) if os.path.exists(part) else 0
    tracker = ProgressTracker(item.bytes_total, start, clock=deps.clock)
    state = {"live_at": -1e9, "db_at": deps.clock(), "db_bytes": start}
    _rset(r, item.id, {"phase": S.downloading.value, "bytes_done": start, "bytes_total": item.bytes_total,
                       "speed_bps": None, "at": time.time()})

    async def on_progress(done: int) -> None:
        tracker.update(done)
        now = deps.clock()
        if now - state["live_at"] >= LIVE_EVERY_S:
            state["live_at"] = now
            _rset(r, item.id, {"phase": S.downloading.value, "bytes_done": done, "bytes_total": item.bytes_total,
                               "speed_bps": round(tracker.speed or 0, 1), "eta_s": tracker.eta, "at": time.time()})
            if deps.heartbeat:
                deps.heartbeat()
            if _cancel_flag(r, item.id):
                raise ImportCancelled()
        step = (item.bytes_total or 0) * DB_EVERY_FRACTION
        if now - state["db_at"] >= DB_EVERY_S or (step and done - state["db_bytes"] >= step):
            state["db_at"], state["db_bytes"] = now, done
            item.bytes_done = done
            item.speed_bps = tracker.speed
            item.updated_at = datetime.utcnow()
            await db.commit()

    try:
        token = await get_token(db, conn, deps.client)
        await db.commit()                         # a rotated refresh token
        retries = 0
        max_retries = getattr(provider, "max_transient_retries", MAX_TRANSIENT_RETRIES)
        failed_at: Optional[int] = None           # bytes on disk at the previous transient failure
        while True:
            offset = os.path.getsize(part) if os.path.exists(part) else 0
            try:
                size = await provider.download(token, item.file_id, part, offset=offset,
                                               on_progress=on_progress, client=deps.client)
                break
            except AuthExpiredError:
                if retries >= MAX_TRANSIENT_RETRIES:
                    raise
                retries += 1
                token = await get_token(db, conn, deps.client)
                await db.commit()
            except TransientCloudError as exc:
                now_at = os.path.getsize(part) if os.path.exists(part) else 0
                retries = resume_retry_count(retries, now_at, failed_at)
                failed_at = now_at
                if retries >= max_retries:
                    raise
                delay = provider.retry_delay(retries) if hasattr(provider, "retry_delay") else backoff_s(retries)
                logger.info(f"[Import {item.id}] transient error ({exc}); retry {retries + 1} "
                            f"in {delay}s, resuming at {now_at} B")
                if hasattr(provider, "note_retry"):
                    provider.note_retry(token, retries, delay, now_at)
                await deps.sleep(delay)
                retries += 1
        if provider.persist_credentials(conn, token):   # SFTP host key trusted on first use
            await db.commit()

        # ── verify and move into the shared media directory ──────────────────
        item.status = S.uploading_to_storage.value
        item.bytes_done = size
        item.bytes_total = item.bytes_total or size
        item.speed_bps = None
        item.updated_at = datetime.utcnow()
        _rset(r, item.id, {"phase": item.status, "bytes_done": size, "bytes_total": item.bytes_total,
                           "speed_bps": None, "eta_s": None, "at": time.time()})
        await db.commit()
        if item.bytes_total and size != item.bytes_total:
            Path(part).unlink(missing_ok=True)
            raise cloud_storage.CloudError(
                f"Downloaded {size} bytes but the provider announced {item.bytes_total} — the file may have "
                f"changed during the transfer; retry the import.")
        if item.checksum:
            digest = await asyncio.to_thread(_md5, part)
            if digest.lower() != item.checksum.lower():
                Path(part).unlink(missing_ok=True)
                raise cloud_storage.CloudError("Checksum mismatch after download — retry the import.")
        if _cancel_flag(r, item.id):
            raise ImportCancelled()
        os.replace(part, dest)

        # ── hand over to the job pipeline ────────────────────────────────────
        models = [m for m in ((batch.models if batch else None) or "").split(",") if m] \
            or list(job_service.DEFAULT_MODELS)
        actor = (batch.created_by if batch else None) or "system"
        job = await job_service.new_job(
            db, filename=item.name, source_type=conn.provider, enabled=models, source_path=dest,
            submitted_by=actor, frame_skip_override=batch.frame_skip if batch else None,
            source_ref=item.file_id, source_name=item.name, connection_id=conn.id,
        )
        # Insert the job first: cloud_import_items.job_id references it and the
        # two mappers have no relationship() telling the flush the order.
        await db.flush()
        item.job_id = job.id
        item.status = S.queued_for_processing.value
        item.updated_at = datetime.utcnow()
        conn.files_imported_total = (conn.files_imported_total or 0) + 1
        enqueue = deps.enqueue or job_service.enqueue_and_commit
        await enqueue(db, job, actor)
        _rset(r, item.id, {"phase": item.status, "at": time.time()})
        return item.status
    except ImportCancelled:
        await db.rollback()
        item = await db.get(CloudImportItem, item_id)
        Path(part).unlink(missing_ok=True)
        mark_item(item, S.cancelled.value)
        item.bytes_done = 0
        await db.commit()
        _clear(r, item_id)
        return item.status
    except Exception as exc:
        logger.warning(f"[Import {item_id}] failed: {exc}",
                       exc_info=not isinstance(exc, cloud_storage.CloudError))
        error = _readable(exc)
        await db.rollback()
        item = await db.get(CloudImportItem, item_id)
        if item is None:
            return "missing"
        mark_item(item, S.failed.value, error)
        item.bytes_done = os.path.getsize(part) if os.path.exists(part) else 0
        await db.commit()
        _clear(r, item_id)
        return item.status


def _clear(r, item_id: str) -> None:
    if r is None:
        return
    try:
        r.delete(live_key(item_id))
        r.delete(cancel_key(item_id))
    except Exception:
        pass


def discard_partial(item: CloudImportItem) -> None:
    """Remove the partial download of an item that will not be resumed."""
    if item.dest_path:
        Path(f"{item.dest_path}.part").unlink(missing_ok=True)
