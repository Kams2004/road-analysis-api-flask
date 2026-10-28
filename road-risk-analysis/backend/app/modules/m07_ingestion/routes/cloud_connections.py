"""
Cloud sources (RRAP-07-002/004) — Google Drive / OneDrive accounts as an
input source of the platform, next to manual upload and streams.

Connect: POST / → {connection, authorize_url} → the operator consents at the
provider → the web page /settings/cloud/callback (the redirect URI registered
at the provider) posts {code, state} to /oauth/complete → refresh token
stored encrypted, connection active.

Use: GET /{id}/browse (folders, files, already-imported flags) →
POST /{id}/imports (selected files / folders) → progress under /imports.
Optional watched folder: `auto_import` imports its new files every
cloud.sync_interval_minutes (or POST /{id}/sync).

SFTP servers are one more provider of the same pipeline: POST /sftp creates
the server profile (no OAuth; the password typed in the web console is stored
encrypted, decrypted only at connect time and never logged or returned — legacy
servers read theirs from infra/secrets/sftp.env), then the same browse /
imports endpoints apply; /{id}/sftp/* adds connect / disconnect / host key /
connection log (see the "SFTP servers" section below and docs/SFTP-SOURCE.md).
"""
import asyncio
import re
from datetime import date, datetime
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from pydantic import BaseModel, Field, SecretStr, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, actor_name, api_error, get_principal
from app.api.openapi import err, errors, legacy
from app.core.security import encrypt_secret
from app.core.db.session import get_db
from app.modules.m07_ingestion.models.cloud_connection import CloudConnection, ConnectionStatus
from app.modules.m08_historical import event_feed
from app.modules.m08_historical.models.event_file import EventFile
from app.core import audit
from app.modules.m07_ingestion import (
    cloud_import, cloud_providers, cloud_storage, job_service, sftp_core, sftp_source,
)
from app.core.storage.redis_client import get_redis
from app.core.settings_service import get_section
from app.modules.m07_ingestion.schemas.cloud_imports import (
    BrowseOut, CrumbOut, DriveEntryOut, ImportBatchOut, ImportIn, ImportInfoOut, ProviderOut,
)

router = APIRouter()


class ConnectionIn(BaseModel):
    """A new cloud storage connection (created `pending_auth`)."""
    provider:    Literal["google_drive", "onedrive"] = Field(description="Storage provider.", examples=["google_drive"])
    name:        str = Field(min_length=1, max_length=120, description="Display name.", examples=["Fleet dashcams"])
    folder_id:   Optional[str] = Field(None, description="Provider id of the watched folder (`null` = drive root; "
                                                         "can be chosen after authorisation with `/folders`).")
    folder_name: Optional[str] = Field(None, description="Display name of the watched folder.", examples=["Dashcam uploads"])


SAVED_FOLDERS_MAX = 50


class SavedCrumb(BaseModel):
    id:   str = Field(min_length=1, max_length=1024)
    name: str = Field(min_length=1, max_length=255)


class SavedFolder(BaseModel):
    """A folder bookmarked in the browser — reopened in one click (e.g. a OneDrive folder shared
    by someone else, first opened from its link)."""
    id:          str = Field(min_length=1, max_length=1024, description="Folder id to browse "
                                                                         "(`<driveId>~<itemId>` when shared).")
    name:        str = Field(min_length=1, max_length=120, description="Label shown in the list.",
                             examples=["Road Analysis IA – VIDEOS"])
    breadcrumbs: list[SavedCrumb] = Field(default_factory=list, max_length=30,
                                          description="Path shown when it is opened.")
    link:        Optional[str] = Field(None, max_length=2048, description="Address it was opened from, if any.")

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("The name is empty.")
        return v

    @field_validator("link")
    @classmethod
    def _link(cls, v):
        if v and not v.startswith("https://"):
            raise ValueError("The link must start with https://")
        return v or None


class ConnectionPatch(BaseModel):
    """Partial update: only the fields sent are changed."""
    name:        Optional[str] = Field(None, min_length=1, max_length=120, description="Display name.")
    folder_id:   Optional[str] = Field(None, description="Watched folder id; `null`/empty = drive root (also clears "
                                                         "`folder_name`).")
    folder_name: Optional[str] = Field(None, description="Display name of the watched folder.")
    enabled:     Optional[bool] = Field(None, description="`false` disables the connection — no browsing, import "
                                                          "or sync (status `disabled`); `true` restores `active` (or "
                                                          "`pending_auth` if never authorised).")
    auto_import: Optional[bool] = Field(None, description="Import the new files of the watched folder automatically "
                                                          "every `cloud.sync_interval_minutes`.")
    auto_import_models: Optional[list[str]] = Field(
        None, description="Models run on auto-imported files (`null` = pothole, signs, speedbump).",
        examples=[["pothole", "signs"]])
    auto_import_frame_skip: Optional[int] = Field(None, ge=1, le=60,
                                                  description="Frame sampling of auto-imported videos (`null` = "
                                                              "processing.frame_skip_by_source).")
    saved_folders: Optional[list[SavedFolder]] = Field(
        None, max_length=SAVED_FOLDERS_MAX, description="The whole list of saved folders (replaces it; `[]` "
                                                        "clears it). Same folder id twice is kept once.")
    event_alert_hour: Optional[int] = Field(None, ge=0, le=23, description="Event sources: hour (UTC) of the day "
                                                                            "after D from which a missing day D file "
                                                                            "raises an alert (default 10).")
    event_alert_emails: Optional[list[str]] = Field(None, max_length=10, description="Event sources: addresses also "
                                                                                     "warned by e-mail (`[]` = none).")

    @field_validator("event_alert_emails")
    @classmethod
    def _emails(cls, v):
        if v is None:
            return v
        out = []
        for e in v:
            e = e.strip().lower()
            if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", e) or len(e) > 254:
                raise ValueError(f"Invalid e-mail address: {e!r}")
            if e not in out:
                out.append(e)
        return out

    @field_validator("auto_import_models")
    @classmethod
    def _models(cls, v):
        if v is None:
            return v
        unknown = sorted(set(v) - set(job_service.VALID_MODELS))
        if unknown:
            raise ValueError(f"Unknown model(s): {unknown}. Valid options: {sorted(job_service.VALID_MODELS)}")
        if not v:
            raise ValueError("Choose at least one model.")
        return sorted(set(v))


class OAuthCompleteIn(BaseModel):
    """Parameters the provider appended to the OAuth callback URL."""
    code:  str = Field(description="Authorisation code returned by the provider.", examples=["4/0AbCdEf…"])
    state: str = Field(description="`state` returned by the provider (from the `authorize_url`).")


class SftpInfoOut(BaseModel):
    """SFTP server profile of a connection (provider `sftp`). The password is never returned."""
    host:                 str = Field(description="Server host name (DNS) or IP.", examples=["bi.camtrack.mg"])
    port:                 int = Field(description="SSH port.", examples=[22])
    username:             str = Field(description="SSH user.", examples=["roadusertoread"])
    base_path:            Optional[str] = Field(None, description="Folder browsing starts at and cannot leave "
                                                                  "(`null` = the user's home).",
                                            examples=["/home/Ymane-New/ft_violations"])
    password_saved:       bool = Field(False, description="A password entered in the web console is stored "
                                                          "(encrypted; never returned).")
    password_key:         Optional[str] = Field(None, description="Legacy: key of the password in the secrets file "
                                                                  "(used only when no password is saved).",
                                                examples=["CAMTRACK"])
    password_var:         Optional[str] = Field(None, description="Legacy: line read from `infra/secrets/sftp.env`.",
                                                examples=["SFTP_PASSWORD_CAMTRACK"])
    credentials:          Optional[Literal["password", "key"]] = Field(
        None, description="Credentials available at connect time: the saved password, else the secrets file "
                          "(`null` = none — enter the password).")
    host_key_fingerprint: Optional[str] = Field(None, description="Host key trusted on first use.",
                                                examples=["ssh-ed25519 SHA256:3cX1…"])
    host_key_trusted_at:  Optional[datetime] = Field(None, description="When the host key was first trusted (UTC).")
    link_state:           Literal["disconnected", "connecting", "connected", "error"] = Field(
        "disconnected", description="Live link state (API browse session or a download in progress).")
    link_message:         Optional[str] = Field(None, description="Last error when `link_state` is `error`.")
    link_at:              Optional[str] = Field(None, description="When the state last changed (UTC, ISO 8601).")
    link_held:            bool = Field(False, description="Disconnected by the user: browsing does not reconnect "
                                                          "until *Connect* (`409 sftp_disconnected`).")


