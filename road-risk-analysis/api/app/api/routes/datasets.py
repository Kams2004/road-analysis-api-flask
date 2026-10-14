from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import List, Optional

from app.db.session import get_db
from app.models.dataset import Dataset
from app.schemas.schemas import DatasetOut, DatasetExportIn
from app.services.dataset_service import export_dataset

router = APIRouter()

VALID_TYPES = {"pothole", "traffic_sign", "speed_bump"}


@router.post("/export", response_model=DatasetOut, status_code=201)
async def create_dataset(
    body: DatasetExportIn,
    db: AsyncSession = Depends(get_db),
):
    """
    Snapshot all validated labels for a detection type into a new immutable version.
    Copies crop images into the datasets bucket and writes manifest + metadata JSON.
    """
    if body.detection_type not in VALID_TYPES:
        raise HTTPException(400, f"detection_type must be one of {sorted(VALID_TYPES)}")
    try:
        dataset = await export_dataset(body.detection_type, body.created_by, db)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return dataset


@router.get("/", response_model=List[DatasetOut])
async def list_datasets(
    detection_type: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """List all exported dataset versions, optionally filtered by detection_type."""
    q = select(Dataset).order_by(Dataset.detection_type, Dataset.version.desc())
    if detection_type:
        q = q.where(Dataset.detection_type == detection_type)
    rows = (await db.execute(q)).scalars().all()
    return rows


@router.get("/{dataset_id}", response_model=DatasetOut)
async def get_dataset(dataset_id: str, db: AsyncSession = Depends(get_db)):
    ds = await db.get(Dataset, dataset_id)
    if not ds:
        raise HTTPException(404, "Dataset not found")
    return ds
