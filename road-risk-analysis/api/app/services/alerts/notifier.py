"""
Notification adapter interface for Step 5 — the seam between the alert
engine (WHAT changed, see engine.py) and delivery (HOW a human or system
finds out). Deliberately not implemented beyond structured logging here:
real delivery — Expo push to the mobile app, a BeSafe device channel, SMS,
a fleet dashboard socket — is future work. The point of this interface is
that the alert engine, and everything upstream of it (collision detection,
trajectory prediction, map matching), never needs to import anything
mobile- or channel-specific to do its job; only alert_service.py depends on
this module, and only at the very end of the pipeline.
"""
import logging
from typing import Protocol

from app.services.alerts.models import ConflictAlert

logger = logging.getLogger(__name__)


class NotificationAdapter(Protocol):
    async def notify(self, alert: ConflictAlert) -> None: ...


class LoggingNotificationAdapter:
    """Default adapter: structured logging only. Kept active regardless of
    whichever real adapter(s) get wired in later, so there's always an
    audit trail even before/alongside an actual delivery channel."""

    async def notify(self, alert: ConflictAlert) -> None:
        ttc = f"{alert.time_to_conflict_s:.1f}s" if alert.time_to_conflict_s is not None else "n/a"
        dist = (
            f"{alert.minimum_predicted_distance_m:.1f}m"
            if alert.minimum_predicted_distance_m is not None
            else "n/a"
        )
        logger.warning(
            "[ALERT] conflictId=%s vehicleA=%s vehicleB=%s level=%s transition=%s "
            "ttc=%s minDist=%s action=%s reason=%s",
            alert.conflict_id,
            alert.vehicle_a_id,
            alert.vehicle_b_id,
            alert.level.value,
            alert.transition.value,
            ttc,
            dist,
            alert.recommended_action.value,
            alert.reason,
        )


_default_adapter: NotificationAdapter = LoggingNotificationAdapter()


def get_notification_adapter() -> NotificationAdapter:
    return _default_adapter
