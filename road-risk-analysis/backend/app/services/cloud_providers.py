"""
Drive providers behind cloud sources (RRAP-07-002/004) — one interface for
browsing and pulling files, so the API, the import worker and the
watched-folder sync never branch on the provider.

    provider = get_provider(conn.provider)
    page  = await provider.browse(token, folder_id, page_token=…, query=…)
    crumbs = await provider.path(token, folder_id)
    await provider.download(token, file_id, dest, offset=…, on_progress=…)

OAuth (consent URL, code exchange, refresh) stays in app.services.cloud_storage:
a provider here only needs an access token. Adding a provider (Dropbox,
S3 bucket …) = one DriveProvider subclass registered in PROVIDERS plus
its credential flow (SFTP: app.services.sftp_source, "token" = server profile); the UI lists only the providers returned by
`available_providers()`, never a provider without a working flow.

Every network method takes an optional httpx.AsyncClient so tests inject an
httpx.MockTransport.
"""
from __future__ import annotations

import logging
import os
import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath
from typing import Awaitable, Callable, Optional, Union
from urllib.parse import urlparse

import httpx

from app.services.cloud_storage import (
    GOOGLE_FILES_URL, GOOGLE_FOLDER_MIME, GRAPH_URL, CloudError, _auth, _client, _google_q_quote, _raise_for,
)

logger = logging.getLogger(__name__)

# What the processor can open (cv2.VideoCapture) — same list as POST /jobs/.
VIDEO_EXTS = (".mp4", ".avi", ".mov", ".mkv", ".m4v", ".webm", ".mpg", ".mpeg", ".ts", ".3gp")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp")
IMAGE_MIMES = ("image/jpeg", "image/png", "image/bmp", "image/x-ms-bmp")
PAGE_SIZE = 100
MAX_PATH_DEPTH = 20
MAX_WALK_FILES = 1000
THUMBNAIL_MAX_BYTES = 2 << 20

ProgressFn = Callable[[int], Union[None, Awaitable[None]]]


_ID_RE = re.compile(r"^[A-Za-z0-9_.!~-]{1,256}$")


def check_id(value: Optional[str]) -> Optional[str]:
    """Provider ids go into URL paths sent with our bearer token: refuse
    anything that could change the path (`/`, `..`, `?`, `#`, spaces …)."""
    if value is None or value == "root":
        return value
    if not _ID_RE.match(value) or value in (".", ".."):
        raise CloudError("Invalid file or folder id.")
    return value


class TransientCloudError(CloudError):
    """Network failure, rate limit (429) or provider-side 5xx: retry later."""


class AuthExpiredError(CloudError):
    """401 — the access token expired mid-way: refresh and retry."""


@dataclass
class DriveEntry:
    """A folder or file of a drive, provider-neutral."""
    id: str
    name: str
    kind: str                                  # folder | file
    mime_type: Optional[str] = None
    size: Optional[int] = None
    modified: Optional[str] = None
    version: Optional[str] = None              # changes when the content changes (dedupe key)
    checksum: Optional[str] = None             # md5 hex, verified after download
    thumbnail_url: Optional[str] = None        # provider URL — never sent to the browser
    duration_s: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    child_count: Optional[int] = None
    web_url: Optional[str] = None
    path: Optional[str] = None                 # folder path when found by a recursive walk
    media_hint: Optional[str] = None           # set by the provider: "video" / "image" / "" (not importable)

    @property
    def media(self) -> Optional[str]:
        return media_kind(self)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class BrowsePage:
    entries: list[DriveEntry] = field(default_factory=list)
    next_page_token: Optional[str] = None


@dataclass
class Crumb:
    id: str
    name: str


def media_kind(e: DriveEntry) -> Optional[str]:
    """"video" / "image" when the processor can take the file, else None."""
    if e.kind != "file":
        return None
    if e.media_hint is not None:               # the provider decided (SFTP: its own video list)
        return e.media_hint or None
    mime = (e.mime_type or "").lower()
    ext = PurePosixPath(e.name.lower()).suffix
    if mime.startswith("application/vnd.google-apps."):
        return None                            # Google Docs native formats: not downloadable media
    if mime.startswith("video/") or (not mime.startswith("image/") and ext in VIDEO_EXTS):
        return "video"
    if mime in IMAGE_MIMES or ext in IMAGE_EXTS:
        return "image"
    return None


