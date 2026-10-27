"""
Google Drive / OneDrive access for cloud storage sources (RRAP-07-002/004).

Plain OAuth 2.0 authorization-code flow and REST calls over httpx — no
provider SDK. Every network function takes an optional `client`
(httpx.AsyncClient) so tests can inject an httpx.MockTransport.

    authorize_url → (browser consent) → exchange_code → refresh token stored
    access_token(refresh) → list_folders / list_videos / download
"""
import hashlib
import hmac
import logging
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import urlencode

import httpx

from app.core.config import settings
from app.core.settings_service import CloudSettings, cloud_redirect_uri

logger = logging.getLogger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_SCOPE = "https://www.googleapis.com/auth/drive.readonly openid email"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"

MS_AUTH_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
MS_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
# Files.Read.All: also the folders other people shared with the account (OneDrive for Business /
# SharePoint) — accounts connected with the former Files.Read must reconnect to see them.
MS_SCOPE = "offline_access Files.Read.All User.Read"
GRAPH_URL = "https://graph.microsoft.com/v1.0"

STATE_TTL_S = 15 * 60
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
VIDEO_EXTS = (".mp4", ".avi", ".mov", ".mkv", ".m4v", ".webm", ".mpg", ".mpeg", ".ts")


class CloudError(Exception):
    """Provider refused or failed a call — message is shown to the admin."""


@dataclass
class CloudFile:
    id: str
    name: str
    mime_type: Optional[str] = None
    size: Optional[int] = None
    modified: Optional[str] = None


@dataclass
class Tokens:
    access_token: str
    refresh_token: Optional[str] = None   # None when the provider did not rotate it


# ── OAuth state (signed connection id + nonce, nonce kept in Redis 15 min) ──

def _sign(payload: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]


def make_state(connection_id: str) -> tuple[str, str]:
    """Returns (state, nonce). The caller stores the nonce in Redis."""
    nonce = secrets.token_urlsafe(16)
    payload = f"{connection_id}.{nonce}"
    return f"{payload}.{_sign(payload)}", nonce


def parse_state(state: str) -> Optional[tuple[str, str]]:
    """(connection_id, nonce) when the signature is valid, else None."""
    try:
        connection_id, nonce, sig = state.rsplit(".", 2)
    except (ValueError, AttributeError):
        return None
    if not hmac.compare_digest(sig, _sign(f"{connection_id}.{nonce}")):
        return None
    return connection_id, nonce


def state_key(nonce: str) -> str:
    return f"cloud:oauth:{nonce}"


# ── provider config ───────────────────────────────────────────────────────────

def credentials(provider: str, cfg: CloudSettings) -> tuple[str, str]:
    if provider == "google_drive":
        cid, secret = cfg.google_client_id, cfg.google_client_secret
    elif provider == "onedrive":
        cid, secret = cfg.microsoft_client_id, cfg.microsoft_client_secret
    else:
        raise CloudError(f"Unknown provider '{provider}'.")
    if not cid or not secret:
        raise CloudError(f"OAuth client for {provider} is not configured (Settings → Platform → Cloud storage).")
    return cid, secret


def authorize_url(provider: str, cfg: CloudSettings, state: str) -> str:
    cid, _ = credentials(provider, cfg)
    if provider == "google_drive":
        return GOOGLE_AUTH_URL + "?" + urlencode({
            "client_id": cid, "redirect_uri": cloud_redirect_uri(), "response_type": "code",
            "scope": GOOGLE_SCOPE, "access_type": "offline", "prompt": "consent",
            "include_granted_scopes": "true", "state": state,
        })
    return MS_AUTH_URL.format(tenant=cfg.microsoft_tenant or "common") + "?" + urlencode({
        "client_id": cid, "redirect_uri": cloud_redirect_uri(), "response_type": "code",
        "response_mode": "query", "scope": MS_SCOPE, "state": state,
    })


def _token_url(provider: str, cfg: CloudSettings) -> str:
    return GOOGLE_TOKEN_URL if provider == "google_drive" else MS_TOKEN_URL.format(tenant=cfg.microsoft_tenant or "common")


@asynccontextmanager
async def _client(client: Optional[httpx.AsyncClient]):
    if client is not None:
        yield client
        return
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as c:
        yield c


