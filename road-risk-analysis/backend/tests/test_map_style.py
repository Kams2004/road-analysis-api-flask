"""GET /map-config/style.json: the base map as a MapLibre style (the mobile app's offline packs) —
same tiles as GET /map-config/, source `osm` + layer `osm-tiles` like the app builds them."""
from types import SimpleNamespace

import pytest

from app.core.settings_service import MapSettings
from app.modules.m03_maps.routes import map_config as m


async def test_style_has_the_same_tiles_as_map_config(monkeypatch):
    async def section(db, name):
        return MapSettings()
    monkeypatch.setattr(m, "get_section", section)
    client = SimpleNamespace(kind="client", user=None)
    style = await m.map_style(db=None, principal=client)
    cfg = await m.map_config(db=None, principal=client)
    assert style["version"] == 8 and style["layers"] == [{"id": "osm-tiles", "type": "raster", "source": "osm"}]
    assert style["sources"]["osm"]["tiles"] == cfg["tile_urls"]
    assert style["sources"]["osm"]["maxzoom"] == cfg["max_zoom"]


async def test_style_needs_a_caller(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(m.settings, "AUTH_ENFORCED", True)
    with pytest.raises(HTTPException) as e:
        await m.map_style(db=None, principal=SimpleNamespace(kind="anonymous", user=None))
    assert e.value.status_code == 401