def importable(e: DriveEntry) -> bool:
    return media_kind(e) is not None


def _status_error(resp: httpx.Response, what: str) -> None:
    """_raise_for, with the retryable / re-auth cases told apart."""
    if resp.status_code < 400:
        return
    try:
        _raise_for(resp, what)
    except CloudError as exc:
        if resp.status_code == 401:
            raise AuthExpiredError(str(exc)) from None
        if resp.status_code in (408, 429) or resp.status_code >= 500:
            raise TransientCloudError(str(exc)) from None
        raise


async def _call(coro_factory, what: str):
    try:
        return await coro_factory()
    except httpx.TransportError as exc:
        raise TransientCloudError(f"{what} failed: {type(exc).__name__}: {exc}") from None


class DriveProvider(ABC):
    """A storage provider the platform can import from."""
    id: str = ""
    label: str = ""
    auth: str = "oauth"                        # oauth (account consent) | server (host + secrets file)
    supports_search: bool = True
    supports_thumbnails: bool = True
    supports_resume: bool = True
    search_recursive: bool = False             # search covers sub-folders too

    @abstractmethod
    async def browse(self, token: str, folder_id: Optional[str], *, page_token: Optional[str] = None,
                     query: Optional[str] = None, client: Optional[httpx.AsyncClient] = None) -> BrowsePage: ...

    @abstractmethod
    async def path(self, token: str, folder_id: Optional[str],
                   client: Optional[httpx.AsyncClient] = None) -> list[Crumb]:
        """Ancestors of `folder_id` from the drive root (excluded) down to the folder itself."""

    @abstractmethod
    async def metadata(self, token: str, file_id: str, client: Optional[httpx.AsyncClient] = None) -> DriveEntry: ...

    @abstractmethod
    def _download_request(self, file_id: str) -> tuple[str, Optional[dict]]: ...

    def thumbnail_host_ok(self, url: str) -> tuple[bool, bool]:
        """(allowed, send_bearer) for a thumbnail URL the provider returned."""
        return False, False

    async def list_all(self, token: str, folder_id: Optional[str],
                       client: Optional[httpx.AsyncClient] = None) -> list[DriveEntry]:
        """Every entry of a folder (all pages)."""
        out: list[DriveEntry] = []
        page_token = None
        async with _client(client) as c:
            while True:
                page = await self.browse(token, folder_id, page_token=page_token, client=c)
                out.extend(page.entries)
                page_token = page.next_page_token
                if not page_token:
                    return out

    async def walk_media(self, token: str, folder_id: Optional[str], *, recursive: bool = True,
                         max_depth: int = 10, max_files: int = MAX_WALK_FILES, base_path: str = "",
                         client: Optional[httpx.AsyncClient] = None) -> list[DriveEntry]:
        """Importable files of a folder (and its sub-folders when `recursive`),
        each with `path` = its folder path relative to `folder_id`."""
        found: list[DriveEntry] = []
        seen_folders: set[str] = set()

        async def walk(fid: Optional[str], path: str, depth: int, c) -> None:
            if len(found) >= max_files or (fid or "root") in seen_folders:
                return
            seen_folders.add(fid or "root")
            entries = await self.list_all(token, fid, c)
            for e in entries:
                if e.kind == "file" and importable(e):
                    e.path = path or None
                    found.append(e)
                    if len(found) >= max_files:
                        return
            if recursive and depth < max_depth:
                for e in entries:
                    if e.kind == "folder":
                        await walk(e.id, f"{path}/{e.name}" if path else e.name, depth + 1, c)

        async with _client(client) as c:
            await walk(folder_id, base_path, 0, c)
        return found

    async def download(self, token: str, file_id: str, dest: str, *, offset: int = 0,
                       on_progress: Optional[ProgressFn] = None,
                       client: Optional[httpx.AsyncClient] = None) -> int:
        """Stream the file into `dest`, resuming at `offset` bytes when the
        provider honours a Range request (else restarting from 0). Calls
        `on_progress(bytes_on_disk)` after each chunk; it may raise to abort.
        Returns the bytes on disk. Raises TransientCloudError /
        AuthExpiredError for the retryable cases."""
        url, params = self._download_request(file_id)
        headers = _auth(token)
        if offset > 0:
            headers["Range"] = f"bytes={offset}-"

        async def run(c) -> int:
            async with c.stream("GET", url, params=params, headers=headers) as resp:
                if resp.status_code == 416 and offset > 0:      # already complete
                    return offset
                if resp.status_code >= 400:
                    await resp.aread()
                    _status_error(resp, "Download")
                resumed = offset > 0 and resp.status_code == 206
                done = offset if resumed else 0
                with open(dest, "ab" if resumed else "wb") as fh:
                    async for chunk in resp.aiter_bytes():   # as received: a failure loses nothing written
                        fh.write(chunk)
                        done += len(chunk)
                        if on_progress is not None:
                            r = on_progress(done)
                            if hasattr(r, "__await__"):
                                await r
                return done

        async with _client(client) as c:
            return await _call(lambda: run(c), "Download")

    async def resolve_selection(self, token, files: list[str], folder_ids: list[str], *, include_subfolders: bool,
                                max_files: int, client: Optional[httpx.AsyncClient] = None) -> list[DriveEntry]:
        """The entries of an import selection: each file of `files` plus the
        media files of each folder of `folder_ids` (sub-folders too with
        `include_subfolders`). Stops past `max_files`."""
        import asyncio
        sem = asyncio.Semaphore(8)
        async with _client(client) as c:
            async def meta(fid: str):
                async with sem:
                    return await self.metadata(token, fid, c)
            entries = list(await asyncio.gather(*(meta(f) for f in dict.fromkeys(files))))
            for fid in dict.fromkeys(folder_ids):
                folder = await self.metadata(token, fid, c)
                entries += await self.walk_media(token, fid, recursive=include_subfolders, base_path=folder.name,
                                                 max_files=max_files + 1, client=c)
                if len(entries) > max_files:
                    break
        return entries

    def persist_credentials(self, conn, token) -> bool:
        """Write back what an operation learned about the connection (SFTP:
        the host key trusted on first use). True when the row changed; the
        caller commits."""
        return False

    async def thumbnail(self, token: str, file_id: str,
                        client: Optional[httpx.AsyncClient] = None) -> Optional[tuple[bytes, str]]:
        """(image bytes, content type) of the file's thumbnail, None when it has none."""
        meta = await self.metadata(token, file_id, client)
        if not meta.thumbnail_url:
            return None
        allowed, bearer = self.thumbnail_host_ok(meta.thumbnail_url)
        if not allowed:
            return None
        async with _client(client) as c:
            resp = await _call(lambda: c.get(meta.thumbnail_url, headers=_auth(token) if bearer else None),
                               "Thumbnail")
        if resp.status_code >= 400 or len(resp.content) > THUMBNAIL_MAX_BYTES:
            return None
        ctype = resp.headers.get("content-type", "image/jpeg").split(";")[0]
        if not ctype.startswith("image/"):
            return None
        return resp.content, ctype

    def describe(self) -> dict:
        return {"id": self.id, "label": self.label, "auth": self.auth, "supports_search": self.supports_search,
                "search_recursive": self.search_recursive, "supports_thumbnails": self.supports_thumbnails,
                "supports_resume": self.supports_resume}


