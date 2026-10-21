from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_
from pydantic import BaseModel, Field
from typing import Literal, Optional
from datetime import datetime

from app.api.openapi import errors, legacy

from app.core.db.session import get_db
from app.modules.m06_hazards.models.rejection_reason import RejectionReason

router = APIRouter()

RejectionKind = Literal["false_positive", "real_redundant", "unusable"]
_KIND_DOC = ("What a rejection with this reason teaches the training data: `false_positive` (AI mistake → "
             "negative example), `real_redundant` (real object, redundant → positive sample) or `unusable` "
             "(cannot be judged → not used for training).")


class RejectionReasonOut(BaseModel):
    """A rejection reason offered to validators."""
    id: str = Field(description="Reason id (UUID).")
    detection_type: str = Field(description="Detection type it applies to, or `all`.", examples=["pothole"])
    code: str = Field(description="Code sent as `reason_code` when rejecting.", examples=["shadow"])
    label: str = Field(description="Short label shown to validators.", examples=["Shadow"])
    description: str = Field(description="Longer explanation.", examples=["Dark shadow mistaken for a pothole"])
    is_custom: bool = Field(description="`false` for the seeded reasons, `true` for those added by users.")
    kind: RejectionKind = Field(description=_KIND_DOC, examples=["false_positive"])
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
    kind: RejectionKind = Field("false_positive", description=_KIND_DOC)


class RejectionReasonUpdate(BaseModel):
    """Changes to a custom rejection reason (omitted fields are kept)."""
    label: Optional[str] = Field(None, description="Short label shown to validators.")
    description: Optional[str] = Field(None, description="Longer explanation.")
    kind: Optional[RejectionKind] = Field(None, description=_KIND_DOC)


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
        kind=body.kind,
    )
    db.add(reason)
    await db.commit()
    await db.refresh(reason)
    return reason


@router.patch("/{reason_id}", response_model=RejectionReasonOut, summary="Edit a custom rejection reason",
              responses=errors(legacy(404, "Rejection reason not found"),
                               legacy(409, "Built-in rejection reasons cannot be edited")))
async def update_reason(
    reason_id: str,
    body: RejectionReasonUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Edit the label, description or kind of a custom reason. Changing the kind
    re-files the training samples of past rejections with this code
    (docs/TRAINING-DATA.md)."""
    reason = await db.get(RejectionReason, reason_id)
    if reason is None:
        raise HTTPException(404, "Rejection reason not found")
    if not reason.is_custom:
        raise HTTPException(409, "Built-in rejection reasons cannot be edited")
    if body.label is not None:
        reason.label = body.label
    if body.description is not None:
        reason.description = body.description
    kind_changed = body.kind is not None and body.kind != reason.kind
    if body.kind is not None:
        reason.kind = body.kind
    await db.commit()
    await db.refresh(reason)
    if kind_changed:
        from app.modules.m06_hazards import training_data
        from app.modules.m06_hazards.training_data import sync as training_sync
        await training_data.resync_rejections(db, codes=[reason.code])
        training_sync.kick()
        await db.refresh(reason)
    return reason
