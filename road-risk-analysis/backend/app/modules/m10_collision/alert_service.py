"""
Orchestrates Step 5: for a vehicle, read its cached Step 4 conflict report
(never recomputed here), run each conflict through the alert engine against
its previously-stored state, persist the result, and notify only on an
actual transition (never on an unchanged ongoing alert — this is the "avoid
alert spam" mechanism, in practice: engine.evaluate decides the transition,
this module decides whether to act on it).

Also detects resolution: a pair that WAS active for this vehicle last time
but no longer appears in its current conflict list at all gets explicitly
resolved (see engine.resolve) — a conflict can vanish either because the
predicted trajectories diverged or because one vehicle stopped reporting
live telemetry altogether; either way the alert shouldn't linger forever.

Note on the two-sided nature of a conflict: both vehicles in a pair run
this independently (their own ping triggers their own background chain),
and both land on the *same* Redis record via the canonical pair_key below —
so the alert is genuinely one shared object, not two divergent halves.

Delivery: every transition (anything but UNCHANGED) is published on the Redis
pub/sub channels `collision:alerts:{vehicle_id}` of BOTH vehicles — whichever
API process holds a vehicle's WebSocket (/alerts/ws, routes/alerts.py)
forwards it at once — counted in the hourly metrics (metrics_service.py) and
handed to the notification adapter (logging). GET /alerts/{vehicle_id} keeps
serving the same records for clients without a socket.
"""
import json
import logging
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.modules.m10_collision import collision_service, metrics_service, vehicle_service
from app.modules.m10_collision.alerts import engine
from app.modules.m10_collision.collision import thresholds
from app.modules.m10_collision.alerts.models import AlertLifecycleStatus, AlertTransition, ConflictAlert
from app.modules.m10_collision.alerts.notifier import get_notification_adapter
from app.core.storage.redis_client import get_redis

logger = logging.getLogger(__name__)


def pair_key(vehicle_a_id: str, vehicle_b_id: str) -> str:
    """Canonical, order-independent key for a vehicle pair."""
    a, b = sorted((vehicle_a_id, vehicle_b_id))
    return f"{a}:{b}"


def _alert_key(key: str) -> str:
    return f"alert:{key}"


def _active_pairs_key(vehicle_id: str) -> str:
    return f"alert:active_pairs:{vehicle_id}"


def _lock_key(key: str) -> str:
    return f"alert:lock:{key}"


async def _try_lock_pair(key: str) -> bool:
    r = get_redis()
    return bool(await r.set(_lock_key(key), "1", nx=True, ex=settings.ALERT_PAIR_LOCK_TTL_S))


async def _unlock_pair(key: str) -> None:
    r = get_redis()
    await r.delete(_lock_key(key))


async def _load_alert(key: str) -> Optional[ConflictAlert]:
    r = get_redis()
    raw = await r.get(_alert_key(key))
    if not raw:
        return None
    try:
        return ConflictAlert.model_validate_json(raw)
    except ValueError:
        logger.warning("Discarding unparseable cached alert for pair %s", key)
        return None


async def _store_alert(key: str, alert: ConflictAlert) -> None:
    r = get_redis()
    await r.set(_alert_key(key), alert.model_dump_json(), ex=settings.ALERT_TTL_S)


def alert_channel(vehicle_id: str) -> str:
    """Redis pub/sub channel of one vehicle's alert transitions (WS /alerts/ws)."""
    return f"collision:alerts:{vehicle_id}"


def alert_message(alert: ConflictAlert, vehicle_id: str) -> dict:
    """The WebSocket message of one transition, as seen by `vehicle_id`."""
    other = alert.vehicle_b_id if alert.vehicle_a_id == vehicle_id else alert.vehicle_a_id
    return {"type": "alert", "vehicle_id": vehicle_id, "other_vehicle_id": other,
            "alert": json.loads(alert.model_dump_json())}


async def publish(alert: ConflictAlert) -> None:
    r = get_redis()
    for vid in {alert.vehicle_a_id, alert.vehicle_b_id}:
        await r.publish(alert_channel(vid), json.dumps(alert_message(alert, vid)))


async def _notify_if_changed(alert: ConflictAlert) -> None:
    if alert.transition == AlertTransition.UNCHANGED:
        return
    try:
        await publish(alert)
    except Exception:
        logger.exception("Alert publish failed for conflict %s", alert.conflict_id)
    try:
        await metrics_service.record(get_redis(), alert)
    except Exception:
        logger.warning("Alert metrics not recorded for conflict %s", alert.conflict_id, exc_info=True)
    try:
        await get_notification_adapter().notify(alert)
    except Exception:
        logger.exception("Notification adapter failed for conflict %s", alert.conflict_id)