# ── Google Drive ─────────────────────────────────────────────────────────────

_GOOGLE_FIELDS = ("id,name,mimeType,size,modifiedTime,md5Checksum,version,thumbnailLink,webViewLink,parents,"
                  "videoMediaMetadata(durationMillis,width,height),imageMediaMetadata(width,height)")


def _google_entry(f: dict) -> DriveEntry:
    folder = f.get("mimeType") == GOOGLE_FOLDER_MIME
    video = f.get("videoMediaMetadata") or {}
    image = f.get("imageMediaMetadata") or {}
    dur = video.get("durationMillis")
    return DriveEntry(
        id=f["id"], name=f.get("name") or f["id"], kind="folder" if folder else "file",
        mime_type=f.get("mimeType"), size=int(f["size"]) if f.get("size") not in (None, "") else None,
        modified=f.get("modifiedTime"),
        version=None if folder else (f.get("md5Checksum") or f.get("version") or f.get("modifiedTime")),
        checksum=f.get("md5Checksum"), thumbnail_url=f.get("thumbnailLink"),
        duration_s=round(int(dur) / 1000, 1) if dur not in (None, "") else None,
        width=video.get("width") or image.get("width"), height=video.get("height") or image.get("height"),
        web_url=f.get("webViewLink"),
    )