def _raise_for(resp: httpx.Response, what: str) -> None:
    if resp.status_code < 400:
        return
    try:
        body = resp.json()
        err = body.get("error")
        detail = (body.get("error_description") or (err.get("message") if isinstance(err, dict) else err)
                  or resp.text)
    except Exception:
        detail = resp.text
    raise CloudError(f"{what} failed ({resp.status_code}): {str(detail)[:300]}")


async def _token_request(provider: str, cfg: CloudSettings, data: dict, what: str,
                         client: Optional[httpx.AsyncClient]) -> Tokens:
    cid, secret = credentials(provider, cfg)
    payload = {"client_id": cid, "client_secret": secret, **data}
    if provider == "onedrive":
        payload["scope"] = MS_SCOPE
    async with _client(client) as c:
        resp = await c.post(_token_url(provider, cfg), data=payload)
    _raise_for(resp, what)
    body = resp.json()
    if not body.get("access_token"):
        raise CloudError(f"{what} failed: no access token returned.")
    return Tokens(body["access_token"], body.get("refresh_token"))


async def exchange_code(provider: str, cfg: CloudSettings, code: str,
                        client: Optional[httpx.AsyncClient] = None) -> Tokens:
    tokens = await _token_request(provider, cfg, {
        "grant_type": "authorization_code", "code": code, "redirect_uri": cloud_redirect_uri(),
    }, "Authorization code exchange", client)
    if not tokens.refresh_token:
        raise CloudError("The provider did not return a refresh token — remove the app's access in "
                         "your account settings and connect again.")
    return tokens


async def refresh(provider: str, cfg: CloudSettings, refresh_token: str,
                  client: Optional[httpx.AsyncClient] = None) -> Tokens:
    return await _token_request(provider, cfg, {
        "grant_type": "refresh_token", "refresh_token": refresh_token,
    }, "Token refresh", client)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def account_email(provider: str, access_token: str, client: Optional[httpx.AsyncClient] = None) -> Optional[str]:
    async with _client(client) as c:
        if provider == "google_drive":
            resp = await c.get(GOOGLE_USERINFO_URL, headers=_auth(access_token))
            _raise_for(resp, "Account lookup")
            return resp.json().get("email")
        resp = await c.get(f"{GRAPH_URL}/me", headers=_auth(access_token))
        _raise_for(resp, "Account lookup")
        body = resp.json()
        return body.get("mail") or body.get("userPrincipalName")


# ── listing ──────────────────────────────────────────────────────────────────

def is_video(f: CloudFile) -> bool:
    if f.mime_type:
        return f.mime_type.lower().startswith("video/")
    return f.name.lower().endswith(VIDEO_EXTS)


def select_new_files(files: Iterable[CloudFile], imported_refs: Iterable[str], limit: Optional[int] = None) -> list[CloudFile]:
    """Video files not imported yet (by provider file id), oldest first, at most `limit`."""
    seen = set(imported_refs)
    out: list[CloudFile] = []
    for f in sorted(files, key=lambda x: (x.modified or "", x.name)):
        if f.id in seen or not is_video(f):
            continue
        seen.add(f.id)
        out.append(f)
        if limit is not None and len(out) >= limit:
            break
    return out


def _google_q_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


