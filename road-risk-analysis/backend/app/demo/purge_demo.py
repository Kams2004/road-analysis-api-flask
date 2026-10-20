"""
Remove ALL demo / seeded data in one go (switch to the TEST PHASE), keeping
every real row. Dry-run by default (prints what would go, per kind); pass
--yes to delete.

    docker exec -w /app -e PYTHONPATH=/app roaddemo_api_1 python -m app.demo.purge_demo          # dry run
    docker exec -w /app -e PYTHONPATH=/app roaddemo_api_1 python -m app.demo.purge_demo --yes    # delete
    … --revoke-simulator   also deactivate the API client named "simulator" (kept, not deleted)

Demo rows are identified ONLY by these tags / prefixes (nothing else is touched):

  seed_web_test.py      (calls seed_web_test.purge, tester included = --purge-users)
    * ids ending "-5eed7e575eed": jobs, signalements, cloud connections,
      cloud import batches / items, driver users chauffeur{1,2,3}.webtest@…;
      detections of those jobs; their hazards (deleted when every member is
      seeded, recomputed otherwise), hazard events / votes, reviews,
      validation labels, audit rows, job events / track points;
    * cluster zones with created_by = 'seed-web-test' (other zones kept);
    * the console user testeur@viasafe.local;
    * MinIO detections/<seeded job id>/…, signalements/seed-web-test/….
  seed_presentation.py  (calls seed_presentation.purge)
    * ids ending "-9e5e47a7104e": jobs, detections, signalements (+ hazards
      etc. as above); historical_events.source_event_id LIKE 'seed-presentation-%'
      (the presentation risk zones); MinIO detections/<job>/…, signalements/seed-presentation/….
  seed_mobile_demo.py   (calls seed_mobile_demo.clean)
    * jobs.submitted_by = 'demo-seed' (+ their detections, hazards, events),
      signalements.reported_by = 'demo-seed',
      historical_events.source_event_id LIKE 'demo-seed-mobile-%';
      MinIO detections/<job>/…, signalements/demo-seed/….
  seed_demo_exceptions.py
    * historical_events with source = 'manual' and source_event_id starting
      with one of seed_demo_exceptions.PURGE_PREFIXES
      ('demo-tier-', 'demo-n5-', 'demo-route-', 'demo-douala_').
  vehicle_simulator.py / sim_presentation.py
    * vehicles with id LIKE 'sim-%' (+ their vehicle_road_states) and the
      Redis live keys of those ids (vehicles:live:geo members, *:sim-* keys).
  dataset_samples that reference a validation label of a demo detection
    (FK — removed first so the labels can go).

Not removed: the 'simulator' API client (only revoked with --revoke-simulator),
vehicles registered by alert_demo_vehicle.py / presentation_probe.py (plain
uuids, indistinguishable from phones), anything else.

The seed scripts keep working: re-run them later to go back to demo mode.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime

from sqlalchemy import delete, func, or_, select, text

from app.demo import seed_web_test as wt   # registers every mapper
from app.demo import seed_presentation as sp
from app.demo import seed_mobile_demo as md
from app.demo import seed_demo_exceptions as ex
from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.api_client import ApiClient
from app.models.cloud_connection import CloudConnection
from app.models.cloud_import import CloudImportBatch
from app.models.cluster_zone import ClusterZone
from app.models.detection import Detection
from app.models.hazard import Hazard
from app.models.historical_event import HistoricalEventRecord
from app.models.job import Job
from app.models.signalement import Signalement
from app.models.user import User
from app.models.validation_label import ValidationLabel
from app.models.vehicle import Vehicle
from app.models.vehicle_road_state import VehicleRoadState
from app.services.minio_service import get_minio
from app.services.redis_client import get_redis

SIM_PREFIX = "sim-"


def _like_prefix(col, prefix: str):
    esc = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return col.like(esc + "%", escape="\\")


def exception_filter():
    return (HistoricalEventRecord.source == "manual") & or_(
        *[_like_prefix(HistoricalEventRecord.source_event_id, p) for p in ex.PURGE_PREFIXES])


async def _ids(db, stmt) -> list:
    return list((await db.execute(stmt)).scalars().all())


async def _count(db, model, cond) -> int:
    return (await db.execute(select(func.count()).select_from(model).where(cond))).scalar() or 0


async def demo_sets(db) -> dict:
    """Ids of every tagged demo row, per seeder."""
    web_like, pres_like = f"%-{wt.SUFFIX}", f"%-{sp.SUFFIX}"
    jobs = {
        "web_test": await _ids(db, select(Job.id).where(Job.id.like(web_like))),
        "presentation": await _ids(db, select(Job.id).where(Job.id.like(pres_like))),
        "mobile": await _ids(db, select(Job.id).where(Job.submitted_by == md.TAG)),
    }
    all_jobs = [j for v in jobs.values() for j in v]
    dets = await _ids(db, select(Detection.id).where(or_(
        Detection.job_id.in_(all_jobs or ["-"]), Detection.id.like(pres_like))))
    sigs = await _ids(db, select(Signalement.id).where(or_(
        Signalement.id.like(web_like), Signalement.id.like(pres_like), Signalement.reported_by == md.TAG)))
    return {"jobs": jobs, "all_jobs": all_jobs, "dets": dets, "sigs": sigs}


async def counts(db, revoke_sim: bool) -> dict:
    s = await demo_sets(db)
    dets = s["dets"]
    hz_ids = set(await _ids(db, select(Detection.hazard_id).where(
        Detection.id.in_(dets or ["-"]), Detection.hazard_id.isnot(None))))
    hz_full = 0
    for hid in hz_ids:
        others = await _count(db, Detection, (Detection.hazard_id == hid) & Detection.id.notin_(dets or ["-"]))
        hz_full += 0 if others else 1
    web_like = f"%-{wt.SUFFIX}"
    users = await _count(db, User, or_(User.id.like(web_like), User.email == wt.TESTER_EMAIL))
    label_ids = await _ids(db, select(ValidationLabel.id).where(ValidationLabel.detection_id.in_(dets or ["-"])))
    out = {
        "jobs (web test)": len(s["jobs"]["web_test"]),
        "jobs (presentation)": len(s["jobs"]["presentation"]),
        "jobs (mobile demo)": len(s["jobs"]["mobile"]),
        "detections": len(dets),
        "hazards deleted (only demo members)": hz_full,
        "hazards recomputed (mixed)": len(hz_ids) - hz_full,
        "signalements": len(s["sigs"]),
        "validation labels": len(label_ids),
        "dataset samples on demo labels": await _dataset_samples(db, label_ids, count_only=True),
        "users (tester + drivers)": users,
        "cluster zones (seed-web-test)": await _count(db, ClusterZone, ClusterZone.created_by == wt.TAG),
        "cloud connections": await _count(db, CloudConnection, CloudConnection.id.like(web_like)),
        "cloud import batches": await _count(db, CloudImportBatch, CloudImportBatch.id.like(web_like)),
        "historical events (presentation)": await _count(
            db, HistoricalEventRecord, _like_prefix(HistoricalEventRecord.source_event_id, sp.HIST_PREFIX)),
        "historical events (mobile demo)": await _count(
            db, HistoricalEventRecord, _like_prefix(HistoricalEventRecord.source_event_id, md.HIST_PREFIX)),
        "historical events (demo exceptions)": await _count(db, HistoricalEventRecord, exception_filter()),
        "simulator vehicles (sim-*)": await _count(db, Vehicle, Vehicle.id.like(f"{SIM_PREFIX}%")),
        "simulator redis keys": len(await _sim_redis_keys()),
        "minio objects": _minio_count(s["all_jobs"]),
    }
    if revoke_sim:
        out["api client 'simulator' to revoke"] = await _count(
            db, ApiClient, (ApiClient.name == "simulator") & ApiClient.is_active.is_(True))
    return out


async def _dataset_samples(db, label_ids: list, count_only: bool) -> int:
    """dataset_samples reference validation_labels without ON DELETE — demo ones go first."""
    if not label_ids:
        return 0
    exists = (await db.execute(text("select to_regclass('public.dataset_samples') is not null"))).scalar()
    if not exists:
        return 0
    params = {"ids": list(label_ids)}
    if count_only:
        return (await db.execute(text(
            "select count(*) from dataset_samples where validation_label_id = any(:ids)"), params)).scalar() or 0
    return (await db.execute(text(
        "delete from dataset_samples where validation_label_id = any(:ids)"), params)).rowcount or 0


def _minio_count(job_ids: list) -> int:
    try:
        client = get_minio()
        n = 0
        for jid in job_ids:
            n += sum(1 for _ in client.list_objects(settings.MINIO_BUCKET_DETECTIONS, prefix=f"{jid}/", recursive=True))
        for tag in (wt.TAG, sp.TAG, md.TAG):
            n += sum(1 for _ in client.list_objects(settings.MINIO_BUCKET_SIGNALEMENTS, prefix=f"{tag}/", recursive=True))
        return n
    except Exception as exc:
        print(f"  ! MinIO not reachable: {exc}")
        return -1


async def _sim_redis_keys() -> list:
    try:
        r = get_redis()
        return [k async for k in r.scan_iter(match=f"*:{SIM_PREFIX}*", count=500)]
    except Exception as exc:
        print(f"  ! Redis not reachable: {exc}")
        return []


async def purge_simulator(db) -> dict:
    ids = await _ids(db, select(Vehicle.id).where(Vehicle.id.like(f"{SIM_PREFIX}%")))
    await db.execute(delete(VehicleRoadState).where(VehicleRoadState.vehicle_id.in_(ids or ["-"])))
    n = (await db.execute(delete(Vehicle).where(Vehicle.id.in_(ids or ["-"])))).rowcount
    await db.commit()
    keys = await _sim_redis_keys()
    geo = 0
    try:
        r = get_redis()
        if keys:
            await r.delete(*keys)
        from app.services.vehicle_service import GEO_KEY
        members = [m async for m in r.zscan_iter(GEO_KEY) if str(m[0]).startswith(SIM_PREFIX)]
        if members:
            geo = await r.zrem(GEO_KEY, *[m[0] for m in members])
    except Exception as exc:
        print(f"  ! Redis cleanup failed: {exc}")
    return {"vehicles": n, "redis_keys": len(keys), "geo_members": geo}


async def run(yes: bool, revoke_sim: bool) -> None:
    async with AsyncSessionLocal() as db:
        before = await counts(db, revoke_sim)
        width = max(len(k) for k in before)
        print("Demo data found (tagged rows only):" if yes else "DRY RUN — would remove (tagged rows only):")
        for k, v in before.items():
            print(f"  {k:<{width}}  {v}")
        if not yes:
            print("\nNothing deleted. Re-run with --yes to purge.")
            return
        if not any(v > 0 for v in before.values()):
            print("\nNothing to purge.")
            return

        print("\n▶ purging …")
        s = await demo_sets(db)
        labels = await _ids(db, select(ValidationLabel.id).where(ValidationLabel.detection_id.in_(s["dets"] or ["-"])))
        n = await _dataset_samples(db, labels, count_only=False)
        await db.commit()
        print(f"[dataset samples] {n}")
        print("[web test]", end=" ")
        await wt.purge(db, drop_tester=True)
        print("[presentation]", end=" ")
        await sp.purge(db)
        print("[mobile demo]", end=" ")
        await md.clean(db)
        n = (await db.execute(delete(HistoricalEventRecord).where(exception_filter()))).rowcount
        await db.commit()
        print(f"[demo exceptions] historical events: {n}")
        print(f"[simulator] {await purge_simulator(db)}")
        if revoke_sim:
            client = (await db.execute(select(ApiClient).where(ApiClient.name == "simulator"))).scalars().first()
            if client is not None and client.is_active:
                client.is_active, client.revoked_at = False, datetime.utcnow()
                await db.commit()
                print("[api client] 'simulator' revoked")

    async with AsyncSessionLocal() as db:
        after = await counts(db, revoke_sim)
        left = {k: v for k, v in after.items() if v > 0}
        print("\n✔ purge done." + (f" Still present: {left}" if left else " No tagged demo row left."))


def main() -> None:
    ap = argparse.ArgumentParser(description="Remove all demo / seeded data (see module docstring).")
    ap.add_argument("--yes", action="store_true", help="really delete (default: dry run)")
    ap.add_argument("--revoke-simulator", action="store_true",
                    help="also deactivate the 'simulator' API client (it is kept)")
    args = ap.parse_args()
    asyncio.run(run(args.yes, args.revoke_simulator))


if __name__ == "__main__":
    main()
