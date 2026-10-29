"""/collision/metrics — collision-prevention activity and quality (metrics_service.py)."""
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.session import get_db
from app.core.storage.redis_client import get_redis
from app.modules.m10_collision import metrics_service
from app.modules.m10_collision.schemas import CollisionMetricsOut

router = APIRouter()


@router.get("/metrics", response_model=CollisionMetricsOut, response_model_by_alias=True,
            summary="Collision-prevention metrics")
async def collision_metrics(
    period: Literal["1h", "6h", "24h", "7d"] = Query("24h", description="Period ending now."),
    db: AsyncSession = Depends(get_db),
):
    """
    Alerts raised (transition `NEW` or `ESCALATED`) per hour and per level, how many the context
    raised (`context_boosts`: one-step boost; `context_raised`: level above the physical level),
    detection latency (newest telemetry fix of the pair → alert evaluation; median and 90th
    percentile) and the drivers' feedback with the false-alert rate. Hourly counters are kept 8 days.
    """
    return await metrics_service.metrics(db, get_redis(), period)