async def _google_list(token: str, q: str, client: Optional[httpx.AsyncClient]) -> list[CloudFile]:
    files: list[CloudFile] = []
    page_token = None
    async with _client(client) as c:
        while True:
            params = {"q": q, "fields": "nextPageToken,files(id,name,mimeType,size,modifiedTime)",
                      "pageSize": 200, "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
            if page_token:
                params["pageToken"] = page_token
            resp = await c.get(GOOGLE_FILES_URL, params=params, headers=_auth(token))
            _raise_for(resp, "Google Drive listing")
            body = resp.json()
            for f in body.get("files", []):
                files.append(CloudFile(f["id"], f.get("name") or f["id"], f.get("mimeType"),
                                       int(f["size"]) if f.get("size") else None, f.get("modifiedTime")))
            page_token = body.get("nextPageToken")
            if not page_token:
                return files


async def _graph_children(token: str, folder_id: Optional[str], client: Optional[httpx.AsyncClient]) -> list[dict]:
    url = (f"{GRAPH_URL}/me/drive/items/{folder_id}/children" if folder_id and folder_id != "root"
           else f"{GRAPH_URL}/me/drive/root/children")
    items: list[dict] = []
    params: Optional[dict] = {"$top": 200}
    async with _client(client) as c:
        while url:
            resp = await c.get(url, params=params, headers=_auth(token))
            _raise_for(resp, "OneDrive listing")
            body = resp.json()
            items.extend(body.get("value", []))
            url, params = body.get("@odata.nextLink"), None
    return items


async def list_folders(provider: str, token: str, parent: Optional[str] = None,
                       client: Optional[httpx.AsyncClient] = None) -> list[dict]:
    """Sub-folders of `parent` (None = drive root) as [{id, name}]."""
    if provider == "google_drive":
        q = (f"'{_google_q_quote(parent or 'root')}' in parents and mimeType = '{GOOGLE_FOLDER_MIME}' "
             f"and trashed = false")
        files = await _google_list(token, q, client)
    else:
        files = [CloudFile(i["id"], i.get("name") or i["id"]) for i in await _graph_children(token, parent, client)
                 if "folder" in i]
    return sorted(({"id": f.id, "name": f.name} for f in files), key=lambda x: x["name"].lower())


async def list_videos(provider: str, token: str, folder_id: Optional[str],
                      client: Optional[httpx.AsyncClient] = None) -> list[CloudFile]:
    if provider == "google_drive":
        q = (f"'{_google_q_quote(folder_id or 'root')}' in parents and mimeType contains 'video/' "
             f"and trashed = false")
        return [f for f in await _google_list(token, q, client) if is_video(f)]
    out = []
    for i in await _graph_children(token, folder_id, client):
        mime = (i.get("file") or {}).get("mimeType")
        if not mime:
            continue   # folders and other non-file items
        f = CloudFile(i["id"], i.get("name") or i["id"], mime, i.get("size"), i.get("lastModifiedDateTime"))
        if is_video(f):
            out.append(f)
    return out


async def download(provider: str, token: str, file_id: str, dest: str,
                   client: Optional[httpx.AsyncClient] = None) -> int:
    """Stream the file to `dest`. Returns the byte count."""
    url = (f"{GOOGLE_FILES_URL}/{file_id}" if provider == "google_drive"
           else f"{GRAPH_URL}/me/drive/items/{file_id}/content")
    params = {"alt": "media", "supportsAllDrives": "true"} if provider == "google_drive" else None
    size = 0
    async with _client(client) as c:
        async with c.stream("GET", url, params=params, headers=_auth(token)) as resp:
            if resp.status_code >= 400:
                await resp.aread()
                _raise_for(resp, "Download")
            with open(dest, "wb") as fh:
                async for chunk in resp.aiter_bytes(1 << 20):
                    fh.write(chunk)
                    size += len(chunk)
    return size


# ── connection helpers (API + worker) ────────────────────────────────────────

async def connection_token(db, conn, cfg: Optional[CloudSettings] = None,
                           client: Optional[httpx.AsyncClient] = None) -> str:
    """Fresh access token for a CloudConnection; stores a rotated refresh
    token (Microsoft rotates them). The caller commits. For an SFTP server the
    "token" is its profile (app.modules.m07_ingestion.sftp_source.SftpTarget, no secret):
    the password is read from the secrets file when the provider connects."""
    if conn.provider == "sftp":
        from app.modules.m07_ingestion.sftp_source import SftpTarget
        return SftpTarget.from_connection(conn, source="worker" if _in_worker() else "api")  # type: ignore[return-value]
    from app.core.security import decrypt_secret, encrypt_secret
    from app.core.settings_service import get_section
    refresh_token = decrypt_secret(conn.refresh_token_enc)
    if not refresh_token:
        raise CloudError("This connection is not authorised — connect the account again.")
    cfg = cfg or await get_section(db, "cloud")  # type: ignore[assignment]
    tokens = await refresh(conn.provider, cfg, refresh_token, client)
    if tokens.refresh_token and tokens.refresh_token != refresh_token:
        conn.refresh_token_enc = encrypt_secret(tokens.refresh_token)
    return tokens.access_token


def _in_worker() -> bool:
    import sys
    return any("celery" in (a or "") for a in sys.argv[:1])
