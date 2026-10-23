"""
Processing capacity C (RRAP-07-009/014): at most `processing.max_concurrent_jobs`
jobs run at once, across every worker process.

Slots live in one Redis sorted set (member = job id, score = last heartbeat).
Acquire/release are serialised by a short SET NX lock, so the check-then-add
is atomic without Lua. A slot whose heartbeat is older than `stale_s` (worker
killed mid-job) is dropped on the next acquire; the beat watchdog also
removes slots of jobs that are no longer processing.

Synchronous redis client on purpose: the Celery task calls this outside of
its per-task event loop.
"""
import time
import uuid
from typing import Optional

SLOTS_KEY = "jobs:slots"
LOCK_KEY = "jobs:slots:lock"
DEFAULT_STALE_S = 15 * 60

_sync_client = None


def client():
    global _sync_client
    if _sync_client is None:
        import redis
        from app.core.config import settings
        _sync_client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _sync_client


def _lock(r, tries: int = 100, lock_key: str = LOCK_KEY) -> Optional[str]:
    token = uuid.uuid4().hex
    for _ in range(tries):
        if r.set(lock_key, token, nx=True, ex=5):
            return token
        time.sleep(0.02)
    return None


def _unlock(r, token: str, lock_key: str = LOCK_KEY) -> None:
    if r.get(lock_key) == token:
        r.delete(lock_key)


def acquire(r, job_id: str, capacity: int, now: Optional[float] = None,
            stale_s: float = DEFAULT_STALE_S, key: str = SLOTS_KEY) -> bool:
    """Take a slot for job_id. Re-entrant (a job holding a slot keeps it).
    False when all `capacity` slots are taken (or the lock is contended).
    `key` selects another pool (cloud imports use their own)."""
    now = time.time() if now is None else now
    lock_key = LOCK_KEY if key == SLOTS_KEY else f"{key}:lock"
    token = _lock(r, lock_key=lock_key)
    if token is None:
        return False
    try:
        r.zremrangebyscore(key, "-inf", now - stale_s)
        if r.zscore(key, job_id) is not None:
            r.zadd(key, {job_id: now})
            return True
        if r.zcard(key) >= capacity:
            return False
        r.zadd(key, {job_id: now})
        return True
    finally:
        _unlock(r, token, lock_key)


def heartbeat(r, job_id: str, now: Optional[float] = None, key: str = SLOTS_KEY) -> None:
    r.zadd(key, {job_id: time.time() if now is None else now}, xx=True)


def last_beat(r, job_id: str, key: str = SLOTS_KEY) -> Optional[float]:
    """Epoch of the job's last heartbeat (None: holds no slot)."""
    score = r.zscore(key, job_id)
    return float(score) if score is not None else None


def release(r, job_id: str, key: str = SLOTS_KEY) -> None:
    r.zrem(key, job_id)


def holders(r) -> list[str]:
    return list(r.zrange(SLOTS_KEY, 0, -1))
