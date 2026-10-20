"""
GET /mobile-config — runtime distances and thresholds for the mobile app
(Settings → Mobile app distances / Community reporting / Collision
prevention). Reachable with the app's API key or by a signed-in user.
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, api_error, get_principal
from app.core.config import settings
from app.core.db.session import get_db
from app.modules.m12_integration import mobile_config
from app.modules.m10_collision.collision.thresholds import CollisionLevel
from app.core.settings_service import CommunitySettings, MobileSettings

router = APIRouter()


class MobileCollisionOut(BaseModel):
    """Collision-prevention thresholds the app needs (settings section `collision`)."""
    candidate_radius_m: float = Field(..., description="Vehicles closer than this are analysed as conflict "
                                                       "candidates (metres).", examples=[500.0])
    stale_after_s: float = Field(..., description="Telemetry older than this is ignored (seconds).", examples=[30.0])
    levels: dict[str, CollisionLevel] = Field(..., description="Alert level → distance (m) / time-to-collision (s) "
                                                               "limits, for `CRITICAL`, `HIGH_WARNING`, `WARNING`, "
                                                               "`MONITOR`.")
    scenario_multipliers: dict[str, float] = Field(..., description="Distance multiplier per conflict scenario "
                                                                    "(`head_on`, `crossing`, `same_direction`).",
                                                   examples=[{"head_on": 1.0, "crossing": 1.0, "same_direction": 1.0}])


class MobileConfigOut(BaseModel):
    """Runtime configuration of the mobile app, one object per settings section."""
    mobile: MobileSettings = Field(..., description="Settings section `mobile` — distances (m) and alert cooldown (s).")
    community: CommunitySettings = Field(..., description="Settings section `community` — community-report voting "
                                                          "and expiry rules.")
    collision: MobileCollisionOut


@router.get("/", response_model=MobileConfigOut, summary="Get the mobile app configuration",
            openapi_extra={"x-access": "any_caller"})
async def get_mobile_config(db: AsyncSession = Depends(get_db), principal: Principal = Depends(get_principal)):
    """Distances and thresholds the mobile app works with (Settings → Mobile app distances /
    Community reporting / Collision prevention). The app should refresh it at start-up and
    periodically; changes made in the console apply within seconds."""
    if settings.AUTH_ENFORCED and principal.kind == "anonymous":
        raise api_error(401, "not_authenticated", "Please sign in.")
    return await mobile_config.payload(db)
