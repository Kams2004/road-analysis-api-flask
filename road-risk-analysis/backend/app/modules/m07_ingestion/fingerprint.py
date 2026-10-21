"""
Content fingerprint of a job's media — the same video processed twice is
detected whatever its file name or source (upload, cloud drive, SFTP).

    fingerprint = "sha256:" + SHA-256 of the whole file content

Uploads hash while the file is written (Hasher), cloud / SFTP imports hash
the downloaded file (file_fingerprint, in a thread). A job whose media has
the same fingerprint as a done (or still queued / running) job is a
duplicate: the upload API answers 409 `duplicate_video` unless `force=true`,
imports mark the item `skipped_duplicate` unless the batch re-imports.
Even when forced, cluster-first attachment (docs/CLUSTERS.md) prevents
duplicate hazards.
"""
import hashlib
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

PREFIX = "sha256:"
CHUNK = 1 << 20


class Hasher:
    def __init__(self) -> None:
        self._h = hashlib.sha256()

    def update(self, chunk: bytes) -> None:
        self._h.update(chunk)

    def fingerprint(self) -> str:
        return PREFIX + self._h.hexdigest()


def file_fingerprint(path: str) -> str:
    h = Hasher()
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.fingerprint()


async def find_duplicate(db: AsyncSession, fingerprint: Optional[str], exclude_job_id: Optional[str] = None):
    """The most recent job (done, pending or processing) with this fingerprint, or None."""
    from app.modules.m07_ingestion.models.job import Job, JobStatus
    if not fingerprint:
        return None
    q = select(Job).where(Job.content_fingerprint == fingerprint,
                          Job.status.in_([JobStatus.done, JobStatus.pending, JobStatus.processing]))
    if exclude_job_id:
        q = q.where(Job.id != exclude_job_id)
    return (await db.execute(q.order_by(Job.created_at.desc()).limit(1))).scalars().first()


def duplicate_details(job) -> dict:
    """Extra fields of the 409 duplicate_video error."""
    status = job.status.value if hasattr(job.status, "value") else job.status
    when = job.finished_at or job.created_at
    return {"job_id": job.id, "job_status": status, "filename": job.source_name or job.filename,
            "processed_at": when.isoformat() if when else None}