class GoogleDriveProvider(DriveProvider):
    id = "google_drive"
    label = "Google Drive"

    async def browse(self, token, folder_id, *, page_token=None, query=None, client=None) -> BrowsePage:
        q = f"'{_google_q_quote(folder_id or 'root')}' in parents and trashed = false"
        if query and query.strip():
            q += f" and name contains '{_google_q_quote(query.strip())}'"
        params = {"q": q, "fields": f"nextPageToken,files({_GOOGLE_FIELDS})", "pageSize": PAGE_SIZE,
                  "orderBy": "folder,name_natural", "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
        if page_token:
            params["pageToken"] = page_token
        async with _client(client) as c:
            resp = await _call(lambda: c.get(GOOGLE_FILES_URL, params=params, headers=_auth(token)),
                               "Google Drive listing")
        _status_error(resp, "Google Drive listing")
        body = resp.json()
        return BrowsePage([_google_entry(f) for f in body.get("files", [])], body.get("nextPageToken"))

    async def _get(self, c, token: str, file_id: str, fields: str) -> dict:
        check_id(file_id)
        resp = await _call(lambda: c.get(f"{GOOGLE_FILES_URL}/{file_id}",
                                         params={"fields": fields, "supportsAllDrives": "true"},
                                         headers=_auth(token)), "Google Drive lookup")
        _status_error(resp, "Google Drive lookup")
        return resp.json()

    async def path(self, token, folder_id, client=None) -> list[Crumb]:
        if not folder_id or folder_id == "root":
            return []
        crumbs: list[Crumb] = []
        async with _client(client) as c:
            root_id = (await self._get(c, token, "root", "id")).get("id")
            fid: Optional[str] = folder_id
            for _ in range(MAX_PATH_DEPTH):
                if not fid or fid == root_id:
                    break
                f = await self._get(c, token, fid, "id,name,parents")
                parents = f.get("parents") or []
                crumbs.append(Crumb(f["id"], f.get("name") or f["id"]))
                fid = parents[0] if parents else None
        return list(reversed(crumbs))

    async def metadata(self, token, file_id, client=None) -> DriveEntry:
        async with _client(client) as c:
            return _google_entry(await self._get(c, token, file_id, _GOOGLE_FIELDS))

    def _download_request(self, file_id):
        return f"{GOOGLE_FILES_URL}/{check_id(file_id)}", {"alt": "media", "supportsAllDrives": "true"}

    def thumbnail_host_ok(self, url):
        host = (urlparse(url).hostname or "").lower()
        ok = urlparse(url).scheme == "https" and (host.endswith(".googleusercontent.com")
                                                   or host in ("drive.google.com", "www.googleapis.com"))
        return ok, ok


# ── Microsoft OneDrive ───────────────────────────────────────────────────────

def _graph_item_url(item_id: Optional[str]) -> str:
    check_id(item_id)
    return f"{GRAPH_URL}/me/drive/items/{item_id}" if item_id and item_id != "root" else f"{GRAPH_URL}/me/drive/root"


def _onedrive_entry(i: dict) -> DriveEntry:
    file = i.get("file") or {}
    video = i.get("video") or {}
    image = i.get("image") or i.get("photo") or {}
    thumbs = i.get("thumbnails") or []
    thumb = None
    if thumbs:
        t0 = thumbs[0] or {}
        thumb = ((t0.get("medium") or t0.get("small") or t0.get("large") or {}).get("url"))
    folder = "folder" in i
    hashes = file.get("hashes") or {}
    dur = video.get("duration")
    return DriveEntry(
        id=i["id"], name=i.get("name") or i["id"], kind="folder" if folder else "file",
        mime_type=file.get("mimeType"), size=i.get("size") if not folder else None,
        modified=i.get("lastModifiedDateTime"),
        version=None if folder else (i.get("cTag") or i.get("eTag") or i.get("lastModifiedDateTime")),
        checksum=None if "md5Hash" not in hashes else str(hashes["md5Hash"]).lower(),
        thumbnail_url=thumb, duration_s=round(int(dur) / 1000, 1) if dur not in (None, "") else None,
        width=video.get("width") or image.get("width"), height=video.get("height") or image.get("height"),
        child_count=(i.get("folder") or {}).get("childCount") if folder else None, web_url=i.get("webUrl"),
    )


class OneDriveProvider(DriveProvider):
    id = "onedrive"
    label = "Microsoft OneDrive"
    search_recursive = True                    # Graph search(q=…) covers the whole sub-tree

    async def browse(self, token, folder_id, *, page_token=None, query=None, client=None) -> BrowsePage:
        if page_token:
            # nextLink is an absolute Graph URL — never follow anything else with our token.
            if not page_token.startswith(GRAPH_URL + "/"):
                raise CloudError("Invalid page token.")
            url, params = page_token, None
        elif query and query.strip():
            safe = query.strip().replace("'", "''")
            url = f"{_graph_item_url(folder_id)}/search(q='{safe}')"
            params = {"$top": PAGE_SIZE}
        else:
            url = f"{_graph_item_url(folder_id)}/children"
            params = {"$top": PAGE_SIZE, "$expand": "thumbnails"}
        async with _client(client) as c:
            resp = await _call(lambda: c.get(url, params=params, headers=_auth(token)), "OneDrive listing")
        _status_error(resp, "OneDrive listing")
        body = resp.json()
        entries = [_onedrive_entry(i) for i in body.get("value", []) if "folder" in i or "file" in i]
        return BrowsePage(entries, body.get("@odata.nextLink"))

    async def _item(self, c, token: str, item_id: Optional[str]) -> dict:
        resp = await _call(lambda: c.get(_graph_item_url(item_id), headers=_auth(token)), "OneDrive lookup")
        _status_error(resp, "OneDrive lookup")
        return resp.json()

    async def path(self, token, folder_id, client=None) -> list[Crumb]:
        if not folder_id or folder_id == "root":
            return []
        crumbs: list[Crumb] = []
        async with _client(client) as c:
            iid: Optional[str] = folder_id
            for _ in range(MAX_PATH_DEPTH):
                if not iid:
                    break
                i = await self._item(c, token, iid)
                if "root" in i:
                    break
                crumbs.append(Crumb(i["id"], i.get("name") or i["id"]))
                iid = (i.get("parentReference") or {}).get("id")
        return list(reversed(crumbs))

    async def metadata(self, token, file_id, client=None) -> DriveEntry:
        async with _client(client) as c:
            resp = await _call(lambda: c.get(_graph_item_url(file_id), params={"$expand": "thumbnails"},
                                             headers=_auth(token)), "OneDrive lookup")
        _status_error(resp, "OneDrive lookup")
        return _onedrive_entry(resp.json())

    def _download_request(self, file_id):
        return f"{GRAPH_URL}/me/drive/items/{check_id(file_id)}/content", None

    def thumbnail_host_ok(self, url):
        # Graph thumbnail URLs are pre-authenticated: fetched without our token.
        return urlparse(url).scheme == "https", False


PROVIDERS: dict[str, DriveProvider] = {p.id: p for p in (GoogleDriveProvider(), OneDriveProvider())}


def _registry() -> dict[str, DriveProvider]:
    """PROVIDERS, with SFTP servers (host + secrets file, no OAuth —
    app.services.sftp_source) registered on first use (it imports this module)."""
    if "sftp" not in PROVIDERS:
        from app.services.sftp_source import PROVIDER as sftp
        PROVIDERS[sftp.id] = sftp
    return PROVIDERS


def get_provider(provider_id: str) -> DriveProvider:
    try:
        return _registry()[provider_id]
    except KeyError:
        raise CloudError(f"Unknown provider '{provider_id}'.") from None


def available_providers(cfg) -> list[dict]:
    """Providers with a working connection flow, and whether it is configured
    (OAuth client set; an SFTP server needs no platform setting)."""
    configured = {
        "google_drive": bool(cfg.google_client_id and cfg.google_client_secret),
        "onedrive": bool(cfg.microsoft_client_id and cfg.microsoft_client_secret),
        "sftp": True,
    }
    return [{**p.describe(), "configured": configured.get(p.id, False)} for p in _registry().values()]


def local_ext(name: str) -> str:
    """Extension used for the local copy (the processor sniffs by content)."""
    ext = os.path.splitext(name or "")[1].lower()
    return ext if ext in VIDEO_EXTS + IMAGE_EXTS + (".h264",) else ".mp4"
