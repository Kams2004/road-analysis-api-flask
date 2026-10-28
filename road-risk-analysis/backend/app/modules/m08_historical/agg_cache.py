"""
Cache of the Historical analysis / Driver behaviour answers (Redis, 10 minutes). The key
carries a data version bumped whenever events or their summaries change (import, summary
rebuild, behaviour settings saved): a new day of events makes every cached answer obsolete at
once, without listing keys. Without Redis the answer is simply computed.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Awaitable, Callable

from fastapi.encoders import jsonable_encoder

logger = logging.getLogger(__name__)

VERSION_KEY = "events:data_version"
TTL_S = 600


def bump() -> None:
    try:
        from app.modules.m07_ingestion import job_slots
        job_slots.client().incr(VERSION_KEY)
    except Exception as exc:                           # no Redis: nothing is cached either
        logger.debug("data version not bumped: %s", exc)


def _key(namespace: str, params: dict, version: str) -> str:
    raw = json.dumps(jsonable_encoder(params), sort_keys=True, default=str)
    return f"events:agg:{version}:{namespace}:{hashlib.sha1(raw.encode()).hexdigest()}"


async def cached(namespace: str, params: dict, compute: Callable[[], Awaitable[Any]], enabled: bool = True) -> Any:
    """`compute()` once per (namespace, params, data version) for TTL_S; JSON-ready result.
    `enabled` is false until the daily summaries exist (and in tests): nothing is cached then."""
    if not enabled:
        return jsonable_encoder(await compute())
    r = key = None
    try:
        from app.core.storage.redis_client import get_redis
        r = get_redis()
        key = _key(namespace, params, str(await r.get(VERSION_KEY) or "0"))
        hit = await r.get(key)
        if hit:
            return json.loads(hit)
    except Exception as exc:
        logger.debug("aggregate cache unavailable: %s", exc)
        r = None
    data = jsonable_encoder(await compute())
    if r is not None:
        try:
            await r.set(key, json.dumps(data), ex=TTL_S)
        except Exception:
            pass
    return data