Purpose = Literal["videos", "events"]


class EventFeedOut(BaseModel):
    """Daily event files of an `events` source (counts over every file seen)."""
    files:          int = Field(0, description="Files found on the server.")
    imported:       int = Field(0, description="Files imported.")
    failed:         int = Field(0, description="Files whose last import failed.")
    events:         int = Field(0, description="Events stored from this source.")
    first_day:      Optional[date] = Field(None, description="Oldest day imported.")
    last_day:       Optional[date] = Field(None, description="Latest day imported.")
    last_file:      Optional[str] = Field(None, description="Latest file imported.", examples=["2026-10-27.csv"])
    last_imported_at: Optional[datetime] = None
    due_until:      Optional[date] = Field(None, description="Last day whose file is due by now (day D: D+1 at "
                                                             "`alert_hour` UTC).")
    complete_until: Optional[date] = Field(None, description="Every day from the first one up to this one is imported.")
    missing:        list[date] = Field(default_factory=list, description="Due days without a file (last 60 days).")
    failed_days:    list[date] = Field(default_factory=list, description="Days whose file could not be imported.")
    alert_hour:     int = Field(10, description="Hour (UTC) after which a missing file raises an alert.")
    alert_emails:   list[str] = Field(default_factory=list, description="Also warned by e-mail.")


class ConnectionOut(BaseModel):
    """A cloud storage connection (the OAuth tokens / SFTP password are never returned)."""
    id:                   str = Field(description="Connection id (UUID).")
    provider:             str = Field(description="`google_drive`, `onedrive` or `sftp`.", examples=["google_drive"])
    name:                 str = Field(description="Display name.", examples=["Fleet dashcams"])
    account_email:        Optional[str] = Field(description="Provider account that granted access.")
    folder_id:            Optional[str] = Field(description="Watched folder id (`null` = drive root).")
    folder_name:          Optional[str] = Field(description="Watched folder name.")
    status:               str = Field(description="`pending_auth` (consent not given yet), `active`, `error` (last "
                                                  "authorisation or sync failed) or `disabled`.", examples=["active"])
    authorized:           bool = Field(False, description="A refresh token is stored.")
    enabled:              bool = Field(description="Whether the connection can be used (browse, import, sync).")
    auto_import:          bool = Field(False, description="The watched folder is imported automatically.")
    auto_import_models:   Optional[list[str]] = Field(None, description="Models of auto-imported files "
                                                                        "(`null` = defaults).")
    auto_import_frame_skip: Optional[int] = Field(None, description="Frame sampling of auto-imported videos.")
    created_by:           Optional[str] = Field(description="Creator.")
    created_at:           datetime = Field(description="Creation time (UTC).")
    last_sync_at:         Optional[datetime] = Field(description="Last sync (UTC).")
    last_sync_status:     Optional[str] = Field(description="Outcome of the last sync.")
    last_sync_error:      Optional[str] = Field(description="Error of the last sync or authorisation.")
    last_sync_files:      int = Field(0, description="Files imported by the last sync.")
    files_imported_total: int = Field(0, description="Files imported since creation.")
    sftp:                 Optional[SftpInfoOut] = Field(None, description="SFTP server profile (provider `sftp`).")
    purpose:              Purpose = Field("videos", description="`videos` (browse / import → processing jobs) or "
                                                                "`events` (daily driving-event CSV files → historical "
                                                                "analysis and driver behaviour).")
    event_feed:           Optional[EventFeedOut] = Field(None, description="Daily files (purpose `events`).")
    saved_folders:        list[SavedFolder] = Field(default_factory=list, description="Folders bookmarked in "
                                                                                     "the browser.")


class ConnectionAuthorizeOut(BaseModel):
    """The connection and the provider consent URL to open."""
    connection:    ConnectionOut
    authorize_url: str = Field(description="Provider consent page; valid 15 minutes (single use).",
                               examples=["https://accounts.google.com/o/oauth2/v2/auth?client_id=…&state=…"])


class SyncQueuedOut(BaseModel):
    """Acknowledgement of a queued sync."""
    message: str = Field(description="Confirmation text.", examples=["Sync queued."])


class CloudFolderOut(BaseModel):
    """A folder of the connected drive."""
    id:   str = Field(description="Provider folder id (use as `folder_id` / `parent`).")
    name: str = Field(description="Folder name.", examples=["Dashcam uploads"])


ConnectionId = Annotated[str, Path(description="Cloud connection id (UUID).")]
E_NOT_FOUND = legacy(404, "Cloud connection not found")
E_NOT_CONFIGURED = err(400, "cloud_not_configured",
                       "OAuth client for google_drive is not configured (Settings → Platform → Cloud storage).")


def _sftp_out(c: CloudConnection, link: Optional[dict] = None) -> Optional[SftpInfoOut]:
    if c.provider != "sftp":
        return None
    link = link or {}
    key = c.sftp_password_key or None
    saved = bool(c.sftp_password_enc)
    return SftpInfoOut(
        host=c.sftp_host or "", port=c.sftp_port or 22, username=c.sftp_username or "", base_path=c.sftp_base_path,
        password_saved=saved, password_key=key, password_var=sftp_core.password_var(key) if key else None,
        credentials="password" if saved else (sftp_core.credentials_status(key) if key else None),
        host_key_fingerprint=c.sftp_host_key_fp, host_key_trusted_at=c.sftp_host_key_at,
        link_state=link.get("state") or "disconnected", link_message=link.get("message"), link_at=link.get("at"),
        link_held=bool(link.get("held")))


def _out(c: CloudConnection, link: Optional[dict] = None, feed: Optional[EventFeedOut] = None) -> ConnectionOut:
    sftp = _sftp_out(c, link)
    return ConnectionOut(
        id=c.id, provider=c.provider, name=c.name,
        account_email=c.account_email or (f"{c.sftp_username}@{c.sftp_host}" + (f":{c.sftp_port}" if (c.sftp_port or 22) != 22 else "")
                                          if sftp else None),
        folder_id=c.folder_id,
        folder_name=c.folder_name, status=c.status,
        authorized=bool(c.refresh_token_enc) if not sftp else sftp.credentials is not None, enabled=c.enabled,
        created_by=c.created_by, created_at=c.created_at, last_sync_at=c.last_sync_at,
        last_sync_status=c.last_sync_status, last_sync_error=c.last_sync_error,
        last_sync_files=c.last_sync_files or 0, files_imported_total=c.files_imported_total or 0,
        auto_import=bool(c.auto_import), auto_import_models=c.auto_import_models,
        auto_import_frame_skip=c.auto_import_frame_skip, sftp=sftp,
        saved_folders=_saved(c.saved_folders), purpose=c.purpose or "videos",
        event_feed=(feed or EventFeedOut()) if c.purpose == event_feed.PURPOSE else None,
    )


async def _feeds(db: AsyncSession, conns) -> dict[str, EventFeedOut]:
    """Event-file counters of the `events` sources, one grouped query."""
    ids = [c.id for c in conns if c.purpose == event_feed.PURPOSE]
    if not ids:
        return {}
    F = EventFile
    rows = (await db.execute(
        select(F.connection_id, func.count(), func.count().filter(F.status == "imported"),
               func.count().filter(F.status == "failed"), func.coalesce(func.sum(F.inserted), 0),
               func.min(F.day).filter(F.status == "imported"), func.max(F.day).filter(F.status == "imported"),
               func.max(F.imported_at))
        .where(F.connection_id.in_(ids)).group_by(F.connection_id))).all()
    out = {}
    by_id = {c.id: c for c in conns}
    now = datetime.utcnow()
    for cid, n, ok, bad, events, first, last, at in rows:
        last_file = (await db.execute(select(F.name).where(F.connection_id == cid, F.status == "imported")
                                      .order_by(F.day.desc()).limit(1))).scalar_one_or_none()
        c = by_id[cid]
        h = event_feed.health(await event_feed.file_rows(db, cid), now, c.event_alert_hour)
        out[cid] = EventFeedOut(files=n, imported=ok, failed=bad, events=int(events or 0), first_day=first,
                                last_day=last, last_file=last_file, last_imported_at=at, due_until=h["due_until"],
                                complete_until=h["complete_until"], missing=h["missing"], failed_days=h["failed"],
                                alert_hour=_alert_hour(c), alert_emails=c.event_alert_emails or [])
    for cid in ids:
        if cid not in out:
            c = by_id[cid]
            out[cid] = EventFeedOut(alert_hour=_alert_hour(c), alert_emails=c.event_alert_emails or [])
    return out


