from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

from app.db.session import get_db
from app.models.rejection_reason import RejectionReason

router = APIRouter()


class RejectionReasonOut(BaseModel):
    id: str
    detection_type: str
    code: str
    label: str
    description: str
    is_custom: bool
    created_at: datetime

    class Config:
        from_attributes = True


class RejectionReasonCreate(BaseModel):
    detection_type: str   # pothole | traffic_sign | speed_bump | all
    code: str
    label: str
    description: str


@router.get("/", response_model=list[RejectionReasonOut])
async def list_reasons(
    detection_type: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """Return built-in + custom reasons. Optionally filter by detection_type."""
    q = select(RejectionReason).order_by(RejectionReason.is_custom, RejectionReason.label)
    if detection_type:
        q = q.where(
            or_(RejectionReason.detection_type == detection_type,
                RejectionReason.detection_type == "all")
        )
    rows = (await db.execute(q)).scalars().all()
    return rows


@router.post("/", response_model=RejectionReasonOut, status_code=201)
async def create_reason(
    body: RejectionReasonCreate,
    db: AsyncSession = Depends(get_db),
):
    """Add a new custom rejection reason."""
    # Prevent duplicate codes per type
    existing = (await db.execute(
        select(RejectionReason).where(
            RejectionReason.code == body.code,
            RejectionReason.detection_type == body.detection_type,
        )
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(409, f"Reason code '{body.code}' already exists for type '{body.detection_type}'")

    reason = RejectionReason(
        detection_type=body.detection_type,
        code=body.code,
        label=body.label,
        description=body.description,
        is_custom=True,
    )
    db.add(reason)
    await db.commit()
    await db.refresh(reason)
    return reason
