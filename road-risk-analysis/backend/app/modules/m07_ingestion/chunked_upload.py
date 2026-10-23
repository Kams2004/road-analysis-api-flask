"""
Chunked (resumable) uploads of large videos — POST /jobs/uploads … (routes/jobs.py).

A single multipart request is cut by the web console's server after 5 minutes (Node
`requestTimeout`) and by any network hiccup: on a slow link a 150 MB video never arrives.
The browser therefore sends the file in CHUNK_SIZE pieces, each a short request that is
retried on its own, then asks for the job:

    POST /jobs/uploads/                        {filename, size}   → upload_id, chunk_size, total_chunks
    POST /jobs/uploads/{id}/chunks/{index}/    raw bytes (application/octet-stream)
    GET  /jobs/uploads/{id}/                   chunks already received (resume)
    POST /jobs/uploads/{id}/complete/          → the job (same checks as POST /jobs/)
    POST /jobs/uploads/{id}/abort/
(trailing slashes, as every route the web console's proxy calls: a redirect would have to
re-send the chunk body)

Storage: <TEMP_DIR>/chunked/<id>/ — `data` (pre-sized, each chunk written at its offset, so
a repeated chunk simply overwrites itself), `meta.json` (filename, size, owner) and one
`<index>.ok` marker per received chunk (no shared counter: parallel / retried chunks never
race). TEMP_DIR is shared by the API processes and the worker. Uploads left unfinished are
removed after STALE_AFTER_S.
"""
from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

CHUNK_SIZE = 8 * 1024 * 1024                 # 8 MiB: a few seconds even on a slow uplink
MAX_FILE_SIZE = 20 * 1024 * 1024 * 1024      # 20 GiB
STALE_AFTER_S = 24 * 3600
_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class UploadError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass
class Upload:
    id: str
    dir: Path
    filename: str
    size: int
    owner: str
    chunk_size: int

    @property
    def total_chunks(self) -> int:
        return max(1, -(-self.size // self.chunk_size))

    @property
    def data(self) -> Path:
        return self.dir / "data"

    def chunk_len(self, index: int) -> int:
        return min(self.chunk_size, self.size - index * self.chunk_size)

    def received(self) -> list[int]:
        out = []
        for p in self.dir.glob("*.ok"):
            try:
                out.append(int(p.stem))
            except ValueError:
                continue
        return sorted(i for i in out if 0 <= i < self.total_chunks)

    def missing(self) -> list[int]:
        got = set(self.received())
        return [i for i in range(self.total_chunks) if i not in got]


def root(temp_dir: Path) -> Path:
    r = Path(temp_dir) / "chunked"
    r.mkdir(parents=True, exist_ok=True)
    return r


def sweep(temp_dir: Path, now: Optional[float] = None) -> int:
    """Remove uploads untouched for STALE_AFTER_S; returns how many."""
    now = time.time() if now is None else now
    n = 0
    for d in root(temp_dir).iterdir():
        try:
            if d.is_dir() and now - d.stat().st_mtime > STALE_AFTER_S:
                shutil.rmtree(d, ignore_errors=True)
                n += 1
        except OSError:
            continue
    return n


def create(temp_dir: Path, upload_id: str, filename: str, size: int, owner: str) -> Upload:
    if size <= 0:
        raise UploadError(422, "invalid_size", "The file is empty.")
    if size > MAX_FILE_SIZE:
        raise UploadError(413, "file_too_large", f"Files are limited to {MAX_FILE_SIZE // 1024 ** 3} GB.")
    d = root(temp_dir) / upload_id
    d.mkdir(parents=True, exist_ok=False)
    with open(d / "data", "wb") as fh:
        fh.truncate(size)
    (d / "meta.json").write_text(json.dumps({"filename": filename, "size": size, "owner": owner,
                                             "chunk_size": CHUNK_SIZE, "created": time.time()}))
    return Upload(upload_id, d, filename, size, owner, CHUNK_SIZE)


def load(temp_dir: Path, upload_id: str, owner: str) -> Upload:
    if not _ID_RE.match(upload_id or ""):
        raise UploadError(404, "upload_not_found", "Unknown or expired upload.")
    d = root(temp_dir) / upload_id
    try:
        meta = json.loads((d / "meta.json").read_text())
    except (OSError, ValueError):
        raise UploadError(404, "upload_not_found", "Unknown or expired upload.")
    if meta.get("owner") != owner:
        raise UploadError(404, "upload_not_found", "Unknown or expired upload.")
    return Upload(upload_id, d, meta["filename"], int(meta["size"]), meta["owner"], int(meta["chunk_size"]))


def write_chunk(up: Upload, index: int, body: bytes) -> None:
    if not 0 <= index < up.total_chunks:
        raise UploadError(422, "invalid_chunk", f"Chunk index must be 0–{up.total_chunks - 1}.")
    if len(body) != up.chunk_len(index):
        raise UploadError(422, "invalid_chunk",
                          f"Chunk {index} must be {up.chunk_len(index)} bytes (got {len(body)}).")
    with open(up.data, "r+b") as fh:
        fh.seek(index * up.chunk_size)
        fh.write(body)
    (up.dir / f"{index}.ok").touch()
    up.dir.touch()                              # keeps the upload fresh for sweep()


def take_file(up: Upload, dest: Path) -> Path:
    """Move the assembled file to `dest` (all chunks received) and drop the upload."""
    missing = up.missing()
    if missing:
        raise UploadError(409, "upload_incomplete",
                          f"{len(missing)} chunk(s) missing, first: {missing[0]}.")
    shutil.move(str(up.data), dest)
    shutil.rmtree(up.dir, ignore_errors=True)
    return dest


def remove(up: Upload) -> None:
    shutil.rmtree(up.dir, ignore_errors=True)