def _alert_hour(c) -> int:
    return event_feed.DEFAULT_ALERT_HOUR if c.event_alert_hour is None else c.event_alert_hour


def _saved(raw) -> list[SavedFolder]:
    out = []
    for f in raw or []:
        try:
            out.append(SavedFolder.model_validate(f))
        except Exception:          # a malformed entry is dropped, not a 500
            continue
    return out


def _snapshot(c: CloudConnection) -> dict:
    snap = {"provider": c.provider, "name": c.name, "folder_id": c.folder_id, "folder_name": c.folder_name,
            "enabled": c.enabled, "status": c.status, "auto_import": bool(c.auto_import),
            "auto_import_models": c.auto_import_models, "auto_import_frame_skip": c.auto_import_frame_skip,
            "saved_folders": [f.get("name") for f in (c.saved_folders or []) if isinstance(f, dict)],
            "purpose": c.purpose or "videos", "event_alert_hour": getattr(c, "event_alert_hour", None),
            "event_alert_emails": getattr(c, "event_alert_emails", None)}
    if c.provider == "sftp":
        snap.update({"sftp_host": c.sftp_host, "sftp_port": c.sftp_port, "sftp_username": c.sftp_username,
                     "sftp_base_path": c.sftp_base_path, "sftp_password_key": c.sftp_password_key,
                     "sftp_password_saved": bool(c.sftp_password_enc),       # never the value
                     "sftp_host_key_fp": c.sftp_host_key_fp})
    return snap


async def _links(conns) -> dict[str, dict]:
    """Live link state of the SFTP connections ({} when Redis is down)."""
    ids = [c.id for c in conns if c.provider == "sftp"]
    if not ids:
        return {}
    try:
        await asyncio.to_thread(sftp_source.POOL.reap)
        r = get_redis()
        pipe = r.pipeline()
        for i in ids:
            pipe.hgetall(sftp_source.state_key(i))
        res = await pipe.execute()
        return {i: sftp_source.link_state(st or {}) for i, st in zip(ids, res)}
    except Exception:
        return {}


async def _get(db: AsyncSession, connection_id: str) -> CloudConnection:
    conn = await db.get(CloudConnection, connection_id)
    if conn is None:
        raise HTTPException(404, "Cloud connection not found")
    return conn


async def _authorize_url(db: AsyncSession, conn: CloudConnection) -> str:
    cfg = await get_section(db, "cloud")
    state, nonce = cloud_storage.make_state(conn.id)
    try:
        url = cloud_storage.authorize_url(conn.provider, cfg, state)  # type: ignore[arg-type]
    except cloud_storage.CloudError as exc:
        raise api_error(400, "cloud_not_configured", str(exc))
    await get_redis().set(cloud_storage.state_key(nonce), conn.id, ex=cloud_storage.STATE_TTL_S)
    return url


@router.get("/", response_model=list[ConnectionOut], summary="List cloud connections")
async def list_connections(db: AsyncSession = Depends(get_db)):
    """Every Google Drive / OneDrive connection and SFTP server, newest first (SFTP: with the live
    link state)."""
    rows = (await db.execute(select(CloudConnection).order_by(CloudConnection.created_at.desc()))).scalars().all()
    links = await _links(rows)
    feeds = await _feeds(db, rows)
    return [_out(c, links.get(c.id), feeds.get(c.id)) for c in rows]


@router.post("/", response_model=ConnectionAuthorizeOut, status_code=201, summary="Create a cloud connection",
             responses=errors(E_NOT_CONFIGURED))
async def create_connection(body: ConnectionIn, request: Request, db: AsyncSession = Depends(get_db),
                            principal: Principal = Depends(get_principal)):
    """
    Create a connection in status `pending_auth` and return the provider's consent URL (RRAP-07-002).
    Flow: open `authorize_url` → the provider redirects to the web console callback, which posts
    `{code, state}` to `POST /cloud-connections/oauth/complete` → the connection becomes `active`: its
    files can be browsed and imported; set a watched folder + `auto_import` to import new files
    automatically. Requires the provider's OAuth client in *Platform settings → cloud* (see
    `GET /cloud-connections/providers`). Audited as `cloud_connection.create`.
    SFTP servers are created with `POST /cloud-connections/sftp` instead.
    """
    conn = CloudConnection(provider=body.provider, name=body.name.strip(), folder_id=body.folder_id or None,
                           folder_name=body.folder_name, status=ConnectionStatus.pending_auth.value,
                           created_by=actor_name(principal, principal.label), enabled=True, auto_import=False,
                           last_sync_files=0, files_imported_total=0)
    db.add(conn)
    await db.flush()
    url = await _authorize_url(db, conn)
    audit.record(db, request, "cloud_connection.create", principal=principal, object_type="cloud_connection",
                 object_id=conn.id, details=_snapshot(conn))
    await db.commit()
    return {"connection": _out(conn), "authorize_url": url}


@router.post("/oauth/complete", response_model=ConnectionOut, summary="Complete the OAuth authorisation",
             responses=errors(
                 err(400, "invalid_state", "Invalid authorisation state — start the connection again."),
                 err(400, "state_expired", "This authorisation link expired — start the connection again."),
                 E_NOT_FOUND,
                 err(502, "oauth_failed", "Token exchange failed (400): invalid_grant")))
async def oauth_complete(body: OAuthCompleteIn, request: Request, db: AsyncSession = Depends(get_db),
                         principal: Principal = Depends(get_principal)):
    """
    Exchange the authorisation `code` for tokens. The `state` is single-use and valid 15 minutes. On
    success the refresh token is stored encrypted, `account_email` is filled and the status becomes
    `active`; on a provider error the status becomes `error` (`502 oauth_failed`). Audited as
    `cloud_connection.authorize`.
    """
    parsed = cloud_storage.parse_state(body.state)
    if parsed is None:
        raise api_error(400, "invalid_state", "Invalid authorisation state — start the connection again.")
    connection_id, nonce = parsed
    stored = await get_redis().getdel(cloud_storage.state_key(nonce))
    if stored != connection_id:
        raise api_error(400, "state_expired", "This authorisation link expired — start the connection again.")
    conn = await _get(db, connection_id)
    cfg = await get_section(db, "cloud")
    try:
        tokens = await cloud_storage.exchange_code(conn.provider, cfg, body.code)  # type: ignore[arg-type]
        email = await cloud_storage.account_email(conn.provider, tokens.access_token)
    except cloud_storage.CloudError as exc:
        conn.status = ConnectionStatus.error.value
        conn.last_sync_error = str(exc)[:500]
        audit.record(db, request, "cloud_connection.authorize", principal=principal, outcome="failure",
                     object_type="cloud_connection", object_id=conn.id, details={"error": str(exc)[:500]})
        await db.commit()
        raise api_error(502, "oauth_failed", str(exc))
    conn.refresh_token_enc = encrypt_secret(tokens.refresh_token)
    conn.account_email = email
    conn.status = ConnectionStatus.active.value
    conn.last_sync_error = None
    audit.record(db, request, "cloud_connection.authorize", principal=principal, object_type="cloud_connection",
                 object_id=conn.id, details={"provider": conn.provider, "account_email": email})
    await db.commit()
    return _out(conn)


@router.post("/{connection_id}/authorize", response_model=ConnectionAuthorizeOut,
             summary="Get a new consent URL", responses=errors(E_NOT_FOUND, E_NOT_CONFIGURED))
async def reauthorize(connection_id: ConnectionId, db: AsyncSession = Depends(get_db)):
    """New consent URL for an existing connection (first consent abandoned, or token expired/revoked);
    finish with `POST /cloud-connections/oauth/complete`. Changes nothing by itself."""
    conn = await _get(db, connection_id)
    if conn.provider == "sftp":
        raise api_error(409, "not_oauth", "SFTP servers have no consent step — use POST …/sftp/connect.")
    return {"connection": _out(conn), "authorize_url": await _authorize_url(db, conn)}


@router.patch("/{connection_id}", response_model=ConnectionOut, summary="Update a cloud connection",
              responses=errors(E_NOT_FOUND))
