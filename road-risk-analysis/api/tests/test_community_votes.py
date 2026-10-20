"""
Community votes on signalements, runtime community / mobile / collision
settings. The first part is pure; the second drives the real API against a
throwaway PostgreSQL (RUN_DB_TESTS=1), like test_mobile_auth.py.
"""
import os
import uuid
from datetime import datetime, timedelta

import httpx
import pytest
from pydantic import ValidationError

from app.core.config import settings
from app.models.signalement import Signalement, SignalementType
from app.services import signalement_votes as votes
from app.services.alerts import engine
from app.services.collision import detector, thresholds
from app.services.collision.models import ConflictRiskLevel, ConflictStatus, VehicleConflict
from app.services.mobile_config import clamp_radius
from app.services.settings_service import (
    SECRET_FIELDS, SECTIONS, CommunitySettings, MobileSettings, redact,
)


@pytest.fixture(autouse=True)
def _reset_thresholds():
    thresholds.set_current(None)
    yield
    thresholds.set_current(None)


def _sig(**kw) -> Signalement:
    base = dict(id="s1", type=SignalementType.police, latitude=4.05, longitude=9.70,
                reported_at=datetime(2026, 1, 1, 12, 0), last_confirmed_at=None)
    base.update(kw)
    return Signalement(**base)


# ── votes: pure rules ────────────────────────────────────────────────────────

def test_judge_vote():
    assert votes.judge_vote(None, 300) == (False, "no_position")
    assert votes.judge_vote(301, 300) == (False, "too_far")
    assert votes.judge_vote(300, 300) == (True, None)
    assert votes.judge_vote(12.5, 300) == (True, None)


def test_vote_distance():
    sig = _sig()
    assert votes.vote_distance(sig, None, 9.7) is None
    assert votes.vote_distance(sig, 95.0, 9.7) is None           # invalid coordinate = no position
    assert votes.vote_distance(sig, 4.05, 9.70) == 0.0
    d = votes.vote_distance(sig, 4.051, 9.70)                     # ~111 m north
    assert 105 < d < 117


def test_expiry_by_type_and_confirmation():
    cfg = CommunitySettings()
    police = _sig()                                               # police: 2 h
    assert votes.expires_at(police, cfg) == police.reported_at + timedelta(hours=2)
    jam = _sig(type=SignalementType.embouteillage)                # 1 h
    assert votes.expires_at(jam, cfg) == jam.reported_at + timedelta(hours=1)
    other = _sig(type=SignalementType.autre)                      # default 6 h
    assert votes.expires_at(other, cfg) == other.reported_at + timedelta(hours=6)

    # a counted "present" vote keeps it visible confirm_extends_minutes after the vote
    late = police.reported_at + timedelta(hours=1, minutes=50)
    police.last_confirmed_at = late
    assert votes.expires_at(police, cfg) == late + timedelta(minutes=30)
    # …but never shortens the base expiry
    police.last_confirmed_at = police.reported_at + timedelta(minutes=5)
    assert votes.expires_at(police, cfg) == police.reported_at + timedelta(hours=2)

    now = police.reported_at + timedelta(hours=3)
    assert not votes.is_fresh(police, cfg, now)
    votes.annotate([police], cfg, now)
    assert police.expired is True and police.expires_at == police.reported_at + timedelta(hours=2)


def test_community_settings_validation():
    cfg = CommunitySettings()
    assert (cfg.vote_max_distance_m, cfg.cancel_threshold, cfg.confirm_extends_minutes,
            cfg.default_expiry_hours) == (300, 3, 30, 6)
    assert (cfg.hide_after_absent_votes, cfg.keep_visible_present_votes, cfg.ignore_threshold,
            cfg.ignore_window_minutes, cfg.hide_when_ignored) == (3, 2, 5, 60, False)
    # cancel_threshold is a legacy alias of hide_after_absent_votes
    assert CommunitySettings(cancel_threshold=7).hide_after_absent_votes == 7
    assert CommunitySettings(hide_after_absent_votes=4).cancel_threshold == 4
    assert cfg.expiry_hours_by_type["police"] == 2 and cfg.expiry_hours_by_type["accident"] == 3
    with pytest.raises(ValidationError):
        CommunitySettings(expiry_hours_by_type={"not_a_type": 2})
    with pytest.raises(ValidationError):
        CommunitySettings(expiry_hours_by_type={"police": 0})
    with pytest.raises(ValidationError):
        CommunitySettings(cancel_threshold=0)


