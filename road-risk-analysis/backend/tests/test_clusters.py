"""
Cluster-first hazards (docs/CLUSTERS.md): every located detection joins a
cluster at creation; candidates (CL-…) are reviewed once as a cluster and
stay out of every hazard screen; re-sightings of confirmed hazards are
auto-validated; candidates are dismissed by valid "not seen" passes; the
same video is not processed twice; recluster-detections merges duplicates.

Pure tests run everywhere; DB tests need a throwaway PostgreSQL
(RUN_DB_TESTS=1, DATABASE_URL=…).
"""
import os
import random
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from app.modules.m06_hazards import detection_workflow as wf, hazard_service
from app.modules.m06_hazards.clustering import zones as geo
from app.modules.m06_hazards.clustering.resolver import DEFAULT_PRIORITY, Snapshot, ZoneRule
from app.modules.m06_hazards.clustering.settings import ClusteringSettings
from app.modules.m06_hazards.lifecycle.settings import HazardLifecycleSettings
from app.modules.m06_hazards.lifecycle.video_pass import PassRules, TrackPt, approached, evaluate_pass
from app.modules.m06_hazards.models.hazard import OPEN_STATUSES, PUBLIC_STATUSES, HazardStatus

M = 1 / 111_320.0          # one metre of latitude, in degrees


# ── pure rules ────────────────────────────────────────────────────────────────

def test_statuses_and_codes():
    assert PUBLIC_STATUSES == ("active", "resolved")
    assert OPEN_STATUSES == ("candidate", "active")
    assert {s.value for s in HazardStatus} == {"candidate", "active", "resolved", "dismissed"}
    assert hazard_service.candidate_code(-42) == "CL-000042" and hazard_service.hazard_code(7) == "HZ-000007"


def test_clusterable_detections():
    det = lambda **kw: SimpleNamespace(**{"review_status": "pending", "latitude": 4.0, "longitude": 9.0,
                                          "location_status": "confirmed", **kw})
    assert hazard_service.is_clusterable(det())
    assert hazard_service.is_clusterable(det(location_status="approximate", review_status="location_pending"))
    assert not hazard_service.is_clusterable(det(location_status="missing", latitude=None))
    assert not hazard_service.is_clusterable(det(review_status="rejected"))


def test_resight_transitions():
    assert wf.is_allowed("pending", "validated", "resight")
    assert wf.is_allowed("location_pending", "validated", "resight")
    assert not wf.is_allowed("rejected", "validated", "resight")
    assert not wf.is_allowed("location_pending", "validated", "accept")       # humans fix the position first


def test_centroid_prefers_confirmed_positions():
    a = SimpleNamespace(latitude=4.0, longitude=9.0, confidence=0.9, location_status="confirmed", captured_at=None,
                        created_at=datetime(2026, 1, 1), location_name=None, city=None, country=None)
    b = SimpleNamespace(**{**a.__dict__, "latitude": 4.001, "location_status": "approximate"})
    agg = hazard_service.aggregate([a, b])
    assert agg["latitude"] == 4.0 and agg["detection_count"] == 2


def test_new_lifecycle_defaults_and_saved_settings_upgrade():
    cfg = HazardLifecycleSettings()
    for t in ("pothole", "speed_bump"):
        p = cfg.policies[t]
        assert p.absence_resolution and p.absences_to_resolve == 3 and p.require_same_direction
    assert cfg.policies["traffic_sign"].absences_to_resolve == 3
    assert cfg.policies["parked_vehicle"].absences_to_resolve == 2
    assert cfg.pass_require_approach and cfg.pass_approach_min_m == 10
    # stored before version 2 with the old untouched values → new defaults apply
    old = HazardLifecycleSettings(policies={"pothole": {"absence_resolution": False, "absences_to_resolve": 2,
                                                        "require_same_direction": False, "votes_enabled": False}})
    assert old.policies["pothole"].absence_resolution and not old.policies["pothole"].votes_enabled
    # an explicit administrator choice is kept
    kept = HazardLifecycleSettings(policies={"speed_bump": {"absence_resolution": False, "absences_to_resolve": 4}})
    assert not kept.policies["speed_bump"].absence_resolution
    assert HazardLifecycleSettings(**old.model_dump()).defaults_version == 2


