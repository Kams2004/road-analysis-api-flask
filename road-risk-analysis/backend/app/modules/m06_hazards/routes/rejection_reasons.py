from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import case, func, select, or_
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


class RejectionReasonPageOut(BaseModel):
    """One page of the rejection reasons of a detection type."""
    total: int = Field(description="Reasons matching the filters.")
    items: list[RejectionReasonOut] = Field(description="This page, AI mistakes first, then real-but-redundant, "
                                                        "then can't-judge; system before custom; by code.")


class RejectionReasonTypeCount(BaseModel):
    detection_type: str = Field(description="Detection type (or `all`).", examples=["pothole"])
    total: int = Field(description="Reasons of this type.")


_KIND_ORDER = case({"false_positive": 0, "real_redundant": 1, "unusable": 2}, value=RejectionReason.kind, else_=3)


@router.get("/", response_model=list[RejectionReasonOut], summary="List rejection reasons")
async def list_reasons(
    detection_type: Optional[str] = Query(None, description="Only the reasons for this type plus the `all` ones.",
                                          examples=["pothole"]),
    skip: int = Query(0, ge=0, description="Number of reasons to skip (offset)."),
    limit: int = Query(200, ge=1, le=500, description="Maximum number of reasons."),
    db: AsyncSession = Depends(get_db),
):
    """Seeded and custom rejection reasons (seeded first, then by label) — the list offered when rejecting.
    The settings page uses the paginated `GET /rejection-reasons/page`."""
    q = select(RejectionReason).order_by(RejectionReason.is_custom, RejectionReason.label, RejectionReason.id)
    if detection_type:
        q = q.where(
            or_(RejectionReason.detection_type == detection_type,
                RejectionReason.detection_type == "all")
        )
    rows = (await db.execute(q.offset(skip).limit(limit))).scalars().all()
    return rows


@router.get("/types", response_model=list[RejectionReasonTypeCount], summary="Count rejection reasons per type")
async def count_reasons_by_type(db: AsyncSession = Depends(get_db)):
    """How many reasons each detection type has (the settings page shows one paginated table per type)."""
    rows = (await db.execute(select(RejectionReason.detection_type, func.count())
                             .group_by(RejectionReason.detection_type)
                             .order_by(RejectionReason.detection_type))).all()
    return [{"detection_type": t, "total": n} for t, n in rows]


@router.get("/page", response_model=RejectionReasonPageOut, summary="Rejection reasons of one type, paginated")
async def page_reasons(
    detection_type: str = Query(..., description="Exactly this type (`all` = the reasons offered for every type).",
                                examples=["pothole"]),
    kind: Optional[RejectionKind] = Query(None, description="Only this kind."),
    q: Optional[str] = Query(None, max_length=100, description="Search in code, label and description."),
    skip: int = Query(0, ge=0, description="Number of reasons to skip (offset)."),
    limit: int = Query(10, ge=1, le=100, description="Page size."),
    db: AsyncSession = Depends(get_db),
):
    """The reasons of one detection type, one page at a time, with the total for the pager."""
    stmt = select(RejectionReason).where(RejectionReason.detection_type == detection_type)
    if kind:
        stmt = stmt.where(RejectionReason.kind == kind)
    if q and q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(RejectionReason.code.ilike(like), RejectionReason.label.ilike(like),
                              RejectionReason.description.ilike(like)))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.order_by(_KIND_ORDER, RejectionReason.is_custom, RejectionReason.code,
                                           RejectionReason.id).offset(skip).limit(limit))).scalars().all()
    return {"total": total, "items": rows}


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
