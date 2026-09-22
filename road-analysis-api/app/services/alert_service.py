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
"""
import logging
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services import collision_service, vehicle_service
from app.services.alerts import engine
from app.services.alerts.models import AlertLifecycleStatus, AlertTransition, ConflictAlert
from app.services.alerts.notifier import get_notification_adapter
from app.services.redis_client import get_redis

logger = logging.getLogger(__name__)


def pair_key(vehicle_a_id: str, vehicle_b_id: str) -> str:
    """Canonical, order-independent key for a vehicle pair."""
    a, b = sorted((vehicle_a_id, vehicle_b_id))
    return f"{a}:{b}"


def _alert_key(key: str) -> str:
    return f"alert:{key}"


def _active_pairs_key(vehicle_id: str) -> str:
    return f"alert:active_pairs:{vehicle_id}"


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


async def _notify_if_changed(alert: ConflictAlert) -> None:
    if alert.transition == AlertTransition.UNCHANGED:
        return
    try:
        await get_notification_adapter().notify(alert)
    except Exception:
        logger.exception("Notification adapter failed for conflict %s", alert.conflict_id)


async def process_vehicle(vehicle_id: str, db: AsyncSession) -> List[ConflictAlert]:
    """Process every currently-reported conflict for this vehicle through
    the alert engine, and resolve any previously-active pair that dropped
    out entirely. Returns every alert touched this call (new, updated, or
    resolved) — an empty list means nothing changed."""
    report = await collision_service.get_conflicts(vehicle_id, db)
    now = datetime.now(timezone.utc)

    touched: List[ConflictAlert] = []
    current_keys = set()

    for conflict in report.conflicts:
        key = pair_key(conflict.vehicle_a_id, conflict.vehicle_b_id)
        current_keys.add(key)
        previous = await _load_alert(key)
        alert = engine.evaluate(conflict, previous, now)
        await _store_alert(key, alert)
        await _notify_if_changed(alert)
        touched.append(alert)

    r = get_redis()
    active_key = _active_pairs_key(vehicle_id)
    previously_active = await r.smembers(active_key)
    for key in previously_active:
        if key in current_keys:
            continue
        previous = await _load_alert(key)
        if previous is None or previous.status == AlertLifecycleStatus.RESOLVED:
            continue
        resolved = engine.resolve(previous, now)
        await _store_alert(key, resolved)
        await _notify_if_changed(resolved)
        touched.append(resolved)

    await r.delete(active_key)
    if current_keys:
        await r.sadd(active_key, *current_keys)
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
