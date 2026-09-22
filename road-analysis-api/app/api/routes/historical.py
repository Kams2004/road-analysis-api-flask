from datetime import date
from typing import List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_db
from app.services import historical_service
from app.services.historical.models import HistoricalEvent, ImportSummary, ZoneRiskProfile
from app.services.historical.sources.manual import ManualEventSource
from app.services.historical.sources.ymane import YmaneAuthError, YmaneEventSource, YmaneRequestError

router = APIRouter()


# ── Import ──────────────────────────────────────────────────────────────────

class ImportIn(BaseModel):
    source: str                              # "manual" | "ymane"
    start: date
    end: date
    events: Optional[List[HistoricalEvent]] = None  # required for source="manual", ignored otherwise


@router.post("/import", response_model=ImportSummary)
async def import_historical_events(body: ImportIn, db: AsyncSession = Depends(get_db)):
    if body.source == "manual":
        if not body.events:
            raise HTTPException(422, "source=manual requires a non-empty 'events' list")
        source = ManualEventSource(body.events)
    elif body.source == "ymane":
        source = YmaneEventSource()
    else:
        raise HTTPException(422, f"Unknown source '{body.source}' — expected 'manual' or 'ymane'")

    try:
        return await historical_service.import_events(source, body.start, body.end, db)
    except YmaneAuthError as e:
        raise HTTPException(502, f"Ymane authentication failed: {e}")
    except YmaneRequestError as e:
        raise HTTPException(502, f"Ymane request failed: {e}")


# ── Read: zone / route risk (what the mobile app's route-colouring idea
#    would call) ─────────────────────────────────────────────────────────────

@router.get("/risk/zone", response_model=List[ZoneRiskProfile])
async def zone_risk(
    latitude: float,
    longitude: float,
    radius_m: float = 500.0,
    db: AsyncSession = Depends(get_db),
):
    return await historical_service.get_zone_risk_profiles(latitude, longitude, radius_m, db)


class RouteRiskIn(BaseModel):
    waypoints: List[Tuple[float, float]]  # [(lat, lon), ...] — the full route polyline
    corridor_m: float = settings.HISTORICAL_ROUTE_CORRIDOR_M


@router.post("/risk/route", response_model=List[ZoneRiskProfile])
async def route_risk(body: RouteRiskIn, db: AsyncSession = Depends(get_db)):
    if len(body.waypoints) < 2:
        raise HTTPException(422, "At least 2 waypoints required.")
    return await historical_service.get_zone_risk_profiles_along_route(body.waypoints, body.corridor_m, db)
