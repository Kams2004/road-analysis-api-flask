"""
Daily event files of an "events" source (an SFTP server with purpose `events`) →
`historical_events`, the store read by Historical analysis (Module 8) and Driver
behaviour (Module 9).

The fleet platform drops one CSV per day around 03:00 for the previous day,
`YYYY-MM-DD.csv` (`YYYY_MM_DD.csv` accepted), and the files stay there. Every hour
(beat task `sync_event_feeds`, app/workers/event_feed_tasks.py) — or on demand —
`sync_connection`:

    1. lists the source folder (one SFTP listing: cheap) and compares with `event_files`:
       a file is fetched when it is new, when its size / modification time changed
       (regenerated) or when its last import failed (MAX_ATTEMPTS). At most
       MAX_FILES_PER_RUN per run, oldest day first — the first sync catches up on the
       history a few weeks at a time;
    2. archives each fetched file as received (gzip) in the private `events-raw` bucket:
       the SFTP server is not the only copy, everything can be re-imported from there;
    3. parses it (sources/ymane_csv.py), places the new events on the grid and, when
       they lie on a road OSRM knows, on that road (matcher.py);
    4. inserts or updates them in bulk (INSERT … ON CONFLICT on source + exceptionid): a
       corrected file updates its events, never duplicates them. An unchanged file
       (same sha256) is not imported twice.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import logging
import posixpath
import re
import stat as stat_mod
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable, Iterable, Optional

from sqlalchemy import literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.modules.m08_historical.grid import grid_cell_id
from app.modules.m08_historical.models.event_file import EventFile
from app.modules.m08_historical.models.historical_event import HistoricalEventRecord as E
from app.modules.m08_historical.sources import ymane_csv

logger = logging.getLogger(__name__)

PURPOSE = "events"
FILE_RE = re.compile(r"^(\d{4})[-_](\d{2})[-_](\d{2})\.csv$", re.I)
MAX_FILES_PER_RUN = 31
MAX_FILE_BYTES = 200 * 1024 * 1024
MAX_ATTEMPTS = 5
UPSERT_CHUNK = 500
MATCH_CONCURRENCY = 8

# Columns a re-imported (corrected) file may change; map matching of existing rows is kept.
UPDATABLE = ("event_type", "occurred_at", "ended_at", "latitude", "longitude", "vehicle_external_id",
             "driver_external_id", "speed_mps", "speed_limit_mps", "severity", "country", "transporter",
             "duration_s", "distance_km", "grid_cell_id", "raw", "event_file_id")


@dataclass
class RemoteFile:
    name: str
    path: str
    size: Optional[int]
    mtime: Optional[datetime]

    @property
    def day(self) -> Optional[date]:
        return day_of(self.name)


def day_of(name: str) -> Optional[date]:
    m = FILE_RE.match(name or "")
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _mtime(v) -> Optional[datetime]:
    return datetime.utcfromtimestamp(int(v)) if v else None


def plan(remote: Iterable[RemoteFile], known: dict[str, Any], force: Optional[set[str]] = None) -> list[RemoteFile]:
    """Files to fetch this run, oldest day first: new, changed (size / mtime), pending, failed
    with attempts left, or named in `force`."""
    out = []
    for f in sorted((f for f in remote if f.day), key=lambda f: (f.day, f.name)):
        k = known.get(f.name)
        if force and f.name in force:
            out.append(f)
        elif k is None or k.status == "pending":
            out.append(f)
        elif k.status == "failed":
            if (k.attempts or 0) < MAX_ATTEMPTS or (k.size, k.mtime) != (f.size, f.mtime):
                out.append(f)
        elif (k.size, k.mtime) != (f.size, f.mtime):
            out.append(f)
    return out[:MAX_FILES_PER_RUN]


# ── SFTP side (blocking, inside one session) ─────────────────────────────────

def list_files(session) -> list[RemoteFile]:
    folder = session.base
    out = []
    for a in session.listdir(folder):
        if not FILE_RE.match(a.filename or "") or stat_mod.S_ISDIR(a.st_mode or 0):
            continue
        out.append(RemoteFile(a.filename, posixpath.join(folder, a.filename), a.st_size, _mtime(a.st_mtime)))
    return out


def read_file(session, f: RemoteFile) -> bytes:
    if f.size is not None and f.size > MAX_FILE_BYTES:
        raise ValueError(f"{f.name} is {f.size // (1024 * 1024)} MB (limit {MAX_FILE_BYTES // (1024 * 1024)} MB).")
    session.log.status(f"Starting download of {f.path}")
    with session.sftp.open(f.path, "rb") as fh:
        fh.prefetch()
        data = fh.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"{f.name} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB.")
    session.log.status(f"File transfer successful, transferred {len(data)} bytes")
    return data


# ── archive ──────────────────────────────────────────────────────────────────

def archive_key(connection_id: str, day: date, name: str) -> str:
    return f"{connection_id}/{day:%Y}/{day:%m}/{name}.gz"


def _archive(key: str, data: bytes) -> None:
    from app.core.storage.minio_service import get_minio
    client = get_minio()
    if not client.bucket_exists(settings.MINIO_BUCKET_EVENTS):
        client.make_bucket(settings.MINIO_BUCKET_EVENTS)
    blob = gzip.compress(data, mtime=0)
    client.put_object(settings.MINIO_BUCKET_EVENTS, key, io.BytesIO(blob), length=len(blob),
                      content_type="application/gzip")


# ── storage ──────────────────────────────────────────────────────────────────

async def _existing_ids(db: AsyncSession, ids: list[str]) -> set[str]:
    found: set[str] = set()
    for i in range(0, len(ids), 1000):
        chunk = ids[i:i + 1000]
        found.update((await db.execute(select(E.source_event_id).where(
            E.source == ymane_csv.SOURCE, E.source_event_id.in_(chunk)))).scalars().all())
    return found


async def _match(events: list[dict], matcher: Optional[Callable] = None) -> None:
    """Road of the new events (OSRM /nearest), MATCH_CONCURRENCY at a time; never raises."""
    if not events:
        return
    from app.modules.m08_historical import matcher as m
    from app.modules.m08_historical.domain import HistoricalEvent, HistoricalEventType
    match = matcher or m.match_event
    sem = asyncio.Semaphore(MATCH_CONCURRENCY)

    async def one(e: dict) -> None:
        async with sem:
            try:
                ev = HistoricalEvent(source=e["source"], source_event_id=e["source_event_id"],
                                     event_type=HistoricalEventType(e["event_type"]), occurred_at=e["occurred_at"],
                                     latitude=e["latitude"], longitude=e["longitude"])
                r = await match(ev)
            except Exception as exc:                       # OSRM down, odd point …: no road, keep the cell
                logger.debug("event %s not matched: %s", e["source_event_id"], exc)
                return
            e.update(matched_latitude=r.matched_latitude, matched_longitude=r.matched_longitude,
                     road_name=r.road_name, road_segment_id=r.road_segment_id, match_confidence=r.match_confidence)

    await asyncio.gather(*(one(e) for e in events))


def upsert_statement(rows: list[dict]):
    """INSERT … ON CONFLICT (source, source_event_id) DO UPDATE the UPDATABLE columns, returning
    one flag per row: true = inserted (xmax = 0), false = updated."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    stmt = pg_insert(E).values(rows)
    stmt = stmt.on_conflict_do_update(constraint="uq_historical_event_source",
                                      set_={c: stmt.excluded[c] for c in UPDATABLE})
    return stmt.returning(literal_column("(xmax = 0)"))