async def update_connection(connection_id: ConnectionId, body: ConnectionPatch, request: Request,
                            db: AsyncSession = Depends(get_db), principal: Principal = Depends(get_principal)):
    """Rename, change the watched folder and its automatic import, the saved folders, or
    enable/disable the connection (see `ConnectionPatch`). Audited as `cloud_connection.update`."""
    conn = await _get(db, connection_id)
    before = _snapshot(conn)
    data = body.model_dump(exclude_unset=True)
    if data.get("name"):
        conn.name = data["name"].strip()
    if "folder_id" in data:
        conn.folder_id = data["folder_id"] or None
        conn.folder_name = data.get("folder_name") if conn.folder_id else None
    elif "folder_name" in data:
        conn.folder_name = data["folder_name"]
    if data.get("auto_import") is not None:
        conn.auto_import = data["auto_import"]
    if "auto_import_models" in data:
        conn.auto_import_models = data["auto_import_models"]
    if "auto_import_frame_skip" in data:
        conn.auto_import_frame_skip = data["auto_import_frame_skip"]
    if "event_alert_hour" in data:
        conn.event_alert_hour = data["event_alert_hour"]
    if body.event_alert_emails is not None:
        conn.event_alert_emails = body.event_alert_emails or None
    if body.saved_folders is not None:
        seen: set[str] = set()
        conn.saved_folders = [f.model_dump() for f in body.saved_folders
                              if not (f.id in seen or seen.add(f.id))]
    if data.get("enabled") is not None:
        conn.enabled = data["enabled"]
        if not conn.enabled:
            conn.status = ConnectionStatus.disabled.value
        elif conn.status == ConnectionStatus.disabled.value:
            conn.status = (ConnectionStatus.active.value if conn.refresh_token_enc or conn.provider == "sftp"
                           else ConnectionStatus.pending_auth.value)
        if not conn.enabled and conn.provider == "sftp":
            await asyncio.to_thread(sftp_source.POOL.close, conn.id, sftp_source.make_log(conn.id))
    audit.record(db, request, "cloud_connection.update", principal=principal, object_type="cloud_connection",
                 object_id=conn.id, details=audit.diff(before, _snapshot(conn)))
    await db.commit()
    return _out(conn, None, (await _feeds(db, [conn])).get(conn.id))


@router.delete("/{connection_id}", status_code=204, summary="Delete a cloud connection",
               responses=errors(E_NOT_FOUND))
async def delete_connection(connection_id: ConnectionId, request: Request, db: AsyncSession = Depends(get_db),
                            principal: Principal = Depends(get_principal)):
    """Remove the connection and its stored tokens (SFTP: its profile, trusted host key and connection
    log and saved password). Jobs already imported stay (their `connection_id`
    is cleared); imports still downloading fail with a readable error. Audited as
    `cloud_connection.delete`."""
    conn = await _get(db, connection_id)
    await cloud_import.forget_access_token(conn.id)
    if conn.provider == "sftp":
        await asyncio.to_thread(sftp_source.POOL.close, conn.id, sftp_source.make_log(conn.id))
        try:
            await sftp_source.forget_state(conn.id)
        except Exception:
            pass
    audit.record(db, request, "cloud_connection.delete", principal=principal, object_type="cloud_connection",
                 object_id=conn.id, details=_snapshot(conn))
    await db.delete(conn)
    await db.commit()


@router.post("/{connection_id}/sync", response_model=SyncQueuedOut, status_code=202, summary="Sync a connection now",
             responses=errors(E_NOT_FOUND,
                              err(409, "not_authorized", "Connect the account before syncing."),
                              err(409, "connection_disabled", "Enable the connection before syncing."),
                              err(503, "queue_unavailable", "Could not queue the sync: Connection refused")))
async def sync_now(connection_id: ConnectionId, request: Request, db: AsyncSession = Depends(get_db),
                   principal: Principal = Depends(get_principal)):
    """
    Import the new files of the watched folder now instead of waiting for the next automatic run:
    they form an import batch (`origin` `sync`, see `GET /imports`) whose jobs have `source_type`
    `google_drive`/`onedrive`. Files whose current version was already imported are skipped. Answers
    `202` once queued; the outcome appears in `last_sync_*`. Audited as `cloud_connection.sync_requested`.
    """
    conn = await _get(db, connection_id)
    if not conn.refresh_token_enc and conn.provider != "sftp":
        raise api_error(409, "not_authorized", "Connect the account before syncing.")
    if not conn.enabled:
        raise api_error(409, "connection_disabled", "Enable the connection before syncing.")
    from app.workers.job_tasks import sync_cloud_connection
    from app.workers.event_feed_tasks import sync_event_feed
    try:
        if conn.purpose == event_feed.PURPOSE:
            sync_event_feed.delay(conn.id)
        else:
            sync_cloud_connection.delay(conn.id, actor_name(principal, principal.label) or "system")
    except Exception as exc:
        raise api_error(503, "queue_unavailable", f"Could not queue the sync: {exc}")
    audit.record(db, request, "cloud_connection.sync_requested", principal=principal,
                 object_type="cloud_connection", object_id=conn.id)
    await db.commit()
    return {"message": "Sync queued."}


@router.get("/{connection_id}/folders", response_model=list[CloudFolderOut], summary="Browse the drive's folders",
            responses=errors(E_NOT_FOUND,
                             err(409, "not_authorized", "Connect the account first."),
                             err(502, "cloud_failed", "List folders failed (401): invalid credentials")))
async def list_folders(connection_id: ConnectionId,
                       parent: Optional[str] = Query(None, description="Parent folder id (default: drive root)."),
                       db: AsyncSession = Depends(get_db)):
    """Sub-folders of `parent` (default: the drive root), sorted by name — used to pick the watched
    folder. Requires an authorised connection. (`/browse` also returns files.)"""
    conn = await _get(db, connection_id)
    if conn.provider == "sftp":
        page = await _with_token(db, conn, lambda target: cloud_providers.get_provider("sftp").browse(target, parent or None))
        return [CloudFolderOut(id=e.id, name=e.name) for e in sorted(page.entries, key=lambda e: e.name.casefold())
                if e.kind == "folder"]
    if not conn.refresh_token_enc:
        raise api_error(409, "not_authorized", "Connect the account first.")
    # same listing as /browse (OneDrive: "Shared with me" and folders of other people's drives)
    provider = cloud_providers.get_provider(conn.provider)
    entries = await _with_token(db, conn, lambda token: provider.list_all(token, parent or None))
    await db.commit()   # a rotated refresh token
    return [CloudFolderOut(id=e.id, name=e.name) for e in entries if e.kind == "folder"]


class ResolvedLinkOut(BaseModel):
    """A drive item opened from its address."""
    id: str = Field(description="Item id to browse / watch (another person's drive: `<driveId>~<itemId>`).")
    name: str
    kind: str = Field(description="`folder` or `file`.")
    breadcrumbs: list[CrumbOut] = Field(description="Path to show (stops where this account's access starts).")


@router.get("/{connection_id}/resolve", response_model=ResolvedLinkOut, summary="Open a folder from its address",
            responses=errors(E_NOT_FOUND, err(502, "cloud_failed", "This address does not lead to a OneDrive / "
                                                                     "SharePoint folder this account can open.")))
async def resolve_link(connection_id: ConnectionId,
                       url: str = Query(..., max_length=4000, description="OneDrive / SharePoint address: a share "
                                                                           "link or the browser's address bar."),
                       db: AsyncSession = Depends(get_db)):
    """OneDrive only: a folder shared with the account through a link appears neither in "Shared with
    me" nor under shortcuts in Microsoft's API — paste its address to open it (browse, import, watch)."""
    conn = await _usable(db, connection_id)
    provider = cloud_providers.get_provider(conn.provider)
    if not hasattr(provider, "resolve_link"):
        raise api_error(422, "not_supported", "Opening an address is available for OneDrive only.")

    async def run(token: str):
        entry = await provider.resolve_link(token, url)
        crumbs = await provider.path(token, entry.id) if entry.kind == "folder" else []
        return entry, crumbs

    entry, crumbs = await _with_token(db, conn, run)
    await db.commit()
    return ResolvedLinkOut(id=entry.id, name=entry.name, kind=entry.kind,
                           breadcrumbs=[CrumbOut(id=c.id, name=c.name) for c in crumbs])


# ── Browsing & imports (input source) ───────────────────────────────────────

E_NOT_AUTHORIZED = err(409, "not_authorized", "Connect the account first.")
E_DISABLED = err(409, "connection_disabled", "Enable the connection first.")
E_CLOUD = err(502, "cloud_failed", "Google Drive listing failed (403): insufficient scope")
MAX_IMPORT_FILES = 1000


