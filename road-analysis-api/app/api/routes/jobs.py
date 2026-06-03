import uuid
from pathlib import Path
from fastapi import APIRouter, UploadFile, File, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import List, Optional

from app.db.session import get_db
from app.models.job import Job
from app.schemas.schemas import JobOut
from app.workers.celery_app import process_video
from app.core.config import settings

router = APIRouter()
TEMP_DIR = Path(settings.TEMP_DIR)
TEMP_DIR.mkdir(parents=True, exist_ok=True)

IMAGE_EXTS   = {".jpg", ".jpeg", ".png", ".bmp"}
VIDEO_EXTS   = {".mp4", ".avi", ".mov", ".mkv"}
VALID_MODELS = {"pothole", "signs", "speedbump"}


@router.post("/", response_model=JobOut, status_code=202)
async def submit_job(
    file: UploadFile = File(...),
    models: Optional[str] = Query(
        default=None,
        description=(
            "Comma-separated list of models to run. "
            "Allowed values: pothole, signs, speedbump. "
            "Example: ?models=pothole  or  ?models=pothole,signs  "
            "Defaults to all three when omitted."
        ),
    ),
    db: AsyncSession = Depends(get_db),
):
    ext = Path(file.filename).suffix.lower()
    if ext not in IMAGE_EXTS | VIDEO_EXTS:
        raise HTTPException(400, f"Unsupported file type: {ext}")

    if models:
        requested = {m.strip().lower() for m in models.split(",") if m.strip()}
        unknown   = requested - VALID_MODELS
        if unknown:
            raise HTTPException(
                400,
                f"Unknown model(s): {sorted(unknown)}. "
                f"Valid options: {sorted(VALID_MODELS)}"
            )
        if not requested:
            raise HTTPException(400, "models parameter is empty.")
        enabled = sorted(requested)
    else:
        enabled = sorted(VALID_MODELS)

    tmp_path = TEMP_DIR / f"{uuid.uuid4().hex}{ext}"
    tmp_path.write_bytes(await file.read())

    job = Job(filename=file.filename, enabled_models=",".join(enabled))
    db.add(job)
    await db.commit()
    await db.refresh(job)

    process_video.apply_async(
        args=[str(job.id), str(tmp_path), enabled],
        queue="processing",
    )
    return job


@router.get("/", response_model=List[JobOut])
async def list_jobs(
    skip: int = 0,
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Job).offset(skip).limit(limit).order_by(Job.created_at.desc())
    )
    return result.scalars().all()


@router.get("/{job_id}", response_model=JobOut)
async def get_job(job_id: str, db: AsyncSession = Depends(get_db)):
    job = await db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job