async def upsert(db: AsyncSession, events: list[dict], file_id: str) -> tuple[int, int]:
    """Insert or update (source, source_event_id); returns (inserted, updated)."""
    import uuid
    inserted = updated = 0
    now = datetime.utcnow()
    for i in range(0, len(events), UPSERT_CHUNK):
        rows = [{**e, "id": str(uuid.uuid4()), "event_file_id": file_id, "imported_at": now}
                for e in events[i:i + UPSERT_CHUNK]]
        flags = (await db.execute(upsert_statement(rows))).scalars().all()
        n = sum(1 for f in flags if f)
        inserted += n
        updated += len(flags) - n
    return inserted, updated


async def ingest(db: AsyncSession, conn_id: str, ef: EventFile, data: bytes,
                 matcher: Optional[Callable] = None, touched: Optional[set] = None) -> str:
    """Import one fetched file into `ef` (caller commits). "imported" or "unchanged". The UTC
    days of its events are added to `touched` (their daily summaries need a rebuild)."""
    sha = hashlib.sha256(data).hexdigest()
    if ef.sha256 == sha and ef.status == "imported":
        ef.error = None
        return "unchanged"
    result = ymane_csv.parse(data)                         # CsvFormatError → the caller marks it failed
    key = archive_key(conn_id, ef.day, ef.name)
    await asyncio.to_thread(_archive, key, data)
    cell_m = settings.HISTORICAL_GRID_CELL_SIZE_M
    for e in result.events:
        e["grid_cell_id"] = grid_cell_id(e["latitude"], e["longitude"], cell_m)
    known = await _existing_ids(db, [e["source_event_id"] for e in result.events])
    await _match([e for e in result.events if e["source_event_id"] not in known], matcher)
    inserted, updated = await upsert(db, result.events, ef.id)
    if touched is not None:
        touched.update(e["occurred_at"].date() for e in result.events)
    ef.sha256, ef.status, ef.error, ef.archive_key = sha, "imported", None, key
    ef.rows, ef.inserted, ef.updated, ef.invalid = result.rows, inserted, updated, result.invalid
    ef.problems, ef.by_type = result.problems or None, result.by_type()
    ef.imported_at = datetime.utcnow()
    return "imported"