def _track(lat, lon, start_offset_m=-100, n=21, step_m=10):
    """Eastward drive along the latitude of the spot, starting `start_offset_m` from it."""
    k = 1 / (111_320.0 * 0.99756)
    return [TrackPt(lat + 8 * M, lon + (start_offset_m + i * step_m) * k, 30.0, i * 1.0, "ocr", i * 10)
            for i in range(n)]


def test_pass_requires_the_camera_to_approach_the_spot():
    lat, lon = 4.05, 9.70
    rules = PassRules(require_approach=True, approach_min_m=10)
    t0 = datetime(2026, 10, 1, 10)
    ok = evaluate_pass(_track(lat, lon), lat, lon, last_seen_at=None, ref_headings=[], start_time=t0, rules=rules)
    assert ok.valid
    # the video starts right at the spot: the camera never filmed it
    late = _track(lat, lon, start_offset_m=0)
    res = evaluate_pass(late, lat, lon, last_seen_at=None, ref_headings=[], start_time=t0, rules=rules)
    assert not res.valid and res.reason == "not_approaching"
    assert not approached(late, 0, lat, lon, 10)
    # same track without the rule: valid
    assert evaluate_pass(late, lat, lon, last_seen_at=None, ref_headings=[], start_time=t0,
                         rules=PassRules()).valid


# ── database ─────────────────────────────────────────────────────────────────

db_only = pytest.mark.skipif(os.environ.get("RUN_DB_TESTS") != "1",
                             reason="needs a throwaway PostgreSQL (RUN_DB_TESTS=1)")


@pytest.fixture
async def db():
    from app.core.db.session import AsyncSessionLocal, engine as db_engine, init_db
    from app.core import settings_service
    from app.modules.m06_hazards.rejection_seeds import seed_rejection_reasons
    await init_db()
    async with AsyncSessionLocal() as session:
        await seed_rejection_reasons(session)
        await settings_service.update_section(session, "hazard_lifecycle", HazardLifecycleSettings().model_dump(),
                                              "pytest")
        settings_service.clear_cache()
        yield session
    await db_engine.dispose()


def _spot():
    return round(random.uniform(-30, 30), 5), round(random.uniform(-60, 60), 5)


def _ctx(zone_at=None, zone_radius=15.0):
    """Unzoned: rural profile 60 m (pothole 50 m); optional 300 m zone around `zone_at` with `zone_radius`."""
    settings = ClusteringSettings(urban={"default_radius_m": 30, "type_radius": {}, "signalement_radius": {}},
                                  rural={"default_radius_m": 60, "type_radius": {"pothole": 50},
                                         "signalement_radius": {}})
    zones = []
    if zone_at is not None:
        zones.append(ZoneRule(id=1, name="Zone", kind="custom", environment="urban",
                              shape=geo.compile_shape(geo.circle_polygon(zone_at[0], zone_at[1], 300)),
                              priority=DEFAULT_PRIORITY["custom"], default_radius_m=zone_radius))
    return hazard_service.ClusterContext(snapshot=Snapshot(settings=settings, zones=zones),
                                         distinct_subtype_types=["traffic_sign"])


async def _job(db, models="pothole,signs", track_at=None):
    from app.modules.m07_ingestion.models.job import Job, JobStatus
    from app.modules.m07_ingestion.models.job_track_point import JobTrackPoint
    now = datetime.utcnow()
    job = Job(id=str(uuid.uuid4()), filename="pytest.mp4", status=JobStatus.done, enabled_models=models,
              source_type="upload", finished_at=now, started_at=now)
    db.add(job)
    await db.flush()
    if track_at is not None:
        k = 1 / (111_320.0 * 0.99)
        for i in range(21):
            db.add(JobTrackPoint(job_id=job.id, frame_number=i * 10, video_time_s=i * 2.0,
                                 latitude=track_at[0] + 8 * M, longitude=track_at[1] + (i - 10) * 10 * k,
                                 speed_kmh=40.0, source="ocr",
                                 recorded_at=(now + timedelta(seconds=i * 2)).replace(tzinfo=timezone.utc)))
        await db.flush()
    return job


