"""
Module 6 hazard lifecycle (app.modules.m06_hazards.lifecycle,
docs/spec-module6-hazard-lifecycle.md).

The first part is pure: policy resolution, the valid-pass rules, absence
counting, vote eligibility, expiry (stale evidence, heavy vehicles),
placement near resolved hazards, driver visibility, recurring spots. The
second part drives the services and the API against a throwaway PostgreSQL
(RUN_DB_TESTS=1, DATABASE_URL pointing at it), like test_community_votes.py.
"""
import os
import random
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from app.modules.m06_hazards.models.hazard import HazardStatus
from app.modules.m06_hazards.lifecycle import expiry, recurring, service, votes
from app.modules.m06_hazards.lifecycle.policy import (
    hazard_vehicle_class, policy_for_hazard, resolve_policy, vehicle_class_of,
)
from app.modules.m06_hazards.lifecycle.settings import HazardLifecycleSettings, TypePolicy
from app.modules.m06_hazards.lifecycle.timeutil import in_daylight, recording_time, to_utc_naive
from app.modules.m06_hazards.lifecycle.video_pass import (
    PassRules, TrackPt, angle_diff, bearing, eligible_types, evaluate_pass, heading_at, recording_start, speed_at,
)
from app.core.settings_service import SECTIONS

CFG = HazardLifecycleSettings()
T0 = datetime(2026, 10, 1, 10, 0)     # 11:00 in Douala (UTC+1)


# ── settings & policy ────────────────────────────────────────────────────────

def test_section_registered_with_defaults():
    assert SECTIONS["hazard_lifecycle"] is HazardLifecycleSettings
    assert CFG.expiry_check_minutes == 5 and CFG.timezone == "Africa/Douala" and CFG.daylight_hours == [6, 18]
    assert (CFG.pass_max_distance_m, CFG.pass_max_speed_kmh, CFG.pass_heading_tolerance_deg) == (30, 60, 60)
    assert (CFG.recurring_min_occurrences, CFG.recurring_window_days) == (3, 30)


def test_persistent_types_resolve_by_absence_after_three_passes():
    for t in ("pothole", "speed_bump"):
        p = resolve_policy(CFG, t)
        assert p.expiry_hours is None and not p.recurring_enabled
        assert p.absence_resolution and p.absences_to_resolve == 3 and p.require_same_direction
        assert p.votes_enabled and p.not_there_to_resolve == 3 and p.show_to_drivers
        assert p.auto_confirm_resightings
    sign = resolve_policy(CFG, "traffic_sign", "speed_limit_50")
    assert sign.expiry_hours is None and sign.absence_resolution and sign.absences_to_resolve == 3
    assert sign.require_same_direction and sign.not_there_to_resolve == 3


def test_parked_vehicle_policy_by_subtype_and_vehicle_class():
    blocking = resolve_policy(CFG, "parked_vehicle", "blocking", "car")
    assert blocking.expiry_hours == 2 and blocking.alert_priority == "high"
    assert blocking.absence_resolution and blocking.absences_to_resolve == 2 and blocking.not_there_to_resolve == 2
    assert blocking.still_there_extends and blocking.reopen_on_sighting and blocking.recurring_enabled
    assert blocking.reopen_window_hours == 2
    assert resolve_policy(CFG, "parked_vehicle", "wrong_side").expiry_hours == 4
    roadside = resolve_policy(CFG, "parked_vehicle", "roadside")
    assert roadside.expiry_hours == 6 and roadside.alert_priority == "low" and roadside.show_to_drivers
    # heavy vehicles stay longer, whatever the subtype
    for vc in ("truck", "BUS"):
        heavy = resolve_policy(CFG, "parked_vehicle", "blocking", vc)
        assert heavy.expiry_hours == 24 and heavy.heavy_vehicle
    assert resolve_policy(CFG, "parked_vehicle", "unknown_subtype").expiry_hours == 6


def test_unknown_type_uses_default_policy():
    p = resolve_policy(CFG, "landslide")
    assert p.expiry_hours is None and p.votes_enabled and not p.absence_resolution


