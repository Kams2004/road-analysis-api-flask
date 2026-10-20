"""
Global Cluster Configuration (Module 6 — RRAP-06-009/010/011): geometry,
resolution order, legacy migration, and that every consumer (hazard
clustering, processor geo de-duplication, community-report merging) goes
through the one resolver. Pure / fake-DB tests — no PostgreSQL, no network.
"""
import asyncio
import sys
import types
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models.signalement import Signalement, SignalementType, SignalementStatus
from app.services import hazard_service
from app.services.clustering import boundaries, service as clustering_service, zones as geo
from app.services.clustering.resolver import (
    DEFAULT_PRIORITY, Snapshot, ZoneRule, resolve, resolve_all, zone_at,
)
from app.services.clustering.settings import ClusteringSettings, ClusterProfile
from app.services.settings_service import SECTIONS, CommunitySettings

# A square ~2.2 km wide around (4.05, 9.70) with a ~440 m hole in the middle.
SQUARE = [[9.69, 4.04], [9.71, 4.04], [9.71, 4.06], [9.69, 4.06], [9.69, 4.04]]
HOLE = [[9.698, 4.048], [9.702, 4.048], [9.702, 4.052], [9.698, 4.052], [9.698, 4.048]]


def _zone(id_, geometry, kind="agglomeration", env="urban", width=None, **kw) -> ZoneRule:
    kw.setdefault("priority", DEFAULT_PRIORITY[kind])
    return ZoneRule(id=id_, name=kw.pop("name", f"Z{id_}"), kind=kind, environment=env,
                    shape=geo.compile_shape(geometry, width), **kw)


def _settings(**kw) -> ClusteringSettings:
    base = dict(urban={"default_radius_m": 30, "type_radius": {"speed_bump": 40}, "signalement_radius": {}},
                rural={"default_radius_m": 60, "type_radius": {"pothole": 50},
                       "signalement_radius": {"embouteillage": 500}})
    base.update(kw)
    return ClusteringSettings(**base)


# ── geometry ──────────────────────────────────────────────────────────────────

def test_point_in_polygon_with_hole():
    rings = [SQUARE, HOLE]
    assert geo.point_in_polygon(4.045, 9.695, rings)          # inside, outside the hole
    assert not geo.point_in_polygon(4.05, 9.70, rings)        # in the hole
    assert not geo.point_in_polygon(4.07, 9.70, rings)        # outside


def test_multipolygon_shape():
    far = [[10.0, 4.0], [10.01, 4.0], [10.01, 4.01], [10.0, 4.01], [10.0, 4.0]]
    shape = geo.compile_shape({"type": "MultiPolygon", "coordinates": [[SQUARE, HOLE], [far]]})
    assert shape.contains(4.045, 9.695)
    assert shape.contains(4.005, 10.005)
    assert not shape.contains(4.05, 9.70)                    # hole of the first part
    assert not shape.contains(4.005, 9.85)                   # between the parts (in the bbox)
    # area ≈ 2.2 km × 2.2 km − hole + the small square
    assert 4.5 < shape.area_m2 / 1e6 < 6.5


def test_corridor_buffer():
    road = {"type": "LineString", "coordinates": [[9.70, 4.00], [9.80, 4.00]]}   # ~11 km east-west
    shape = geo.compile_shape(road, corridor_width_m=300)
    assert shape.contains(4.0 + 100 / 111_320, 9.75)            # 100 m off the road
    assert not shape.contains(4.0 + 200 / 111_320, 9.75)        # 200 m off (half-width 150)
    assert shape.contains(4.0, 9.70 - 100 / 111_000)            # round end cap
    assert not shape.contains(4.0, 9.70 - 300 / 111_000)
    assert shape.area_m2 == pytest.approx(geo.line_length_m(road["coordinates"]) * 300)
    outline = geo.corridor_polygon(road["coordinates"], 300)
    assert outline[0] == outline[-1] and len(outline) > 10
    assert geo.point_in_polygon(4.0, 9.75, [outline])


