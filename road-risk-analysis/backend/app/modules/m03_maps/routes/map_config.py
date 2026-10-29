"""
GET /map-config — the base map the caller should display. Signed-in users
get their own preference (RRAP-01-012); the mobile app (API key) and
anonymous callers get the platform default.
"""
from typing import Literal, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, api_error, get_principal
from app.core.config import settings
from app.core.db.session import get_db
from app.modules.m03_maps.map_tiles import resolve_tiles
from app.core.settings_service import MapSettings, get_section

router = APIRouter()


class MapConfigOut(BaseModel):
    """Base map to display (Leaflet-style tile URL templates)."""
    provider: Literal["osm", "google"] = Field(..., description="Provider actually served.", examples=["osm"])
    tile_urls: list[str] = Field(..., description="Tile URL templates (`{z}/{x}/{y}`; OSM may use `{s}` subdomains "
                                                  "expanded into several URLs). Google URLs embed a session and key.",
                                 examples=[["https://a.tile.openstreetmap.org/{z}/{x}/{y}.png"]])
    attribution: str = Field(..., examples=["© OpenStreetMap contributors"])
    max_zoom: int = Field(..., examples=[19])
    requested: str = Field(..., description="Provider the caller asked for (user preference or platform default).",
                           examples=["google"])
    fallback_reason: Optional[str] = Field(..., description="Why OSM is served instead of the requested Google map: "
                                                            "`google_not_configured` or `google_unavailable`; null "
                                                            "otherwise.", examples=[None])
    google_configured: bool = Field(..., description="A Google Maps Platform key is configured.")


@router.get("/", response_model=MapConfigOut, summary="Get the base map to display",
            openapi_extra={"x-access": "any_caller"})
async def map_config(db: AsyncSession = Depends(get_db), principal: Principal = Depends(get_principal)):
    """Tiles for the caller: a signed-in user gets their own provider preference (RRAP-01-012), the
    mobile app (API key) and anonymous callers get the platform default (Settings → Platform → Maps).
    When Google is requested but not configured or unreachable, OpenStreetMap is served with a
    `fallback_reason`."""
    if settings.AUTH_ENFORCED and principal.kind == "anonymous":
        raise api_error(401, "not_authenticated", "Please sign in.")
    cfg: MapSettings = await get_section(db, "maps")  # type: ignore[assignment]
    pref = principal.user.map_provider.value if principal.user is not None else None
    tiles = await resolve_tiles(cfg, pref)
    return {**tiles.as_dict(), "google_configured": bool(cfg.google_api_key)}


@router.get("/style.json", summary="Get the base map as a MapLibre style",
            openapi_extra={"x-access": "any_caller"},
            responses={200: {"description": "MapLibre style v8 (one raster source `osm`, one layer `osm-tiles`).",
                             "content": {"application/json": {}}}})
async def map_style(db: AsyncSession = Depends(get_db), principal: Principal = Depends(get_principal)):
    """The same base map as `GET /map-config/` as a MapLibre style document — what the mobile app's
    offline map packs need (MapLibre downloads packs from a style URL). Its tiles are exactly the
    `tile_urls` of `/map-config/`, so a downloaded pack fills the same tile cache the live map
    reads (source `osm`, layer `osm-tiles`, as built by the app)."""
    if settings.AUTH_ENFORCED and principal.kind == "anonymous":
        raise api_error(401, "not_authenticated", "Please sign in.")
    cfg: MapSettings = await get_section(db, "maps")  # type: ignore[assignment]
    pref = principal.user.map_provider.value if principal.user is not None else None
    tiles = (await resolve_tiles(cfg, pref)).as_dict()
    return {
        "version": 8,
        "name": f"VIASafe {tiles['provider']}",
        "sources": {"osm": {"type": "raster", "tiles": tiles["tile_urls"], "tileSize": 256,
                            "attribution": tiles["attribution"], "maxzoom": tiles["max_zoom"]}},
        "layers": [{"id": "osm-tiles", "type": "raster", "source": "osm"}],
    }