def test_visibility():
    from app.models.signalement import SignalementStatus
    cfg = CommunitySettings()
    now = datetime(2026, 1, 1, 12, 30)
    sig = _sig(status=SignalementStatus.actif, uncertain=False)
    assert votes.is_visible(sig, cfg, now)
    sig.uncertain = True
    assert votes.is_visible(sig, cfg, now)                                   # flagged only
    assert not votes.is_visible(sig, CommunitySettings(hide_when_ignored=True), now)
    sig.uncertain = False
    assert not votes.is_visible(sig, cfg, now + timedelta(hours=3))          # expired
    sig.status = SignalementStatus.annule
    assert not votes.is_visible(sig, cfg, now)


def test_mobile_settings_and_radius_cap():
    m = MobileSettings()
    assert (m.visibility_radius_m, m.approach_alert_m, m.vote_prompt_m,
            m.route_corridor_m, m.alert_cooldown_s) == (1000, 400, 150, 60, 60)
    with pytest.raises(ValidationError):
        MobileSettings(vote_prompt_m=500, approach_alert_m=400)
    with pytest.raises(ValidationError):
        MobileSettings(approach_alert_m=2000, visibility_radius_m=1000)
    assert clamp_radius(None, 1000) == 1000
    assert clamp_radius(0, 1000) == 1000
    assert clamp_radius(500, 1000) == 500
    assert clamp_radius(5000, 1000) == 1000


def test_new_sections_registered_without_secrets():
    for name in ("community", "mobile", "collision"):
        assert name in SECTIONS and name not in SECRET_FIELDS
        assert redact(name, SECTIONS[name]()) == SECTIONS[name]().model_dump()


# ── collision thresholds ─────────────────────────────────────────────────────

def test_collision_defaults_come_from_env():
    cfg = thresholds.CollisionSettings()
    assert cfg.candidate_radius_m == settings.COLLISION_CANDIDATE_RADIUS_M
    assert cfg.stale_after_s == settings.TRAJECTORY_STALE_TELEMETRY_S
    assert cfg.levels["CRITICAL"].max_distance_m == settings.COLLISION_CRITICAL_DISTANCE_M
    # legacy distance_ttc_s folded into one TTC limit per level (larger wins)
    assert cfg.levels["CRITICAL"].distance_ttc_s is None
    assert cfg.levels["CRITICAL"].max_ttc_s == max(settings.COLLISION_CRITICAL_TTC_S, settings.ALERT_HIGH_WARNING_TTC_S)
    assert cfg.levels["HIGH_WARNING"].max_ttc_s == max(settings.COLLISION_HIGH_TTC_S, settings.ALERT_WARNING_TTC_S)
    assert cfg.levels["WARNING"].max_ttc_s == settings.ALERT_SAFE_TTC_S
    assert cfg.levels["MONITOR"].max_distance_m == settings.COLLISION_LOW_DISTANCE_M
    assert cfg.scenario_multipliers == {"head_on": 1.0, "crossing": 1.0, "same_direction": 1.0}


def test_collision_settings_validation():
    levels = thresholds.CollisionSettings().model_dump()["levels"]
    levels["CRITICAL"]["max_distance_m"] = 500      # wider than MONITOR
    with pytest.raises(ValidationError):
        thresholds.CollisionSettings(levels=levels)
    with pytest.raises(ValidationError):
        thresholds.CollisionSettings(scenario_multipliers={"head_on": 10})
    with pytest.raises(ValidationError):
        thresholds.CollisionSettings(scenario_multipliers={"diagonal": 1})
    with pytest.raises(ValidationError):
        thresholds.CollisionSettings(fresh_after_s=20, stale_after_s=10)
    # a partial levels dict is completed with defaults
    partial = thresholds.CollisionSettings(levels={"CRITICAL": {"max_distance_m": 10, "distance_ttc_s": 3, "max_ttc_s": 2}})
    assert set(partial.levels) == set(thresholds.LEVELS)


def test_scenario():
    assert thresholds.scenario(None) is None
    assert thresholds.scenario(170) == "head_on"
    assert thresholds.scenario(90) == "crossing"
    assert thresholds.scenario(10) == "same_direction"