async def _det(db, job, at, *, north_m=0.0, type_="pothole", subtype=None, status="pending",
               location="confirmed", recorded_at=None, conf=0.8):
    from app.modules.m06_hazards.models.detection import Detection, ReviewStatus
    det = Detection(job_id=job.id, type=type_, subtype=subtype, confidence=conf, frame_number=100,
                    latitude=at[0] + north_m * M, longitude=at[1], location_status=location,
                    location_source="ocr" if location == "confirmed" else "interpolated",
                    review_status=ReviewStatus(status), video_time_s=20.0,
                    recorded_at=(recorded_at or datetime.utcnow() - timedelta(hours=1)).replace(tzinfo=timezone.utc))
    db.add(det)
    await db.flush()
    return det


USER = wf.Actor("user", "reviewer@example.com")


@db_only
async def test_attach_rules_zone_radius_subtype_and_nearest(db):
    spot = _spot()
    ctx = _ctx(zone_at=spot, zone_radius=15)
    job = await _job(db)
    a = await _det(db, job, spot)
    ha = await hazard_service.attach(db, a, ctx)
    assert ha.status == "candidate" and ha.code.startswith("CL-") and ha.number < 0 and ha.radius_m == 15
    b = await _det(db, job, spot, north_m=12)
    assert (await hazard_service.attach(db, b, ctx)).id == ha.id                     # within the zone radius
    c = await _det(db, job, spot, north_m=40)                                         # 28 m from the centroid
    hc = await hazard_service.attach(db, c, ctx)
    assert hc.id != ha.id                                                             # zone radius 15 m

    far = _spot()                                                                     # unzoned: pothole 50 m
    d1 = await _det(db, job, far)
    d2 = await _det(db, job, far, north_m=45)
    h1 = await hazard_service.attach(db, d1, ctx)
    assert (await hazard_service.attach(db, d2, ctx)).id == h1.id
    await db.refresh(h1)
    assert h1.detection_count == 2

    # nearest: two clusters 100 m apart, a detection at 70 m from the first joins the second
    n = _spot()
    first = await hazard_service.attach(db, await _det(db, job, n), ctx)
    second = await hazard_service.attach(db, await _det(db, job, n, north_m=100), ctx)
    assert first.id != second.id
    assert (await hazard_service.attach(db, await _det(db, job, n, north_m=70), ctx)).id == second.id

    # distinct-subtype types: two signs 5 m apart stay separate, the same sign joins
    s = _spot()
    stop = await hazard_service.attach(db, await _det(db, job, s, type_="traffic_sign", subtype="stop"), ctx)
    limit = await hazard_service.attach(db, await _det(db, job, s, north_m=5, type_="traffic_sign",
                                                       subtype="speed_limit_50"), ctx)
    again = await hazard_service.attach(db, await _det(db, job, s, north_m=3, type_="traffic_sign",
                                                       subtype="stop"), ctx)
    assert stop.id != limit.id and again.id == stop.id

    # approximate positions join too; missing ones wait for a position
    approx = await _det(db, job, far, north_m=5, status="location_pending", location="approximate")
    assert (await hazard_service.attach(db, approx, ctx)).id == h1.id
    missing = await _det(db, job, far, status="location_pending", location="missing")
    missing.latitude = missing.longitude = None
    assert await hazard_service.attach(db, missing, ctx) is None
    await db.commit()