def test_circle_and_simplify():
    circle = geo.circle_polygon(4.05, 9.70, 1000)
    ring = circle["coordinates"][0]
    assert len(ring) == 65 and ring[0] == ring[-1]
    assert geo.polygon_area_m2(circle) == pytest.approx(3.1416e6, rel=0.01)
    dense = geo.circle_polygon(4.05, 9.70, 20_000, vertices=5000)
    simple = geo.simplify(dense, 0.0005, 2000)
    assert geo.vertex_count(simple) <= 2000
    assert geo.compile_shape(simple).contains(4.05, 9.70)
    tiny = geo.simplify(dense, 0.0005, 50)
    assert geo.vertex_count(tiny) <= 50


def test_validate_geometry():
    g = geo.validate_geometry({"type": "Polygon", "coordinates": [[[9.6, 4.0], [9.7, 4.0], [9.7, 4.1]]]},
                              allow_lines=False)
    assert g["coordinates"][0][0] == g["coordinates"][0][-1]          # ring closed
    with pytest.raises(ValueError):
        geo.validate_geometry({"type": "LineString", "coordinates": [[9.6, 4.0], [9.7, 4.0]]}, allow_lines=False)
    with pytest.raises(ValueError):
        geo.validate_geometry({"type": "Polygon", "coordinates": [[[200, 4.0], [9.7, 4.0], [9.7, 4.1]]]},
                              allow_lines=False)


# ── settings ──────────────────────────────────────────────────────────────────

def test_settings_section_and_validation():
    assert SECTIONS["clustering"] is ClusteringSettings
    s = ClusteringSettings()
    assert s.urban.default_radius_m == 30 and s.rural.default_radius_m == 60
    assert s.unzoned_environment == "rural"
    with pytest.raises(ValueError):
        ClusterProfile(default_radius_m=30, signalement_radius={"not_a_type": 10})
    with pytest.raises(ValueError):
        ClusterProfile(default_radius_m=30, type_radius={"pothole": 0})
    # null removes an override; partial profiles keep the defaults
    assert ClusterProfile(default_radius_m=30, type_radius={"pothole": None}).type_radius == {}
    merged = ClusteringSettings.merge_patch(s.model_dump(), {"urban": {"default_radius_m": 20}})
    assert ClusteringSettings(**merged).urban.default_radius_m == 20
    assert ClusteringSettings(**merged).urban.signalement_radius == s.urban.signalement_radius


# ── resolution order ──────────────────────────────────────────────────────────

def test_unzoned_resolves_to_rural_profile():
    snap = Snapshot(settings=_settings(), zones=[])
    r = resolve(snap, 5.0, 12.0, "detection", "pothole")
    assert (r.radius_m, r.environment, r.source, r.zone_id) == (50, "rural", "profile_type", None)
    assert resolve(snap, 5.0, 12.0, "detection", "traffic_sign").source == "profile_default"
    assert resolve(snap, 5.0, 12.0, "signalement", "embouteillage").radius_m == 500
    urban_default = Snapshot(settings=_settings(unzoned_environment="urban"))
    assert resolve(urban_default, 5.0, 12.0, "detection", "speed_bump").radius_m == 40
    assert resolve(snap, None, None, "detection", "pothole").environment == "rural"


def test_nested_zones_smallest_wins_and_override_inheritance():
    city = _zone(1, {"type": "Polygon", "coordinates": [SQUARE]}, name="Douala")
    district = _zone(2, geo.circle_polygon(4.05, 9.70, 300), kind="custom", name="Akwa",
                     default_radius_m=15, type_radius={"pothole": 10})
    snap = Snapshot(settings=_settings(), zones=[city, district])
    # in the district: zone type override, then zone default
    r = resolve(snap, 4.05, 9.70, "detection", "pothole")
    assert (r.radius_m, r.source, r.zone_name) == (10, "zone_type", "Akwa")
    assert resolve(snap, 4.05, 9.70, "detection", "speed_bump").radius_m == 15         # zone default
    # in the city only: inherits the urban profile (type override, then default)
    assert resolve(snap, 4.045, 9.695, "detection", "speed_bump").radius_m == 40
    r = resolve(snap, 4.045, 9.695, "detection", "pothole")
    assert (r.radius_m, r.source, r.environment, r.zone_name) == (30, "profile_default", "urban", "Douala")
    assert r.label == "Douala · urban · 30 m"


