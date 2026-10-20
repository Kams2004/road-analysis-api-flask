"""
Security primitives: password hashing, opaque tokens, TOTP and encryption
of secrets at rest.

- Passwords: scrypt (stdlib), per-password random salt, constant-time check.
- Tokens: 256-bit random, only their SHA-256 is persisted.
- TOTP: RFC 6238 (SHA-1, 6 digits, 30 s) — the variant every authenticator
  app (Google/Microsoft Authenticator, Authy, 1Password…) supports.
- Secrets at rest: Fernet (AES-128-CBC + HMAC-SHA256) keyed from SECRET_KEY.
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

# ── Passwords ────────────────────────────────────────────────────────────────

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _SCRYPT_LEN = 2 ** 14, 8, 1, 64


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_LEN)
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N, _SCRYPT_R, _SCRYPT_P,
        base64.b64encode(salt).decode(), base64.b64encode(dk).decode(),
    )


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algo, n, r, p, salt_b64, dk_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        expected = base64.b64decode(dk_b64)
        dk = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt_b64),
            n=int(n), r=int(r), p=int(p), dklen=len(expected),
        )
        return hmac.compare_digest(dk, expected)
    except (ValueError, TypeError):
        return False


# Burn comparable CPU when the user doesn't exist, so response time doesn't
# reveal which e-mails are registered.
_DUMMY_HASH = hash_password(secrets.token_hex(8))


def dummy_verify() -> None:
    verify_password("x", _DUMMY_HASH)


def password_problems(password: str, min_length: int) -> list[str]:
    problems = []
    if len(password) < min_length:
        problems.append(f"at least {min_length} characters")
    if not any(c.isalpha() for c in password):
        problems.append("at least one letter")
    if not any(c.isdigit() for c in password):
        problems.append("at least one digit")
    return problems


# ── Opaque tokens ────────────────────────────────────────────────────────────

def new_token(prefix: str = "") -> str:
    return prefix + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ── TOTP (RFC 6238) ──────────────────────────────────────────────────────────

TOTP_PERIOD = 30
TOTP_DIGITS = 6


def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _hotp(secret_b32: str, counter: int) -> str:
    key = base64.b32decode(secret_b32 + "=" * (-len(secret_b32) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** TOTP_DIGITS)
    return str(code).zfill(TOTP_DIGITS)


def totp_now(secret_b32: str, at: float | None = None) -> str:
    return _hotp(secret_b32, int((at if at is not None else time.time()) // TOTP_PERIOD))


def verify_totp(secret_b32: str, code: str, last_step: int | None = None,
                window: int = 1, at: float | None = None) -> int | None:
    """
    Return the matched time-step (store it as `last_step`) or None.
    Accepts ±`window` steps of clock drift and refuses any step <= last_step
    so an observed code can't be replayed.
    """
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) != TOTP_DIGITS:
        return None
    current = int((at if at is not None else time.time()) // TOTP_PERIOD)
    for step in range(current - window, current + window + 1):
        if last_step is not None and step <= last_step:
            continue
        if hmac.compare_digest(_hotp(secret_b32, step), code):
            return step
    return None


def totp_uri(secret_b32: str, account: str, issuer: str = "VIASafe") -> str:
    return (
        f"otpauth://totp/{quote(issuer)}:{quote(account)}"
        f"?secret={secret_b32}&issuer={quote(issuer)}&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_PERIOD}"
    )


# ── Encryption of secrets at rest ────────────────────────────────────────────

def _fernet() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode()).digest())
    return Fernet(key)


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt_secret(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken:
        # SECRET_KEY changed since this was stored — treat as unset.
        return None
