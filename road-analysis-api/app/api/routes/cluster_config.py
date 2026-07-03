from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.db.session import get_db
from app.models.cluster_config import ClusterConfig

router = APIRouter()


class ClusterConfigOut(BaseModel):
    radius_m: float

    class Config:
        from_attributes = True


class ClusterConfigIn(BaseModel):
    radius_m: float


async def _get_or_create(db: AsyncSession) -> ClusterConfig:
    row = await db.get(ClusterConfig, 1)
    if not row:
        row = ClusterConfig(id=1, radius_m=50.0)
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row


@router.get("/", response_model=ClusterConfigOut)
async def get_cluster_config(db: AsyncSession = Depends(get_db)):
    """Return the current cluster radius configuration."""
    return await _get_or_create(db)


@router.put("/", response_model=ClusterConfigOut)
async def set_cluster_config(body: ClusterConfigIn, db: AsyncSession = Depends(get_db)):
    """
    Define the cluster radius (in metres).
    Two detections within this distance are grouped into the same cluster.
    """
    row = await _get_or_create(db)
    row.radius_m = body.radius_m
    await db.commit()
    await db.refresh(row)
    return row
