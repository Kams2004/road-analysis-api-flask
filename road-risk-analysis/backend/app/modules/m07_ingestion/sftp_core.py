"""
SFTP video source — the pure rules, importable without paramiko (unit-tested
in the light venv): secrets file parsing, remote path safety, the video
filter, the dedupe key, resume offsets, host-key trust on first use, error
classification and the FileZilla-style connection log lines.

The network side (paramiko sessions, the provider, the log store) lives in
app.modules.m07_ingestion.sftp_source.

Secrets: the password typed in the web console is stored encrypted on the
connection (`sftp_password_enc`, app.core.security.encrypt_secret — Fernet keyed
from SECRET_KEY) and decrypted only at connect time (`saved_credentials`).
A connection may instead name a *password key* (e.g. CAMTRACK): the password is
then read at connect time from the secrets file `<secrets dir>/sftp.env`
(infra/secrets/sftp.env, mounted read-only at /app/secrets in the containers):

    SFTP_PASSWORD_CAMTRACK=…
    # or key-based auth (file inside the same directory):
    SFTP_KEYFILE_CAMTRACK=camtrack_ed25519
    SFTP_KEY_PASSPHRASE_CAMTRACK=…          (optional)

An environment variable of the same name is used when the file has no entry
(handy for a throwaway local test). The value is never logged, returned by
the API or kept after the connection is authenticated.
"""
from __future__ import annotations

import base64
import errno
import hashlib
import os
import posixpath
import re
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional

from app.modules.m07_ingestion.cloud_storage import CloudError

# ── constants ────────────────────────────────────────────────────────────────

SFTP_VIDEO_EXTS = (".mp4", ".mov", ".avi", ".mkv", ".h264", ".ts")
SECRETS_FILE = "sftp.env"
KEY_RE = re.compile(r"^[A-Z0-9_]{1,64}$")
LOG_MAX_LINES = 500
LEVELS = ("status", "error")

CONNECT_TIMEOUT_S = 15
BANNER_TIMEOUT_S = 20
AUTH_TIMEOUT_S = 20
IO_TIMEOUT_S = 30
KEEPALIVE_S = 15
IDLE_CLOSE_S = 180                 # pooled browse session closed after this idle time
PER_HOST_DOWNLOADS = 2             # polite: at most 2 parallel downloads per server
DOWNLOAD_MAX_RETRIES = 8           # transient failures in a row (reset when bytes progressed)
LIST_MAX_RETRIES = 3

LINK_STATES = ("disconnected", "connecting", "connected", "error")


class SftpError(CloudError):
    """A permanent SFTP failure (authentication, host key, missing password,
    path refused …) — retrying will not help until something is fixed."""


class HostKeyMismatch(SftpError):
    """The server presents another host key than the one trusted."""


class SftpTransientError(CloudError):
    """Network failure / dropped connection / timeout: retry with backoff."""


# ── secrets ──────────────────────────────────────────────────────────────────

def normalize_key(name: Optional[str]) -> str:
    """'camtrack' / 'cam-track' → 'CAMTRACK' / 'CAM_TRACK'; refuses anything
    that is not a safe environment-variable suffix."""
    key = re.sub(r"[\s\-.]+", "_", (name or "").strip()).upper()
    # Typed as the full variable name ("SFTP_PASSWORD_CAMTRACK"): keep the suffix only.
    key = re.sub(r"^SFTP_(?:PASSWORD|KEYFILE|KEY_PASSPHRASE)_(?=.)", "", key)
    if not KEY_RE.match(key):
        raise ValueError("The password key may only contain letters, digits and _ (max 64), e.g. CAMTRACK.")
    return key


def password_var(key: str) -> str:
    return f"SFTP_PASSWORD_{key}"


def keyfile_var(key: str) -> str:
    return f"SFTP_KEYFILE_{key}"


def passphrase_var(key: str) -> str:
    return f"SFTP_KEY_PASSPHRASE_{key}"


