"""
Collision-prevention metrics (GET /collision/metrics) and driver feedback on alerts
(POST /alerts/{conflict_id}/feedback).

Alerts live in Redis only (they are ephemeral), so the alert service also records each
raised alert (transition NEW or ESCALATED) into hourly Redis buckets, kept METRICS_TTL_S:

  collision:metrics:h:{YYYYMMDDHH}     hash  raised:{LEVEL} → n, boosted → n, widened → n
  collision:metrics:lat:{YYYYMMDDHH}   list  detection latencies, ms (last 1000 of the hour)

Detection latency = evaluation time − newest telemetry timestamp of the pair (device clock
vs server clock: a phone with a skewed clock skews it; values are clamped at 0).

Feedback is durable (table collision_alert_feedback, one row per conflict × vehicle — a
second answer replaces the first). The false-alert rate is false_alert / all answers of the
period.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.m10_collision.alerts.models import AlertLevel, AlertTransition, ConflictAlert

logger = logging.getLogger(__name__)

METRICS_TTL_S = 8 * 24 * 3600
LATENCY_SAMPLES = 1000
PERIODS = {"1h": 1, "6h": 6, "24h": 24, "7d": 168}
LEVELS = [lvl.value for lvl in AlertLevel if lvl != AlertLevel.SAFE]


def _hour(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y%m%d%H")


def hour_key(ts: datetime) -> str:
    return f"collision:metrics:h:{_hour(ts)}"


def latency_key(ts: datetime) -> str:
    return f"collision:metrics:lat:{_hour(ts)}"


async def record(r, alert: ConflictAlert, now: Optional[datetime] = None) -> None:
    """Count a raised alert (NEW / ESCALATED) in its hour bucket."""
    if alert.transition not in (AlertTransition.NEW, AlertTransition.ESCALATED):
        return
    now = now or datetime.now(timezone.utc)
    hk = hour_key(now)
    await r.hincrby(hk, f"raised:{alert.level.value}", 1)
    if alert.context_level_boost:
        await r.hincrby(hk, "boosted", 1)
    if alert.physical_level is not None and alert.physical_level != alert.level:
        await r.hincrby(hk, "context_raised", 1)
    await r.expire(hk, METRICS_TTL_S)
    if alert.detection_latency_ms is not None:
        lk = latency_key(now)
        await r.lpush(lk, round(alert.detection_latency_ms, 1))
        await r.ltrim(lk, 0, LATENCY_SAMPLES - 1)
        await r.expire(lk, METRICS_TTL_S)


def _percentile(values: List[float], q: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (k - lo), 1)


def summarise(buckets: Dict[datetime, dict], latencies: List[float], feedback_rows: List[tuple]) -> dict:
    """Pure: hourly buckets {hour_start: {field: n}} + latency samples + feedback rows
    (verdict, level, n) → the /collision/metrics answer (without period bounds)."""
    by_level = {lvl: 0 for lvl in LEVELS}
    by_hour = []
    boosted = context_raised = 0
    for hour in sorted(buckets):
        b = buckets[hour]
        levels = {lvl: int(b.get(f"raised:{lvl}", 0) or 0) for lvl in LEVELS}
        for lvl, n in levels.items():
            by_level[lvl] += n
        boosted += int(b.get("boosted", 0) or 0)
        context_raised += int(b.get("context_raised", 0) or 0)
        by_hour.append({"hour": hour.isoformat(), "total": sum(levels.values()), "by_level": levels})
    fb_total = sum(n for _, _, n in feedback_rows)
    false_n = sum(n for v, _, n in feedback_rows if v == "false_alert")
    fb_by_level: Dict[str, dict] = defaultdict(lambda: {"false_alert": 0, "useful": 0})
    for verdict, level, n in feedback_rows:
        fb_by_level[level or "UNKNOWN"][verdict] += n
    return {
        "alerts_total": sum(by_level.values()),
        "by_level": by_level,
        "by_hour": by_hour,
        "context_boosts": boosted,
        "context_raised": context_raised,
        "detection_latency_ms": {"median": _percentile(latencies, 0.5), "p90": _percentile(latencies, 0.9),
                                 "samples": len(latencies)},
        "feedback": {"total": fb_total, "false_alert": false_n, "useful": fb_total - false_n,
                     "false_alert_rate": round(false_n / fb_total, 3) if fb_total else None,
                     "by_level": dict(fb_by_level)},
    }


async def metrics(db: AsyncSession, r, period: str, now: Optional[datetime] = None) -> dict:
    from app.modules.m10_collision.models.alert_feedback import AlertFeedback
    now = now or datetime.now(timezone.utc)
    hours = PERIODS[period]
    start = (now - timedelta(hours=hours - 1)).replace(minute=0, second=0, microsecond=0)
    buckets: Dict[datetime, dict] = {}
    latencies: List[float] = []
    for i in range(hours):
        h = start + timedelta(hours=i)
        try:
            buckets[h] = await r.hgetall(hour_key(h)) or {}
            latencies += [float(x) for x in await r.lrange(latency_key(h), 0, -1)]
        except Exception:
            logger.warning("collision metrics: Redis unavailable", exc_info=True)
            buckets[h] = {}
    since = (now - timedelta(hours=hours)).astimezone(timezone.utc).replace(tzinfo=None)
    rows = (await db.execute(
        select(AlertFeedback.verdict, AlertFeedback.level, func.count())
        .where(AlertFeedback.updated_at >= since)
        .group_by(AlertFeedback.verdict, AlertFeedback.level))).all()
    out = summarise(buckets, latencies, [(v, lvl, int(n)) for v, lvl, n in rows])
    return {"period": period, "from": start.isoformat(), "to": now.isoformat(), **out}
