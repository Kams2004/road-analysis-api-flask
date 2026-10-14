"""
Out-of-order write protection for the ping-triggered background pipeline
(Steps 2-4: road_state_service, trajectory_service, collision_service).

Each ping triggers its own independent background chain with its own OSRM
round-trip. If the same vehicle pings twice in quick succession (a retry, a
fast interval, a flaky connection), nothing serializes the two chains — the
one carrying the *older* telemetry can simply finish *after* the newer one
(OSRM latency varies per call), and an unconditional `SET` would let it
silently overwrite fresher cached data with a stale result.

This module is split into a pure comparison (`is_stale`, no I/O, fully
testable the same way the rest of this codebase tests orchestration logic —
see tests/test_alerts.py, tests/test_collision.py) and a thin Redis wrapper
(`skip_if_stale`) around it, "live-verified" the same way the rest of the
Step 2-4 Redis plumbing is.
"""
from datetime import datetime
from typing import Optional

from app.services.redis_client import get_redis


def is_stale(cached_ts_raw: Optional[str], new_ts: datetime) -> bool:
    """True if `cached_ts_raw` (an ISO timestamp string previously recorded
    by `skip_if_stale`, or None) represents telemetry newer than `new_ts` —
    i.e. a write carrying `new_ts` arrived late and should be skipped.

    Fails open (returns False — "not stale", proceed with the write) on a
    missing/unparseable value or a naive/aware datetime mismatch, rather
    than raising: a comparison that can't be made safely shouldn't block a
    write that would have been allowed before this guard existed.
    """
    if not cached_ts_raw:
        return False
    try:
        cached_ts = datetime.fromisoformat(cached_ts_raw)
        return cached_ts > new_ts
    except (ValueError, TypeError):
        return False


async def skip_if_stale(guard_key: str, new_ts: datetime, ttl_s: int) -> bool:
    """Returns True if the caller should SKIP its write (a fresher result
    already won the race). Otherwise records `new_ts` as the new high-water
    mark under `guard_key` and returns False (proceed with the write).

    Not a full distributed lock — there's a narrow check-then-act window
    between the GET and the SET below, same as the throttle keys elsewhere
    in this codebase (see vehicle_service._throttle_key,
    road_state_service._db_throttle_key). It closes the specific race this
    module's docstring describes; it does not guarantee strict ordering
    under arbitrary concurrency.
    """
    r = get_redis()
    cached_ts_raw = await r.get(guard_key)
    if is_stale(cached_ts_raw, new_ts):
        return True
    await r.set(guard_key, new_ts.isoformat(), ex=ttl_s)
    return False
