"""
Database side of Module 9 (Exception patterns): the settings, the stored data extent and — for
the PDF reports only, which filter event by event on a zone / path / town — the anonymous
exception rows of a period. The screens use grouped reads (views.py).
"""
from datetime import datetime
from typing import List, Optional, Sequence, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_service
from app.modules.m08_historical.models.historical_event import HistoricalEventRecord as E
from app.modules.m09_exception_patterns.settings import EXCEPTION_TYPES, ExceptionPatternSettings

MAX_ROWS = 200_000

# Never a driver / vehicle column: the events carry none.
COLUMNS = (E.id, E.source, E.event_type, E.occurred_at, E.latitude, E.longitude, E.country, E.transporter,
           E.speed_mps, E.speed_limit_mps, E.severity, E.road_name, E.grid_cell_id)

BBox = Tuple[float, float, float, float]   # south, west, north, east


async def get_settings(db: AsyncSession) -> ExceptionPatternSettings:
    return await settings_service.get_cached(db, "driver_behaviour")


def clean_types(types: Optional[Sequence[str]]) -> List[str]:
    if not types:
        return list(EXCEPTION_TYPES)
    out = []
    for t in types:
        for part in str(t).split(","):
            p = part.strip().upper()
            if p and p in EXCEPTION_TYPES and p not in out:
                out.append(p)
    return out or list(EXCEPTION_TYPES)


async def load_events(db: AsyncSession, start: Optional[datetime], end: Optional[datetime],
                      types: Sequence[str] = EXCEPTION_TYPES, bbox: Optional[BBox] = None,
                      countries: Optional[Sequence[str]] = None) -> list:
    """Raw anonymous rows of a period (at most MAX_ROWS, oldest first) — reports only."""
    conds = [E.event_type.in_(list(types))]
    if countries:
        conds.append(func.coalesce(E.country, "").in_(list(countries)))
    if start is not None:
        conds.append(E.occurred_at >= start)
    if end is not None:
        conds.append(E.occurred_at < end)
    if bbox:
        s, w, n, e = bbox
        conds += [E.latitude.between(s, n), E.longitude.between(w, e)]
    rows = (await db.execute(select(*COLUMNS).where(*conds).order_by(E.occurred_at).limit(MAX_ROWS))).all()
    return list(rows)


async def data_extent(db: AsyncSession) -> dict:
    row = (await db.execute(select(func.count(), func.min(E.occurred_at), func.max(E.occurred_at)))).one()
    return {"events": row[0], "first_event_at": row[1], "last_event_at": row[2]}
