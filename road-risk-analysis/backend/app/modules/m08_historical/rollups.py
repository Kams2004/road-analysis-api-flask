"""
Daily summaries of `historical_events` (models/event_rollup.py) and the per-event columns they
are made of. Both are portable SQL (PostgreSQL in production, SQLite in tests).

    rebuild(db, cfg)              every summary, from all events (severity rules changed, first start)
    rebuild(db, cfg, days={d})    only these days (after an import touching them)
    ready(db, cfg)                summaries complete for the current severity rules?

app/modules/m08_historical/aggregates.py reads the summaries when `ready`, the raw events
otherwise (or when a filter the summaries do not carry is used: map area). The same per-event
columns feed both paths, so they give the same numbers. No driver / vehicle dimension: the
events and their summaries are anonymous (anonymity.py).
"""
from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime, time, timedelta
from typing import Iterable, Optional

from sqlalchemy import Integer, and_, case, cast, delete, extract, func, insert, literal, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.m08_historical.models.event_rollup import (
    EventDaily, EventDailyCell, EventDailyHour, EventRollupState,
)
from app.modules.m08_historical.models.historical_event import HistoricalEventRecord as E

logger = logging.getLogger(__name__)

ENTITY_KEYS = ("day", "source", "country", "transporter", "event_type", "severity", "speed_band", "excess_band")
ENTITY_SUMS = ("n", "speed_sum", "speed_n", "limit_sum", "limit_n", "excess_sum", "excess_n")
HOUR_KEYS = ("day", "source", "country", "event_type", "severity", "hour")
CELL_KEYS = ("day", "source", "country", "k_lat", "k_lon", "event_type", "severity", "road_name")
CELL_SUMS = ("n", "lat_sum", "lon_sum")

# Bands of the recorded speed (km/h) and of the excess over the limit (% of the limit): lower
# bounds of each band after the first. Labels "0-30" … "110+"; '' when the speed / limit is unknown.
SPEED_BANDS_KMH = (30, 50, 70, 90, 110)
EXCESS_BANDS_PCT = (10, 20, 30, 50)
ROLLUP_VERSION = "v2"          # dimensions of the summaries; a new value makes `ready` false → full rebuild


def band_labels(bounds: Iterable[float]) -> list[str]:
    b = [0, *bounds]
    return [f"{b[i]:g}-{b[i + 1]:g}" for i in range(len(b) - 1)] + [f"{b[-1]:g}+"]


SPEED_BANDS = band_labels(SPEED_BANDS_KMH)
EXCESS_BANDS = band_labels(EXCESS_BANDS_PCT)


def band_of(value: Optional[float], bounds: Iterable[float]) -> str:
    """Python twin of _band_expr (tests, per-event reads)."""
    if value is None:
        return ""
    labels = band_labels(bounds)
    for i, b in enumerate(bounds):
        if value < b:
            return labels[i]
    return labels[-1]


def _synonyms():
    from app.modules.m09_exception_patterns.analytics import _SEVERITY_SYNONYMS
    out: dict[str, list[str]] = {"low": [], "medium": [], "high": []}
    for k, v in _SEVERITY_SYNONYMS.items():
        out[v].append(k)
    return out


def severity_expr(cfg):
    """SQL twin of analytics.normalize_severity: the source's level, else (speeding with a
    speed and a limit) the excess over the limit, else unknown."""
    syn = _synonyms()
    s = func.lower(func.trim(func.coalesce(E.severity, "")))
    speeding = and_(E.event_type == "SPEEDING", E.speed_mps.isnot(None), E.speed_limit_mps > 0)
    excess = (E.speed_mps - E.speed_limit_mps) / E.speed_limit_mps * 100.0
    return case(
        (s.in_(syn["low"]), "low"), (s.in_(syn["medium"]), "medium"), (s.in_(syn["high"]), "high"),
        (and_(speeding, excess >= cfg.speeding_high_excess_pct), "high"),
        (and_(speeding, excess >= cfg.speeding_medium_excess_pct), "medium"),
        (speeding, "low"), else_="unknown")