def test_vehicle_class_from_details():
    car = SimpleNamespace(ai_details={"vehicle_class": "car"}, subtype="roadside")
    truck = SimpleNamespace(ai_details={"vehicle_class": "truck"}, subtype="blocking")
    none = SimpleNamespace(ai_details=None, subtype=None)
    assert vehicle_class_of(car) == "car" and vehicle_class_of(none) is None
    assert hazard_vehicle_class([car, truck, none], CFG) == "truck"
    hz = SimpleNamespace(type="parked_vehicle", subtype=None)
    assert policy_for_hazard(CFG, hz, [car]).expiry_hours == 6          # most recent member's subtype
    assert policy_for_hazard(CFG, hz, [truck, car]).expiry_hours == 24


def test_policies_merge_over_defaults_and_validate():
    cfg = HazardLifecycleSettings(policies={"parked_vehicle": {"absences_to_resolve": 3,
                                                               "subtype_overrides": {"blocking": {"expiry_hours": 1}}}})
    pv = cfg.policies["parked_vehicle"]
    assert pv.absences_to_resolve == 3 and pv.reopen_on_sighting           # other fields kept
    assert pv.subtype_overrides["blocking"] == {"expiry_hours": 1, "alert_priority": "high"}
    assert "roadside" in pv.subtype_overrides and "pothole" in cfg.policies
    # a PUT patch merges into the CURRENT values, not the defaults
    merged = HazardLifecycleSettings.merge_patch(cfg.model_dump(), {"policies": {"parked_vehicle": {"votes_enabled": False}}})
    again = HazardLifecycleSettings(**merged).policies["parked_vehicle"]
    assert again.absences_to_resolve == 3 and not again.votes_enabled
    with pytest.raises(ValidationError):
        TypePolicy(subtype_overrides={"blocking": {"nope": 1}})
    with pytest.raises(ValidationError):
        TypePolicy(subtype_overrides={"blocking": {"expiry_hours": -1}})
    with pytest.raises(ValidationError):
        HazardLifecycleSettings(timezone="Mars/Olympus")
    with pytest.raises(ValidationError):
        HazardLifecycleSettings(daylight_hours=[18, 6])


# ── time helpers ─────────────────────────────────────────────────────────────

def test_recording_time_order_and_timezones():
    aware = datetime(2026, 10, 1, 11, 0, tzinfo=timezone(timedelta(hours=1)))
    assert recording_time(SimpleNamespace(recorded_at=aware, captured_at=None, created_at=T0)) == T0
    # OSD time is camera local time (Douala, UTC+1)
    osd = SimpleNamespace(recorded_at=None, captured_at=datetime(2026, 10, 1, 11, 0), created_at=None)
    assert recording_time(osd) == T0
    assert recording_time(osd, osd_time_is_local=False) == datetime(2026, 10, 1, 11, 0)
    assert recording_time(SimpleNamespace(recorded_at=None, captured_at=None, created_at=T0)) == T0
    assert to_utc_naive(None) is None
    assert in_daylight(T0, "Africa/Douala", [6, 18])                     # 11:00 local
    assert not in_daylight(datetime(2026, 10, 1, 17, 30), "Africa/Douala", [6, 18])  # 18:30 local


# ── valid-pass rules ─────────────────────────────────────────────────────────

LAT, LON = 4.05, 9.70
STEP = 0.0002          # ≈ 22 m of longitude at 4° N


