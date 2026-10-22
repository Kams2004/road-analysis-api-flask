"""
Instant cache invalidation across processes (Redis pub/sub).

Every API process (uvicorn --workers N) and every Celery worker process keeps
short in-process caches of the platform settings (settings_service, cluster
rules, collision thresholds, map tile sessions). The process that saves a change
clears its own copy and `publish()`es the topic; every other process receives it
on the channel and clears the same copy at once — no more waiting for the 10 s
TTL, which stays as the safety net (a message missed while Redis was down, a
process that was restarting).

    topics      "settings:<section>" (update_section), "clustering" (zones / areas)
    listeners   API: start_listener() in the lifespan (asyncio task)
                Celery: start_thread_listener() on worker_process_init (daemon thread)
    reconnect   with back-off; after a reconnect every cache is cleared (messages
                may have been missed meanwhile)

Publishing never raises: without Redis the TTL still applies.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import threading
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

CHANNEL = "viasafe:cache-invalidate"
ALL = "*"
_RUN = uuid.uuid4().hex[:8]


def _origin() -> str:
    """This process (forked Celery children share _RUN, not the pid)."""
    return f"{socket.gethostname()}:{os.getpid()}:{_RUN}"


# ── local effects ────────────────────────────────────────────────────────────

def apply_local(topic: str) -> None:
    """Drop this process's cached copy for `topic` (never publishes)."""
    from app.core import settings_service

    if topic == ALL:
        settings_service.clear_cache()
        _clustering_local()
        _collision_local()
        _map_tiles_local()
        return
    if topic == "clustering":
        _clustering_local()
        return
    if topic.startswith("settings:"):
        section = topic.split(":", 1)[1]
        settings_service.clear_cache(section)
        if section == "clustering":
            _clustering_local()
        elif section == "collision":
            _collision_local()
        elif section == "maps":
            _map_tiles_local()


def _clustering_local() -> None:
    from app.modules.m06_hazards.clustering import service as clustering_service
    clustering_service.invalidate(broadcast=False)


def _collision_local() -> None:
    from app.modules.m10_collision.collision import thresholds
    thresholds.expire()


def _map_tiles_local() -> None:
    from app.modules.m03_maps import map_tiles
    map_tiles.clear_cache()


# ── publish ──────────────────────────────────────────────────────────────────

def _message(topic: str) -> str:
    return json.dumps({"t": topic, "o": _origin()})


def publish(topic: str) -> None:
    """Tell the other processes to drop their copy of `topic` (the caller has
    already cleared its own). Fire-and-forget, never raises."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        loop.create_task(_publish_async(topic))
    else:
        _publish_sync(topic)


async def _publish_async(topic: str) -> None:
    try:
        from app.core.storage.redis_client import get_redis
        await get_redis().publish(CHANNEL, _message(topic))
    except Exception as exc:  # noqa: BLE001
        logger.warning("cache bus: publish %s failed (%s) — other processes refresh within the TTL", topic, exc)


def _publish_sync(topic: str) -> None:
    try:
        import redis
        from app.core.config import settings
        client = redis.Redis.from_url(settings.REDIS_URL, socket_timeout=2, socket_connect_timeout=2)
        try:
            client.publish(CHANNEL, _message(topic))
        finally:
            client.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("cache bus: publish %s failed (%s) — other processes refresh within the TTL", topic, exc)


def handle(raw: object) -> Optional[str]:
    """Apply one received message; returns the topic applied (None = ignored)."""
    try:
        msg = json.loads(raw if isinstance(raw, str) else bytes(raw).decode())  # type: ignore[arg-type]
        topic, origin = str(msg["t"]), msg.get("o")
    except Exception:
        return None
    if origin == _origin():
        return None
    try:
        apply_local(topic)
    except Exception:
        logger.exception("cache bus: could not apply %s", topic)
        return None
    return topic


# ── API listener (asyncio) ───────────────────────────────────────────────────

_task: Optional[asyncio.Task] = None


def start_listener() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.get_running_loop().create_task(_listen())


async def stop_listener() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):
            pass
        _task = None


async def _listen() -> None:
    import redis.asyncio as aredis
    from app.core.config import settings

    delay, first = 1.0, True
    while True:
        client = None
        try:
            client = aredis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
            pubsub = client.pubsub(ignore_subscribe_messages=True)
            await pubsub.subscribe(CHANNEL)
            if not first:
                apply_local(ALL)                 # messages may have been missed while disconnected
            first, delay = False, 1.0
            async for message in pubsub.listen():
                if message.get("type") == "message":
                    handle(message.get("data"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("cache bus: listener disconnected (%s) — retrying in %.0f s", exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)
        finally:
            if client is not None:
                try:
                    await client.aclose()
                except Exception:
                    pass


# ── Celery listener (thread, one per worker process) ─────────────────────────

_thread: Optional[threading.Thread] = None


def start_thread_listener() -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _thread = threading.Thread(target=_listen_blocking, name="cache-bus", daemon=True)
    _thread.start()


def _listen_blocking() -> None:
    import time

    import redis
    from app.core.config import settings

    delay, first = 1.0, True
    while True:
        try:
            client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True, health_check_interval=30)
            pubsub = client.pubsub(ignore_subscribe_messages=True)
            pubsub.subscribe(CHANNEL)
            if not first:
                apply_local(ALL)
            first, delay = False, 1.0
            for message in pubsub.listen():
                if message.get("type") == "message":
                    handle(message.get("data"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("cache bus: listener disconnected (%s) — retrying in %.0f s", exc, delay)
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
