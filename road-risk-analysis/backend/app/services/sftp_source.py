"""
SFTP server as a cloud source — one more DriveProvider in the cloud-import
pipeline (browse → select → import batch → resumable download with progress
→ job), plus the FileZilla-style connection log and the live link state.

    target = SftpTarget.from_connection(conn)          # what the provider gets instead of an OAuth token
    page   = await PROVIDER.browse(target, "/home/x/video")
    await PROVIDER.download(target, "/home/x/video/a.mp4", dest, offset=…, on_progress=…)

Connections are short-lived and per operation (the CAMTRACK server drops idle
sessions): the API keeps at most one browse session per connection for
IDLE_CLOSE_S (keepalive on), every download opens its own session and closes
it at the end. Timeouts: TCP connect 15 s, banner / key exchange / auth 20 s,
SFTP reads 30 s. Listings retry LIST_MAX_RETRIES times with backoff;
downloads are retried by the import worker (run_item), resuming the `.part`
file at its size (SFTP seek).

Host key: trusted on first use (stored on the connection by the caller via
`persist_credentials`), a different key afterwards is refused.

The password is read from infra/secrets/sftp.env at connect time
(app.services.sftp_core.load_credentials) and dropped after authentication;
it is never logged.
"""
from __future__ import annotations

import asyncio
import json
import logging
import posixpath
import socket
import stat as stat_mod
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional

from app.services import sftp_core as core
from app.services.cloud_providers import (
    MAX_WALK_FILES, PAGE_SIZE, BrowsePage, Crumb, DriveEntry, DriveProvider, TransientCloudError,
)
from app.services.sftp_core import HostKeyMismatch, SftpError, SftpTransientError

logger = logging.getLogger(__name__)

SFTP_PAGE_SIZE = max(PAGE_SIZE, 500)
READ_CHUNK = 256 * 1024
PREFETCH_REQUESTS = 64
PROGRESS_EVERY_S = 0.25


# ── Redis keys ───────────────────────────────────────────────────────────────

def log_key(connection_id: str) -> str:
    return f"cloud:sftp:log:{connection_id}"


def seq_key(connection_id: str) -> str:
    return f"cloud:sftp:logseq:{connection_id}"


def sessions_key(connection_id: str) -> str:
    return f"cloud:sftp:sessions:{connection_id}"


def state_key(connection_id: str) -> str:
    return f"cloud:sftp:state:{connection_id}"


def host_slots_key(host: str, port: int) -> str:
    return f"cloud:sftp:hostslots:{host.lower()}:{port}"


# ── target (the provider's "token") ─────────────────────────────────────────

@dataclass
class SftpTarget:
    """Server profile of a connection — no secret in here."""
    connection_id: str
    host: str
    port: int
    username: str
    base_path: Optional[str]
    password_key: str
    host_key: Optional[str] = None
    learned_host_key: Optional[str] = None      # set when trusted on first use (the caller persists it)
    source: str = "api"                         # api | worker (log attribution)

    @classmethod
    def from_connection(cls, conn, source: str = "api") -> "SftpTarget":
        if not conn.sftp_host or not conn.sftp_username or not conn.sftp_password_key:
            raise SftpError("This SFTP server is missing its host, username or password key — edit it.")
        return cls(connection_id=conn.id, host=conn.sftp_host, port=int(conn.sftp_port or 22),
                   username=conn.sftp_username, base_path=conn.sftp_base_path or None,
                   password_key=conn.sftp_password_key, host_key=conn.sftp_host_key, source=source)

    @property
    def signature(self) -> tuple:
        return (self.host, self.port, self.username, self.base_path, self.password_key)

    @property
    def label(self) -> str:
        return self.host if self.port == 22 else f"{self.host}:{self.port}"


def remember_host_key(conn, target: Any) -> bool:
    """Store a host key trusted on first use. True when the row changed."""
    learned = getattr(target, "learned_host_key", None)
    if not learned or conn.sftp_host_key:
        return False
    kt, b64 = core.split_key(learned)
    conn.sftp_host_key = learned
    conn.sftp_host_key_fp = f"{kt} {core.fingerprint(b64)}"
    conn.sftp_host_key_at = datetime.utcnow()
    target.host_key = learned
    return True


# ── connection log (Redis list, last LOG_MAX_LINES lines) ────────────────────

