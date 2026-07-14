import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.signalement import Signalement, SignalementType, SignalementStatus
from app.schemas.schemas import (
    SignalementOut, SignalementListOut,
    SignalementModerationIn, SignalementNearbyIn, AlongRouteQueryIn,
)
from app.services.signalement_service import (
    query_signalements_nearby, query_signalements_along_route,
)
from app.services.minio_service import upload_signalement_image, upload_signalement_audio
from app.services.geocoding import reverse_geocode

router = APIRouter()


# ── Mobile: create a signalement (multipart to allow optional image) ──────────

@router.post("/", response_model=SignalementOut, status_code=201)
async def create_signalement(
    type:            SignalementType   = Form(...),
    latitude:        float             = Form(...),
    longitude:       float             = Form(...),
    description:     Optional[str]     = Form(None),
    reported_by:     Optional[str]     = Form(None),
    blocked_bearing: Optional[float]   = Form(None),
    image:           Optional[UploadFile] = File(None),
    audio:           Optional[UploadFile] = File(None),
    db: AsyncSession = Depends(get_db),
):
    image_url = None
    if image:
        data = await image.read()
        ext  = (image.filename or "img.jpg").rsplit(".", 1)[-1].lower()
        object_name = f"signalements/{uuid.uuid4().hex}.{ext}"
        image_url = upload_signalement_image(data, object_name, image.content_type or "image/jpeg")

    audio_url = None
    if audio:
        data = await audio.read()
        ext  = (audio.filename or "audio.m4a").rsplit(".", 1)[-1].lower()
        object_name = f"signalements/audio/{uuid.uuid4().hex}.{ext}"
        audio_url = upload_signalement_audio(data, object_name, audio.content_type or "audio/m4a")

    sig = Signalement(
        type=type,
        latitude=latitude,
        longitude=longitude,
        description=description,
        reported_by=reported_by,
        image_url=image_url,
        audio_url=audio_url,
        blocked_bearing=blocked_bearing,
        location_name=reverse_geocode(latitude, longitude),
        reported_at=datetime.utcnow(),
    )
    db.add(sig)
    await db.commit()
    await db.refresh(sig)
    return sig


# ── Mobile: list signalements in a radius around the user's position ──────────

@router.post("/nearby", response_model=SignalementListOut)
async def nearby_signalements(
    body: SignalementNearbyIn,
    db: AsyncSession = Depends(get_db),
):
    ids = await query_signalements_nearby(db, body.latitude, body.longitude, body.radius_m)
    if not ids:
        return SignalementListOut(total=0, items=[])
    items = (
        await db.execute(select(Signalement).where(Signalement.id.in_(ids)))
    ).scalars().all()
    return SignalementListOut(total=len(items), items=list(items))


# ── Mobile: signalements along a planned route ────────────────────────────────

@router.post("/along-route", response_model=SignalementListOut)
async def signalements_along_route(
    body: AlongRouteQueryIn,
    db: AsyncSession = Depends(get_db),
):
    if len(body.waypoints) < 2:
        raise HTTPException(422, "At least 2 waypoints required.")
    pairs = [(wp.latitude, wp.longitude) for wp in body.waypoints]
    ids   = await query_signalements_along_route(db, pairs, body.corridor_m)
    if not ids:
        return SignalementListOut(total=0, items=[])
    items = (
        await db.execute(select(Signalement).where(Signalement.id.in_(ids)))
    ).scalars().all()
    return SignalementListOut(total=len(items), items=list(items))


# ── Community voting ─────────────────────────────────────────────────────────

NOT_THERE_THRESHOLD = 3  # auto-remove after this many "not there" votes


@router.post("/{signalement_id}/confirm", response_model=SignalementOut)
async def confirm_signalement(
    signalement_id: str,
    db: AsyncSession = Depends(get_db),
):
    sig = await db.get(Signalement, signalement_id)
    if not sig:
        raise HTTPException(404, "Signalement not found")
    sig.confirmations = (sig.confirmations or 0) + 1
    await db.commit()
    await db.refresh(sig)
    return sig


@router.post("/{signalement_id}/not-there", response_model=SignalementOut)
async def not_there_signalement(
    signalement_id: str,
    db: AsyncSession = Depends(get_db),
):
    sig = await db.get(Signalement, signalement_id)
    if not sig:
        raise HTTPException(404, "Signalement not found")
    sig.not_there_votes = (sig.not_there_votes or 0) + 1
    if sig.not_there_votes >= NOT_THERE_THRESHOLD:
        sig.status = SignalementStatus.annule
    await db.commit()
    await db.refresh(sig)
    return sig


# ── Admin / listing ───────────────────────────────────────────────────────────

@router.get("/", response_model=SignalementListOut)
async def list_signalements(
    status:      Optional[SignalementStatus] = None,
    type:        Optional[SignalementType]   = None,
    reported_by: Optional[str]              = None,
    skip:  int = 0,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
):
    q = select(Signalement)
    if status:      q = q.where(Signalement.status == status)
    if type:        q = q.where(Signalement.type   == type)
    if reported_by: q = q.where(Signalement.reported_by == reported_by)

    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar()
    items = (
        await db.execute(q.offset(skip).limit(limit).order_by(Signalement.reported_at.desc()))
    ).scalars().all()
    return SignalementListOut(total=total, items=list(items))


@router.get("/{signalement_id}", response_model=SignalementOut)
async def get_signalement(signalement_id: str, db: AsyncSession = Depends(get_db)):
    sig = await db.get(Signalement, signalement_id)
    if not sig:
        raise HTTPException(404, "Signalement not found")
    return sig


# ── Admin: moderate (cancel / reject) ────────────────────────────────────────

@router.patch("/{signalement_id}/moderate", response_model=SignalementOut)
async def moderate_signalement(
    signalement_id: str,
    body: SignalementModerationIn,
    db: AsyncSession = Depends(get_db),
):
    if body.status == SignalementStatus.actif:
        raise HTTPException(422, "Use status 'annule' or 'rejete'.")
    sig = await db.get(Signalement, signalement_id)
    if not sig:
        raise HTTPException(404, "Signalement not found")

    sig.status          = body.status
    sig.moderated_by    = body.moderated_by
    sig.moderated_at    = datetime.utcnow()
    sig.moderation_note = body.note
    await db.commit()
    await db.refresh(sig)
    return sig


# ── Admin: delete ─────────────────────────────────────────────────────────────

@router.delete("/{signalement_id}", status_code=204)
async def delete_signalement(signalement_id: str, db: AsyncSession = Depends(get_db)):
    sig = await db.get(Signalement, signalement_id)
    if not sig:
        raise HTTPException(404, "Signalement not found")
    await db.delete(sig)
    await db.commit()
