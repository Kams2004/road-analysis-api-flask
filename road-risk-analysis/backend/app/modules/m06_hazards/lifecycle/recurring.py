"""
Recurring spots: places where hazards of one type keep coming back (a spot
where vehicles park every day, a pothole patched again and again) — useful
for route risk (Module 4).

  * When a hazard opens (policy recurring_enabled) within the radius of ≥
    recurring_min_occurrences hazards of the same type resolved in the last
    recurring_window_days, it is flagged `recurring_spot` with
    `recurrence_count` = that number.
  * GET /hazards/recurring-spots groups hazards opened in the window into
    spots (greedy clustering by radius) and lists those with ≥ N occurrences.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.m06_hazards.models.hazard import PUBLIC_STATUSES, Hazard, HazardStatus
from app.modules.m06_hazards.lifecycle.settings import HazardLifecycleSettings
from app.core.spatial import _haversine_m


# ── pure rules ────────────────────────────────────────────────────────────────

def count_recurrences(resolved: Iterable, lat: float, lon: float, radius_m: float, since: datetime,
                      det_type: Optional[str] = None, exclude_id: Optional[str] = None) -> int:
    """Hazards resolved at/after `since` within `radius_m` of (lat, lon)."""
    n = 0
    for h in resolved:
        if exclude_id is not None and h.id == exclude_id:
            continue
        if det_type is not None and h.type != det_type:
            continue
        if h.status != HazardStatus.resolved.value or h.resolved_at is None or h.resolved_at < since:
            continue
        if _haversine_m(lat, lon, h.latitude, h.longitude) <= radius_m:
            n += 1
    return n


@dataclass
class Spot:
    type: str
    latitude: float
    longitude: float
    hazards: list = field(default_factory=list)

    @property
    def occurrences(self) -> int:
        return len(self.hazards)

    def add(self, h) -> None:
        self.hazards.append(h)
        n = len(self.hazards)
        self.latitude = sum(x.latitude for x in self.hazards) / n
        self.longitude = sum(x.longitude for x in self.hazards) / n


def find_spots(hazards: Iterable, min_occurrences: int, radius_m: Optional[float] = None) -> list[Spot]:
    """Greedy clustering, oldest first: a hazard joins the nearest spot of its
    type whose centroid lies within the radius (`radius_m`, else the hazard's
    own radius_m), else starts one. Returns spots with ≥ min_occurrences,
    most occurrences first."""
    spots: list[Spot] = []
    for h in sorted(hazards, key=lambda x: x.created_at or datetime.min):
        r = radius_m or h.radius_m or 50.0
        best, best_d = None, None
        for s in spots:
            if s.type != h.type:
                continue
            d = _haversine_m(h.latitude, h.longitude, s.latitude, s.longitude)
            if d <= r and (best_d is None or d < best_d):
                best, best_d = s, d
        if best is None:
            best = Spot(type=h.type, latitude=h.latitude, longitude=h.longitude)
            spots.append(best)
        best.add(h)
    out = [s for s in spots if s.occurrences >= min_occurrences]
    out.sort(key=lambda s: (-s.occurrences, s.type))
    return out


# ── database ─────────────────────────────────────────────────────────────────

async def flag_new_hazard(db: AsyncSession, hazard: Hazard, cfg: HazardLifecycleSettings,
                          now: Optional[datetime] = None) -> bool:
    from app.modules.m06_hazards.hazard_service import _bbox
    now = now or datetime.utcnow()
    radius = cfg.recurring_radius_m or hazard.radius_m
    since = now - timedelta(days=cfg.recurring_window_days)
    dlat, dlon = _bbox(hazard.latitude, hazard.longitude, radius)
    rows = (await db.execute(select(Hazard).where(
        Hazard.status == HazardStatus.resolved.value, Hazard.type == hazard.type,
        Hazard.resolved_at >= since,
        Hazard.latitude.between(hazard.latitude - dlat, hazard.latitude + dlat),
        Hazard.longitude.between(hazard.longitude - dlon, hazard.longitude + dlon),
    ))).scalars().all()
    n = count_recurrences(rows, hazard.latitude, hazard.longitude, radius, since, hazard.type, hazard.id)
    hazard.recurrence_count = n
    hazard.recurring_spot = n >= cfg.recurring_min_occurrences
    return hazard.recurring_spot


async def list_spots(db: AsyncSession, cfg: HazardLifecycleSettings, *, det_type: Optional[str] = None,
                     bbox: Optional[tuple[float, float, float, float]] = None,
                     window_days: Optional[int] = None, min_occurrences: Optional[int] = None,
                     now: Optional[datetime] = None) -> list[Spot]:
    """Spots from hazards opened in the window. Without `det_type`, only the
    types whose policy enables recurring detection."""
    from app.modules.m06_hazards.lifecycle.policy import type_policy
    now = now or datetime.utcnow()
    since = now - timedelta(days=window_days or cfg.recurring_window_days)
    q = select(Hazard).where(Hazard.created_at >= since, Hazard.status.in_(PUBLIC_STATUSES))   # never candidates
    if det_type:
        q = q.where(Hazard.type == det_type)
    if bbox:
        min_lat, min_lon, max_lat, max_lon = bbox
        q = q.where(Hazard.latitude.between(min_lat, max_lat), Hazard.longitude.between(min_lon, max_lon))
    rows = (await db.execute(q)).scalars().all()
    if not det_type:
        rows = [h for h in rows if type_policy(cfg, h.type).recurring_enabled]
    return find_spots(rows, min_occurrences or cfg.recurring_min_occurrences, cfg.recurring_radius_m)