def rules_hash(cfg) -> str:
    key = f"{ROLLUP_VERSION}|{cfg.speeding_medium_excess_pct}|{cfg.speeding_high_excess_pct}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]


EMPTY = literal_column("''")          # a literal, not a bound parameter: the same expression in SELECT and GROUP BY


def _k(col):
    return func.coalesce(col, EMPTY)


def as_day(v) -> date:
    v = v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
    return v.date()


def day_expr():
    return func.date(E.occurred_at)


def _band_expr(value, known, bounds: Iterable[float]):
    bounds = list(bounds)
    labels = band_labels(bounds)
    whens = [(and_(known, value < b), literal_column(f"'{labels[i]}'")) for i, b in enumerate(bounds)]
    return case(*whens, (known, literal_column(f"'{labels[-1]}'")), else_=EMPTY)


def speed_band_expr():
    return _band_expr(E.speed_mps * 3.6, E.speed_mps.isnot(None), SPEED_BANDS_KMH)


def excess_band_expr():
    known = and_(E.speed_mps.isnot(None), E.speed_limit_mps > 0)
    return _band_expr((E.speed_mps - E.speed_limit_mps) / E.speed_limit_mps * 100.0, known, EXCESS_BANDS_PCT)


# ── per-event columns (one row per event, named like the summary columns) ────

def entity_columns(cfg) -> list:
    has_speed = E.speed_mps.isnot(None)
    has_limit = E.speed_limit_mps > 0
    excess = (E.speed_mps - E.speed_limit_mps) / E.speed_limit_mps * 100.0
    return [
        day_expr().label("day"), E.source.label("source"), _k(E.country).label("country"),
        _k(E.transporter).label("transporter"), E.event_type.label("event_type"),
        severity_expr(cfg).label("severity"), speed_band_expr().label("speed_band"),
        excess_band_expr().label("excess_band"), literal(1).label("n"),
        case((has_speed, E.speed_mps), else_=0.0).label("speed_sum"),
        case((has_speed, 1), else_=0).label("speed_n"), E.speed_mps.label("speed_max"),
        case((has_limit, E.speed_limit_mps), else_=0.0).label("limit_sum"),
        case((has_limit, 1), else_=0).label("limit_n"),
        case((and_(has_speed, has_limit), excess), else_=0.0).label("excess_sum"),
        case((and_(has_speed, has_limit), 1), else_=0).label("excess_n"),
        E.occurred_at.label("first_at"), E.occurred_at.label("last_at"),
    ]


def hour_columns(cfg) -> list:
    return [day_expr().label("day"), E.source.label("source"), _k(E.country).label("country"),
            E.event_type.label("event_type"), severity_expr(cfg).label("severity"),
            cast(extract("hour", E.occurred_at), Integer).label("hour"), literal(1).label("n")]


def cell_columns(cfg) -> list:
    return [
        day_expr().label("day"), E.source.label("source"), _k(E.country).label("country"),
        cast(E.latitude * 1000, Integer).label("k_lat"), cast(E.longitude * 1000, Integer).label("k_lon"),
        E.event_type.label("event_type"), severity_expr(cfg).label("severity"), _k(E.road_name).label("road_name"),
        literal(1).label("n"), E.latitude.label("lat_sum"), E.longitude.label("lon_sum"),
        E.occurred_at.label("first_at"), E.occurred_at.label("last_at"),
    ]


def grouped(sub, keys: Iterable[str], sums: Iterable[str], extra: Iterable = ()):
    """SELECT keys, SUM(sums) …, MIN(first_at), MAX(last_at) FROM sub GROUP BY keys."""
    keys = list(keys)
    cols = [sub.c[k] for k in keys] + [func.sum(sub.c[s]).label(s) for s in sums] + list(extra)
    q = select(*cols)
    return q.group_by(*[sub.c[k] for k in keys]) if keys else q


def _entity_extra(sub):
    return [func.max(sub.c.speed_max).label("speed_max"), func.min(sub.c.first_at).label("first_at"),
            func.max(sub.c.last_at).label("last_at")]


