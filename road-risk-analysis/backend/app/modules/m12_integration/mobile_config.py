"""
Distances the mobile app works with (Settings → Mobile app distances):
helpers for the nearby / along-route endpoints and the payload of
GET /mobile-config.
"""
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.m10_collision.collision import thresholds
from app.core.settings_service import CommunitySettings, MobileSettings, get_cached


def clamp_radius(requested: Optional[float], visibility_radius_m: float) -> float:
    """Client radius capped at the visibility radius; absent/invalid → the radius itself."""
    if requested is None or requested <= 0:
        return float(visibility_radius_m)
    return float(min(requested, visibility_radius_m))


async def mobile_radius(db: AsyncSession, requested: Optional[float]) -> float:
    cfg: MobileSettings = await get_cached(db, "mobile")  # type: ignore[assignment]
    return clamp_radius(requested, cfg.visibility_radius_m)


async def route_corridor(db: AsyncSession, requested: Optional[float]) -> float:
    if requested is not None and requested > 0:
        return float(requested)
    cfg: MobileSettings = await get_cached(db, "mobile")  # type: ignore[assignment]
    return float(cfg.route_corridor_m)


async def payload(db: AsyncSession) -> dict:
    mobile: MobileSettings = await get_cached(db, "mobile")  # type: ignore[assignment]
    community: CommunitySettings = await get_cached(db, "community")  # type: ignore[assignment]
    lifecycle = await get_cached(db, "hazard_lifecycle")
    collision = await thresholds.refresh(db)
    return {
        "mobile": mobile.model_dump(),
        "community": community.model_dump(),
        # Only what the app needs to gate the hazard vote buttons (the server
        # still enforces it on POST /hazards/{id}/votes).
        "hazard_lifecycle": {"vote_max_distance_m": lifecycle.vote_max_distance_m},
        "collision": {
            "candidate_radius_m": collision.candidate_radius_m,
            "stale_after_s": collision.stale_after_s,
            "levels": {name: lvl.model_dump() for name, lvl in collision.levels.items()},
            "scenario_multipliers": collision.scenario_multipliers,
        },
    }