async def _usable(db: AsyncSession, connection_id: str) -> CloudConnection:
    conn = await _get(db, connection_id)
    if not conn.refresh_token_enc and conn.provider != "sftp":
        raise api_error(409, "not_authorized", "Connect the account first.")
    if not conn.enabled:
        raise api_error(409, "connection_disabled", "Enable the connection first.")
    return conn


async def _with_token(db: AsyncSession, conn: CloudConnection, fn):
    """Run `fn(token)`; on an expired access token, refresh it once and retry.
    Provider errors become `502 cloud_failed`. What the operation learned
    (SFTP host key trusted on first use) is stored."""
    provider = cloud_providers.get_provider(conn.provider)
    if conn.provider == "sftp" and await sftp_source.is_held(conn.id):
        raise api_error(409, "sftp_disconnected",
                        "Disconnected from the server — click Connect to reconnect.")

    async def call():
        token = await cloud_import.access_token(db, conn)
        result = await fn(token)
        if provider.persist_credentials(conn, token):
            await db.commit()
        return result

    try:
        try:
            return await call()
        except cloud_providers.AuthExpiredError:
            await cloud_import.forget_access_token(conn.id)
            return await call()
    except cloud_storage.CloudError as exc:
        raise api_error(502, "cloud_failed", str(exc))


def _entry_out(e: cloud_providers.DriveEntry, state: Optional[cloud_import.ImportState] = None) -> DriveEntryOut:
    return DriveEntryOut(
        id=e.id, name=e.name, kind=e.kind, mime_type=e.mime_type, media=e.media, importable=e.media is not None,
        size=e.size, modified=e.modified, duration_s=e.duration_s, width=e.width, height=e.height,
        child_count=e.child_count, has_thumbnail=bool(e.thumbnail_url), web_url=e.web_url, path=e.path,
        imported=ImportInfoOut(**state.__dict__) if state is not None else None,
    )


@router.get("/providers", response_model=list[ProviderOut], summary="List the drive providers")
async def list_providers(db: AsyncSession = Depends(get_db)):
    """Providers a connection can be created for, with what their browser supports. Only providers
    with a working OAuth flow are listed; `configured` tells whether the platform's OAuth client is
    set (*Platform settings → cloud*) — connecting a provider that is not configured fails with
    `400 cloud_not_configured`."""
    return cloud_providers.available_providers(await get_section(db, "cloud"))


@router.get("/{connection_id}/browse", response_model=BrowseOut, summary="Browse a folder of the drive",
            responses=errors(E_NOT_FOUND, E_NOT_AUTHORIZED, E_DISABLED, E_CLOUD))
async def browse(connection_id: ConnectionId,
                 folder_id: Optional[str] = Query(None, description="Folder to list (default: the drive root)."),
                 page_token: Optional[str] = Query(None, description="`next_page_token` of the previous page."),
                 q: Optional[str] = Query(None, max_length=200, description="Search by name inside the folder "
                                                                             "(sub-folders too on OneDrive)."),
                 db: AsyncSession = Depends(get_db)):
    """
    One page (≤ 100 entries) of a folder: its sub-folders and files, folders first, by name. Every file
    says whether it is importable (`media` = `video`/`image` the processor supports — other files are
    listed but cannot be selected) and whether it was imported before (`imported.state`: `imported`
    when this very version — checksum / content tag / modification time — was imported, `changed`
    when only an older version was, `importing` while in progress). `breadcrumbs` (first page only)
    give the path from the drive root.
    """
    conn = await _usable(db, connection_id)
    provider = cloud_providers.get_provider(conn.provider)

    async def run(token: str):
        page = await provider.browse(token, folder_id or None, page_token=page_token, query=q)
        crumbs = [] if page_token else await provider.path(token, folder_id or None)
        return page, crumbs

    page, crumbs = await _with_token(db, conn, run)
    files = [e for e in page.entries if e.kind == "file"]
    folders = [e for e in page.entries if e.kind == "folder"]
    priors = await cloud_import.prior_imports(db, conn.id, [f.id for f in files])
    await db.commit()   # a rotated refresh token
    key = lambda e: e.name.casefold()  # noqa: E731
    return BrowseOut(
        connection_id=conn.id, folder_id=folder_id or None,
        breadcrumbs=[CrumbOut(id=c.id, name=c.name) for c in crumbs],
        folders=[_entry_out(f) for f in sorted(folders, key=key)],
        files=[_entry_out(f, cloud_import.import_state(f.version, priors.get(f.id, [])))
               for f in sorted(files, key=key)],
        next_page_token=page.next_page_token, query=q or None,
        search_recursive=bool(q) and provider.search_recursive,
    )


@router.get("/{connection_id}/thumbnail/{file_id}", summary="Thumbnail of a drive file",
            response_class=Response,
            responses={200: {"content": {"image/jpeg": {}}, "description": "The thumbnail image."},
                       **errors(E_NOT_FOUND, E_NOT_AUTHORIZED, E_DISABLED, E_CLOUD,
                                err(404, "no_thumbnail", "This file has no thumbnail."))})
async def thumbnail(connection_id: ConnectionId,
                    file_id: Annotated[str, Path(description="Provider file id.")],
                    db: AsyncSession = Depends(get_db)):
    """The provider's thumbnail of a file (videos and images), fetched server-side so the browser
    never needs a provider token. Cached by the browser for an hour."""
    conn = await _usable(db, connection_id)
    provider = cloud_providers.get_provider(conn.provider)
    found = await _with_token(db, conn, lambda token: provider.thumbnail(token, file_id))
    await db.commit()
    if not found:
        raise api_error(404, "no_thumbnail", "This file has no thumbnail.")
    content, ctype = found
    return Response(content=content, media_type=ctype, headers={"Cache-Control": "private, max-age=3600"})


@router.post("/{connection_id}/imports", response_model=ImportBatchOut, status_code=202,
             summary="Import files from the drive",
             responses=errors(E_NOT_FOUND, E_NOT_AUTHORIZED, E_DISABLED, E_CLOUD,
                              err(422, "empty_selection", "Select at least one file or folder."),
                              err(422, "nothing_to_import", "None of the selected files is a supported video or image."),
                              err(422, "too_many_files", "The selection holds more than 1000 files — import fewer "
                                                         "folders at once."),
                              legacy(400, "Unknown model(s): ['cars']. Valid options: ['pothole', 'signs', "
                                          "'speedbump', 'vehicles']")))
async def create_import(connection_id: ConnectionId, body: ImportIn, request: Request,
                        db: AsyncSession = Depends(get_db), principal: Principal = Depends(get_principal)):
    """
    Create an import batch: one item per media file of `files` and of the `folder_ids` (recursively
    with `options.include_subfolders`). Non-media files are ignored; files whose current version was
    already imported become `skipped_duplicate` unless `options.reimport`. Each queued item is then
    downloaded (chunked, resumable, at most `processing.cloud_import_concurrency` at once), verified
    and handed to the processing queue as a job with these `models` / `frame_skip`. Answers `202`
    with the batch; follow it with `GET /imports/{batch_id}`. Audited as `cloud_import.create`.
    """
    if not body.files and not body.folder_ids:
        raise api_error(422, "empty_selection", "Select at least one file or folder.")
    try:
        models = job_service.parse_models(",".join(body.models) if body.models is not None else None)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    conn = await _usable(db, connection_id)
    if conn.purpose == event_feed.PURPOSE:
        raise api_error(409, "not_a_video_source", "This source provides event files, not videos: they are "
                                                   "imported automatically every day.")
    provider = cloud_providers.get_provider(conn.provider)

    async def collect(token) -> list[cloud_providers.DriveEntry]:
        import httpx
        async with httpx.AsyncClient(timeout=cloud_storage.TIMEOUT, follow_redirects=True) as client:
            return await provider.resolve_selection(
                token, body.files, body.folder_ids, include_subfolders=body.options.include_subfolders,
                max_files=MAX_IMPORT_FILES, client=client)

    entries = await _with_token(db, conn, collect)
    if len(entries) > MAX_IMPORT_FILES:
        raise api_error(422, "too_many_files", "The selection holds more than 1000 files — import fewer folders at once.")
    entries = [e for e in entries if cloud_providers.importable(e)]
    if not entries:
        raise api_error(422, "nothing_to_import", "None of the selected files is a supported video or image.")
    actor = actor_name(principal, principal.label) or principal.kind
    batch, items = await cloud_import.import_entries(
        db, conn, entries, origin="manual", models=models, frame_skip=body.frame_skip,
        options={**body.options.model_dump(), "folder_ids": body.folder_ids}, actor=actor,
        reimport=body.options.reimport)
    audit.record(db, request, "cloud_import.create", principal=principal, object_type="cloud_import",
                 object_id=batch.id, details={
                     "connection_id": conn.id, "provider": conn.provider, "files": len(items),
                     "queued": sum(1 for i in items if i.status == "queued"),
                     "skipped_duplicate": sum(1 for i in items if i.status == "skipped_duplicate"),
                     "folders": len(body.folder_ids), "models": models, "frame_skip": body.frame_skip})
    await db.commit()
    return (await cloud_import.batch_views(db, [batch]))[0]


