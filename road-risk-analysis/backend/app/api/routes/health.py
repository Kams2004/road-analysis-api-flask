from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

router = APIRouter()


class HealthOut(BaseModel):
    """Liveness answer."""
    status: Literal["ok"] = Field(..., examples=["ok"])


@router.get("/health", response_model=HealthOut, summary="Liveness probe")
async def health():
    """Answers `{"status": "ok"}` while the API process is up. Does not check the database or the
    other services."""
    return {"status": "ok"}