def _east_track(n=21, dt=2.0, speed=40.0, lat=LAT + 0.00008, source="ocr"):
    """A drive due east through the hazard (≈ 9 m north of it), one fix every 2 s."""
    start = LON - STEP * (n // 2)
    return [TrackPt(lat, start + i * STEP, speed, i * dt, source, i * 10) for i in range(n)]


def _pass(points, **kw):
    args = dict(last_seen_at=T0 - timedelta(hours=1), ref_headings=[90.0], start_time=T0, rules=PassRules())
    args.update(kw)
    return evaluate_pass(points, LAT, LON, **args)


def test_geometry_helpers():
    assert round(bearing(0, 0, 0, 1)) == 90 and round(bearing(0, 0, 1, 0)) == 0
    assert angle_diff(350, 10) == 20 and angle_diff(90, 270) == 180
    pts = _east_track()
    assert round(heading_at(pts, 10)) == 90
    stopped = [TrackPt(LAT, LON, 0.0, i, "ocr", i) for i in range(5)]
    assert heading_at(stopped, 2) is None
    no_speed = [TrackPt(LAT, LON + i * STEP, None, i * 2.0, "ocr", i) for i in range(3)]
    assert 35 < speed_at(no_speed, 1) < 45                               # derived from positions/times


def test_valid_pass():
    r = _pass(_east_track())
    assert r.passed and r.valid and r.reason == "valid"
    assert r.distance_m < 10 and r.speed_kmh == 40 and round(r.heading_deg) == 90
    assert r.recorded_at == T0 + timedelta(seconds=20)                  # closest fix at video_time_s 20


def test_pass_prefers_track_point_recording_time():
    """A fix's own recorded_at (OSD time, aware) wins over start + video_time_s,
    and makes the pass datable even without a recording start."""
    from zoneinfo import ZoneInfo
    pts = _east_track()
    osd = (T0 + timedelta(minutes=5)).replace(tzinfo=timezone.utc).astimezone(ZoneInfo("Africa/Douala"))
    for p in pts:
        p.recorded_at = osd
    r = _pass(pts)
    assert r.valid and r.recorded_at == T0 + timedelta(minutes=5)       # UTC naive, like every lifecycle instant
    assert _pass(pts, start_time=None).valid


def test_pass_rules_each_refusal():
    pts = _east_track()
    far = [TrackPt(p.latitude + 0.001, p.longitude, p.speed_kmh, p.video_time_s) for p in pts]   # ≈ 110 m
    assert _pass(far).reason == "not_passed" and not _pass(far).passed
    interpolated = _east_track(source="interpolated")
    assert _pass(interpolated).reason == "not_passed"                    # confirmed GPS only
    assert _pass(pts, detected_nearby=True).reason == "detected"          # a sighting, not an absence
    assert _pass(_east_track(speed=80)).reason == "too_fast"
    assert _pass(pts, ref_headings=[270.0]).reason == "wrong_direction"
    assert _pass(pts, ref_headings=[120.0]).valid                         # 30° off: within 60°
    assert _pass(pts, start_time=None).reason == "unknown_time"
    assert _pass(pts, last_seen_at=T0 + timedelta(hours=1)).reason == "before_last_seen"
    stopped = [TrackPt(LAT + 0.00008, LON, 0.0, i * 2.0, "ocr", i) for i in range(5)]
    assert _pass(stopped).reason == "unknown_heading"


def test_pass_direction_when_original_heading_unknown():
    pts = _east_track()
    assert _pass(pts, ref_headings=[]).valid
    strict = PassRules(require_same_direction=True)
    assert _pass(pts, ref_headings=[], rules=strict).reason == "unknown_reference_heading"
    assert _pass(pts, ref_headings=[90.0], rules=strict).valid


def test_pass_daylight_window():
    night = datetime(2026, 10, 1, 19, 0)                                  # 20:00 local
    rules = PassRules(daylight_only=True)
    assert _pass(_east_track(), start_time=night, rules=rules).reason == "not_daylight"
    assert _pass(_east_track(), start_time=night).valid                   # daylight not required
    assert _pass(_east_track(), rules=rules).valid


def test_recording_start_and_eligible_types():
    dets = [SimpleNamespace(recorded_at=None, captured_at=datetime(2026, 10, 1, 11, 0, 30), created_at=None,
                            video_time_s=30.0),
            SimpleNamespace(recorded_at=None, captured_at=None, created_at=T0, video_time_s=5.0),   # no clock
            SimpleNamespace(recorded_at=None, captured_at=datetime(2026, 10, 1, 11, 1, 0), created_at=None,
                            video_time_s=60.0)]
    assert recording_start(dets, CFG) == T0
    assert recording_start(dets[1:2], CFG) is None
    assert eligible_types(["pothole", "vehicles"], CFG.type_models) == {"pothole", "parked_vehicle"}
    assert eligible_types(["signs"], CFG.type_models) == {"traffic_sign"}


def test_absences_count_since_last_sighting():
    passes = [T0 + timedelta(hours=1), T0 + timedelta(hours=2)]
    assert service.count_since(passes, T0) == 2
    assert service.count_since(passes, T0 + timedelta(minutes=90)) == 1   # a sighting in between resets
    assert service.count_since(passes + [None], None) == 2


# ── votes ────────────────────────────────────────────────────────────────────

def test_vote_eligibility():
    now = T0
    ok = dict(hazard_active=True, votes_enabled=True, distance_m=50.0, max_distance_m=300,
              last_vote_at=None, cooldown_minutes=60, now=now)
    assert votes.judge_vote(**ok) is None
    assert votes.judge_vote(**{**ok, "hazard_active": False}) == "hazard_resolved"
    assert votes.judge_vote(**{**ok, "votes_enabled": False}) == "votes_disabled"
    assert votes.judge_vote(**{**ok, "distance_m": 301.0}) == "too_far"
    assert votes.judge_vote(**{**ok, "distance_m": None}) == "too_far"
    assert votes.judge_vote(**{**ok, "last_vote_at": now - timedelta(minutes=30)}) == "cooldown"
    assert votes.judge_vote(**{**ok, "last_vote_at": now - timedelta(minutes=61)}) is None
    assert votes.judge_vote(**{**ok, "last_vote_at": now, "cooldown_minutes": 0}) is None
    assert not votes.should_resolve(1, 2) and votes.should_resolve(2, 2)


def test_vote_distance():
    hz = SimpleNamespace(latitude=LAT, longitude=LON)
    assert votes.vote_distance(hz, None, LON) is None
    assert votes.vote_distance(hz, 95.0, LON) is None
    assert 105 < votes.vote_distance(hz, LAT + 0.001, LON) < 117


# ── expiry ───────────────────────────────────────────────────────────────────

def test_expiry_computation():
    assert expiry.compute_expires_at(T0, None) is None                    # persistent
    assert expiry.compute_expires_at(None, 2) is None
    assert expiry.compute_expires_at(T0, 2) == T0 + timedelta(hours=2)
    assert not expiry.is_expired(None, T0) and expiry.is_expired(T0, T0)
    # late-processed video: recorded 3 h ago, a blocking car expires after 2 h
    now = T0 + timedelta(hours=3)
    blocking = resolve_policy(CFG, "parked_vehicle", "blocking", "car")
    assert expiry.is_stale_evidence(T0, blocking.expiry_hours, now)
    # … unless it is a truck (24 h)
    assert not expiry.is_stale_evidence(T0, resolve_policy(CFG, "parked_vehicle", "blocking", "truck").expiry_hours, now)
    assert not expiry.is_stale_evidence(T0, None, now + timedelta(days=999))
    assert expiry.type_can_expire(CFG.policies["parked_vehicle"])
    assert not expiry.type_can_expire(CFG.policies["pothole"])


# ── placement, visibility ────────────────────────────────────────────────────

def test_placement_near_resolved_hazard():
    resolved_at = T0
    assert service.placement(T0 - timedelta(minutes=1), resolved_at, True) == "history"
    assert service.placement(T0 + timedelta(minutes=1), resolved_at, True) == "reopen"
    assert service.placement(T0 + timedelta(minutes=1), resolved_at, False) == "new"
    assert service.placement(T0 + timedelta(hours=3), resolved_at, True, 2.0) == "new"     # too late to reopen
    assert service.placement(T0 + timedelta(hours=1), resolved_at, True, 2.0) == "reopen"
    assert service.placement(None, resolved_at, True) == "reopen"


def test_driver_visibility():
    pol = resolve_policy(CFG, "parked_vehicle", "blocking")
    active = SimpleNamespace(status=HazardStatus.active.value, expires_at=T0 + timedelta(hours=1))
    assert service.driver_visible(active, pol, T0)
    assert not service.driver_visible(active, pol, T0 + timedelta(hours=2))              # expired, not yet swept
    resolved = SimpleNamespace(status=HazardStatus.resolved.value, expires_at=None)
    assert not service.driver_visible(resolved, pol, T0)
    hidden = HazardLifecycleSettings(policies={"parked_vehicle": {"subtype_overrides": {
        "roadside": {"show_to_drivers": False}}}})
    assert not service.driver_visible(active, resolve_policy(hidden, "parked_vehicle", "roadside"), T0)
    assert service.driver_visible(None, pol, T0)


# ── recurring spots ──────────────────────────────────────────────────────────

def _hz(i, lat=LAT, lon=LON, status="resolved", resolved_at=T0, type_="parked_vehicle"):
    return SimpleNamespace(id=f"h{i}", type=type_, status=status, resolved_at=resolved_at, latitude=lat,
                           longitude=lon, radius_m=50.0, created_at=T0 + timedelta(minutes=i))


def test_recurring_count():
    since = T0 - timedelta(days=30)
    rows = [_hz(1), _hz(2, lat=LAT + 0.0002), _hz(3, resolved_at=T0 - timedelta(days=40)),
            _hz(4, lat=LAT + 0.01), _hz(5, status="active"), _hz(6, type_="pothole")]
    assert recurring.count_recurrences(rows, LAT, LON, 50, since, "parked_vehicle") == 2
    assert recurring.count_recurrences(rows, LAT, LON, 50, since, "parked_vehicle", exclude_id="h1") == 1


def test_find_spots():
    rows = [_hz(i, lat=LAT + i * 0.00005) for i in range(4)] + [_hz(9, lat=LAT + 0.01)] + \
           [_hz(10 + i, type_="pothole") for i in range(2)]
    spots = recurring.find_spots(rows, 3)
    assert len(spots) == 1 and spots[0].occurrences == 4 and spots[0].type == "parked_vehicle"
    assert recurring.find_spots(rows, 2)[1].type == "pothole"


# ── services & API against a real database (RUN_DB_TESTS=1) ──────────────────

db_only = pytest.mark.skipif(os.environ.get("RUN_DB_TESTS") != "1",
                             reason="needs a throwaway PostgreSQL (RUN_DB_TESTS=1)")


@pytest.fixture
async def db():
    from app.core.db.session import AsyncSessionLocal, engine as db_engine, init_db
    from app.core import settings_service
    await init_db()
    async with AsyncSessionLocal() as session:
        await settings_service.update_section(session, "hazard_lifecycle", HazardLifecycleSettings().model_dump(),
                                              "pytest")
        settings_service.clear_cache()
        yield session
    await db_engine.dispose()


def _spot() -> tuple[float, float]:
    """A fresh place per test so leftovers never cluster together."""
    return round(random.uniform(-30, 30), 5), round(random.uniform(-60, 60), 5)


async def _job(db, models="vehicles,pothole", track_at=None, start=None):
    from app.modules.m07_ingestion.models.job import Job, JobStatus
    from app.modules.m07_ingestion.models.job_track_point import JobTrackPoint
    now = datetime.utcnow()
    job = Job(id=str(uuid.uuid4()), filename="pytest.mp4", status=JobStatus.done, enabled_models=models,
              source_type="upload", finished_at=now, started_at=now)
    db.add(job)
    await db.flush()
    if track_at is not None:
        lat, lon = track_at
        for i in range(21):
            db.add(JobTrackPoint(job_id=job.id, frame_number=i * 10, video_time_s=i * 2.0, latitude=lat + 0.00008,
                                 longitude=lon + (i - 10) * STEP, speed_kmh=40.0, source="ocr"))
    if start is not None:   # a far, rejected detection gives the job its recording clock
        await _det(db, job, (track_at[0] + 0.05, track_at[1]), start, type_="pothole", video_time_s=0.0,
                   status="rejected")
    await db.flush()
    return job


async def _det(db, job, at, recorded_at, *, type_="parked_vehicle", subtype="blocking", vehicle="car",
               video_time_s=20.0, frame=100, status="pending"):
    from app.modules.m06_hazards.models.detection import Detection, ReviewStatus
    det = Detection(job_id=job.id, type=type_, subtype=subtype, confidence=0.9, frame_number=frame,
                    latitude=at[0], longitude=at[1], location_status="confirmed", location_source="ocr",
                    review_status=ReviewStatus(status), video_time_s=video_time_s,
                    recorded_at=recorded_at.replace(tzinfo=timezone.utc),
                    ai_details={"vehicle_class": vehicle} if type_ == "parked_vehicle" else None)
    db.add(det)
    await db.flush()
    return det


async def _accept(db, det):
    from app.modules.m06_hazards import detection_workflow as wf
    await wf.accept(db, det, wf.SYSTEM)
    await db.commit()
    from app.modules.m06_hazards.models.hazard import Hazard
    return await db.get(Hazard, det.hazard_id)


async def _kinds(db, hazard_id):
    from app.modules.m06_hazards.lifecycle import events
    _, rows = await events.timeline(db, hazard_id)
    return [r.kind for r in reversed(rows)]


@db_only
async def test_parked_car_resolved_after_two_valid_passes_then_reopened(db):
    from app.modules.m06_hazards.lifecycle import video_pass
    spot = _spot()
    now = datetime.utcnow()
    j1 = await _job(db, track_at=spot)
    det = await _det(db, j1, spot, now - timedelta(minutes=30))
    hazard = await _accept(db, det)
    assert hazard.status == "active" and hazard.expires_at is not None
    assert abs((hazard.expires_at - hazard.last_seen_at).total_seconds() - 2 * 3600) < 1

    j2 = await _job(db, track_at=spot, start=now - timedelta(minutes=20))
    s2 = await video_pass.evaluate_job(db, j2.id)
    assert s2["not_seen"] == 1
    await db.refresh(hazard)
    assert hazard.status == "active" and hazard.absence_count == 1
    again = await video_pass.evaluate_job(db, j2.id)                      # idempotent per (job, hazard)
    assert again["not_seen"] == 0

    j3 = await _job(db, track_at=spot, start=now - timedelta(minutes=10))
    s3 = await video_pass.evaluate_job(db, j3.id)
    assert hazard.code in s3["resolved"]
    await db.refresh(hazard)
    await db.refresh(det)
    assert hazard.status == "resolved" and hazard.resolution_source == "video_absence"
    assert det.review_status.value == "resolved"
    assert await _kinds(db, hazard.id) == ["seen", "not_seen", "not_seen", "resolved"]

    # seen again later → the same hazard reopens (parked_vehicle reopen_on_sighting)
    j4 = await _job(db)
    det2 = await _det(db, j4, spot, now - timedelta(minutes=1))
    reopened = await _accept(db, det2)
    await db.refresh(det)
    assert reopened.id == hazard.id and reopened.status == "active" and reopened.resolution_source is None
    assert det.review_status.value == "validated"
    assert "reopened" in await _kinds(db, hazard.id)


@db_only
async def test_pending_detection_is_a_sighting_not_an_absence(db):
    from app.modules.m06_hazards.lifecycle import video_pass
    spot = _spot()
    now = datetime.utcnow()
    j1 = await _job(db, track_at=spot)
    hazard = await _accept(db, await _det(db, j1, spot, now - timedelta(minutes=30)))
    first_seen = hazard.last_seen_at
    j2 = await _job(db, track_at=spot, start=now - timedelta(minutes=5))
    await _det(db, j2, spot, now - timedelta(minutes=5))                   # pending review
    s = await video_pass.evaluate_job(db, j2.id)
    await db.refresh(hazard)
    assert s["sightings"] == 1 and s["not_seen"] == 0
    assert hazard.last_seen_at > first_seen and hazard.absence_count == 0
    assert hazard.expires_at > now


@db_only
async def test_stale_late_video_is_history_only(db):
    from app.modules.m06_hazards.models.detection import Detection
    spot = _spot()
    now = datetime.utcnow()
    det = await _det(db, await _job(db), spot, now - timedelta(hours=3))   # blocking car: 2 h expiry
    hazard = await _accept(db, det)
    assert hazard.status == "resolved" and hazard.resolution_source == "expiry"
    assert await _kinds(db, hazard.id) == ["seen", "expired", "resolved"]
    fresh = await db.get(Detection, det.id)
    assert await service.filter_for_drivers(db, [fresh]) == []

    truck = await _det(db, await _job(db), _spot(), now - timedelta(hours=3), vehicle="truck")
    heavy = await _accept(db, truck)
    assert heavy.status == "active"                                       # 24 h for heavy vehicles


@db_only
async def test_expiry_sweep_and_driver_filter(db):
    spot = _spot()
    now = datetime.utcnow()
    det = await _det(db, await _job(db), spot, now - timedelta(minutes=5), subtype="roadside")
    hazard = await _accept(db, det)
    visible = await service.filter_for_drivers(db, [det])
    assert visible == [det] and det.alert_priority == "low"
    hazard.last_seen_at = now - timedelta(hours=7)                       # roadside: 6 h
    await db.commit()
    assert await expiry.sweep(db) >= 1
    await db.refresh(hazard)
    assert hazard.status == "resolved" and hazard.resolution_source == "expiry"


@db_only
async def test_recurring_spot_flag(db):
    from app.modules.m06_hazards.lifecycle import service as lc
    spot = _spot()
    now = datetime.utcnow()
    for i in range(3):          # three cars parked here and gone, days apart
        h = await _accept(db, await _det(db, await _job(db), spot, now - timedelta(days=3 - i, minutes=5)))
        await lc.resolve(db, h, "votes", evidence_at=now - timedelta(days=3 - i))
        await db.commit()
    new = await _accept(db, await _det(db, await _job(db), spot, now - timedelta(minutes=2)))
    assert new.status == "active" and new.recurring_spot and new.recurrence_count == 3
    cfg = await lc.load_settings(db)
    spots = await recurring.list_spots(db, cfg, det_type="parked_vehicle",
                                       bbox=(spot[0] - 0.01, spot[1] - 0.01, spot[0] + 0.01, spot[1] + 0.01))
    assert len(spots) == 1 and spots[0].occurrences == 4


@pytest.fixture
async def api(db):
    from app.core.security import hash_token, new_token
    from app.main import app
    from app.modules.m12_integration.models.api_client import ApiClient
    from app.modules.m01_security.models.auth_token import AuthSession, SessionStage
    from app.modules.m01_security.models.user import User
    key = new_token("vsk_")
    db.add(ApiClient(name="pytest hazards", key_prefix=key[:10], key_hash=hash_token(key), created_by="pytest"))
    tokens = []
    for i in range(4):
        user = User(email=f"driver-{uuid.uuid4().hex[:8]}@example.com", full_name=f"Driver {i}", account_type="mobile")
        db.add(user)
        await db.flush()
        tok = new_token("vss_")
        db.add(AuthSession(user_id=user.id, token_hash=hash_token(tok), stage=SessionStage.full,
                           expires_at=datetime.utcnow() + timedelta(hours=1)))
        tokens.append(tok)
    await db.commit()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        client.key = {"X-API-Key": key}
        client.users = [{"X-API-Key": key, "Authorization": f"Bearer {t}"} for t in tokens]
        yield client


@db_only
async def test_driver_votes(api, db):
    spot = _spot()
    hazard = await _accept(db, await _det(db, await _job(db), spot, datetime.utcnow() - timedelta(minutes=5)))
    url = f"/hazards/{hazard.id}/votes"
    near = {"latitude": spot[0] + 0.0003, "longitude": spot[1]}            # ≈ 33 m
    far = {"latitude": spot[0] + 0.01, "longitude": spot[1]}               # ≈ 1.1 km
    u = api.users

    r = await api.post(url, json={"vote": "not_there", **near}, headers=api.key)
    assert r.status_code == 401                                          # signed-in drivers only
    r = await api.post(url, json={"vote": "not_there", **far}, headers=u[0])
    assert r.status_code == 422 and r.json()["detail"]["code"] == "too_far"
    r = await api.post(url, json={"vote": "still_there", **near}, headers=u[0])
    assert r.status_code == 201 and r.json() == {"message": "Thanks, your vote has been recorded."}
    r = await api.post(url, json={"vote": "not_there", **near}, headers=u[0])
    assert r.status_code == 429 and r.json()["detail"]["code"] == "cooldown"
    assert (await api.post(url, json={"vote": "not_there", **near}, headers=u[1])).status_code == 201
    await db.refresh(hazard)
    assert hazard.status == "active"
    assert (await api.post(url, json={"vote": "not_there", **near}, headers=u[2])).status_code == 201
    await db.refresh(hazard)
    assert hazard.status == "resolved" and hazard.resolution_source == "votes"
    r = await api.post(url, json={"vote": "still_there", **near}, headers=u[3])
    assert r.status_code == 409 and r.json()["detail"]["code"] == "hazard_resolved"

    ev = await api.get(f"/hazards/{hazard.id}/events")
    if ev.status_code == 200:                                            # AUTH_ENFORCED off in tests
        kinds = [e["kind"] for e in ev.json()["items"]]
        assert kinds.count("vote_not_there") == 2 and "vote_still_there" in kinds and kinds[0] == "resolved"