async def process_vehicle(vehicle_id: str, db: AsyncSession) -> List[ConflictAlert]:
    """Process every currently-reported conflict for this vehicle through
    the alert engine, and resolve any previously-active pair that dropped
    out entirely. Returns every alert touched this call (new, updated, or
    resolved) — an empty list means nothing changed."""
    cfg = await thresholds.refresh()
    report = await collision_service.get_conflicts(vehicle_id, db)
    now = datetime.now(timezone.utc)

    touched: List[ConflictAlert] = []
    current_keys = set()

    for conflict in report.conflicts:
        key = pair_key(conflict.vehicle_a_id, conflict.vehicle_b_id)
        current_keys.add(key)
        if not await _try_lock_pair(key):
            # Another concurrent pipeline — most often the *other* vehicle in
            # this pair, whose own ping arrived around the same time — is
            # already evaluating this exact pair. Skip rather than race it:
            # both reading the same `previous` and both writing afterwards is
            # how a single transition becomes two (duplicate notifications).
            # The next tick, moments later, catches up either way.
            logger.info("[ALERT-ENGINE] pair=%s skipped — locked by a concurrent evaluation", key)
            continue
        try:
            previous = await _load_alert(key)
            alert = engine.evaluate(conflict, previous, now)
            await _store_alert(key, alert)
            await _notify_if_changed(alert)
            touched.append(alert)
        finally:
            await _unlock_pair(key)

    cleared = set(report.cleared_vehicle_ids)
    held_keys: set = set()
    r = get_redis()
    active_key = _active_pairs_key(vehicle_id)
    previously_active = await r.smembers(active_key)
    for key in previously_active:
        if key in current_keys:
            continue
        if not await _try_lock_pair(key):
            logger.info("[ALERT-ENGINE] pair=%s resolve skipped — locked by a concurrent evaluation", key)
            continue
        try:
            previous = await _load_alert(key)
            if previous is None or previous.status == AlertLifecycleStatus.RESOLVED:
                continue
            other_id = previous.vehicle_b_id if previous.vehicle_a_id == vehicle_id else previous.vehicle_a_id
            # Resolve hysteresis: a pair that merely slipped below the
            # thresholds is held for resolve_after_s (flicker guard); one
            # assessed as separating / passing / on an unrelated road is
            # resolved at once.
            age_s = (now - previous.last_updated_at).total_seconds()
            if other_id not in cleared and age_s < cfg.resolve_after_s:
                held_keys.add(key)
                continue
            resolved = engine.resolve(previous, now)
            await _store_alert(key, resolved)
            await _notify_if_changed(resolved)
            touched.append(resolved)
        finally:
            await _unlock_pair(key)

    await r.delete(active_key)
    keep = current_keys | held_keys
    if keep:
        await r.sadd(active_key, *keep)
        await r.expire(active_key, settings.ALERT_ACTIVE_PAIRS_TTL_S)

    if touched:
        logger.info(
            "[ALERT-ENGINE] vehicle=%s conflicts=%d touched=%d",
            vehicle_id, len(report.conflicts), len(touched),
        )
    return touched


async def get_alerts(vehicle_id: str) -> List[ConflictAlert]:
    """Read-only: current alerts for a vehicle, from whatever the last
    background-task run computed. Never triggers computation or
    notification — see process_vehicle / the /refresh route for that."""
    r = get_redis()
    keys = await r.smembers(_active_pairs_key(vehicle_id))
    alerts = []
    for key in keys:
        alert = await _load_alert(key)
        if alert is not None:
            alerts.append(alert)
    alerts.sort(key=lambda a: -engine.LEVEL_SEVERITY.get(a.level, 0))
    return alerts


async def get_active_alerts_fleet_wide() -> List[ConflictAlert]:
    """Fleet-wide view: every currently-active (non-RESOLVED) alert across
    all live vehicles, deduplicated — both vehicles in a pair carry the same
    alert record, so naively concatenating each vehicle's own list would
    double every entry. Read-only, same as get_alerts."""
    live = await vehicle_service.query_all_live()
    seen_conflict_ids = set()
    alerts: List[ConflictAlert] = []
    for v in live:
        for alert in await get_alerts(v["vehicle_id"]):
            if alert.status == AlertLifecycleStatus.RESOLVED:
                continue
            if alert.conflict_id in seen_conflict_ids:
                continue
            seen_conflict_ids.add(alert.conflict_id)
            alerts.append(alert)
    alerts.sort(key=lambda a: -engine.LEVEL_SEVERITY.get(a.level, 0))
    return alerts
