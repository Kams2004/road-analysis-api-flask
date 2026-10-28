"""
Place names of positions, stored: one reverse-geocoding lookup (geocoding.py, Nominatim) per
~110 m square (lat / lon rounded to 0.001°), kept in `place_names` — the in-process cache of
geocoding.py is lost at each restart, and the driving events cover thousands of places.

    names(db, points)      {(lat, lon): "Rue Joss, Akwa, Douala"} for the squares already named;
                           the others are queued (Redis set) for the next lookup run
    attach(db, rows, …)    adds `place` to answer rows (zones, cells, events)
    fill(db)               worker (beat `geocode_places`): queued squares first, then the busiest
                           squares of the driving events, within GEOCODING_DAILY_BUDGET lookups a
                           day (the public Nominatim forbids bulk geocoding: self-host it via
                           GEOCODING_URL for more)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional

from sqlalchemy import Boolean, DateTime, Integer, String, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import settings
from app.core.db.session import Base

logger = logging.getLogger(__name__)

WANTED_KEY = "geo:wanted"
BUDGET_KEY = "geo:budget:{day}"
RETRY_FAILED_AFTER = timedelta(days=7)
RUN_LIMIT = 30                       # lookups per run (≈ 30 s at 1 request / s: video jobs keep their worker slots)


class PlaceName(Base):
    """Address of a ~110 m square (keys = round(lat × 1000), round(lon × 1000))."""
    __tablename__ = "place_names"

    k_lat:     Mapped[int]  = mapped_column(Integer, primary_key=True)
    k_lon:     Mapped[int]  = mapped_column(Integer, primary_key=True)
    display:   Mapped[str | None] = mapped_column(String, nullable=True)   # "Rue Joss, Akwa, Douala"
    road:      Mapped[str | None] = mapped_column(String, nullable=True)
    town:      Mapped[str | None] = mapped_column(String, nullable=True)
    region:    Mapped[str | None] = mapped_column(String, nullable=True)
    country:   Mapped[str | None] = mapped_column(String, nullable=True)
    ok:        Mapped[bool] = mapped_column(Boolean, default=False)       # False = lookup failed / no address
    looked_up_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


def key(lat: float, lon: float) -> tuple[int, int]:
    return round(lat * 1000), round(lon * 1000)


def _redis():
    from app.modules.m07_ingestion import job_slots
    return job_slots.client()


async def names(db: AsyncSession, points: Iterable[tuple[float, float]], queue: bool = True) -> dict:
    """{key: display} of the named squares among `points`; unnamed ones are queued for lookup."""
    keys = list({key(la, lo) for la, lo in points if la is not None and lo is not None})
    out: dict = {}
    known: set = set()
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        try:
            rows = (await db.execute(select(PlaceName.k_lat, PlaceName.k_lon, PlaceName.display, PlaceName.ok)
                                     .where(tuple_(PlaceName.k_lat, PlaceName.k_lon).in_(chunk)))).all()
        except Exception:                                  # table missing (tests): names are optional
            await db.rollback()
            return {}
        for la, lo, disp, ok in rows:
            known.add((la, lo))
            if ok and disp:
                out[(la, lo)] = disp
    missing = [k for k in keys if k not in known]
    if queue and missing and settings.GEOCODING_ENABLED:
        try:
            _redis().sadd(WANTED_KEY, *[f"{la}:{lo}" for la, lo in missing[:500]])
        except Exception:
            pass
    return out


async def attach(db: AsyncSession, rows: list, lat: str = "latitude", lon: str = "longitude",
                 field: str = "place") -> list:
    """Set `field` on each row (dict or object) from its position (None until named)."""
    def get(r, k):
        return r.get(k) if isinstance(r, dict) else getattr(r, k, None)
    found = await names(db, [(get(r, lat), get(r, lon)) for r in rows])
    for r in rows:
        la, lo = get(r, lat), get(r, lon)
        v = found.get(key(la, lo)) if la is not None and lo is not None else None
        if isinstance(r, dict):
            r[field] = v
        else:
            setattr(r, field, v)
    return rows


# ── lookups (worker) ─────────────────────────────────────────────────────────

async def _busiest_unnamed(db: AsyncSession, limit: int) -> list[tuple[int, int, float, float]]:
    """Squares of the driving events with most events and no name yet: (k_lat, k_lon, lat, lon)."""
    from app.modules.m08_historical.models.event_rollup import EventDailyCell as C
    lat = func.sum(C.lat_sum) / func.sum(C.n)
    lon = func.sum(C.lon_sum) / func.sum(C.n)
    q = (select(lat.label("lat"), lon.label("lon"), func.sum(C.n).label("n"))
         .group_by(C.k_lat, C.k_lon).order_by(func.sum(C.n).desc()).limit(limit * 5))
    out, seen = [], set()
    rows = (await db.execute(q)).all()
    named = {(r.k_lat, r.k_lon) for r in (await db.execute(select(PlaceName.k_lat, PlaceName.k_lon).where(
        tuple_(PlaceName.k_lat, PlaceName.k_lon).in_([key(r.lat, r.lon) for r in rows])))).all()} if rows else set()
    for r in rows:
        k = key(r.lat, r.lon)
        if k in named or k in seen:
            continue
        seen.add(k)
        out.append((k[0], k[1], r.lat, r.lon))
        if len(out) >= limit:
            break
    return out


async def fill(db: AsyncSession, limit: int = RUN_LIMIT) -> dict:
    """One lookup run: queued squares (shown on screen) first, then the busiest event squares;
    at most `limit` lookups and GEOCODING_DAILY_BUDGET a day."""
    from app.modules.m03_maps import geocoding
    if not settings.GEOCODING_ENABLED:
        return {"skipped": "geocoding disabled"}
    r = _redis()
    budget_key = BUDGET_KEY.format(day=date.today().isoformat())
    used = int(r.get(budget_key) or 0)
    left = min(limit, max(0, settings.GEOCODING_DAILY_BUDGET - used))
    todo: list[tuple[int, int, float, float]] = []
    while len(todo) < left:
        item = r.spop(WANTED_KEY)
        if not item:
            break
        la, lo = (int(x) for x in item.split(":"))
        todo.append((la, lo, la / 1000, lo / 1000))
    if len(todo) < left:
        try:
            todo += await _busiest_unnamed(db, left - len(todo))
        except Exception:
            await db.rollback()
    done = named = 0
    for la, lo, lat, lon in todo:
        row = await db.get(PlaceName, (la, lo))
        if row is not None and (row.ok or datetime.utcnow() - row.looked_up_at < RETRY_FAILED_AFTER):
            continue
        geo = await asyncio.to_thread(geocoding.reverse_geocode_details, lat, lon)
        done += 1
        ok = bool(geo.get("display"))
        named += ok
        if row is None:
            row = PlaceName(k_lat=la, k_lon=lo)
            db.add(row)
        row.display, row.road, row.town = geo.get("display"), geo.get("road"), geo.get("town")
        row.region, row.country, row.ok, row.looked_up_at = geo.get("region"), geo.get("country"), ok, datetime.utcnow()
        await db.commit()
    if done:
        r.incrby(budget_key, done)
        r.expire(budget_key, 2 * 86400)
        if named:
            from app.modules.m08_historical import agg_cache
            agg_cache.bump()                               # cached answers pick the new names up
    return {"looked_up": done, "named": named, "budget_left": max(0, left - done)}