class SftpLog:
    """Writes log lines and the link state of one connection. Used from
    worker threads (sync Redis client); without Redis it only keeps the
    lines in memory (tests)."""

    def __init__(self, connection_id: str, redis=None, source: str = "api"):
        self.connection_id = connection_id
        self.redis = redis
        self.source = source
        self.lines: list[dict] = []
        self._secrets: tuple = ()

    def hide(self, *values: Optional[str]) -> None:
        self._secrets = tuple(v for v in values if v)

    def append(self, level: str, message: str) -> dict:
        seq = 0
        if self.redis is not None:
            try:
                seq = int(self.redis.incr(seq_key(self.connection_id)))
            except Exception:
                seq = 0
        entry = core.log_entry(level, message, seq=seq, source=self.source, secrets=self._secrets)
        self.lines.append(entry)
        if self.redis is not None:
            try:
                pipe = self.redis.pipeline()
                pipe.rpush(log_key(self.connection_id), json.dumps(entry))
                pipe.ltrim(log_key(self.connection_id), -core.LOG_MAX_LINES, -1)
                pipe.execute()
            except Exception as exc:
                logger.debug(f"sftp log unavailable: {exc}")
        (logger.warning if level == "error" else logger.info)(f"[SFTP {self.connection_id[:8]}] {entry['message']}")
        return entry

    def status(self, message: str) -> None:
        self.append("status", message)

    def error(self, message: str) -> None:
        # The level is the "Error:" label (FileZilla style): keep it out of the text.
        self.append("error", message[7:] if message.startswith("Error: ") else message)

    def state(self, state: str, message: Optional[str] = None) -> None:
        if self.redis is None:
            return
        try:
            self.redis.hset(state_key(self.connection_id), mapping={
                "state": state, "message": message or "", "at": time.time(), "last_used": time.time()})
        except Exception:
            pass

    def touch(self) -> None:
        if self.redis is None:
            return
        try:
            self.redis.hset(state_key(self.connection_id), "last_used", time.time())
        except Exception:
            pass

    # Open sessions of the connection across the API and the workers: the
    # badge shows "connected" while at least one is open.
    def opened(self) -> None:
        if self.redis is None:
            return
        try:
            self.redis.incr(sessions_key(self.connection_id))
            self.redis.expire(sessions_key(self.connection_id), 6 * 3600)
        except Exception:
            pass

    def closed(self) -> None:
        if self.redis is None:
            return
        try:
            left = int(self.redis.decr(sessions_key(self.connection_id)))
            if left <= 0:
                self.redis.delete(sessions_key(self.connection_id))
                if self.redis.hget(state_key(self.connection_id), "state") == "connected":
                    self.state("disconnected")
        except Exception:
            pass


def _sync_redis():
    try:
        from app.services import job_slots
        return job_slots.client()
    except Exception:
        return None


def make_log(connection_id: str, source: str = "api") -> SftpLog:
    return SftpLog(connection_id, _sync_redis(), source)


async def read_log(connection_id: str, after: int = 0, limit: int = core.LOG_MAX_LINES) -> tuple[list[dict], dict]:
    """(lines with seq > after, state hash) — async Redis (API)."""
    from app.services.redis_client import get_redis
    r = get_redis()
    raw = await r.lrange(log_key(connection_id), -core.LOG_MAX_LINES, -1)
    st = await r.hgetall(state_key(connection_id))
    lines = []
    for item in raw:
        try:
            e = json.loads(item)
        except ValueError:
            continue
        if int(e.get("seq") or 0) > after:
            lines.append(e)
    return lines[-limit:], st or {}


async def clear_log(connection_id: str) -> None:
    from app.services.redis_client import get_redis
    await get_redis().delete(log_key(connection_id))


async def forget_state(connection_id: str) -> None:
    from app.services.redis_client import get_redis
    await get_redis().delete(log_key(connection_id), seq_key(connection_id), state_key(connection_id),
                             sessions_key(connection_id))


def link_state(st: dict, now: Optional[float] = None) -> dict:
    """API view of the state hash."""
    now = time.time() if now is None else now
    last = core_float(st.get("last_used"))
    state = core.effective_state(st.get("state") or "disconnected", last, now)
    at = core_float(st.get("at"))
    return {"state": state, "message": (st.get("message") or None) if state == "error" else None,
            "at": datetime.utcfromtimestamp(at).isoformat() + "Z" if at else None}


