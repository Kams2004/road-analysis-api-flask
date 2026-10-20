"""
Fixed-window request counters in Redis (e.g. self-registration per IP).
Fails open: if Redis is unreachable the request is allowed and a warning is
logged — a rate limiter must not take sign-up down with it.
"""
import logging

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
