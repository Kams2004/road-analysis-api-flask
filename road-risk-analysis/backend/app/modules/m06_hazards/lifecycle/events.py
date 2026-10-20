"""
hazard_events rows — the timeline of a hazard (GET /hazards/{id}/events).
The caller commits.
"""
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.m06_hazards.models.hazard_lifecycle import EVENT_KINDS, HazardEvent


def record(db: AsyncSession, hazard_id: str, kind: str, *, source: Optional[str] = None,
           job_id: Optional[str] = None, detection_id: Optional[str] = None, user_id: Optional[str] = None,
           recorded_at: Optional[datetime] = None, details: Optional[dict[str, Any]] = None) -> HazardEvent:
    if kind not in EVENT_KINDS:
        raise ValueError(f"unknown hazard event kind '{kind}'")
    row = HazardEvent(hazard_id=hazard_id, kind=kind, source=source, job_id=job_id, detection_id=detection_id,
                      user_id=user_id, recorded_at=recorded_at, details=details, created_at=datetime.utcnow())
    db.add(row)
    return row


async def exists(db: AsyncSession, hazard_id: str, kinds: tuple[str, ...], *, job_id: Optional[str] = None,
                 detection_id: Optional[str] = None) -> bool:
    q = select(HazardEvent.id).where(HazardEvent.hazard_id == hazard_id, HazardEvent.kind.in_(kinds))
    if job_id is not None:
        q = q.where(HazardEvent.job_id == job_id)
    if detection_id is not None:
        q = q.where(HazardEvent.detection_id == detection_id)
    return (await db.execute(q.limit(1))).first() is not None


async def last_of(db: AsyncSession, hazard_id: str, kind: str) -> Optional[HazardEvent]:
    return (await db.execute(
        select(HazardEvent).where(HazardEvent.hazard_id == hazard_id, HazardEvent.kind == kind)
        .order_by(HazardEvent.created_at.desc()).limit(1)
    )).scalar_one_or_none()


async def timeline(db: AsyncSession, hazard_id: str, *, kinds: Optional[list[str]] = None,
                   skip: int = 0, limit: int = 200) -> tuple[int, list[HazardEvent]]:
    from sqlalchemy import func
    q = select(HazardEvent).where(HazardEvent.hazard_id == hazard_id)
    if kinds:
        q = q.where(HazardEvent.kind.in_(kinds))
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0
    rows = (await db.execute(q.order_by(HazardEvent.created_at.desc()).offset(skip).limit(limit))).scalars().all()
    return total, list(rows)