async def sync_connection(db: AsyncSession, connection_id: str, force: Optional[set[str]] = None,
                          matcher: Optional[Callable] = None) -> dict:
    """One check of an events source: list, fetch what is new / changed, import. Never raises
    for a source problem — it is recorded on the connection (last_sync_*) and in its log."""
    from app.modules.m07_ingestion import cloud_providers, sftp_source
    from app.modules.m07_ingestion.cloud_storage import CloudError
    from app.modules.m07_ingestion.models.cloud_connection import CloudConnection

    conn = await db.get(CloudConnection, connection_id)
    if conn is None or conn.purpose != PURPOSE or conn.provider != "sftp" or not conn.enabled:
        return {"skipped": True}
    known = {f.name: f for f in (await db.execute(
        select(EventFile).where(EventFile.connection_id == conn.id))).scalars().all()}
    log = sftp_source.make_log(conn.id, source="worker")
    summary: dict = {"listed": 0, "fetched": 0, "imported": 0, "unchanged": 0, "failed": 0, "events": 0}

    def op(session):
        remote = list_files(session)
        todo = plan(remote, known, force)
        got = []
        for f in todo:
            try:
                got.append((f, read_file(session, f)))
            except Exception as exc:                       # this file only; the others go on
                got.append((f, exc))
        return remote, got

    try:
        target = sftp_source.SftpTarget.from_connection(conn, source="worker")
        remote, got = await asyncio.to_thread(cloud_providers.get_provider("sftp").run_session, target, op)
        if sftp_source.remember_host_key(conn, target):
            await db.commit()
    except CloudError as exc:
        conn.last_sync_at, conn.last_sync_status, conn.last_sync_error = datetime.utcnow(), "error", str(exc)[:500]
        await db.commit()
        return {**summary, "error": str(exc)}

    summary["listed"], summary["fetched"] = len(remote), len(got)
    touched: set = set()
    # Files listed but left for a later check (catch-up, MAX_FILES_PER_RUN): known as pending at
    # once, so their days count as "received" and never raise a missing-file alert.
    fetching = {f.name for f, _ in got}
    for f in remote:
        if f.day and f.name not in known and f.name not in fetching:
            pending = EventFile(connection_id=conn.id, name=f.name, path=f.path, day=f.day, size=f.size,
                                mtime=f.mtime, status="pending", attempts=0, rows=0, inserted=0, updated=0,
                                invalid=0)
            db.add(pending)
            known[f.name] = pending
    await db.commit()
    for f, data in got:
        ef = known.get(f.name)
        if ef is None:
            ef = EventFile(connection_id=conn.id, name=f.name, path=f.path, day=f.day, status="pending",
                           attempts=0, rows=0, inserted=0, updated=0, invalid=0)
            db.add(ef)
        ef.path, ef.day, ef.size, ef.mtime = f.path, f.day, f.size, f.mtime
        ef.attempts = (ef.attempts or 0) + 1
        await db.commit()                                  # the file row survives a failed import
        file_id = ef.id
        try:
            if isinstance(data, Exception):
                raise data
            outcome = await ingest(db, conn.id, ef, data, matcher, touched)
            await db.commit()
            summary[outcome] += 1
            if outcome == "imported":
                summary["events"] += ef.inserted + ef.updated
                log.status(f"{f.name}: {ef.rows} line(s) — {ef.inserted} new, {ef.updated} updated, "
                           f"{ef.invalid} skipped")
        except Exception as exc:
            await db.rollback()
            ef = await db.get(EventFile, file_id)
            ef.status, ef.error = "failed", str(exc)[:500]
            await db.commit()
            summary["failed"] += 1
            log.error(f"Error: {f.name}: {str(exc)[:300]}")
            logger.warning("event file %s/%s failed: %s", conn.id, f.name, exc)

    if touched:
        from app.modules.m08_historical import rollups
        await rollups.rebuild_safely(db, touched)
    conn = await db.get(CloudConnection, connection_id)
    conn.last_sync_at = datetime.utcnow()
    conn.last_sync_status = "error" if summary["failed"] else "ok"
    conn.last_sync_error = (f"{summary['failed']} file(s) could not be imported." if summary["failed"] else None)
    conn.last_sync_files = summary["imported"]
    conn.files_imported_total = (conn.files_imported_total or 0) + summary["imported"]
    await db.commit()
    if not got:
        log.status(f"Event files: {len(remote)} on the server, nothing new")
    logger.info("[EVENT-FEED] %s %s", conn.id, summary)
    return summary


