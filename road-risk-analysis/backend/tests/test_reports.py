"""
Module 14 — PDF reports: path filter (corridor + km along the path), the
document builders, the reportlab rendering (valid PDF, text, clickable links,
uncropped image previews) and the /reports endpoints on SQLite with an
in-memory file store.
"""
import io
import shutil
import subprocess
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.modules.m09_exception_patterns.analytics import resolve_window
from app.modules.m14_reports import collectors, pathfilter, pdf
from app.modules.m14_reports.i18n import translator

NOW = datetime(2026, 10, 20, 12, 0)
# A straight north-bound path of ~11 km
LINE = [(4.00, 9.70), (4.05, 9.70), (4.10, 9.70)]


def _text(data: bytes) -> str:
    if not shutil.which("pdftotext"):
        pytest.skip("pdftotext not installed")
    return subprocess.run(["pdftotext", "-layout", "-", "-"], input=data, capture_output=True, check=True).stdout.decode()


def _png(w: int, h: int) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 30, 30)).save(buf, "PNG")
    return buf.getvalue()


def _hazard(i, lat, lon, type_="pothole", status="active"):
    return SimpleNamespace(id=f"h{i}", code=f"HZ-{i:06d}", type=type_, subtype=None, status=status,
                           latitude=lat, longitude=lon, display_location=f"Rue {i}, Douala", road=None,
                           locality=None, town="Douala", city="Douala", location_name=None, detection_count=3,
                           avg_confidence=0.8, last_seen_at=NOW, last_detected_at=NOW, recurring_spot=False)


def _ctx(**kw):
    base = dict(report_type="hazards", t=translator("en"), window=resolve_window("all", None, None, NOW),
                web_base="https://console.example")
    base.update(kw)
    return collectors.Ctx(**base)


# ── path filter ──────────────────────────────────────────────────────────────

def test_path_corridor_and_km_order():
    p = pathfilter.Path("Test", LINE, corridor_m=60)
    assert p.length_m == pytest.approx(11_132, rel=0.01)
    near_far_end = SimpleNamespace(latitude=4.09, longitude=9.7003)      # ~33 m off, km ~10
    near_start = SimpleNamespace(latitude=4.01, longitude=9.6997)        # ~33 m off, km ~1.1
    off = SimpleNamespace(latitude=4.05, longitude=9.71)                 # ~1.1 km off
    kept = p.keep([near_far_end, off, near_start])
    assert [it for _, _, it in kept] == [near_start, near_far_end]
    assert kept[0][0] == pytest.approx(1.11, abs=0.05) and kept[1][0] == pytest.approx(10.02, abs=0.05)
    assert kept[0][1] < 60
    assert not p.contains(None, None)


class _FakeOSRM:
    def __init__(self):
        self.calls = []

    async def route_overview(self, points, alternatives=True):
        self.calls.append(points)
        return SimpleNamespace(data={"code": "Ok", "routes": [
            {"geometry": {"coordinates": [[lon, lat] for lat, lon in LINE]}}]})


async def test_resolve_section_and_from_to():
    sec = SimpleNamespace(id="s1", name="Douala - Bafoussam (N5)", from_=SimpleNamespace(lat=4.0, lon=9.7),
                          to=SimpleNamespace(lat=4.1, lon=9.7))
    osrm = _FakeOSRM()
    p = await pathfilter.resolve({"section_id": "s1"}, 80, [sec], osrm)
    assert p.name == "Douala - Bafoussam (N5)" and p.source == "section" and len(p.points) == 3
    p = await pathfilter.resolve({"from": {"lat": 4.0, "lon": 9.7, "label": "A"},
                                  "to": {"lat": 4.1, "lon": 9.7, "label": "B"}}, 60, [], osrm)
    assert p.name == "A - B" and p.source == "route"
    with pytest.raises(pathfilter.PathError):
        await pathfilter.resolve({"section_id": "nope"}, 60, [sec], osrm)
    p = await pathfilter.resolve({"waypoints": LINE, "name": "W"}, 60, [], None)
    assert p.source == "waypoints" and len(osrm.calls) == 2


# ── rendering ────────────────────────────────────────────────────────────────

def test_preview_keeps_whole_image():
    jpeg, w, h = pdf.preview_jpeg(_png(1920, 1080))
    assert (w, h) == (480, 270)                    # scaled, aspect preserved, nothing cropped
    jpeg, w, h = pdf.preview_jpeg(_png(600, 1200))
    assert (w, h) == (240, 480)


def test_hazards_report_on_a_path_renders_pdf_with_links():
    path = pathfilter.Path("Douala - Bafoussam (N5)", LINE, corridor_m=60)
    hazards = [_hazard(1, 4.09, 9.7002), _hazard(2, 4.02, 9.7001), _hazard(3, 4.05, 9.75),
               _hazard(4, 4.03, 9.7002, type_="parked_vehicle")]
    ctx = _ctx(types=["pothole"], path=path)
    kept = ctx.path.keep([h for h in hazards if h.type in ctx.types])
    covers = {"h1": ("d1", "minio:9000/detections/a.jpg", None), "h2": ("d2", None, None)}
    data = collectors.build_hazards(ctx, kept, covers, {"h1": _png(1920, 1080)})
    table = next(b for b in data["blocks"] if b["kind"] == "table")
    assert [r[2]["text"] for r in table["rows"]] == ["HZ-000002", "HZ-000001"]      # path order
    assert table["rows"][0][1] == "2.2"                                             # km along the path
    assert data["summary"]["items"] == 2 and data["map"]["path"] == LINE

    out = pdf.render(data, t=ctx.t, report_id="r-123456789", generated_at=NOW, generated_by="qa@example.com")
    assert out.startswith(b"%PDF") and pdf.page_count(out) >= 1
    assert b"https://console.example/detections?hazard=h1&detection=d1" in out
    assert b"/Subtype /Image" in out or b"/Subtype/Image" in out
    text = _text(out)
    assert "AI hazards report" in text and "HZ-000001" in text and "Douala - Bafoussam (N5)" in text
    assert "Open full image" in text and "never cropped" in text


