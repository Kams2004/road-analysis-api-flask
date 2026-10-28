"""
Grouped reads of the driving events for Historical analysis (Module 8) and Driver behaviour
(Module 9): every query is a GROUP BY in the database; only small grouped results reach Python.

A `Scope` is the period + filters. Each read uses the daily summaries (rollups.py) when they
are complete and carry the filters, the raw events otherwise:

    entity_rows   day / source / country / transporter / driver / vehicle / type / severity
                  → counts, speed / limit / excess sums, first / last     (summaries unless map area)
    hour_rows     weekday / hour (UTC) / type → counts                    (summaries unless map area,
                                                                           driver or vehicle)
    cell_rows     ~110 m squares / type / severity / road → counts, mean position, first / last
                                                                          (same rule)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable, Optional, Sequence

from sqlalchemy import Integer, String, cast, extract, func, literal_column, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.m08_historical import rollups as R
from app.modules.m08_historical.models.event_rollup import EventDaily, EventDailyCell, EventDailyHour
from app.modules.m08_historical.models.historical_event import HistoricalEventRecord as E


@dataclass
class Scope:
    start: Optional[datetime]
    end: datetime
    types: Optional[Sequence[str]] = None
    countries: Optional[Sequence[str]] = None
    driver: Optional[str] = None
    vehicle: Optional[str] = None
    bbox: Optional[tuple] = None
    rollups: bool = False                      # summaries complete for the current rules
    extra: dict = field(default_factory=dict)

    def shifted(self, start: Optional[datetime], end: datetime) -> "Scope":
        return Scope(start, end, self.types, self.countries, self.driver, self.vehicle, self.bbox, self.rollups)

    @property
    def last_day(self) -> date:
        return (self.end - timedelta(microseconds=1)).date()


def as_date(v) -> date:
    return v if isinstance(v, date) and not isinstance(v, datetime) else date.fromisoformat(str(v)[:10])


def as_dt(v) -> Optional[datetime]:
    if v is None or isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v))


def clean_countries(values: Optional[Iterable[str]]) -> Optional[list[str]]:
    out = []
    for v in values or ():
        for part in str(v).split(","):
            p = " ".join(part.split()).upper()
            if p and p not in out:
                out.append(p)
    return out or None


# ── filters ──────────────────────────────────────────────────────────────────

def raw_where(s: Scope) -> list:
    c = []
    if s.start is not None:
        c.append(E.occurred_at >= s.start)
    c.append(E.occurred_at < s.end)
    if s.types:
        c.append(E.event_type.in_(list(s.types)))
    if s.countries:
        c.append(func.coalesce(E.country, "").in_(s.countries))
    if s.driver:
        c.append(E.driver_external_id == s.driver)
    if s.vehicle:
        c.append(E.vehicle_external_id == s.vehicle)
    if s.bbox:
        so, w, n, ea = s.bbox
        c += [E.latitude.between(so, n), E.longitude.between(w, ea)]
    return c


def _roll_where(t, s: Scope, entity: bool) -> list:
    c = [t.day <= s.last_day]
    if s.start is not None:
        c.append(t.day >= s.start.date())
    if s.types:
        c.append(t.event_type.in_(list(s.types)))
    if s.countries:
        c.append(t.country.in_(s.countries))
    if entity and s.driver:
        c.append(t.driver == s.driver)
    if entity and s.vehicle:
        c.append(t.vehicle == s.vehicle)
    return c


# ── reads ────────────────────────────────────────────────────────────────────

async def entity_rows(db: AsyncSession, cfg, s: Scope, keys: Sequence[str]) -> list:
    if s.rollups and not s.bbox:
        sub = select(EventDaily).where(*_roll_where(EventDaily, s, True)).subquery()
    else:
        sub = select(*R.entity_columns(cfg)).where(*raw_where(s)).subquery()
    return (await db.execute(R.grouped(sub, keys, R.ENTITY_SUMS, R._entity_extra(sub)))).all()


async def hour_rows(db: AsyncSession, s: Scope) -> list:
    """(weekday 0 = Sunday, hour, event_type, n) — UTC."""
    if s.rollups and not (s.bbox or s.driver or s.vehicle):
        t = EventDailyHour
        q = (select(cast(extract("dow", t.day), Integer).label("dow"), t.hour, t.event_type, func.sum(t.n).label("n"))
             .where(*_roll_where(t, s, False)))
    else:
        q = (select(cast(extract("dow", E.occurred_at), Integer).label("dow"),
                    cast(extract("hour", E.occurred_at), Integer).label("hour"), E.event_type,
                    func.count().label("n")).where(*raw_where(s)))
    return (await db.execute(q.group_by("dow", "hour", "event_type"))).all()


async def cell_rows(db: AsyncSession, cfg, s: Scope, keys: Sequence[str]) -> list:
    if s.rollups and not (s.bbox or s.driver or s.vehicle):
        sub = select(EventDailyCell).where(*_roll_where(EventDailyCell, s, False)).subquery()
    else:
        sub = select(*R.cell_columns(cfg)).where(*raw_where(s)).subquery()
    return (await db.execute(R.grouped(sub, keys, R.CELL_SUMS, R._cell_extra(sub)))).all()


async def square_entities(db: AsyncSession, s: Scope, squares: Iterable[tuple[int, int]]) -> dict:
    """{(k_lat, k_lon): {driver or vehicle ids}} for these ~110 m squares (raw events)."""
    squares = list(squares)
    out: dict = {}
    k_lat, k_lon = cast(E.latitude * 1000, Integer), cast(E.longitude * 1000, Integer)
    who = func.coalesce(E.driver_external_id, E.vehicle_external_id)
    for i in range(0, len(squares), 400):
        q = (select(k_lat.label("k_lat"), k_lon.label("k_lon"), who.label("who")).distinct()
             .where(*raw_where(s), who.isnot(None), tuple_(k_lat, k_lon).in_(squares[i:i + 400])))
        for la, lo, w in (await db.execute(q)).all():
            out.setdefault((la, lo), set()).add(w)
    return out


async def top_roads_of(db: AsyncSession, s: Scope, kind: str, ids: Sequence[str], n: int = 1) -> dict:
    """Most frequent road of each driver / vehicle in `ids` (raw events, indexed by driver / vehicle)."""
    if not ids:
        return {}
    col = E.driver_external_id if kind == "driver" else E.vehicle_external_id
    q = (select(col, E.road_name, func.count().label("c")).where(*raw_where(s), col.in_(list(ids)),
                                                                   E.road_name.isnot(None))
         .group_by(col, E.road_name))
    best: dict = {}
    for k, road, c in (await db.execute(q)).all():
        if k not in best or c > best[k][1]:
            best[k] = (road, c)
    return {k: v[0] for k, v in best.items()}


async def countries(db: AsyncSession, rollups: bool) -> list[tuple[str, int]]:
    if rollups:
        q = select(EventDaily.country, func.sum(EventDaily.n)).group_by(EventDaily.country)
    else:
        q = select(R._k(E.country), func.count()).group_by(R._k(E.country))
    rows = (await db.execute(q)).all()
    return sorted(((c, int(n)) for c, n in rows if c), key=lambda r: -r[1])


# ── time buckets in SQL (PostgreSQL / SQLite) ─────────────────────────────────

def _dialect(db: AsyncSession) -> str:
    return db.get_bind().dialect.name


def bucket_expr(col, unit: str, dialect: str):
    """Start (date) of the day / week (Monday) / month of a date column."""
    if unit == "day":
        return func.date(col)
    if dialect == "postgresql":
        return func.date(func.date_trunc(literal_column(f"'{unit}'"), col))
    return func.date(col, "weekday 0", "-6 days") if unit == "week" else func.date(col, "start of month")


async def squares_per_bucket(db: AsyncSession, cfg, s: Scope, unit: str) -> dict:
    """{bucket date: distinct ~110 m squares with events} — one grouped read."""
    if s.rollups and not (s.bbox or s.driver or s.vehicle):
        sub = select(EventDailyCell).where(*_roll_where(EventDailyCell, s, False)).subquery()
    else:
        sub = select(*R.cell_columns(cfg)).where(*raw_where(s)).subquery()
    b = bucket_expr(sub.c.day, unit, _dialect(db)).label("b")
    sq = func.count(func.distinct(cast(sub.c.k_lat, String) + ":" + cast(sub.c.k_lon, String)))
    return {as_date(k): int(n) for k, n in (await db.execute(select(b, sq).group_by(b))).all()}


async def import_rows(db: AsyncSession) -> list:
    """(source, import minute, event_type, n, first occurred, last occurred) — raw, one grouped read."""
    minute = (func.date_trunc(literal_column("'minute'"), E.imported_at) if _dialect(db) == "postgresql"
              else func.strftime("%Y-%m-%dT%H:%M:00", E.imported_at)).label("minute")
    q = (select(E.source, minute, E.event_type, func.count().label("n"), func.min(E.occurred_at).label("first_at"),
                func.max(E.occurred_at).label("last_at")).group_by(E.source, minute, E.event_type))
    return (await db.execute(q)).all()