# ── completeness: which days arrived, which are missing ──────────────────────

DEFAULT_ALERT_HOUR = 10          # UTC: the day D file (made ~03:00 on D+1) is late after D+1 10:00
ALERT_WINDOW_DAYS = 60           # missing days looked for (alerts, cards); the calendar shows any month


@dataclass
class DayStatus:
    day: date
    status: str                  # imported | failed | pending | missing | waiting (not due yet) | none (before the feed)
    events: int = 0
    file: Optional[str] = None
    error: Optional[str] = None
    file_id: Optional[str] = None


def expected_until(now: datetime, alert_hour: Optional[int]) -> date:
    """Last day whose file is due by `now` (the day D file is due on D+1 at alert_hour UTC)."""
    hour = DEFAULT_ALERT_HOUR if alert_hour is None else alert_hour
    return (now - timedelta(days=1, hours=hour)).date()


def day_statuses(files: Iterable[Any], first: Optional[date], due: date, start: date, end: date) -> list[DayStatus]:
    """Status of every day of [start, end] from the source's file rows (day, status, inserted,
    updated, name, error). Days before `first` (the feed's first file) are `none`, days after
    `due` without a file `waiting`."""
    by_day: dict[date, Any] = {}
    for f in files:
        d = f.day if isinstance(f.day, date) else date.fromisoformat(str(f.day))
        prev = by_day.get(d)
        if prev is None or (prev.status != "imported" and f.status == "imported"):
            by_day[d] = f
    out, d = [], start
    while d <= end:
        f = by_day.get(d)
        if f is not None:
            out.append(DayStatus(d, f.status, (f.inserted or 0) + (f.updated or 0) if f.status == "imported" else 0,
                                 f.name, f.error if f.status == "failed" else None, getattr(f, "id", None)))
        elif first is None or d < first:
            out.append(DayStatus(d, "none"))
        else:
            out.append(DayStatus(d, "missing" if d <= due else "waiting"))
        d += timedelta(days=1)
    return out


def health(files: list, now: datetime, alert_hour: Optional[int]) -> dict:
    """Completeness of a source: first day, `complete_until` (every day from the first one up to
    it imported), missing / failed days due by now (last ALERT_WINDOW_DAYS days)."""
    due = expected_until(now, alert_hour)
    days = [f.day if isinstance(f.day, date) else date.fromisoformat(str(f.day)) for f in files]
    first = min(days) if days else None
    if first is None:
        return {"first_day": None, "due_until": due, "complete_until": None, "missing": [], "failed": []}
    start = max(first, due - timedelta(days=ALERT_WINDOW_DAYS - 1))
    st = day_statuses(files, first, due, first, due) if first <= due else []
    complete = None
    for s in st:
        if s.status != "imported":
            break
        complete = s.day
    recent = [s for s in st if s.day >= start]
    return {"first_day": first, "due_until": due, "complete_until": complete,
            "missing": [s.day for s in recent if s.status == "missing"],
            "failed": [s.day for s in recent if s.status == "failed"]}


async def file_rows(db: AsyncSession, connection_id: str, since: Optional[date] = None) -> list:
    q = select(EventFile.id, EventFile.day, EventFile.status, EventFile.inserted, EventFile.updated, EventFile.name,
               EventFile.error).where(EventFile.connection_id == connection_id)
    if since is not None:
        q = q.where(EventFile.day >= since)
    return (await db.execute(q)).all()


async def first_day(db: AsyncSession, connection_id: str) -> Optional[date]:
    from sqlalchemy import func
    return (await db.execute(select(func.min(EventFile.day)).where(EventFile.connection_id == connection_id))).scalar()


async def source_health(db: AsyncSession, now: Optional[datetime] = None) -> list[dict]:
    """Every enabled event source with its completeness (health()) — the bell, the analysis pages,
    the e-mail alerts."""
    from app.modules.m07_ingestion.models.cloud_connection import CloudConnection
    now = now or datetime.utcnow()
    conns = (await db.execute(select(CloudConnection).where(
        CloudConnection.purpose == PURPOSE, CloudConnection.enabled.is_(True)))).scalars().all()
    out = []
    for c in conns:
        h = health(await file_rows(db, c.id), now, c.event_alert_hour)
        out.append({"connection_id": c.id, "name": c.name, "emails": c.event_alert_emails or [], **h})
    return out
