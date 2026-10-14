from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, case
from pydantic import BaseModel
from typing import Dict
from datetime import datetime, timedelta

from app.db.session import get_db
from app.models.detection import Detection, ReviewStatus
from app.models.job import Job, JobStatus

router = APIRouter()


class StatsOut(BaseModel):
    total_detections: int
    pending_validations: int
    validated_zones: int
    rejected: int
    detection_rate: float          # validated / (validated + rejected), 0-100
    total_jobs: int
    active_jobs: int
    by_type: Dict[str, int]        # type → count
    by_status: Dict[str, int]      # review_status → count
    recent_jobs: list              # last 5 jobs


@router.get("/", response_model=StatsOut)
async def get_stats(db: AsyncSession = Depends(get_db)):
    # ── counts ────────────────────────────────────────────────────────────
    total = (await db.execute(select(func.count()).select_from(Detection))).scalar() or 0

    status_rows = (await db.execute(
        select(Detection.review_status, func.count())
        .group_by(Detection.review_status)
    )).all()
    by_status = {str(r[0].value): r[1] for r in status_rows}

    pending   = by_status.get("pending",   0)
    validated = by_status.get("validated", 0)
    rejected  = by_status.get("rejected",  0)

    reviewed = validated + rejected
    rate = round((validated / reviewed) * 100, 1) if reviewed > 0 else 0.0

    # ── by detection type ─────────────────────────────────────────────────
    type_rows = (await db.execute(
        select(Detection.type, func.count()).group_by(Detection.type)
    )).all()
    by_type = {r[0]: r[1] for r in type_rows}

    # ── jobs ──────────────────────────────────────────────────────────────
    total_jobs = (await db.execute(select(func.count()).select_from(Job))).scalar() or 0
    active_jobs = (await db.execute(
        select(func.count()).select_from(Job)
        .where(Job.status.in_([JobStatus.pending, JobStatus.processing]))
    )).scalar() or 0

    recent = (await db.execute(
        select(Job).order_by(Job.created_at.desc()).limit(5)
    )).scalars().all()

    recent_jobs = [
        {
            "id": j.id,
            "filename": j.filename,
            "status": j.status.value,
            "detections": j.detections,
            "created_at": j.created_at.isoformat(),
            "finished_at": j.finished_at.isoformat() if j.finished_at else None,
        }
        for j in recent
    ]

    return StatsOut(
        total_detections=total,
        pending_validations=pending,
        validated_zones=validated,
        rejected=rejected,
        detection_rate=rate,
        total_jobs=total_jobs,
        active_jobs=active_jobs,
        by_type=by_type,
        by_status=by_status,
        recent_jobs=recent_jobs,
    )