def test_priority_beats_area_and_corridor_never_overrides_city():
    city = _zone(1, {"type": "Polygon", "coordinates": [SQUARE]}, name="Douala")
    road = _zone(2, {"type": "LineString", "coordinates": [[9.60, 4.05], [9.90, 4.05]]}, kind="road_section",
                 env="rural", width=100, name="N3")
    snap = Snapshot(settings=_settings(), zones=[road, city])
    # the corridor is smaller than the city but has a lower default priority
    assert road.shape.area_m2 < city.shape.area_m2
    assert zone_at(snap, 4.05, 9.695).name == "Douala"
    assert resolve(snap, 4.05, 9.695, "detection", "pothole").environment == "urban"
    # out of the city on the road → rural road section
    r = resolve(snap, 4.05, 9.80, "detection", "pothole")
    assert (r.zone_name, r.environment, r.radius_m) == ("N3", "rural", 50)
    # an explicit priority wins over the smaller area
    road.priority = 50
    assert zone_at(snap, 4.05, 9.695).name == "N3"
    # equal priority and area → lowest id
    twin = _zone(3, {"type": "Polygon", "coordinates": [SQUARE]}, name="Twin")
    snap2 = Snapshot(settings=_settings(), zones=[twin, city])
    assert zone_at(snap2, 4.045, 9.695).id == 1


def test_resolve_all_lists_every_type():
    snap = Snapshot(settings=_settings(), zones=[_zone(1, {"type": "Polygon", "coordinates": [SQUARE]})])
    out = resolve_all(snap, 4.045, 9.695)
    assert out["zone"]["id"] == 1 and out["environment"] == "urban"
    assert set(out["detections"]) >= {"pothole", "traffic_sign", "speed_bump", "parked_vehicle"}
    assert "embouteillage" in out["signalements"]
    assert out["detections"]["speed_bump"]["radius_m"] == 40


# ── legacy migration ──────────────────────────────────────────────────────────

def test_settings_from_legacy_radius():
    assert clustering_service.settings_from_legacy(None).urban.default_radius_m == 30
    s = clustering_service.settings_from_legacy(50.0)            # old built-in default = never set
    assert (s.urban.default_radius_m, s.rural.default_radius_m) == (30, 60)
    s = clustering_service.settings_from_legacy(80.0)
    assert (s.urban.default_radius_m, s.rural.default_radius_m) == (80, 80)


def test_legacy_circle_area_becomes_custom_urban_zone():
    area = SimpleNamespace(id=7, name="Douala centre", country="Cameroon", city="Douala", center_lat=4.05,
                           center_lon=9.70, area_radius_km=2.0, cluster_radius_m=25.0,
                           type_radius={"pothole": 20}, is_active=True, created_at=datetime(2026, 1, 1))
    z = clustering_service.zone_from_legacy_area(area)
    assert (z.kind, z.environment, z.source, z.source_ref) == ("custom", "urban", "legacy_area", "7")
    assert len(z.geometry["coordinates"][0]) == 65
    assert z.default_radius_m == 25 and z.type_radius == {"pothole": 20}
    assert z.area_km2 == pytest.approx(3.1416 * 4, rel=0.01)
    z.id, z.priority = 9, DEFAULT_PRIORITY["custom"]
    rule = clustering_service.rule_from_row(z)
    snap = Snapshot(settings=_settings(), zones=[rule])
    assert resolve(snap, 4.05 + 1.5 / 111.32, 9.70, "detection", "pothole").radius_m == 20     # 1.5 km out
    assert resolve(snap, 4.05 + 2.5 / 111.32, 9.70, "detection", "pothole").zone_id is None    # 2.5 km out


# ── consumers ─────────────────────────────────────────────────────────────────

