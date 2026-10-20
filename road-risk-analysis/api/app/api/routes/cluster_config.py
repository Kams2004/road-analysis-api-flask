"""
DEPRECATED — the single global clustering radius, superseded by the global
Cluster Configuration (`/clustering/config`: urban / out-of-agglomeration
profiles, per-type values, zones). Kept as a thin wrapper so old clients
keep working: GET answers the default radius of the profile used outside
every zone; PUT sets the default radius of BOTH profiles.
"""
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, get_principal
from app.db.session import get_db
from app.services import audit
from app.services.clustering import service as clustering_service
from app.services.settings_service import update_section

router = APIRouter()


class ClusterConfigOut(BaseModel):
    """Global clustering radius (legacy view of the Cluster Configuration)."""
    radius_m: float = Field(description="Default radius of the profile used outside every zone "
                                        "(`unzoned_environment`), metres.", examples=[60.0])


class ClusterConfigIn(BaseModel):
    """New default radius for both profiles."""
    radius_m: float = Field(ge=1, le=10000, description="Clustering radius in metres.", examples=[50.0])


async def _out(db: AsyncSession) -> ClusterConfigOut:
    s = await clustering_service.load_settings(db)
    return ClusterConfigOut(radius_m=s.profile(s.unzoned_environment).default_radius_m)


@router.get("/", response_model=ClusterConfigOut, summary="Get the global clustering radius (deprecated)",
            deprecated=True)
async def get_cluster_config(db: AsyncSession = Depends(get_db)):
    """
    **Deprecated** — use `GET /clustering/config`. Answers the default radius of the profile applied
    outside every cluster zone.
    """
    return await _out(db)


@router.put("/", response_model=ClusterConfigOut, summary="Set the global clustering radius (deprecated)",
            deprecated=True)
async def set_cluster_config(body: ClusterConfigIn, request: Request, db: AsyncSession = Depends(get_db),
                             principal: Principal = Depends(get_principal)):
    """
    **Deprecated** — use `PUT /clustering/config`. Sets `urban.default_radius_m` and
    `rural.default_radius_m` to `radius_m` (per-type values and zones are untouched). Existing hazards
    are re-clustered only by `POST /clustering/rebuild-hazards`.
    """
    before, after = await update_section(db, "clustering", {
        "urban": {"default_radius_m": body.radius_m}, "rural": {"default_radius_m": body.radius_m}},
        principal.label)
    audit.record(db, request, "clustering.update", principal=principal, object_type="settings",
                 object_id="clustering", details=audit.diff(before, after))
    await db.commit()
    return await _out(db)
