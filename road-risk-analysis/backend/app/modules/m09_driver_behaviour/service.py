"""
Database side of Module 5: behaviour events are read from `historical_events`
(the single event store shared with Module 4 — never copied), filtered by
period / type / driver / vehicle / bounding box, then aggregated in Python by
analytics.py.
"""
from datetime import datetime
from typing import List, Optional, Sequence, Tuple

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_service
from app.modules.m08_historical.models.historical_event import HistoricalEventRecord as E
from app.modules.m09_driver_behaviour.settings import BEHAVIOUR_TYPES, DriverBehaviourSettings

MAX_ROWS = 200_000

COLUMNS = (E.id, E.source, E.event_type, E.occurred_at, E.latitude, E.longitude, E.driver_external_id,
           E.vehicle_external_id, E.speed_mps, E.speed_limit_mps, E.severity, E.road_name, E.grid_cell_id)

BBox = Tuple[float, float, float, float]   # south, west, north, east


async def get_settings(db: AsyncSession) -> DriverBehaviourSettings:
    return await settings_service.get_cached(db, "driver_behaviour")


def clean_types(types: Optional[Sequence[str]]) -> List[str]:
    if not types:
        return list(BEHAVIOUR_TYPES)
    out = []
    for t in types:
        for part in str(t).split(","):
            p = part.strip().upper()
            if p and p in BEHAVIOUR_TYPES and p not in out:
                out.append(p)
    return out or list(BEHAVIOUR_TYPES)


def _filters(start: Optional[datetime], end: Optional[datetime], types: Sequence[str],
             driver: Optional[str], vehicle: Optional[str], bbox: Optional[BBox], search: Optional[str] = None):
    conds = [E.event_type.in_(list(types))]
    if start is not None:
        conds.append(E.occurred_at >= start)
    if end is not None:
        conds.append(E.occurred_at < end)
    if driver:
        conds.append(E.driver_external_id == driver)
    if vehicle:
        conds.append(E.vehicle_external_id == vehicle)
    if bbox:
        s, w, n, e = bbox
        conds += [E.latitude.between(s, n), E.longitude.between(w, e)]
    if search:
        like = f"%{search.strip()}%"
        conds.append(or_(E.road_name.ilike(like), E.driver_external_id.ilike(like),
                         E.vehicle_external_id.ilike(like), E.source_event_id.ilike(like)))
    return conds


async def load_events(db: AsyncSession, start: Optional[datetime], end: Optional[datetime],
                      types: Sequence[str] = BEHAVIOUR_TYPES, driver: Optional[str] = None,
                      vehicle: Optional[str] = None, bbox: Optional[BBox] = None) -> list:
    rows = (await db.execute(
        select(*COLUMNS).where(*_filters(start, end, types, driver, vehicle, bbox))
        .order_by(E.occurred_at).limit(MAX_ROWS)
    )).all()
    return list(rows)


async def page_events(db: AsyncSession, start, end, types, driver, vehicle, bbox, search,
                      sort: str, offset: int, limit: int) -> tuple[int, list]:
    conds = _filters(start, end, types, driver, vehicle, bbox, search)
    total = (await db.execute(select(func.count()).select_from(E).where(*conds))).scalar_one()
    order = {
        "-occurred_at": E.occurred_at.desc(), "occurred_at": E.occurred_at.asc(),
        "-speed": E.speed_mps.desc().nulls_last(), "event_type": E.event_type.asc(),
    }.get(sort, E.occurred_at.desc())
    rows = (await db.execute(
        select(*COLUMNS, E.source_event_id, E.matched_latitude, E.matched_longitude)
        .where(*conds).order_by(order, E.id).offset(offset).limit(limit)
    )).all()
    return total, list(rows)


async def data_extent(db: AsyncSession) -> dict:
    row = (await db.execute(
        select(func.count(), func.min(E.occurred_at), func.max(E.occurred_at))
        .where(E.event_type.in_(list(BEHAVIOUR_TYPES)))
    )).one()
    return {"events": row[0], "first_event_at": row[1], "last_event_at": row[2]}