def test_detector_reads_runtime_thresholds():
    assert detector._classify(14, 3) == ConflictRiskLevel.CRITICAL
    assert detector._classify(25, 3) == ConflictRiskLevel.HIGH
    assert detector._classify(100, 3) == ConflictRiskLevel.LOW

    levels = thresholds.CollisionSettings().model_dump()["levels"]
    levels["CRITICAL"]["max_distance_m"] = 26
    thresholds.set_current(thresholds.CollisionSettings(levels=levels))
    assert detector._classify(25, 3) == ConflictRiskLevel.CRITICAL

    # head-on multiplier widens every threshold for that geometry only
    thresholds.set_current(thresholds.CollisionSettings(scenario_multipliers={"head_on": 2.0}))
    assert detector._classify(25, 3, relative_heading_deg=170) == ConflictRiskLevel.CRITICAL
    assert detector._classify(25, 3, relative_heading_deg=10) == ConflictRiskLevel.HIGH


def _conflict(ttc: float, heading=None) -> VehicleConflict:
    return VehicleConflict(vehicle_a_id="a", vehicle_b_id="b", risk_level=ConflictRiskLevel.LOW,
                           status=ConflictStatus.ACTIVE, confidence=0.9,
                           time_to_closest_approach_s=ttc, distance_at_closest_approach_m=100,
                           relative_heading_deg=heading)


def test_ttc_limits_read_from_runtime_thresholds():
    # severity needs distance AND TTC: 25 m but 10 s away is only WARNING
    assert detector._classify(25, 10) == ConflictRiskLevel.MODERATE
    levels = thresholds.CollisionSettings().model_dump()["levels"]
    levels["HIGH_WARNING"]["max_ttc_s"] = 11
    thresholds.set_current(thresholds.CollisionSettings(levels=levels))
    assert detector._classify(25, 10) == ConflictRiskLevel.HIGH

    thresholds.set_current(thresholds.CollisionSettings(scenario_multipliers={"crossing": 0.5}))
    assert detector._classify(25, 6, relative_heading_deg=90) == ConflictRiskLevel.MODERATE   # 25 > 30*0.5
    assert detector._classify(25, 6, relative_heading_deg=10) == ConflictRiskLevel.HIGH


def test_alert_level_maps_detector_risk():
    assert engine.classify_level(_conflict(10)).value == "MONITOR"


async def test_refresh_keeps_previous_values_on_db_error():
    class Broken:
        async def get(self, *a, **k):
            raise RuntimeError("db down")

    cfg = await thresholds.refresh(Broken(), force=True)
    assert cfg == thresholds.CollisionSettings()


# ── API against a real database (RUN_DB_TESTS=1) ─────────────────────────────

db_only = pytest.mark.skipif(os.environ.get("RUN_DB_TESTS") != "1",
                             reason="needs a throwaway PostgreSQL (RUN_DB_TESTS=1)")


