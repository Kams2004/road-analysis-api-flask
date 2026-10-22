"""
Fixed-window request counters in Redis (e.g. self-registration per IP).
Fails open: if Redis is unreachable the request is allowed and a warning is
logged — a rate limiter must not take sign-up down with it.

`middleware` applies per-client-IP limits to the sensitive public endpoints
(sign-in, sign-up, password reset, MFA: RATE_LIMIT_AUTH_PER_MIN; signed Label
Studio images: RATE_LIMIT_PUBLIC_PER_MIN) → 429 `rate_limited` with Retry-After.
Account lockout and the captcha still apply on top (app.modules.m01_security).
"""
import logging

from fastapi import Request
from fastapi.responses import JSONResponse

from app.core.config import settings

from app.core.storage.redis_client import get_redis

logger = logging.getLogger(__name__)


async def allow(key: str, limit: int, window_s: int, redis_client=None) -> bool:
    """Count one hit on `key`; True while the window holds at most `limit` hits."""
    try:
        r = redis_client or get_redis()
        hits = await r.incr(f"ratelimit:{key}")
        if hits == 1:
            await r.expire(f"ratelimit:{key}", window_s)
        return hits <= limit
    except Exception as exc:  # noqa: BLE001 — see module docstring
        logger.warning("Rate limiter unavailable (%s) — allowing %s", exc, key)
        return True


# (method, path prefix) → settings attribute holding the per-minute limit
_RULES: tuple[tuple[str, str, str], ...] = (
    ("POST", "/auth/login", "RATE_LIMIT_AUTH_PER_MIN"),
    ("POST", "/auth/mobile/login", "RATE_LIMIT_AUTH_PER_MIN"),
    ("POST", "/auth/mobile/register", "RATE_LIMIT_AUTH_PER_MIN"),
    ("POST", "/auth/google", "RATE_LIMIT_AUTH_PER_MIN"),
    ("POST", "/auth/mfa/verify", "RATE_LIMIT_AUTH_PER_MIN"),
    ("POST", "/auth/password/", "RATE_LIMIT_AUTH_PER_MIN"),
    ("GET", "/labelstudio/image/", "RATE_LIMIT_PUBLIC_PER_MIN"),
)


def rule_for(method: str, path: str) -> str | None:
    """Settings attribute of the limit that applies to the request, if any."""
    for m, prefix, attr in _RULES:
        if method == m and (path == prefix or path.startswith(prefix if prefix.endswith("/") else prefix + "/")
                            or path == prefix.rstrip("/")):
            return attr
    return None


async def middleware(request: Request, call_next):
    attr = rule_for(request.method, request.url.path)
    if attr is not None:
        limit = int(getattr(settings, attr, 0) or 0)
        if limit > 0:
            from app.core.audit import client_ip
            ip = client_ip(request) or "unknown"
            group = "auth" if attr == "RATE_LIMIT_AUTH_PER_MIN" else "public"
            if not await allow(f"{group}:{ip}", limit, 60):
                return JSONResponse(status_code=429, headers={"Retry-After": "60"}, content={"detail": {
                    "code": "rate_limited", "message": "Too many requests. Please wait a minute and try again."}})
    return await call_next(request)
