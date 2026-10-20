"""
Resolves which base map a given user sees (RRAP-01-012, RRAP-03-001/002).

OpenStreetMap is the free default. Google is used only when the user (or
the platform default) asks for it AND a Google Maps Platform key has been
entered in Settings → Platform → Maps. Google raster tiles come from the
Map Tiles API (enable "Map Tiles API" on the key's Cloud project), which
needs a session token; tokens are cached until shortly before they expire.

Both web (Leaflet) and mobile (MapLibre raster source) consume the same
`tile_urls` templates, so switching provider needs no client release.
"""
import logging
import time
from dataclasses import dataclass

import httpx

from app.services.settings_service import MapSettings

logger = logging.getLogger(__name__)

_CREATE_SESSION = "https://tile.googleapis.com/v1/createSession"
_GOOGLE_TILE = "https://tile.googleapis.com/v1/2dtiles/{z}/{x}/{y}?session={session}&key={key}"

# (api_key, map_type, language, region) → (session, expires_epoch)
_session_cache: dict[tuple, tuple[str, float]] = {}


class GoogleTilesError(Exception):
    pass


@dataclass
class TileConfig:
    provider: str
    tile_urls: list[str]
    attribution: str
    max_zoom: int
    requested: str
    fallback_reason: str | None = None

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def clear_cache() -> None:
    _session_cache.clear()


async def google_session(cfg: MapSettings, force: bool = False) -> str:
    key = (cfg.google_api_key, cfg.google_map_type, cfg.google_language, cfg.google_region)
    cached = _session_cache.get(key)
    if cached and not force and cached[1] - time.time() > 3600:
        return cached[0]
    body = {"mapType": cfg.google_map_type, "language": cfg.google_language, "region": cfg.google_region}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            res = await client.post(_CREATE_SESSION, params={"key": cfg.google_api_key}, json=body)
    except httpx.HTTPError as exc:
        raise GoogleTilesError(f"Google Map Tiles API unreachable: {exc}") from exc
    if res.status_code != 200:
        try:
            message = res.json().get("error", {}).get("message", res.text)
        except ValueError:
            message = res.text
        raise GoogleTilesError(f"Google rejected the key ({res.status_code}): {message[:300]}")
    data = res.json()
    expiry = float(data.get("expiry", time.time() + 86400))
    _session_cache[key] = (data["session"], expiry)
    return data["session"]


def _osm(cfg: MapSettings, requested: str, reason: str | None = None) -> TileConfig:
    subs = list(cfg.osm_subdomains) or [""]
    if "{s}" in cfg.osm_tile_url:
        urls = [cfg.osm_tile_url.replace("{s}", s) for s in subs]
    else:
        urls = [cfg.osm_tile_url]
    return TileConfig("osm", urls, cfg.osm_attribution, 19, requested, reason)


async def resolve_tiles(cfg: MapSettings, user_pref: str | None) -> TileConfig:
    requested = cfg.default_provider if user_pref in (None, "default") else user_pref
    if requested != "google":
        return _osm(cfg, requested)
    if not cfg.google_api_key:
        return _osm(cfg, requested, "google_not_configured")
    try:
        session = await google_session(cfg)
    except GoogleTilesError as exc:
        logger.warning("Falling back to OSM: %s", exc)
        return _osm(cfg, requested, "google_unavailable")
    url = _GOOGLE_TILE.replace("{session}", session).replace("{key}", cfg.google_api_key)
    return TileConfig("google", [url], "Map data ©Google", 22, requested)