def parse_env(text: str) -> dict[str, str]:
    """KEY=VALUE lines; `#` comments, blank lines, an optional `export ` and
    matching single/double quotes around the value. The value is taken
    verbatim otherwise (a password may contain `#`, `=` or spaces)."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, sep, value = line.partition("=")
        name = name.strip()
        if not sep or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[name] = value
    return out


def secrets_dir() -> Path:
    """VIASAFE_SECRETS_DIR, else <app root>/secrets (= /app/secrets in the containers,
    where infra/secrets is mounted), else infra/secrets of the checkout (local run)."""
    env = os.getenv("VIASAFE_SECRETS_DIR")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    mounted = here.parents[3] / "secrets"
    if mounted.is_dir() or len(here.parents) < 5:
        return mounted
    checkout = here.parents[4] / "infra" / "secrets"
    return checkout if checkout.is_dir() else mounted


def safe_secret_file(directory: Path, name: str) -> Path:
    """A key file named in sftp.env must sit inside the secrets directory."""
    if not name or "/" in name or "\\" in name or name in (".", "..") or name.startswith("."):
        raise SftpError("SFTP key file: give a plain file name inside infra/secrets (no path).")
    return directory / name


@dataclass
class Credentials:
    method: str                                         # password | key
    password: Optional[str] = field(default=None, repr=False)
    key_path: Optional[Path] = None
    passphrase: Optional[str] = field(default=None, repr=False)

    def __repr__(self) -> str:                          # never print a secret by accident
        return f"Credentials(method={self.method!r}, key_path={self.key_path!s})"

    __str__ = __repr__


def _read_values(directory: Path) -> dict[str, str]:
    path = directory / SECRETS_FILE
    try:
        return parse_env(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except PermissionError:
        raise SftpError(f"Cannot read {SECRETS_FILE} in the secrets directory (permission denied).") from None


def load_credentials(key: str, directory: Optional[Path] = None,
                     environ: Optional[Mapping[str, str]] = None) -> Credentials:
    """Read the credentials of password key `key` (at connect time)."""
    key = normalize_key(key)
    directory = directory or secrets_dir()
    environ = os.environ if environ is None else environ
    values = _read_values(directory)
    get = lambda var: values.get(var) or environ.get(var) or None  # noqa: E731
    keyfile = get(keyfile_var(key))
    if keyfile:
        path = safe_secret_file(directory, keyfile)
        if not path.is_file():
            raise SftpError(f"SFTP key file '{keyfile}' named by {keyfile_var(key)} is missing from infra/secrets.")
        return Credentials("key", key_path=path, passphrase=get(passphrase_var(key)))
    password = get(password_var(key))
    if not password:
        raise SftpError(f"No password set for this server: add {password_var(key)}=… to infra/secrets/{SECRETS_FILE} "
                        f"(see docs/SFTP-SOURCE.md).")
    return Credentials("password", password=password)


def saved_credentials(password_enc: str) -> Credentials:
    """Decrypt the password saved from the web console (at connect time)."""
    from app.core.security import decrypt_secret
    password = decrypt_secret(password_enc)
    if not password:
        raise SftpError("The saved password can no longer be read (the server's SECRET_KEY changed): "
                        "edit the server and enter the password again.")
    return Credentials("password", password=password)


def credentials_status(key: Optional[str], directory: Optional[Path] = None,
                       environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """"password" / "key" when the secret is present, else None (never the value)."""
    try:
        return load_credentials(key or "", directory, environ).method
    except (ValueError, CloudError):
        return None


# ── remote paths ─────────────────────────────────────────────────────────────

def norm_remote(path: str) -> str:
    """Absolute, normalised POSIX path ('/a/b/../c//' → '/a/c')."""
    if path is None or "\x00" in path:
        raise SftpError("Invalid path.")
    p = posixpath.normpath("/" + path.strip().lstrip("/")) if path.strip() else "/"
    return "/" if p in ("//", "/.") else p.replace("//", "/")


def within(base: str, path: str) -> bool:
    base = norm_remote(base)
    path = norm_remote(path)
    return base == "/" or path == base or path.startswith(base.rstrip("/") + "/")


def resolve_in_base(base: str, requested: Optional[str]) -> str:
    """The absolute path for `requested` (absolute, or relative to `base`),
    refused when it leaves `base` (no `..` escape)."""
    base = norm_remote(base)
    if requested is None or requested in ("", "root"):
        return base
    if "\x00" in requested:
        raise SftpError("Invalid path.")
    joined = requested if requested.startswith("/") else posixpath.join(base, requested)
    path = posixpath.normpath(joined)
    path = "/" if path in ("//", ".") else path
    if path.startswith("//"):
        path = "/" + path.lstrip("/")
    if not within(base, path):
        raise SftpError(f'"{requested}" is outside the base folder of this server ({base}).')
    return path


def crumbs_below(base: str, path: str) -> list[tuple[str, str]]:
    """(path, name) of each folder from `base` (excluded) down to `path`."""
    base = norm_remote(base)
    path = resolve_in_base(base, path)
    if path == base:
        return []
    rel = path[len(base):].strip("/") if base != "/" else path.strip("/")
    out: list[tuple[str, str]] = []
    cur = base
    for part in rel.split("/"):
        cur = posixpath.join(cur, part)
        out.append((cur, part))
    return out


def relative_dir(base: str, folder: str) -> Optional[str]:
    """Folder path shown for a walked file ('video/2026'), None at the base."""
    base, folder = norm_remote(base), norm_remote(folder)
    if folder == base:
        return None
    return folder[len(base):].strip("/") if base != "/" else folder.strip("/")


# ── files ────────────────────────────────────────────────────────────────────

def is_video_name(name: str) -> bool:
    """Only these videos can be imported from an SFTP server (case-insensitive)."""
    return posixpath.splitext((name or "").lower())[1] in SFTP_VIDEO_EXTS


def video_mime(name: str) -> Optional[str]:
    ext = posixpath.splitext((name or "").lower())[1]
    return {".mp4": "video/mp4", ".mov": "video/quicktime", ".avi": "video/x-msvideo", ".mkv": "video/x-matroska",
            ".h264": "video/h264", ".ts": "video/mp2t"}.get(ext)


def mtime_iso(mtime: Optional[float]) -> Optional[str]:
    if mtime is None:
        return None
    return datetime.fromtimestamp(int(mtime), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def file_version(host: str, port: int, size: Optional[int], mtime: Optional[float]) -> str:
    """Version stored with an import item: same size + mtime on the same
    server = same file (with the path as file id → dedupe by host+path+size+mtime)."""
    return f"{size if size is not None else '?'}:{int(mtime) if mtime is not None else '?'}@{host.lower()}:{port}"


def dedupe_key(host: str, port: int, path: str, size: Optional[int], mtime: Optional[float]) -> str:
    return f"sftp://{host.lower()}:{port}{norm_remote(path)}#{file_version(host, port, size, mtime).split('@')[0]}"


def resume_offset(part_size: int, remote_size: Optional[int]) -> int:
    """Where to resume a download: the partial file's size, or 0 when it is
    larger than the remote file (the file changed — start over)."""
    if part_size <= 0:
        return 0
    if remote_size is not None and part_size > remote_size:
        return 0
    return part_size


def retry_delay_s(retry: int) -> float:
    """2, 4, 8, 16, 30, 30 … s."""
    return float(min(30, 2 ** (retry + 1)))


# ── host key (trust on first use) ────────────────────────────────────────────

def fingerprint(key_b64: str) -> str:
    """OpenSSH-style SHA256 fingerprint of a base64 public key blob."""
    blob = base64.b64decode(key_b64)
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def split_key(line: Optional[str]) -> Optional[tuple[str, str]]:
    if not line:
        return None
    parts = line.strip().split()
    return (parts[0], parts[1]) if len(parts) >= 2 else None


def host_key_decision(stored: Optional[str], presented: str) -> str:
    """"new" (nothing trusted yet → trust it), "match" or "mismatch"."""
    have = split_key(stored)
    got = split_key(presented)
    if got is None:
        return "mismatch"
    if have is None:
        return "new"
    return "match" if have == got else "mismatch"


def mismatch_message(host: str, stored: str, presented: str) -> str:
    s, p = split_key(stored), split_key(presented)
    return (f"Host key mismatch for {host}: expected {s[0]} {fingerprint(s[1])}, the server presented "
            f"{p[0]} {fingerprint(p[1])}. Refusing to connect — if the server key was legitimately changed, "
            f"use \"Forget host key\" and connect again.")


# ── errors ───────────────────────────────────────────────────────────────────

DROPPED = "FATAL ERROR: Remote side unexpectedly closed network connection"


def classify(exc: BaseException, host: str = "", connect_timeout: int = CONNECT_TIMEOUT_S) -> tuple[str, str, bool]:
    """(kind, log message, transient) for an exception raised while talking
    to the server. Works on class names so paramiko is not needed here."""
    if isinstance(exc, HostKeyMismatch):
        return "host_key", str(exc), False
    if isinstance(exc, SftpError):
        return "config", str(exc), False
    if isinstance(exc, SftpTransientError):
        return "network", str(exc), True
    name = type(exc).__name__
    text = str(exc) or name
    if name in ("AuthenticationException", "BadAuthenticationType", "PartialAuthentication"):
        return "auth", "Authentication failed. Check the username and the password.", False
    if isinstance(exc, socket.gaierror):
        return "dns", f"Could not resolve host \"{host}\": {exc.strerror or text}", True
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "timeout", f"Connection timed out after {connect_timeout} seconds of inactivity", True
    if isinstance(exc, ConnectionRefusedError):
        return "refused", "Could not connect to server: Connection refused", True
    if isinstance(exc, (EOFError, ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
        return "dropped", DROPPED, True
    if isinstance(exc, OSError) and exc.errno in (errno.EHOSTUNREACH, errno.ENETUNREACH):
        return "unreachable", f"Could not connect to server: {exc.strerror or text}", True
    if name in ("SSHException", "ChannelException", "ProxyCommandFailure"):
        low = text.lower()
        if "banner" in low or "closed" in low or "eof" in low or "reset" in low or "not open" in low \
                or "dropped" in low or "socket" in low:
            return "dropped", DROPPED, True
        if "timeout" in low or "timed out" in low:
            return "timeout", f"Connection timed out: {text}", True
        return "ssh", f"SSH error: {text}", True
    if isinstance(exc, IOError) and getattr(exc, "errno", None) == errno.ENOENT:
        return "not_found", f"No such file or directory: {getattr(exc, 'filename', '') or text}", False
    if isinstance(exc, PermissionError) or (isinstance(exc, IOError) and getattr(exc, "errno", None) == errno.EACCES):
        return "denied", f"Permission denied: {getattr(exc, 'filename', '') or text}", False
    if isinstance(exc, OSError):
        if "socket is closed" in text.lower() or "closed" in text.lower():
            return "dropped", DROPPED, True
        return "network", f"Could not connect to server: {text}", True
    return "unknown", f"{name}: {text}", False


# ── connection log ───────────────────────────────────────────────────────────

def human_size(n: Optional[int]) -> str:
    """457.5 KB, 1.2 MB … (1024-based, like the web console)."""
    if n is None:
        return "?"
    v = float(n)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if v < 1024 or unit == "TB":
            return f"{int(v)} bytes" if unit == "bytes" else f"{v:.1f} {unit}"
        v /= 1024
    return f"{n} bytes"


def transfer_summary(nbytes: int, seconds: float) -> str:
    s = max(1, int(round(seconds)))
    return f"File transfer successful, transferred {human_size(nbytes)} in {s} second{'s' if s != 1 else ''}"


def redact(message: str, secrets: tuple = ()) -> str:
    for s in secrets:
        if s and len(s) >= 3:
            message = message.replace(s, "********")
    return message


def log_entry(level: str, message: str, *, seq: int = 0, ts: Optional[datetime] = None,
              source: str = "api", secrets: tuple = ()) -> dict:
    level = level if level in LEVELS else "status"
    ts = ts or datetime.now(timezone.utc)
    msg = redact(" ".join(str(message).split()), secrets)[:1000]
    return {"seq": seq, "ts": ts.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z", "level": level,
            "message": msg, "source": source}


def format_line(entry: dict) -> str:
    """'2026-10-20 14:02:11  Status:  Connecting to bi.camtrack.mg...'"""
    ts = entry.get("ts", "").replace("T", " ").rstrip("Z")[:19]
    label = "Error:" if entry.get("level") == "error" else "Status:"
    return f"{ts}  {label:<7} {entry.get('message', '')}"


def effective_state(state: Optional[str], last_used: Optional[float], now: float,
                    idle_s: float = IDLE_CLOSE_S) -> str:
    """A pooled session idle for longer than `idle_s` is closed by the API —
    report it as disconnected even before the reaper ran."""
    if state not in LINK_STATES:
        return "disconnected"
    if state == "connected" and last_used is not None and now - last_used > idle_s + 5:
        return "disconnected"
    return state