# ── SFTP servers ─────────────────────────────────────────────────────────────

_HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?)*$")
_IP6_RE = re.compile(r"^[0-9A-Fa-f:]{2,39}$")
_USER_RE = re.compile(r"^[A-Za-z0-9._@][A-Za-z0-9._@-]{0,63}$")


def _check_host(v: str) -> str:
    v = (v or "").strip().strip("[]").lower()
    if not (_HOST_RE.match(v) or _IP6_RE.match(v)):
        raise ValueError("Enter a host name (e.g. bi.camtrack.mg) or an IP address.")
    return v


def _check_user(v: str) -> str:
    v = (v or "").strip()
    if not _USER_RE.match(v):
        raise ValueError("Invalid username.")
    return v


def _check_base(v: Optional[str]) -> Optional[str]:
    if v is None or not v.strip():
        return None
    v = v.strip()
    if not v.startswith("/"):
        raise ValueError("The base path must be absolute (start with /), or empty for the user's home.")
    return sftp_core.norm_remote(v)


def _check_key(v: str) -> str:
    return sftp_core.normalize_key(v)


# Write-only: SecretStr keeps it out of reprs / logs, the API stores it encrypted and never
# returns it. Length is checked in the handlers (a pydantic constraint error would echo it).
PASSWORD_MAX = 1024
PasswordField = Field(None, description="SSH password (write-only: stored encrypted, never returned, "
                                        "logged or audited).", json_schema_extra={"writeOnly": True,
                                                                                  "format": "password"})


def _password(v: Optional[SecretStr]) -> Optional[str]:
    """The submitted password, or None when absent / empty (422 when too long, without echoing it)."""
    pw = v.get_secret_value() if v is not None else ""
    if len(pw) > PASSWORD_MAX:
        raise api_error(422, "invalid_password", f"The password is limited to {PASSWORD_MAX} characters.")
    if "\x00" in pw:
        raise api_error(422, "invalid_password", "The password contains an invalid character.")
    return pw or None


class SftpIn(BaseModel):
    """A new SFTP server. Give its `password` (stored encrypted, never returned) — or, legacy,
    a `password_key` read from `infra/secrets/sftp.env` (`SFTP_PASSWORD_<KEY>=…`)."""
    name:         Optional[str] = Field(None, max_length=120, description="Display name (default: the host).",
                                        examples=["CAMTRACK violations"])
    host:         str = Field(max_length=253, description="Host name (DNS) or IP.", examples=["bi.camtrack.mg"])
    port:         int = Field(22, ge=1, le=65535, description="SSH port.")
    username:     str = Field(max_length=64, description="SSH user.", examples=["roadusertoread"])
    base_path:    Optional[str] = Field(None, max_length=1024,
                                        description="Absolute folder browsing starts at and cannot leave "
                                                    "(empty = the user's home).",
                                        examples=["/home/Ymane-New/ft_violations"])
    password:     Optional[SecretStr] = PasswordField
    purpose:      Purpose = Field("videos", description="`videos`: browse and import videos. `events`: the server "
                                                        "drops one driving-event CSV a day (`YYYY-MM-DD.csv`) in "
                                                        "the base folder — imported automatically every hour.")
    password_key: Optional[str] = Field(None, max_length=64,
                                        description="Legacy, instead of `password`: the API reads "
                                                    "`SFTP_PASSWORD_<KEY>` from the secrets file (letters, digits, _).",
                                        examples=["CAMTRACK"])

    _h = field_validator("host")(classmethod(lambda cls, v: _check_host(v)))
    _u = field_validator("username")(classmethod(lambda cls, v: _check_user(v)))
    _b = field_validator("base_path")(classmethod(lambda cls, v: _check_base(v)))
    _k = field_validator("password_key")(classmethod(lambda cls, v: _check_key(v) if v else None))


class SftpPatch(BaseModel):
    """Edit an SFTP server: only the fields sent change (`password` empty / absent = keep the saved
    one). Changing the host or the port forgets the trusted host key (trusted again at the next
    connection)."""
    name:         Optional[str] = Field(None, min_length=1, max_length=120)
    host:         Optional[str] = Field(None, max_length=253)
    port:         Optional[int] = Field(None, ge=1, le=65535)
    username:     Optional[str] = Field(None, max_length=64)
    base_path:    Optional[str] = Field(None, max_length=1024, description="Empty string = the user's home.")
    password_key: Optional[str] = Field(None, max_length=64)
    purpose:      Optional[Purpose] = None
    password:     Optional[SecretStr] = PasswordField
    clear_password: bool = Field(False, description="Forget the saved password (the secrets-file key, if any, "
                                                    "is used again).")

    _h = field_validator("host")(classmethod(lambda cls, v: v if v is None else _check_host(v)))
    _u = field_validator("username")(classmethod(lambda cls, v: v if v is None else _check_user(v)))
    _b = field_validator("base_path")(classmethod(lambda cls, v: v if v is None else (_check_base(v) or "")))
    _k = field_validator("password_key")(classmethod(lambda cls, v: v if v is None else _check_key(v)))


class SftpConnectOut(BaseModel):
    """Outcome of Connect / Test."""
    connection: ConnectionOut
    base_path:  str = Field(description="Folder browsing starts at (resolved home when no base path is set).")
    entries:    int = Field(description="Entries listed in that folder.")


class SftpLogLineOut(BaseModel):
    seq:     int = Field(description="Increasing line number (pass the last one as `after`).")
    ts:      str = Field(description="UTC timestamp, ISO 8601.", examples=["2026-10-20T14:02:11.204Z"])
    level:   Literal["status", "error"]
    message: str = Field(examples=["Connected to bi.camtrack.mg"])
    source:  str = Field("api", description="`api` (browse / connect) or `worker` (downloads).")


class SftpLogOut(BaseModel):
    """Connection log (last 500 lines) and live link state."""
    connection_id: str
    link_state:    Literal["disconnected", "connecting", "connected", "error"]
    link_message:  Optional[str] = None
    link_at:       Optional[str] = None
    link_held:     bool = Field(False, description="Disconnected by the user (no automatic reconnection).")
    last_seq:      int = Field(description="Highest `seq` known (poll again with `after=last_seq`).")
    lines:         list[SftpLogLineOut]


E_NOT_SFTP = err(409, "not_sftp", "This connection is not an SFTP server.")
E_SFTP = err(502, "sftp_failed", "Authentication failed. Check the username and the password.")
E_NO_PASSWORD = err(422, "password_required", "Enter the server's password.")


async def _sftp(db: AsyncSession, connection_id: str) -> CloudConnection:
    conn = await _get(db, connection_id)
    if conn.provider != "sftp":
        raise api_error(409, "not_sftp", "This connection is not an SFTP server.")
    return conn


async def _link(conn: CloudConnection) -> dict:
    return (await _links([conn])).get(conn.id) or {}


@router.post("/sftp", response_model=ConnectionOut, status_code=201, summary="Add an SFTP server",
             responses=errors(E_NO_PASSWORD))