def test_hazard_context_uses_resolver():
    city = _zone(1, {"type": "Polygon", "coordinates": [SQUARE]}, name="Douala", default_radius_m=25)
    ctx = hazard_service.ClusterContext(snapshot=Snapshot(settings=_settings(), zones=[city]))
    assert ctx.radius_for(4.045, 9.695, "pothole") == 25
    assert ctx.radius_for(5.0, 12.0, "pothole") == 50
    res = ctx.resolve(4.045, 9.695, "pothole")
    assert (res.zone_id, res.zone_name, res.environment) == (1, "Douala", "urban")


def _import_processor(monkeypatch):
    """The processor imports ultralytics at module level; the light test venv may not have it."""
    try:
        import ultralytics  # noqa: F401
    except ImportError:
        stub = types.ModuleType("ultralytics")
        stub.YOLO = object
        monkeypatch.setitem(sys.modules, "ultralytics", stub)
    import importlib
    return importlib.import_module("app.workers.processor")


def test_processor_geo_dedup_uses_resolved_radius(monkeypatch):
    fresh = "app.workers.processor" not in sys.modules
    try:
        proc = _import_processor(monkeypatch)
        assert not hasattr(proc, "GEO_DEDUP_DEG")
        city = _zone(1, {"type": "Polygon", "coordinates": [SQUARE]}, name="Douala")
        ctx = SimpleNamespace(clusters=hazard_service.ClusterContext(
            snapshot=Snapshot(settings=_settings(), zones=[city])))
        # urban pothole → 30 m; rural pothole → 50 m (profile type override)
        assert proc._dedup_radius(ctx, 4.045, 9.695, "pothole") == 30
        assert proc._dedup_radius(ctx, 5.0, 12.0, "pothole") == 50
        seen: list = []
        d40 = 40 / 111_320
        assert not proc._is_geo_duplicate(4.045, 9.695, seen, 30)
        assert not proc._is_geo_duplicate(4.045 + d40, 9.695, seen, 30)    # 40 m apart, urban 30 m → kept
        seen = []
        assert not proc._is_geo_duplicate(5.0, 12.0, seen, 50)
        assert proc._is_geo_duplicate(5.0 + d40, 12.0, seen, 50)           # 40 m apart, rural 50 m → duplicate
        assert not proc._is_geo_duplicate(None, None, seen, 50)
    finally:
        if fresh:   # leave no trace: other test files must still see "processor not importable"
            sys.modules.pop("app.workers.processor", None)
            import app.workers as workers_pkg
            if hasattr(workers_pkg, "processor"):
                delattr(workers_pkg, "processor")


def _report(id_, type_=SignalementType.embouteillage, lat=4.05, lon=9.70, **kw) -> Signalement:
    base = dict(id=id_, type=type_, status=SignalementStatus.actif, latitude=lat, longitude=lon,
                reported_at=datetime.utcnow() - timedelta(minutes=5), last_confirmed_at=None,
                merged_into_id=None, report_count=1, uncertain=False)
    base.update(kw)
    return Signalement(**base)


def test_pick_merge_target_rules():
    from app.services.signalement_service import pick_merge_target
    near = _report("a", lat=4.05 + 80 / 111_320)
    nearer = _report("b", lat=4.05 + 40 / 111_320)
    other_type = _report("c", type_=SignalementType.police)
    merged = _report("d", merged_into_id="a")
    hidden = _report("e", status=SignalementStatus.annule)
    visible = lambda s: s.status == SignalementStatus.actif   # noqa: E731
    rows = [near, nearer, other_type, merged, hidden]
    assert pick_merge_target(rows, "embouteillage", 4.05, 9.70, 150, visible) is nearer
    assert pick_merge_target(rows, "embouteillage", 4.05, 9.70, 30, visible) is None
    assert pick_merge_target(rows, "police", 4.05, 9.70, 30, visible) is other_type


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, rows):
        self.rows, self.added = rows, []

    async def execute(self, *_a, **_k):
        return _FakeResult(self.rows)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        pass

    async def refresh(self, _obj):
        pass