@pytest.fixture
async def api(monkeypatch):
    from app.api.routes import signalements as sig_routes
    from app.core.security import hash_token, new_token
    from app.db.session import AsyncSessionLocal, engine as db_engine, init_db
    from app.main import app
    from app.models.api_client import ApiClient
    from app.services import settings_service

    await init_db()
    key = new_token("vsk_")
    async with AsyncSessionLocal() as db:
        db.add(ApiClient(name="pytest votes", key_prefix=key[:10], key_hash=hash_token(key), created_by="pytest"))
        await settings_service.update_section(db, "community", CommunitySettings().model_dump(), "pytest")
        await settings_service.update_section(db, "mobile", MobileSettings().model_dump(), "pytest")
    monkeypatch.setattr(sig_routes, "reverse_geocode", lambda lat, lon: None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        client.key = {"X-API-Key": key}
        yield client
    await db_engine.dispose()


async def _report(api, lat=4.05, lon=9.70):
    r = await api.post("/signalements/", data={"type": "police", "latitude": str(lat), "longitude": str(lon)},
                       headers=api.key)
    assert r.status_code == 201, r.text
    return r.json()["id"]


@db_only
async def test_vote_flow(api):
    sid = await _report(api)
    dev = lambda: f"dev-{uuid.uuid4().hex[:8]}"  # noqa: E731
    near = {"latitude": 4.0505, "longitude": 9.70}      # ~55 m
    far = {"latitude": 4.10, "longitude": 9.70}         # ~5.5 km

    d1 = dev()
    r = await api.post(f"/signalements/{sid}/vote", json={"vote": "present", **near, "device_id": d1}, headers=api.key)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["counted"] and out["reason"] is None and out["my_vote"] == "present" and 40 < out["distance_m"] < 70
    assert out["signalement"]["present_count"] == 1 and out["signalement"]["confirmations"] == 1

    # same voter changes their mind: replaces, does not add
    r = await api.post(f"/signalements/{sid}/vote", json={"vote": "absent", **near, "device_id": d1}, headers=api.key)
    out = r.json()
    assert out["counted"] and out["my_vote"] == "absent"
    assert out["signalement"]["present_count"] == 0 and out["signalement"]["absent_count"] == 1

    # far away: stored, not counted, flagged
    r = await api.post(f"/signalements/{sid}/vote", json={"vote": "absent", **far, "device_id": dev()}, headers=api.key)
    out = r.json()
    assert not out["counted"] and out["reason"] == "too_far" and out["my_vote"] is None
    assert out["signalement"]["absent_count"] == 1 and out["signalement"]["invalid_vote_count"] == 1

    # legacy wrapper without position never counts
    r = await api.post(f"/signalements/{sid}/not-there", headers=api.key)
    assert r.status_code == 200 and r.json()["absent_count"] == 1 and r.json()["invalid_vote_count"] == 2

    r = await api.get(f"/signalements/{sid}", params={"device_id": d1}, headers=api.key)
    assert r.json()["my_vote"] == "absent"

    # two more counted "absent" votes reach the threshold (3) → auto-cancelled
    for _ in range(2):
        r = await api.post(f"/signalements/{sid}/vote", json={"vote": "absent", **near, "device_id": dev()},
                           headers=api.key)
    assert r.json()["signalement"]["status"] == "annule"
    assert r.json()["signalement"]["moderated_by"] == "community"
    r = await api.post(f"/signalements/{sid}/vote", json={"vote": "present", **near, "device_id": dev()},
                       headers=api.key)
    assert r.status_code == 409

    r = await api.get(f"/signalements/{sid}/votes")
    if r.status_code == 200:   # AUTH_ENFORCED=false in the test environment
        body = r.json()
        assert body["total"] == 6 and body["absent_count"] == 3 and body["invalid_vote_count"] == 2
        assert {v["reason"] for v in body["items"]} == {None, "duplicate", "too_far", "no_position"}


@db_only
async def test_mobile_config_and_capped_radius(api):
    r = await api.get("/mobile-config/", headers=api.key)
    assert r.status_code == 200, r.text
    cfg = r.json()
    assert cfg["mobile"]["visibility_radius_m"] == 1000 and cfg["community"]["vote_max_distance_m"] == 300
    assert set(cfg["collision"]["levels"]) == {"CRITICAL", "HIGH_WARNING", "WARNING", "MONITOR"}

    sid = await _report(api, lat=4.0600, lon=9.70)   # ~1.1 km from the query point
    r = await api.post("/signalements/nearby", json={"latitude": 4.05, "longitude": 9.70, "radius_m": 50000},
                       headers=api.key)
    assert sid not in {s["id"] for s in r.json()["items"]}     # capped at 1000 m


@db_only
async def test_passes_mark_uncertain(api):
    from app.db.session import AsyncSessionLocal
    from app.services import settings_service
    async with AsyncSessionLocal() as db:
        await settings_service.update_section(db, "community", {"ignore_threshold": 2}, "pytest")
    sid = await _report(api)
    near = {"latitude": 4.0505, "longitude": 9.70}
    r = await api.post(f"/signalements/{sid}/pass", json={**near}, headers=api.key)
    assert r.status_code == 422                                 # no device id, no user
    r = await api.post(f"/signalements/{sid}/pass", json={"device_id": "p1", **near}, headers=api.key)
    assert r.status_code == 200 and r.json()["counted"] and r.json()["signalement"]["ignore_count"] == 1
    r = await api.post(f"/signalements/{sid}/pass", json={"device_id": "p1", **near}, headers=api.key)
    assert r.json()["duplicate"] and r.json()["signalement"]["ignore_count"] == 1
    r = await api.post(f"/signalements/{sid}/pass", json={"device_id": "p2", "latitude": 4.2, "longitude": 9.7},
                       headers=api.key)
    assert not r.json()["counted"] and r.json()["signalement"]["ignore_count"] == 1
    r = await api.post(f"/signalements/{sid}/pass", json={"device_id": "p3", **near}, headers=api.key)
    assert r.json()["signalement"]["ignore_count"] == 2 and r.json()["signalement"]["uncertain"]
    # a device that votes is no longer an ignore
    r = await api.post(f"/signalements/{sid}/vote", json={"vote": "present", "device_id": "p3", **near},
                       headers=api.key)
    assert r.json()["signalement"]["ignore_count"] == 1 and not r.json()["signalement"]["uncertain"]
    async with AsyncSessionLocal() as db:
        await settings_service.update_section(db, "community", CommunitySettings().model_dump(), "pytest")