async def create_sftp(body: SftpIn, request: Request, db: AsyncSession = Depends(get_db),
                      principal: Principal = Depends(get_principal)):
    """
    Add an SFTP server as a video source (status `active`). The `password` is stored encrypted
    (Fernet keyed from the server's SECRET_KEY), decrypted only to authenticate, and never
    returned, logged or audited (`sftp.password_saved` tells it is there). Legacy alternative:
    `password_key`, read from `infra/secrets/sftp.env` (`SFTP_PASSWORD_<KEY>=…`). Then
    `POST …/{id}/sftp/connect` to test (the host key is trusted on first use), browse with
    `GET …/{id}/browse` and import with `POST …/{id}/imports` like a drive. Audited as
    `cloud_connection.create`.
    """
    password = _password(body.password)
    if not password and not body.password_key:
        raise api_error(422, "password_required", "Enter the server's password.")
    conn = CloudConnection(
        provider="sftp", name=(body.name or "").strip() or body.host, status=ConnectionStatus.active.value,
        created_by=actor_name(principal, principal.label), enabled=True, auto_import=False, last_sync_files=0,
        files_imported_total=0, sftp_host=body.host, sftp_port=body.port, sftp_username=body.username,
        sftp_base_path=body.base_path, sftp_password_key=None if password else body.password_key,
        sftp_password_enc=encrypt_secret(password) if password else None, purpose=body.purpose)
    del password
    db.add(conn)
    await db.flush()
    audit.record(db, request, "cloud_connection.create", principal=principal, object_type="cloud_connection",
                 object_id=conn.id, details=_snapshot(conn))
    await db.commit()
    sftp_source.make_log(conn.id).status(
        f"Server added: {body.username}@{body.host}:{body.port}, "
        + ("password saved (encrypted)" if conn.sftp_password_enc else f"password key {body.password_key}"))
    return _out(conn, await _link(conn))


@router.patch("/{connection_id}/sftp", response_model=ConnectionOut, summary="Edit an SFTP server",
              responses=errors(E_NOT_FOUND, E_NOT_SFTP, E_NO_PASSWORD))
async def update_sftp(connection_id: ConnectionId, body: SftpPatch, request: Request,
                      db: AsyncSession = Depends(get_db), principal: Principal = Depends(get_principal)):
    """Change the name, host, port, username, base path, password or password key (see `SftpPatch`).
    The open browse session is closed; a new host or port forgets the trusted host key. Audited as
    `cloud_connection.update` (a new password appears as `sftp_password: "changed"`, never its value)."""
    password = _password(body.password)
    conn = await _sftp(db, connection_id)
    before = _snapshot(conn)
    data = body.model_dump(exclude_unset=True)
    if data.get("name"):
        conn.name = data["name"].strip()
    if data.get("host"):
        conn.sftp_host = data["host"]
    if data.get("port"):
        conn.sftp_port = data["port"]
    if data.get("username"):
        conn.sftp_username = data["username"]
    if "base_path" in data:
        conn.sftp_base_path = data["base_path"] or None
    if data.get("password_key"):
        conn.sftp_password_key = data["password_key"]
    if data.get("purpose") and data["purpose"] != conn.purpose:
        conn.purpose = data["purpose"]
        if conn.purpose == event_feed.PURPOSE:          # no watched folder / auto-import of videos
            conn.auto_import = False
    if body.clear_password and not password:
        conn.sftp_password_enc = None
    if password:
        conn.sftp_password_enc = encrypt_secret(password)
    del password
    if not conn.sftp_password_enc and not conn.sftp_password_key:
        raise api_error(422, "password_required", "Enter the server's password.")
    log = sftp_source.make_log(conn.id)
    if (conn.sftp_host, conn.sftp_port) != (before.get("sftp_host"), before.get("sftp_port")) and conn.sftp_host_key:
        conn.sftp_host_key = conn.sftp_host_key_fp = conn.sftp_host_key_at = None
        log.status("Server address changed: the trusted host key was forgotten")
    changes = audit.diff(before, _snapshot(conn))
    if body.password is not None and body.password.get_secret_value():
        changes["sftp_password"] = "changed"
    await asyncio.to_thread(sftp_source.POOL.close, conn.id, log)
    audit.record(db, request, "cloud_connection.update", principal=principal, object_type="cloud_connection",
                 object_id=conn.id, details=changes)
    await db.commit()
    if changes:
        log.status("Server settings updated: " + ", ".join(k.replace("sftp_", "") for k in changes))
    return _out(conn, await _link(conn))


@router.post("/{connection_id}/sftp/connect", response_model=SftpConnectOut,
             summary="Connect to an SFTP server (test / reconnect)",
             responses=errors(E_NOT_FOUND, E_NOT_SFTP, E_DISABLED, E_SFTP))
async def connect_sftp(connection_id: ConnectionId, request: Request, db: AsyncSession = Depends(get_db),
                       principal: Principal = Depends(get_principal)):
    """
    Open a fresh session (closing the current one) and list the base folder — the *Connect / Test*
    and *Reconnect* buttons. Timeouts: 15 s to connect, 20 s for the banner / key exchange /
    authentication. The first successful connection trusts and stores the server's host key;
    a different key later is refused (`502 sftp_failed`, see *Forget host key*). Every step is
    written to the connection log (`GET …/sftp/log`). Audited as `cloud_connection.sftp_connect`.
    """
    conn = await _sftp(db, connection_id)
    if not conn.enabled:
        raise api_error(409, "connection_disabled", "Enable the connection first.")
    await sftp_source.release(conn.id)            # browsing may (re)connect again
    provider = cloud_providers.get_provider("sftp")
    try:
        target = sftp_source.SftpTarget.from_connection(conn)
        res = await asyncio.to_thread(provider.test_sync, target)
    except cloud_storage.CloudError as exc:
        conn.last_sync_error = str(exc)[:500]
        audit.record(db, request, "cloud_connection.sftp_connect", principal=principal, outcome="failure",
                     object_type="cloud_connection", object_id=conn.id, details={"error": str(exc)[:500]})
        await db.commit()
        raise api_error(502, "sftp_failed", str(exc))
    trusted = provider.persist_credentials(conn, target)
    conn.status = ConnectionStatus.active.value
    conn.last_sync_error = None
    audit.record(db, request, "cloud_connection.sftp_connect", principal=principal, object_type="cloud_connection",
                 object_id=conn.id, details={"host": conn.sftp_host, "base_path": res["base_path"],
                                             "host_key_trusted": conn.sftp_host_key_fp if trusted else None})
    await db.commit()
    return {"connection": _out(conn, await _link(conn)), **res}


@router.post("/{connection_id}/sftp/disconnect", response_model=ConnectionOut, summary="Disconnect an SFTP server",
             responses=errors(E_NOT_FOUND, E_NOT_SFTP))
async def disconnect_sftp(connection_id: ConnectionId, db: AsyncSession = Depends(get_db)):
    """Close the API's browse session (downloads in progress keep their own session) and stay
    disconnected: browsing answers `409 sftp_disconnected` until *Connect* is clicked again."""
    conn = await _sftp(db, connection_id)
    log = sftp_source.make_log(conn.id)
    await sftp_source.hold(conn.id)
    had = await asyncio.to_thread(sftp_source.POOL.close, conn.id, log)
    if not had:
        log.state("disconnected")
    log.status("Disconnected by user — no automatic reconnection until Connect is clicked")
    return _out(conn, await _link(conn))


@router.post("/{connection_id}/sftp/forget-host-key", response_model=ConnectionOut,
             summary="Forget the trusted host key", responses=errors(E_NOT_FOUND, E_NOT_SFTP))
async def forget_host_key(connection_id: ConnectionId, request: Request, db: AsyncSession = Depends(get_db),
                          principal: Principal = Depends(get_principal)):
    """Forget the host key trusted on first use — after a legitimate server key change: the next
    connection trusts the key it is shown. Audited as `cloud_connection.host_key_forget`."""
    conn = await _sftp(db, connection_id)
    old = conn.sftp_host_key_fp
    conn.sftp_host_key = conn.sftp_host_key_fp = conn.sftp_host_key_at = None
    log = sftp_source.make_log(conn.id)
    await asyncio.to_thread(sftp_source.POOL.close, conn.id, log)
    audit.record(db, request, "cloud_connection.host_key_forget", principal=principal, object_type="cloud_connection",
                 object_id=conn.id, details={"host": conn.sftp_host, "fingerprint": old})
    await db.commit()
    log.status(f"Trusted host key forgotten ({old or 'none'}); the next connection will trust the key it is shown")
    return _out(conn, await _link(conn))


@router.get("/{connection_id}/sftp/log", response_model=SftpLogOut, summary="Connection log of an SFTP server",
            responses=errors(E_NOT_FOUND, E_NOT_SFTP))
