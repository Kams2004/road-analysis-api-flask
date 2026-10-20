"""
Back-fill of the structured address (app.services.geocoding) on detections
stored before it existed, then of their hazards.

Idempotent and throttled: only located detections with `geocoded_at IS NULL`
are looked up, hazard members first (they are what the Hazard Track shows),
one Nominatim request per second at most (the geocoder's own lock), nearby
detections sharing a cached grid cell. A run stops after `limit` detections
or after MAX_CONSECUTIVE_FAILURES failed lookups in a row (offline, blocked),
and only one run per process at a time.

Started by POST /hazards/geocode/backfill, by `python -m app.cli
geocode-backfill`, and (bounded by settings.GEOCODING_BACKFILL_ON_STARTUP) in
the background at API start-up. Hazards whose members got an address are
re-aggregated so they pick up the address of their most central member.
"""
import asyncio
import logging
from dataclasses import dataclass, field, fields
from datetime import datetime
from typing import Optional

from sqlalchemy import func, select, true

from app.core.config import settings
from app.models.detection import Detection
from app.models.hazard import Hazard
from app.services import geocoding

logger = logging.getLogger(__name__)

MAX_CONSECUTIVE_FAILURES = 5
COMMIT_EVERY = 20


@dataclass
class BackfillState:
    running: bool = False
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    limit: int = 0
    processed: int = 0
    updated: int = 0
    failed: int = 0
    hazards_updated: int = 0
    stopped_reason: Optional[str] = None   # done | limit | failures | disabled | error
    _task: Optional[asyncio.Task] = field(default=None, repr=False)

    def public(self) -> dict:
        # Not asdict(): it deep-copies every field, and a running asyncio.Task cannot be copied.
        return {f.name: getattr(self, f.name) for f in fields(self) if not f.name.startswith("_")}


state = BackfillState()


def _pending_query():
    return select(Detection).where(
        Detection.geocoded_at.is_(None),
        Detection.latitude.isnot(None), Detection.longitude.isnot(None),
    )


async def pending_count(db) -> int:
    return (await db.execute(select(func.count()).select_from(_pending_query().subquery()))).scalar() or 0


async def run(db, limit: int = 200) -> BackfillState:
    """Geocode up to `limit` detections lacking an address (see module doc)."""
    from app.services import hazard_service
    state.__dict__.update(running=True, started_at=datetime.utcnow(), finished_at=None, limit=limit,
                          processed=0, updated=0, failed=0, hazards_updated=0, stopped_reason=None)
    try:
        if not settings.GEOCODING_ENABLED:
            state.stopped_reason = "disabled"
            return state
        hazards: set[str] = set()
        streak, seen_ids = 0, set()
        while state.processed < limit:
            batch = (await db.execute(
                _pending_query().where(Detection.id.notin_(seen_ids) if seen_ids else true())
                .order_by(Detection.hazard_id.is_(None), Detection.created_at.desc())
                .limit(min(COMMIT_EVERY, limit - state.processed))
            )).scalars().all()
            if not batch:
                state.stopped_reason = "done"
                break
            for det in batch:
                seen_ids.add(det.id)
                geo = await asyncio.to_thread(geocoding.reverse_geocode_details, det.latitude, det.longitude)
                state.processed += 1
                if geocoding.has_address(geo):
                    geocoding.apply_to(det, geo, keep_location_name=True)
                    det.geocoded_at = datetime.utcnow()
                    state.updated += 1
                    streak = 0
                    if det.hazard_id:
                        hazards.add(det.hazard_id)
                else:
                    state.failed += 1
                    streak += 1
                    if streak >= MAX_CONSECUTIVE_FAILURES:
                        break
            await db.commit()
            if streak >= MAX_CONSECUTIVE_FAILURES:
                state.stopped_reason = "failures"
                logger.warning("Geocoding back-fill stopped: %d failed lookups in a row", streak)
                break
        else:
            state.stopped_reason = "limit"
        if hazards:
            await hazard_service.recompute_ids(db, hazards)
            await db.commit()
            state.hazards_updated = len(hazards)
        # Hazards whose members already had an address but which were never
        # aggregated with it (built before the columns existed).
        stale = (await db.execute(select(Hazard.id).where(
            Hazard.display_location.is_(None),
            Hazard.id.in_(select(Detection.hazard_id).where(Detection.display_location.isnot(None))),
        ).limit(500))).scalars().all()
        if stale:
            await hazard_service.recompute_ids(db, stale)
            await db.commit()
            state.hazards_updated += len(stale)
        logger.info("Geocoding back-fill: %d processed, %d updated, %d failed, %d hazard(s) refreshed (%s)",
                    state.processed, state.updated, state.failed, state.hazards_updated, state.stopped_reason)
        return state
    except Exception:
        state.stopped_reason = "error"
        logger.exception("Geocoding back-fill failed")
        await db.rollback()
        return state
    finally:
        state.running = False
        state.finished_at = datetime.utcnow()


def start_background(limit: int) -> bool:
    """Run `run()` in a background task with its own session. False when a
    run is already in progress."""
    if state.running or (state._task is not None and not state._task.done()):
        return False
    from app.db.session import AsyncSessionLocal

    async def _go():
        async with AsyncSessionLocal() as db:
            await run(db, limit)

    state.running = True
    state._task = asyncio.get_running_loop().create_task(_go())
    return True