def test_signalement_create_merges_with_resolved_radius(monkeypatch):
    """POST /signalements/ resolves the radius per report type at the position and merges."""
    from app.api.routes import signalements as route
    from app.services import signalement_service

    city = _zone(1, {"type": "Polygon", "coordinates": [SQUARE]}, name="Douala",
                 signalement_radius={"embouteillage": 120})
    snap = Snapshot(settings=_settings(), zones=[city])
    existing = _report("old", lat=4.045 + 100 / 111_320, lon=9.695)
    db = _FakeDB([existing])
    radii: list = []

    async def nearby(_db, lat, lon, radius_m):
        radii.append(radius_m)
        return ["old"]

    async def get_snapshot(_db):
        return snap

    async def community(_db):
        return CommunitySettings()

    monkeypatch.setattr(signalement_service, "query_signalements_nearby", nearby)
    monkeypatch.setattr(clustering_service, "get_snapshot", get_snapshot)
    monkeypatch.setattr(route, "_community", community)
    monkeypatch.setattr(route, "reverse_geocode", lambda lat, lon: None)
    principal = SimpleNamespace(kind="anonymous", user=None, label="anon")
    monkeypatch.setattr(route, "actor_name", lambda p, name: name)

    async def create(lat, lon, type_=SignalementType.embouteillage):
        return await route.create_signalement(
            type=type_, latitude=lat, longitude=lon, description=None, reported_by="t", blocked_bearing=None,
            image=None, audio=None, db=db, principal=principal)

    sig = asyncio.run(create(4.045, 9.695))
    assert radii == [120]                                     # zone override for the report type
    assert sig.merged_into_id == "old" and existing.report_count == 2
    assert existing.last_reported_at == sig.reported_at
    assert (sig.cluster_zone_id, sig.cluster_environment, sig.cluster_radius_m) == (1, "urban", 120)

    # out of every zone: rural profile (500 m for traffic jams), same rule
    radii.clear()
    existing.latitude, existing.longitude = 5.0 + 300 / 111_320, 12.0
    sig2 = asyncio.run(create(5.0, 12.0))
    assert radii == [500] and sig2.merged_into_id == "old" and sig2.cluster_environment == "rural"

    # merging disabled → kept separate
    snap.settings.merge_signalements = False
    sig3 = asyncio.run(create(5.0, 12.0))
    assert sig3.merged_into_id is None


def test_merged_report_restarts_freshness():
    from app.services import signalement_votes as votes
    cfg = CommunitySettings()
    old = _report("x", reported_at=datetime(2026, 1, 1, 0, 0))
    base = votes.expires_at(old, cfg)
    old.last_reported_at = datetime(2026, 1, 1, 3, 0)
    assert votes.expires_at(old, cfg) == base + timedelta(hours=3)


# ── boundary search parsing ───────────────────────────────────────────────────

def test_boundary_candidates_polygon_and_point_fallback():
    poly = {"lat": "4.05", "lon": "9.70", "osm_type": "relation", "osm_id": 3832073, "category": "boundary",
            "type": "administrative", "display_name": "Communauté urbaine de Douala, Wouri, Littoral, Cameroun",
            "name": "Communauté urbaine de Douala", "boundingbox": ["4.04", "4.06", "9.69", "9.71"],
            "geojson": {"type": "Polygon", "coordinates": [SQUARE]}}
    point = {"lat": "4.0429", "lon": "9.7062", "osm_type": "node", "osm_id": 732125487, "category": "place",
             "type": "city", "display_name": "Douala, Littoral, Cameroun", "name": "Douala",
             "boundingbox": ["3.8829", "4.2029", "9.5462", "9.8662"],
             "geojson": {"type": "Point", "coordinates": [9.7062, 4.0429]}}
    road = {"lat": "4.0", "lon": "9.7", "geojson": {"type": "LineString", "coordinates": [[9.7, 4.0], [9.8, 4.0]]}}
    cands = boundaries.rank([c for c in map(boundaries.candidate_from, [point, poly, road]) if c])
    assert [c["osm_type"] for c in cands] == ["relation", "node"]          # real boundary first, road dropped
    assert not cands[0]["approximate"] and cands[0]["area_km2"] > 4
    circle = cands[1]
    assert circle["approximate"] and circle["geometry"]["type"] == "Polygon"
    assert 10 < circle["area_km2"] < 3000