def _cell_extra(sub):
    return [func.min(sub.c.first_at).label("first_at"), func.max(sub.c.last_at).label("last_at")]


# ── state ────────────────────────────────────────────────────────────────────

async def ready(db: AsyncSession, cfg) -> bool:
    try:
        st = await db.get(EventRollupState, 1)
    except Exception:                                      # tables not created (tests, first start)
        await db.rollback()
        return False
    return bool(st and st.built_at and st.rules == rules_hash(cfg))


async def _set_state(db: AsyncSession, built: Optional[datetime], rules: Optional[str]) -> None:
    st = await db.get(EventRollupState, 1)
    if st is None:
        db.add(EventRollupState(id=1, built_at=built, rules=rules))
    else:
        st.built_at, st.rules = built, rules


# ── build ────────────────────────────────────────────────────────────────────

async def _insert(db: AsyncSession, cfg, where: list) -> None:
    sub = select(*entity_columns(cfg)).where(*where).subquery()
    q = grouped(sub, ENTITY_KEYS, ENTITY_SUMS, _entity_extra(sub))
    await db.execute(insert(EventDaily).from_select(list(ENTITY_KEYS) + list(ENTITY_SUMS)
                                                    + ["speed_max", "first_at", "last_at"], q))
    sub = select(*hour_columns(cfg)).where(*where).subquery()
    await db.execute(insert(EventDailyHour).from_select(list(HOUR_KEYS) + ["n"], grouped(sub, HOUR_KEYS, ["n"])))
    sub = select(*cell_columns(cfg)).where(*where).subquery()
    await db.execute(insert(EventDailyCell).from_select(list(CELL_KEYS) + list(CELL_SUMS) + ["first_at", "last_at"],
                                                        grouped(sub, CELL_KEYS, CELL_SUMS, _cell_extra(sub))))


async def rebuild(db: AsyncSession, cfg, days: Optional[Iterable[date]] = None) -> int:
    """Rebuild the summaries (all, or only `days`) and commit. Day rebuilds are skipped while the
    summaries are not complete — the full rebuild that makes them complete covers those days."""
    tables = (EventDaily, EventDailyHour, EventDailyCell)
    if db.get_bind().dialect.name == "postgresql":          # one rebuild at a time (API, worker processes)
        from sqlalchemy import text
        await db.execute(text("SELECT pg_advisory_xact_lock(7412309002)"))
    if days is None:
        await _set_state(db, None, None)
        # Days older than the oldest raw event (purged after RAW_EVENT_RETENTION_DAYS) keep their summaries.
        oldest = (await db.execute(select(func.min(E.occurred_at)))).scalar()
        for t in tables:
            await db.execute(delete(t).where(t.day >= as_day(oldest)) if oldest is not None else delete(t))
        await _insert(db, cfg, [])
        await _set_state(db, datetime.utcnow(), rules_hash(cfg))
        await db.commit()
        n = 0
    else:
        days = sorted(set(days))
        if not days or not await ready(db, cfg):
            return 0
        for d in days:
            for t in tables:
                await db.execute(delete(t).where(t.day == d))
            start = datetime.combine(d, time.min)
            await _insert(db, cfg, [E.occurred_at >= start, E.occurred_at < start + timedelta(days=1)])
        await db.commit()
        n = len(days)
    from app.modules.m08_historical import agg_cache
    agg_cache.bump()
    logger.info("[EVENT-ROLLUPS] rebuilt %s", "all" if days is None else f"{n} day(s)")
    return n


async def rebuild_safely(db: AsyncSession, days: Optional[Iterable[date]] = None) -> None:
    """After an import: refresh the touched days; never fails the import itself."""
    from app.core import settings_service
    try:
        cfg = await settings_service.get_cached(db, "driver_behaviour")
        await rebuild(db, cfg, days)
    except Exception:
        await db.rollback()
        logger.exception("event summaries not refreshed (the next full rebuild will)")