async def sftp_log(connection_id: ConnectionId,
                   after: int = Query(0, ge=0, description="Only lines with `seq` greater than this."),
                   limit: int = Query(500, ge=1, le=500, description="Most recent lines returned."),
                   db: AsyncSession = Depends(get_db)):
    """FileZilla-style log of the server (last 500 lines, written by the API when browsing /
    connecting and by the worker when downloading): `Status` lines (connecting, listing, transfers,
    disconnects) and `Error` lines (could not connect, authentication failed, host key mismatch,
    timeout, *Remote side unexpectedly closed network connection*). Poll every 1–2 s with
    `after=last_seq`; also returns the live link state."""
    conn = await _sftp(db, connection_id)
    await asyncio.to_thread(sftp_source.POOL.reap)
    try:
        lines, st = await sftp_source.read_log(conn.id, after, limit)
    except Exception:
        lines, st = [], {}
    link = sftp_source.link_state(st)
    last = max([int(x.get("seq") or 0) for x in lines] + [after])
    return SftpLogOut(connection_id=conn.id, link_state=link["state"], link_message=link["message"],
                      link_at=link["at"], link_held=link["held"], last_seq=last, lines=[SftpLogLineOut(**x) for x in lines])


@router.delete("/{connection_id}/sftp/log", status_code=204, summary="Clear the connection log",
               responses=errors(E_NOT_FOUND, E_NOT_SFTP))
async def clear_sftp_log(connection_id: ConnectionId, db: AsyncSession = Depends(get_db)):
    """Empty the connection log of the server (the link state is kept)."""
    conn = await _sftp(db, connection_id)
    await sftp_source.clear_log(conn.id)


# ── Event files (purpose "events") ───────────────────────────────────────────

class EventFileOut(BaseModel):
    """One daily file of an `events` source."""
    id:          str
    name:        str = Field(examples=["2026-10-27.csv"])
    day:         date = Field(description="Day the file covers.")
    size:        Optional[int] = None
    status:      Literal["pending", "imported", "failed"]
    rows:        int = Field(0, description="Data lines read.")
    inserted:    int = Field(0, description="New events.")
    updated:     int = Field(0, description="Events already known, updated (corrected file).")
    invalid:     int = Field(0, description="Lines skipped (see `problems`).")
    problems:    Optional[list[dict]] = Field(None, description="First skipped lines: `{line, reason}`.")
    by_type:     Optional[dict[str, int]] = Field(None, description="Events per type.",
                                                  examples=[{"SPEEDING": 947, "HARD_ACCELERATION": 693}])
    error:       Optional[str] = None
    attempts:    int = 0
    found_at:    datetime
    imported_at: Optional[datetime] = None


class EventFilesOut(BaseModel):
    total: int
    items: list[EventFileOut]


E_NOT_EVENTS = err(409, "not_an_event_source", "This source is not an event source.")


async def _events_source(db: AsyncSession, connection_id: str) -> CloudConnection:
    conn = await _get(db, connection_id)
    if conn.purpose != event_feed.PURPOSE:
        raise api_error(409, "not_an_event_source", "This source is not an event source.")
    return conn


@router.get("/{connection_id}/event-files", response_model=EventFilesOut, summary="List the daily event files",
            responses=errors(E_NOT_FOUND, E_NOT_EVENTS))
async def list_event_files(connection_id: ConnectionId, db: AsyncSession = Depends(get_db),
                           status: Optional[Literal["pending", "imported", "failed"]] = Query(None),
                           skip: int = Query(0, ge=0), limit: int = Query(31, ge=1, le=200)):
    """Files found on an `events` source, newest day first, with what each import did."""
    await _events_source(db, connection_id)
    conds = [EventFile.connection_id == connection_id]
    if status:
        conds.append(EventFile.status == status)
    total = (await db.execute(select(func.count()).select_from(EventFile).where(*conds))).scalar_one()
    rows = (await db.execute(select(EventFile).where(*conds).order_by(EventFile.day.desc(), EventFile.name)
                             .offset(skip).limit(limit))).scalars().all()
    return {"total": total, "items": [EventFileOut.model_validate(r, from_attributes=True) for r in rows]}


@router.post("/{connection_id}/event-files/sync", response_model=SyncQueuedOut, status_code=202,
             summary="Check an event source now",
             responses=errors(E_NOT_FOUND, E_NOT_EVENTS, E_DISABLED,
                              err(503, "queue_unavailable", "Could not queue the check: Connection refused")))
async def check_event_files(connection_id: ConnectionId, request: Request, db: AsyncSession = Depends(get_db),
                            principal: Principal = Depends(get_principal)):
    """List the server folder now and import the new or changed files (the hourly check, run at
    once). The outcome appears in `last_sync_*`, `event_feed` and `GET …/event-files`."""
    conn = await _events_source(db, connection_id)
    if not conn.enabled:
        raise api_error(409, "connection_disabled", "Enable the connection first.")
    from app.workers.event_feed_tasks import sync_event_feed
    try:
        sync_event_feed.delay(conn.id)
    except Exception as exc:
        raise api_error(503, "queue_unavailable", f"Could not queue the check: {exc}")
    audit.record(db, request, "cloud_connection.events_check", principal=principal, object_type="cloud_connection",
                 object_id=conn.id)
    await db.commit()
    return {"message": "Check queued."}


@router.post("/{connection_id}/event-files/{file_id}/reimport", response_model=SyncQueuedOut, status_code=202,
             summary="Import a daily event file again",
             responses=errors(E_NOT_FOUND, E_NOT_EVENTS, E_DISABLED,
                              err(503, "queue_unavailable", "Could not queue the import: Connection refused")))
async def reimport_event_file(connection_id: ConnectionId, file_id: str, request: Request,
                              db: AsyncSession = Depends(get_db), principal: Principal = Depends(get_principal)):
    """Fetch and import this file again even if it did not change (its events are updated, never
    duplicated)."""
    conn = await _events_source(db, connection_id)
    if not conn.enabled:
        raise api_error(409, "connection_disabled", "Enable the connection first.")
    ef = await db.get(EventFile, file_id)
    if ef is None or ef.connection_id != conn.id:
        raise HTTPException(404, "Event file not found")
    from app.workers.event_feed_tasks import sync_event_feed
    try:
        sync_event_feed.delay(conn.id, [ef.name])
    except Exception as exc:
        raise api_error(503, "queue_unavailable", f"Could not queue the import: {exc}")
    audit.record(db, request, "cloud_connection.events_reimport", principal=principal, object_type="cloud_connection",
                 object_id=conn.id, details={"file": ef.name})
    await db.commit()
    return {"message": "Import queued."}



class EventDayOut(BaseModel):
    day: date
    status: Literal["imported", "failed", "pending", "missing", "waiting", "none"] = Field(
        description="`imported`, `failed` (see `error`), `pending` (being imported), `missing` (due, no file), "
                    "`waiting` (not due yet), `none` (before the source's first file).")
    events: int = 0
    file: Optional[str] = None
    error: Optional[str] = None
    file_id: Optional[str] = Field(None, description="Event file id (`…/event-files/{file_id}/reimport`).")


class EventCalendarOut(BaseModel):
    """One month of an event source, day by day (UTC days)."""
    month: str = Field(examples=["2026-10"])
    days: list[EventDayOut]
    feed: EventFeedOut


@router.get("/{connection_id}/event-calendar", response_model=EventCalendarOut,
            summary="Get the days received / missing of an event source", responses=errors(E_NOT_FOUND, E_NOT_EVENTS))
async def event_calendar(connection_id: ConnectionId, db: AsyncSession = Depends(get_db),
                         month: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}$",
                                                      description="`YYYY-MM` (default: the current month).")):
    """Every day of the month with its file's status and events — gaps in the history at a glance."""
    import calendar as _cal
    conn = await _events_source(db, connection_id)
    now = datetime.utcnow()
    y, m = (int(month[:4]), int(month[5:])) if month else (now.year, now.month)
    if not 1 <= m <= 12:
        raise api_error(422, "invalid_month", "month must be YYYY-MM.")
    start, end = date(y, m, 1), date(y, m, _cal.monthrange(y, m)[1])
    rows = await event_feed.file_rows(db, conn.id, start)
    first = await event_feed.first_day(db, conn.id)
    due = event_feed.expected_until(now, conn.event_alert_hour)
    days = event_feed.day_statuses([r for r in rows if r.day <= end], first, due, start, end)
    feed = (await _feeds(db, [conn])).get(conn.id) or EventFeedOut(alert_hour=_alert_hour(conn))
    return EventCalendarOut(month=f"{y:04d}-{m:02d}", days=[EventDayOut(**d.__dict__) for d in days], feed=feed)
