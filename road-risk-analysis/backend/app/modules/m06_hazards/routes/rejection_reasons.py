from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime

from app.api.openapi import errors, legacy

from app.core.db.session import get_db
from app.modules.m06_hazards.models.rejection_reason import RejectionReason

router = APIRouter()


class RejectionReasonOut(BaseModel):
    """A rejection reason offered to validators."""
    id: str = Field(description="Reason id (UUID).")
    detection_type: str = Field(description="Detection type it applies to, or `all`.", examples=["pothole"])
    code: str = Field(description="Code sent as `reason_code` when rejecting.", examples=["shadow"])
    label: str = Field(description="Short label shown to validators.", examples=["Shadow"])
    description: str = Field(description="Longer explanation.", examples=["Dark shadow mistaken for a pothole"])
    is_custom: bool = Field(description="`false` for the seeded reasons, `true` for those added by users.")
    created_at: datetime = Field(description="Creation time (UTC).")

    class Config:
        from_attributes = True


class RejectionReasonCreate(BaseModel):
    """A new custom rejection reason."""
    detection_type: str = Field(description="`pothole`, `traffic_sign`, `speed_bump` or `all`.",
                                examples=["pothole"])
    code: str = Field(description="Code, unique per detection type.", examples=["water_puddle"])
    label: str = Field(description="Short label shown to validators.", examples=["Water puddle"])
    description: str = Field(description="Longer explanation.", examples=["Standing water, no road damage visible"])


@router.get("/", response_model=list[RejectionReasonOut], summary="List rejection reasons")
async def list_reasons(
    detection_type: Optional[str] = Query(None, description="Only the reasons for this type plus the `all` ones.",
                                          examples=["pothole"]),
    db: AsyncSession = Depends(get_db),
):
    """Seeded and custom rejection reasons (seeded first, then by label)."""
    q = select(RejectionReason).order_by(RejectionReason.is_custom, RejectionReason.label)
    if detection_type:
        q = q.where(
            or_(RejectionReason.detection_type == detection_type,
                RejectionReason.detection_type == "all")
        )
    rows = (await db.execute(q)).scalars().all()
    return rows


@router.post("/", response_model=RejectionReasonOut, status_code=201, summary="Add a custom rejection reason",
             responses=errors(legacy(409, "Reason code 'water_puddle' already exists for type 'pothole'")))
async def create_reason(
    body: RejectionReasonCreate,
    db: AsyncSession = Depends(get_db),
):
    """Add a custom rejection reason (`is_custom` = `true`). The code must be new for that detection type."""
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