def test_every_report_type_builds_and_renders_even_empty():
    t = translator("fr")
    for kind, build in (("community", lambda c: collectors.build_community(c, [], {})),
                        ("historical", lambda c: collectors.build_historical(c, [])),
                        ("collision", lambda c: collectors.build_collision(c, [], []))):
        c = _ctx(report_type=kind, t=t)
        out = pdf.render(build(c), t=t, report_id="x" * 12, generated_at=NOW, generated_by=None)
        assert out.startswith(b"%PDF")
        assert "Aucune donnée" in _text(out) or kind == "collision"


def test_community_report_links_to_console_popup():
    s = SimpleNamespace(id="s1", type="nid_de_poule", status="actif", latitude=4.02, longitude=9.7,
                        location_name="Akwa", description="Gros trou", image_url="minio:9000/signalements/x.jpg",
                        reported_at=NOW, present_count=2, absent_count=0, uncertain=False)
    ctx = _ctx(report_type="community")
    data = collectors.build_community(ctx, [(None, None, s)], {"s1": _png(800, 600)})
    out = pdf.render(data, t=ctx.t, report_id="abc", generated_at=NOW, generated_by=None)
    assert b"https://console.example/signalements?report=s1" in out


# ── endpoints (SQLite + in-memory storage) ───────────────────────────────────

@pytest.fixture
async def client(monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.api.deps import Principal, get_principal
    from app.core import settings_service
    from app.core.db.session import Base, get_db
    from app.core.storage import minio_service
    from app.modules.m08_historical.models.historical_event import HistoricalEventRecord as E
    from app.modules.m13_admin.models.system_setting import SystemSetting
    from app.modules.m14_reports.models.report import GeneratedReport
    from app.modules.m14_reports.routes.reports import router

    store = {}
    monkeypatch.setattr(minio_service, "upload_report", lambda data, name: store.__setitem__(name, data))
    monkeypatch.setattr(minio_service, "get_report", lambda name: store[name])
    monkeypatch.setattr(minio_service, "remove_report", lambda name: store.pop(name, None))

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(
            c, tables=[E.__table__, SystemSetting.__table__, GeneratedReport.__table__]))
    Session = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.utcnow()
    async with Session() as s:
        for i, (lat, t) in enumerate([(4.02, "HARD_BRAKING"), (4.06, "SPEEDING"), (4.30, "SPEEDING")]):
            s.add(E(source="pytest", source_event_id=f"r{i}", event_type=t, occurred_at=now - timedelta(days=1),
                    latitude=lat, longitude=9.7001, grid_cell_id="c", road_name="N5", raw={}))
        await s.commit()
    settings_service.clear_cache()

    async def _db():
        async with Session() as s:
            yield s

    perms = {"reports": {"view", "add", "delete"}, "driver_behaviour": {"view"}}
    app = FastAPI()
    app.include_router(router, prefix="/reports")
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_principal] = lambda: Principal(kind="user", id="u1", label="qa@example.com",
                                                                permissions=perms)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c, store
    settings_service.clear_cache()
    await engine.dispose()


async def test_generate_list_download_delete(client):
    c, store = client
    body = {"report_type": "driver_behaviour", "period": "30d", "types": ["SPEEDING", "HARD_BRAKING"],
            "path": {"waypoints": LINE, "name": "Test path"}, "corridor_m": 60, "language": "en"}
    r = await c.post("/reports/", json=body)
    assert r.status_code == 201, r.text
    rep = r.json()
    assert rep["status"] == "ready" and rep["page_count"] >= 1 and rep["summary"]["items"] == 2   # 4.30 is off-path
    assert rep["filters"]["resolved_path"]["name"] == "Test path" and rep["title"].endswith("Test path")
    assert rep["methodology"]["driver_behaviour"] == "exp-1"
    assert len(store) == 1

    lst = (await c.get("/reports/")).json()
    assert lst["total"] == 1 and lst["items"][0]["id"] == rep["id"]
    d = await c.get(f"/reports/{rep['id']}/download")
    assert d.status_code == 200 and d.headers["content-type"] == "application/pdf"
    assert d.content.startswith(b"%PDF") and "attachment" in d.headers["content-disposition"]
    text = _text(d.content)
    assert "Exception patterns report" in text and "Exceptions by source / provider" in text
    assert "Driver" not in text and "Vehicle" not in text and "ranking" not in text.lower()

    assert (await c.delete(f"/reports/{rep['id']}")).status_code == 204
    assert store == {} and (await c.get(f"/reports/{rep['id']}")).status_code == 404


async def test_generate_needs_the_data_module_right(client):
    c, _ = client
    r = await c.post("/reports/", json={"report_type": "hazards"})
    assert r.status_code == 403
    r = await c.post("/reports/", json={"report_type": "driver_behaviour", "period": "5d"})
    assert r.status_code == 422
    r = await c.post("/reports/", json={"report_type": "driver_behaviour", "path": {"section_id": "nope"}})
    assert r.status_code == 422