def core_float(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


# ── session ──────────────────────────────────────────────────────────────────

class SftpSession:
    """One authenticated SSH transport + SFTP channel."""

    def __init__(self, target: SftpTarget, log: SftpLog):
        self.target = target
        self.log = log
        self.transport = None
        self.sftp = None
        self.base: str = "/"
        self.opened_at = time.monotonic()
        self.counted = False

    # connect ------------------------------------------------------------
    def open(self) -> "SftpSession":
        import paramiko
        t = self.target
        log = self.log
        log.state("connecting")
        log.status(f"Connecting to {t.label}...")
        sock = None
        creds = None
        try:
            creds = core.load_credentials(t.password_key)       # read now (file may change), dropped after auth
            try:
                sock = socket.create_connection((t.host, t.port), timeout=core.CONNECT_TIMEOUT_S)
            except Exception as exc:
                kind, msg, transient = core.classify(exc, t.host)
                if kind in ("refused", "unreachable", "network", "timeout"):
                    log.error(f"Error: {msg}")
                    log.error("Error: Could not connect to server")
                    raise SftpTransientError("Could not connect to server: " + msg) from None
                raise
            sock.settimeout(core.BANNER_TIMEOUT_S)
            tr = paramiko.Transport(sock)
            self.transport = tr
            tr.banner_timeout = core.BANNER_TIMEOUT_S
            tr.handshake_timeout = core.BANNER_TIMEOUT_S
            tr.auth_timeout = core.AUTH_TIMEOUT_S
            stored = core.split_key(t.host_key)
            if stored:                                   # ask for the key type we trust
                opts = tr.get_security_options()
                types = list(opts.key_types)
                if stored[0] in types:
                    opts.key_types = [stored[0]] + [k for k in types if k != stored[0]]
            tr.start_client(timeout=core.BANNER_TIMEOUT_S)
            key = tr.get_remote_server_key()
            presented = f"{key.get_name()} {key.get_base64()}"
            decision = core.host_key_decision(t.host_key, presented)
            fp = f"{key.get_name()} {core.fingerprint(key.get_base64())}"
            if decision == "mismatch":
                raise HostKeyMismatch(core.mismatch_message(t.host, t.host_key or "", presented))
            if decision == "new":
                t.learned_host_key = presented
                log.status(f"Host key trusted on first use: {fp}")
            log.status(f"Using username \"{t.username}\".")
            self._authenticate(tr, creds)
            creds = None
            sftp = paramiko.SFTPClient.from_transport(tr)
            sftp.get_channel().settimeout(core.IO_TIMEOUT_S)
            tr.set_keepalive(core.KEEPALIVE_S)
            self.sftp = sftp
            self.base = core.norm_remote(t.base_path) if t.base_path else core.norm_remote(sftp.normalize("."))
            log.status(f"Connected to {t.host}")
            log.state("connected")
            log.opened()
            self.counted = True
            return self
        except Exception as exc:
            if creds is not None:
                creds.password = creds.passphrase = None
            self._abort()
            if isinstance(exc, SftpTransientError):
                log.state("error", str(exc))
                raise
            kind, msg, transient = core.classify(exc, t.host)
            log.error(f"Error: {msg}")
            if kind in ("dropped", "timeout", "dns", "refused", "unreachable", "network", "ssh"):
                log.error("Error: Could not connect to server")
            log.state("error", msg)
            if transient:
                raise SftpTransientError(msg) from None
            raise (exc if isinstance(exc, SftpError) else SftpError(msg)) from None

    def _authenticate(self, tr, creds: core.Credentials) -> None:
        import paramiko
        t = self.target
        try:
            if creds.method == "key":
                pkey = _load_pkey(str(creds.key_path), creds.passphrase)
                tr.auth_publickey(t.username, pkey)
            else:
                self.log.hide(creds.password)
                try:
                    tr.auth_password(t.username, creds.password)
                except paramiko.BadAuthenticationType as exc:
                    if "keyboard-interactive" not in (exc.allowed_types or []):
                        raise
                    pw = creds.password
                    tr.auth_interactive(t.username, lambda title, instr, prompts: [pw for _ in prompts])
            if not tr.is_authenticated():
                raise paramiko.AuthenticationException("Authentication failed.")
        finally:
            creds.password = None
            creds.passphrase = None
            self.log.hide()

    # operations ---------------------------------------------------------
    def alive(self) -> bool:
        return bool(self.transport is not None and self.transport.is_active() and self.sftp is not None)

    def resolve(self, requested: Optional[str]) -> str:
        return core.resolve_in_base(self.base, requested)

    def listdir(self, path: str) -> list:
        self.log.status(f"Retrieving directory listing of \"{path}\"...")
        self.log.status(f"Listing directory {path}")
        attrs = self.sftp.listdir_attr(path)
        self.log.status(f"Directory listing of \"{path}\" successful")
        self.log.touch()
        return attrs

    def entry(self, folder: str, attr) -> DriveEntry:
        path = posixpath.join(folder, attr.filename)
        mode = attr.st_mode or 0
        is_dir = stat_mod.S_ISDIR(mode)
        if stat_mod.S_ISLNK(mode):                      # follow symlinks to tell folders from files
            try:
                real = self.sftp.stat(path)
                is_dir = stat_mod.S_ISDIR(real.st_mode or 0)
                attr = real if not is_dir else attr
            except OSError:
                pass
        return to_entry(self.target, path, attr.filename if hasattr(attr, "filename") else posixpath.basename(path),
                        is_dir, None if is_dir else attr.st_size, attr.st_mtime)

    def close(self, quiet: bool = False) -> None:
        was = self.transport is not None
        self._abort()
        if was and not quiet:
            self.log.status("Disconnected from server")
        if self.counted:
            self.counted = False
            self.log.closed()

    def _abort(self) -> None:
        for obj in (self.sftp, self.transport):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        self.sftp = None
        self.transport = None


def _load_pkey(path: str, passphrase: Optional[str]):
    import paramiko
    errors = []
    for cls_name in ("Ed25519Key", "ECDSAKey", "RSAKey"):
        cls = getattr(paramiko, cls_name, None)
        if cls is None:
            continue
        try:
            return cls.from_private_key_file(path, password=passphrase)
        except paramiko.PasswordRequiredException:
            raise SftpError("The SFTP private key is encrypted: set SFTP_KEY_PASSPHRASE_<KEY> in sftp.env.") from None
        except Exception as exc:             # wrong type → try the next one
            errors.append(type(exc).__name__)
    raise SftpError("Could not read the SFTP private key (supported: ed25519, ecdsa, rsa).")


def to_entry(target: SftpTarget, path: str, name: str, is_dir: bool, size: Optional[int],
             mtime: Optional[float]) -> DriveEntry:
    if is_dir:
        return DriveEntry(id=path, name=name, kind="folder", modified=core.mtime_iso(mtime), media_hint="")
    video = core.is_video_name(name)
    return DriveEntry(
        id=path, name=name, kind="file", mime_type=core.video_mime(name), size=size,
        modified=core.mtime_iso(mtime), version=core.file_version(target.host, target.port, size, mtime),
        media_hint="video" if video else "",
    )


def connect(target: SftpTarget, log: SftpLog) -> SftpSession:
    return SftpSession(target, log).open()


# ── API-side pool: one short-lived browse session per connection ────────────

class _Pooled:
    def __init__(self):
        self.lock = threading.Lock()
        self.session: Optional[SftpSession] = None
        self.signature: Optional[tuple] = None
        self.last_used = 0.0


class SessionPool:
    def __init__(self, idle_s: float = core.IDLE_CLOSE_S, sleep: Callable[[float], None] = time.sleep):
        self.idle_s = idle_s
        self.sleep = sleep
        self._entries: dict[str, _Pooled] = {}
        self._guard = threading.Lock()

    def _entry(self, connection_id: str) -> _Pooled:
        with self._guard:
            return self._entries.setdefault(connection_id, _Pooled())

    def run(self, target: SftpTarget, log: SftpLog, fn: Callable[[SftpSession], Any],
            retries: int = core.LIST_MAX_RETRIES) -> Any:
        """fn(session) on the pooled session (reconnecting when needed),
        retried with backoff on network failures."""
        self.reap()
        e = self._entry(target.connection_id)
        with e.lock:
            attempt = 0
            while True:
                try:
                    if e.session is not None and (e.signature != target.signature or not e.session.alive()):
                        if e.session.transport is not None and not e.session.alive():
                            log.error(f"Error: {core.DROPPED}")
                        e.session.close(quiet=e.signature != target.signature)
                        e.session = None
                    if e.session is None:
                        e.session = SftpSession(target, log).open()
                        e.signature = target.signature
                    e.session.log = log
                    if e.session.target is not target and e.session.target.learned_host_key \
                            and not target.host_key:
                        target.learned_host_key = e.session.target.learned_host_key
                    result = fn(e.session)
                    e.last_used = time.monotonic()
                    log.touch()
                    return result
                except (HostKeyMismatch, SftpError):
                    self._drop(e)
                    raise
                except SftpTransientError:
                    self._drop(e)
                    if attempt >= retries:
                        raise
                except Exception as exc:
                    kind, msg, transient = core.classify(exc, target.host)
                    if not transient:
                        if kind in ("not_found", "denied"):
                            log.error(f"Error: {msg}")
                            e.last_used = time.monotonic()
                            raise SftpError(msg) from None
                        log.error(f"Error: {msg}")
                        self._drop(e, log)
                        raise SftpError(msg) from None
                    log.error(f"Error: {msg}")
                    log.state("error", msg)
                    self._drop(e)
                    if attempt >= retries:
                        raise SftpTransientError(msg) from None
                delay = core.retry_delay_s(attempt)
                attempt += 1
                log.status(f"Retrying in {int(delay)} seconds (attempt {attempt + 1}/{retries + 1})...")
                self.sleep(delay)

    def _drop(self, e: _Pooled, log: Optional[SftpLog] = None) -> None:
        if e.session is not None:
            e.session.close(quiet=log is None)
        e.session = None
        e.signature = None

    def close(self, connection_id: str, log: Optional[SftpLog] = None) -> bool:
        e = self._entry(connection_id)
        with e.lock:
            had = e.session is not None and e.session.alive()
            if e.session is not None:
                if log is not None:
                    e.session.log = log
                e.session.close(quiet=not had)
            e.session = None
            e.signature = None
        return had

    def reap(self) -> None:
        now = time.monotonic()
        with self._guard:
            items = list(self._entries.items())
        for cid, e in items:
            if e.session is None or now - e.last_used <= self.idle_s:
                continue
            if e.lock.acquire(blocking=False):
                try:
                    if e.session is not None and now - e.last_used > self.idle_s:
                        e.session.close()
                        e.session = None
                        e.signature = None
                finally:
                    e.lock.release()


POOL = SessionPool()


# ── provider ─────────────────────────────────────────────────────────────────

def _async_progress(on_progress, loop) -> Optional[Callable[[int], None]]:
    """Call the (possibly async) progress callback of run_item from the
    download thread, at most every PROGRESS_EVERY_S."""
    if on_progress is None:
        return None
    state = {"at": 0.0}

    def cb(done: int, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - state["at"] < PROGRESS_EVERY_S:
            return
        state["at"] = now
        r = on_progress(done)
        if hasattr(r, "__await__"):
            asyncio.run_coroutine_threadsafe(r, loop).result()

    return cb


class SftpProvider(DriveProvider):
    id = "sftp"
    label = "SFTP server"
    auth = "server"
    supports_search = True
    supports_thumbnails = False
    supports_resume = True
    search_recursive = False
    max_transient_retries = core.DOWNLOAD_MAX_RETRIES

    def __init__(self, pool: Optional[SessionPool] = None, log_factory=None):
        self.pool = pool or POOL
        self.log_factory = log_factory or (lambda target: make_log(target.connection_id, target.source))

    def _log(self, target: SftpTarget) -> SftpLog:
        return self.log_factory(target)

    @staticmethod
    def _target(token) -> SftpTarget:
        if not isinstance(token, SftpTarget):
            raise SftpError("Invalid SFTP connection.")
        return token

    # browsing -----------------------------------------------------------
    def _list_sync(self, target: SftpTarget, folder_id: Optional[str]) -> tuple[str, list[DriveEntry]]:
        log = self._log(target)

        def op(s: SftpSession):
            path = s.resolve(folder_id)
            return s.base, path, [s.entry(path, a) for a in s.listdir(path)]

        base, path, entries = self.pool.run(target, log, op)
        target._base = base                                    # for path() without a round trip
        return path, entries

    async def browse(self, token, folder_id, *, page_token=None, query=None, client=None) -> BrowsePage:
        target = self._target(token)
        _, entries = await asyncio.to_thread(self._list_sync, target, folder_id)
        if query and query.strip():
            q = query.strip().casefold()
            entries = [e for e in entries if q in e.name.casefold()]
        entries.sort(key=lambda e: (e.kind != "folder", e.name.casefold()))
        try:
            start = max(0, int(page_token)) if page_token else 0
        except ValueError:
            start = 0
        page = entries[start:start + SFTP_PAGE_SIZE]
        nxt = str(start + SFTP_PAGE_SIZE) if start + SFTP_PAGE_SIZE < len(entries) else None
        return BrowsePage(page, nxt)

    async def _base(self, target: SftpTarget) -> str:
        base = getattr(target, "_base", None)
        if base:
            return base
        if target.base_path:
            return core.norm_remote(target.base_path)
        base = await asyncio.to_thread(self.pool.run, target, self._log(target), lambda s: s.base)
        target._base = base
        return base

    async def path(self, token, folder_id, client=None) -> list[Crumb]:
        target = self._target(token)
        if not folder_id or folder_id == "root":
            return []
        base = await self._base(target)
        return [Crumb(p, n) for p, n in core.crumbs_below(base, folder_id)]

    async def metadata(self, token, file_id, client=None) -> DriveEntry:
        target = self._target(token)

        def op(s: SftpSession) -> DriveEntry:
            path = s.resolve(file_id)
            a = s.sftp.stat(path)
            return to_entry(target, path, posixpath.basename(path) or path, stat_mod.S_ISDIR(a.st_mode or 0),
                            a.st_size, a.st_mtime)

        return await asyncio.to_thread(self.pool.run, target, self._log(target), op)

    def _download_request(self, file_id):          # pragma: no cover — SFTP downloads do not use HTTP
        raise NotImplementedError

    # walking (fresh session, one per selection) --------------------------
    def _walk(self, s: SftpSession, folder: str, recursive: bool, max_depth: int, max_files: int,
              found: list, seen: set, depth: int = 0) -> None:
        if len(found) >= max_files or folder in seen:
            return
        seen.add(folder)
        entries = [s.entry(folder, a) for a in s.listdir(folder)]
        for e in sorted(entries, key=lambda x: x.name.casefold()):
            if e.kind == "file" and e.media:
                e.path = core.relative_dir(s.base, folder)
                found.append(e)
                if len(found) >= max_files:
                    return
        if recursive and depth < max_depth:
            for e in sorted(entries, key=lambda x: x.name.casefold()):
                if e.kind == "folder":
                    self._walk(s, e.id, recursive, max_depth, max_files, found, seen, depth + 1)

    def _with_fresh(self, target: SftpTarget, fn: Callable[[SftpSession], Any]) -> Any:
        log = self._log(target)
        attempt = 0
        while True:
            s = SftpSession(target, log)
            try:
                s.open()
                return fn(s)
            except (SftpError, HostKeyMismatch):
                raise
            except Exception as exc:
                kind, msg, transient = core.classify(exc, target.host)
                if not isinstance(exc, SftpTransientError):
                    log.error(f"Error: {msg}")
                if not transient or attempt >= core.LIST_MAX_RETRIES:
                    raise (SftpTransientError(msg) if transient else SftpError(msg)) from None
                delay = core.retry_delay_s(attempt)
                attempt += 1
                log.status(f"Retrying in {int(delay)} seconds (attempt {attempt + 1}/{core.LIST_MAX_RETRIES + 1})...")
                time.sleep(delay)
            finally:
                s.close(quiet=s.transport is None)

    async def walk_media(self, token, folder_id, *, recursive=True, max_depth=10, max_files=MAX_WALK_FILES,
                         base_path="", client=None) -> list[DriveEntry]:
        target = self._target(token)

        def op(s: SftpSession) -> list[DriveEntry]:
            found: list[DriveEntry] = []
            self._walk(s, s.resolve(folder_id), recursive, max_depth, max_files, found, set())
            return found

        return await asyncio.to_thread(self._with_fresh, target, op)

    async def resolve_selection(self, token, files: list[str], folder_ids: list[str], *, include_subfolders: bool,
                                max_files: int, client=None) -> list[DriveEntry]:
        """Files + media of the folders, all through one session."""
        target = self._target(token)

        def op(s: SftpSession) -> list[DriveEntry]:
            out: list[DriveEntry] = []
            s.log.status(f"Checking the selection: {len(files)} file(s), {len(folder_ids)} folder(s)"
                         + (" with sub-folders" if folder_ids and include_subfolders else ""))
            for fid in dict.fromkeys(files):
                path = s.resolve(fid)
                a = s.sftp.stat(path)
                if stat_mod.S_ISDIR(a.st_mode or 0):
                    continue
                e = to_entry(target, path, posixpath.basename(path), False, a.st_size, a.st_mtime)
                e.path = core.relative_dir(s.base, posixpath.dirname(path))
                out.append(e)
            for fid in dict.fromkeys(folder_ids):
                self._walk(s, s.resolve(fid), include_subfolders, 10, max_files + 1 - len(out), out, set())
                if len(out) > max_files:
                    break
            return out

        return await asyncio.to_thread(self._with_fresh, target, op)

    # download -------------------------------------------------------------
    def _download_sync(self, target: SftpTarget, file_id: str, dest: str, offset: int,
                       progress: Optional[Callable[..., None]]) -> int:
        log = self._log(target)
        s = SftpSession(target, log)
        try:
            s.open()
            path = s.resolve(file_id)
            size = s.sftp.stat(path).st_size
            start = core.resume_offset(offset, size)
            if size is not None and 0 < size == start:
                log.status(f"{path} is already complete ({core.human_size(size)})")
                return start
            if start:
                log.status(f"Resuming download of {path} at {core.human_size(start)} of {core.human_size(size)}")
            else:
                log.status(f"Starting download of {path}")
            t0 = time.monotonic()
            done = start
            touched = time.monotonic()
            with s.sftp.open(path, "rb") as rf, open(dest, "ab" if start else "wb") as fh:
                rf.settimeout(core.IO_TIMEOUT_S)
                if start:
                    rf.seek(start)
                rf.prefetch(size, max_concurrent_requests=PREFETCH_REQUESTS)
                while True:
                    chunk = rf.read(READ_CHUNK)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if progress is not None:
                        progress(done)
                    if time.monotonic() - touched > 5:
                        touched = time.monotonic()
                        log.touch()
                fh.flush()
            if progress is not None:
                progress(done, True)
            log.status(core.transfer_summary(done - start, time.monotonic() - t0))
            return done
        except (SftpError, SftpTransientError):
            raise
        except Exception as exc:
            if type(exc).__name__ == "ImportCancelled":
                log.status("Transfer cancelled by the user")
                raise
            kind, msg, transient = core.classify(exc, target.host)
            log.error(f"Error: {msg}")
            if kind in ("dropped", "timeout"):
                log.error("Error: File transfer failed after transferring "
                          f"{core.human_size(_size(dest))}")
            log.state("error", msg)
            if transient:
                raise SftpTransientError(msg) from None
            raise SftpError(msg) from None
        finally:
            s.close(quiet=s.transport is None)

    async def download(self, token, file_id, dest, *, offset=0, on_progress=None, client=None) -> int:
        target = self._target(token)
        cb = _async_progress(on_progress, asyncio.get_running_loop())
        try:
            return await asyncio.to_thread(self._download_sync, target, file_id, dest, offset, cb)
        except SftpTransientError as exc:
            raise TransientCloudError(str(exc)) from None

    def note_retry(self, token, retry: int, delay: float, offset: int) -> None:
        """Called by the import worker before it retries a failed download."""
        if isinstance(token, SftpTarget):
            self._log(token).status(f"Retrying in {int(delay)} seconds (attempt {retry + 1}/"
                                    f"{self.max_transient_retries}), will resume at {core.human_size(offset)}...")

    def retry_delay(self, retry: int) -> float:
        return core.retry_delay_s(retry)

    async def thumbnail(self, token, file_id, client=None):
        return None

    def persist_credentials(self, conn, token) -> bool:
        return remember_host_key(conn, token)

    # API actions ------------------------------------------------------------
    def test_sync(self, target: SftpTarget) -> dict:
        """Connect / Reconnect: a fresh pooled session + listing of the base folder."""
        log = self._log(target)
        self.pool.close(target.connection_id, log)

        def op(s: SftpSession) -> dict:
            n = len(s.listdir(s.base))
            return {"base_path": s.base, "entries": n}

        return self.pool.run(target, log, op, retries=0)

    def describe(self) -> dict:
        return {**super().describe(), "auth": self.auth}


def _size(path: str) -> int:
    try:
        import os
        return os.path.getsize(path)
    except OSError:
        return 0


PROVIDER = SftpProvider()