@db_only
async def test_confirmation_resighting_and_policy_override(db):
    from app.core import settings_service
    from app.modules.m06_hazards.lifecycle import events
    from app.modules.m06_hazards.models.detection_review import DetectionReview
    from sqlalchemy import select
    spot = _spot()
    ctx = _ctx()
    det1 = await _det(db, await _job(db), spot)
    cand = await hazard_service.attach(db, det1, ctx)
    cl_code = cand.code
    await wf.accept(db, det1, USER)
    await db.commit()
    await db.refresh(cand)
    assert cand.status == "active" and cand.code.startswith("HZ-") and cand.number > 0
    _, evs = await events.timeline(db, cand.id)
    confirmed = [e for e in evs if e.kind == "confirmed"]
    assert confirmed and confirmed[0].details["previous_code"] == cl_code

    # a later detection of the same pothole: auto-validated re-sighting, never in the queue
    det2 = await _det(db, await _job(db), spot, north_m=8, recorded_at=datetime.utcnow() - timedelta(minutes=5))
    assert (await hazard_service.attach(db, det2, ctx)).id == cand.id
    await db.commit()
    await db.refresh(det2)
    assert det2.review_status.value == "validated" and det2.reviewed_by == "system"
    assert det2.review_note == f"matches confirmed hazard {cand.code}"
    hist = (await db.execute(select(DetectionReview).where(DetectionReview.detection_id == det2.id))).scalars().all()
    assert [h.action for h in hist] == ["resight"]
    assert await events.exists(db, cand.id, ("seen",), detection_id=det2.id)

    # policy override: re-sightings of potholes go to review
    await settings_service.update_section(db, "hazard_lifecycle",
                                          {"policies": {"pothole": {"auto_confirm_resightings": False}}}, "pytest")
    settings_service.clear_cache()
    det3 = await _det(db, await _job(db), spot, north_m=-6)
    assert (await hazard_service.attach(db, det3, ctx)).id == cand.id
    await db.commit()
    await db.refresh(det3)
    assert det3.review_status.value == "pending"
    await settings_service.update_section(db, "hazard_lifecycle", HazardLifecycleSettings().model_dump(), "pytest")
    settings_service.clear_cache()


@db_only
async def test_reject_detaches_and_deletes_empty_candidate(db):
    from app.modules.m06_hazards.models.hazard import Hazard
    spot = _spot()
    ctx = _ctx()
    job = await _job(db)
    a, b = await _det(db, job, spot), await _det(db, job, spot, north_m=5)
    h = await hazard_service.attach(db, a, ctx)
    await hazard_service.attach(db, b, ctx)
    hid = h.id
    await wf.reject(db, a, USER, reason_code="shadow")
    await db.commit()
    assert a.hazard_id is None and (await db.get(Hazard, hid)).detection_count == 1
    await wf.reject(db, b, USER, reason_code="shadow")
    await db.commit()
    db.expunge_all()
    assert await db.get(Hazard, hid) is None                                         # empty candidate deleted


@pytest.fixture
async def api(db, monkeypatch):
    from app.core.config import settings
    from app.main import app
    monkeypatch.setattr(settings, "AUTH_ENFORCED", False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def _candidate(db, n=3, *, approx=0):
    spot = _spot()
    job = await _job(db)
    ctx = _ctx()
    dets = [await _det(db, job, spot, north_m=i * 3, conf=0.6 + i / 10) for i in range(n)]
    dets += [await _det(db, job, spot, north_m=-4 - i, status="location_pending", location="approximate")
             for i in range(approx)]
    hazard = None
    for d in dets:
        hazard = await hazard_service.attach(db, d, ctx)
    await db.commit()
    return spot, hazard, dets


@db_only
async def test_cluster_accept_and_reject_endpoints(api, db):
    from app.modules.m06_hazards.models.training_sample import TrainingSample
    spot, cand, dets = await _candidate(db, 3, approx=1)
    q = (await api.get("/hazards/review-queue", params={"search": cand.code})).json()
    assert q["total"] == 1 and q["candidates"] == 1
    row = q["items"][0]
    assert row["hazard"]["status"] == "candidate" and not row["hazard"]["confirmed"]
    assert row["hazard"]["pending_count"] == 4 and len(row["detections"]) == 4
    assert row["detections"][0]["confidence"] >= row["detections"][-1]["confidence"]
    assert row["detections"][0]["cluster"]["hazard_code"] == cand.code

    r = await api.post(f"/hazards/{cand.id}/accept", json={"note": "seen it"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["accepted"] == 3 and out["auto_validated"] == 1 and out["previous_code"] == cand.code
    assert out["hazard_status"] == "active" and out["hazard_code"].startswith("HZ-")
    assert out["hazard"]["member_counts"]["validated"] == 4
    assert (await api.post(f"/hazards/{cand.id}/accept", json={})).json()["detail"]["code"] == "nothing_to_review"
    sample = await db.get(TrainingSample, dets[-1].id)                               # the auto-validated one
    assert sample is not None and sample.decided_by == "system"

    # reject: unknown reason refused, then every unreviewed member rejected and the candidate removed
    _, cand2, dets2 = await _candidate(db, 2, approx=1)
    r = await api.post(f"/hazards/{cand2.id}/reject", json={"reason_code": "nope"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_reason"
    r = await api.post(f"/hazards/{cand2.id}/reject", json={"reason_code": "shadow", "note": "shade"})
    assert r.status_code == 200, r.text
    assert r.json()["rejected"] == 3 and r.json()["hazard_status"] == "deleted"
    for d in dets2:
        await db.refresh(d)
        assert d.review_status.value == "rejected" and d.rejection_code == "shadow" and d.hazard_id is None


@db_only
async def test_candidates_never_leak_to_hazard_screens(api, db):
    spot, cand, dets = await _candidate(db, 2)
    near = {"latitude": spot[0], "longitude": spot[1]}
    assert cand.code not in {h["code"] for h in (await api.get("/hazards/", params={"limit": 200})).json()["items"]}
    assert (await api.get("/hazards/", params={"status": "candidate"})).status_code == 422
    assert (await api.get(f"/hazards/{cand.id}")).status_code == 404
    assert (await api.get(f"/hazards/{cand.id}/events")).status_code == 404
    assert (await api.post(f"/hazards/{cand.id}/resolve", json={})).status_code == 404
    summary = (await api.get("/hazards/summary")).json()
    assert set(summary["by_status"]) == {"active", "resolved"}
    spots = (await api.get("/hazards/recurring-spots", params={"type": "pothole", "min_occurrences": 2})).json()
    assert all(cand.id not in s["hazard_ids"] for s in spots["items"])
    dash = (await api.get("/stats/dashboard")).json()
    assert all(h["id"] != cand.id for h in dash["map_hazards"])
    nearby = (await api.post("/detections/nearby", json={**near, "radius_m": 500})).json()
    assert {d["id"] for d in nearby["items"]}.isdisjoint({d.id for d in dets})
    route = (await api.post("/detections/along-route", json={"waypoints": [
        {"latitude": spot[0] - 0.001, "longitude": spot[1]}, {"latitude": spot[0] + 0.001, "longitude": spot[1]}]})
    ).json()
    assert {d["id"] for d in route["items"]}.isdisjoint({d.id for d in dets})
    det = (await api.get(f"/detections/{dets[0].id}")).json()
    assert det["hazard"] is None and det["hazard_code"] is None and det["hazard_status"] == "candidate"
    # once confirmed it is a hazard everywhere
    assert (await api.post(f"/hazards/{cand.id}/accept", json={})).status_code == 200
    assert (await api.get(f"/hazards/{cand.id}")).status_code == 200


@db_only
async def test_candidate_dismissed_after_valid_passes(db):
    from app.modules.m06_hazards.lifecycle import events, video_pass
    from app.modules.m06_hazards.models.hazard import Hazard
    spot = _spot()
    j0 = await _job(db, track_at=spot)
    det = await _det(db, j0, spot, recorded_at=datetime.utcnow() - timedelta(days=2))
    cand = await hazard_service.attach(db, det)
    await db.commit()
    assert cand.status == "candidate"
    summaries = []
    for _ in range(3):                                                                # pothole: 3 passes
        job = await _job(db, track_at=spot)
        await db.commit()
        summaries.append(await video_pass.evaluate_job(db, job.id))
    assert [s["not_seen"] for s in summaries] == [1, 1, 1], summaries
    assert summaries[-1].get("dismissed") == [cand.code]
    hazard = await db.get(Hazard, cand.id)
    await db.refresh(hazard)
    await db.refresh(det)
    assert hazard.status == "dismissed" and hazard.resolution_source == "video_absence"
    assert det.review_status.value == "rejected" and det.rejection_code == "not_seen_again"
    _, evs = await events.timeline(db, cand.id)
    passes = sorted(e.details["pass_number"] for e in evs if e.kind == "not_seen")
    assert passes == [1, 2, 3] and all(e.details["threshold"] == 3 for e in evs if e.kind == "not_seen")
    # a job of a model that cannot see potholes is never a pass
    other = await _job(db, models="signs", track_at=spot)
    await db.commit()
    assert (await video_pass.evaluate_job(db, other.id))["not_seen"] == 0


@db_only
async def test_recluster_merges_duplicates_and_is_idempotent(db):
    from app.modules.m06_hazards import recluster
    from app.modules.m06_hazards.models.hazard import Hazard
    from app.modules.m06_hazards.lifecycle import events
    spot = _spot()
    job1, job2 = await _job(db), await _job(db)
    # legacy data: two confirmed hazards of the same pothole (old cross-job duplicates) + a pending re-sighting
    a = await _det(db, job1, spot, status="validated")
    b = await _det(db, job2, spot, north_m=10, status="validated")
    ctx = _ctx()
    h1 = await hazard_service._new_cluster(db, a, ctx, "active")
    a.hazard_id = h1.id
    await hazard_service.recompute(db, h1)
    h2 = await hazard_service._new_cluster(db, b, ctx, "active")
    b.hazard_id = h2.id
    await hazard_service.recompute(db, h2)
    pend = await _det(db, job2, spot, north_m=4)
    await db.commit()

    plan = await recluster.run(db, dry_run=True, area=(spot[0] - 0.01, spot[1] - 0.01, spot[0] + 0.01, spot[1] + 0.01))
    assert plan["merges"] == 1 and plan["auto_validated"] == 1
    await db.refresh(h2)
    assert h2.status == "active"                                                     # dry run: nothing changed

    res = await recluster.run(db, area=(spot[0] - 0.01, spot[1] - 0.01, spot[0] + 0.01, spot[1] + 0.01))
    assert res["merges"] == 1 and res["auto_validated"] == 1
    db.expunge_all()
    assert await db.get(Hazard, h2.id) is None
    kept = await db.get(Hazard, h1.id)
    assert kept.code == h1.code and kept.detection_count == 3
    assert any(e.kind == "merged" and e.details["merged_code"] == h2.code
               for e in (await events.timeline(db, kept.id))[1])
    again = await recluster.run(db, area=(spot[0] - 0.01, spot[1] - 0.01, spot[0] + 0.01, spot[1] + 0.01))
    assert again["merges"] == 0 and again["attached"] == 0 and again["auto_validated"] == 0


@db_only
async def test_upload_of_the_same_video_is_refused_unless_forced(api, db, monkeypatch, tmp_path):
    from app.modules.m07_ingestion import job_service
    from app.modules.m07_ingestion.models.job import Job, JobStatus
    from app.modules.m07_ingestion.routes import jobs as jobs_routes
    monkeypatch.setattr(jobs_routes, "TEMP_DIR", tmp_path)

    async def no_queue(db_, job, actor=None):
        await db_.commit()
    monkeypatch.setattr(job_service, "enqueue_and_commit", no_queue)
    content = os.urandom(4096)
    files = {"file": ("drive.mp4", content, "video/mp4")}
    r1 = await api.post("/jobs/", files=files, params={"models": "pothole"})
    assert r1.status_code == 202, r1.text
    first = await db.get(Job, r1.json()["id"])
    first.status = JobStatus.done
    await db.commit()
    r2 = await api.post("/jobs/", files={"file": ("renamed.mp4", content, "video/mp4")})
    assert r2.status_code == 409
    detail = r2.json()["detail"]
    assert detail["code"] == "duplicate_video" and detail["job_id"] == first.id and detail["job_status"] == "done"
    assert detail["fingerprint"].startswith("sha256:")
    r3 = await api.post("/jobs/", files={"file": ("renamed.mp4", content, "video/mp4")}, params={"force": "true"})
    assert r3.status_code == 202
    assert (await api.post("/jobs/", files={"file": ("other.mp4", os.urandom(4096), "video/mp4")})).status_code == 202
