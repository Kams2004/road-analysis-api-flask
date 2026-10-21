"""
Admin command line.

    python -m app.cli create-admin --email admin@camtrack.net --name "Jane Doe"
    python -m app.cli create-api-client --name "VIASafe mobile"
    python -m app.cli rebuild-hazards
    python -m app.cli recluster-detections [--dry-run] [--verbose]
    python -m app.cli geocode-backfill --limit 500

create-admin prompts for the password (or reads ADMIN_PASSWORD) and adds the
Administrator role to an existing user with that e-mail if there is one.
rebuild-hazards re-clusters every accepted/resolved detection into hazards
(after changing cluster radii, or to repair hazard aggregates).
recluster-detections migrates existing data to cluster-first hazards
(docs/CLUSTERS.md): merges duplicate hazards, attaches every located
detection to its cluster, auto-validates re-sightings of confirmed hazards;
short transactions and no DDL, so it is safe while jobs are processing.
geocode-backfill reverse-geocodes (street, quartier, town …) located
detections that have no structured address yet, ≤ 1 Nominatim request/s.
"""
import argparse
import asyncio
import getpass
import os
import sys
from datetime import datetime

from sqlalchemy import func, select

from app.core.security import hash_password, hash_token, new_token, password_problems
from app.core.db.session import AsyncSessionLocal, init_db
from app.modules.m12_integration.models.api_client import ApiClient
from app.modules.m01_security.models.user import User, UserStatus
from app.modules.m01_security.auth_service import admin_role, seed_roles


async def create_admin(email: str, name: str) -> None:
    password = os.environ.get("ADMIN_PASSWORD") or getpass.getpass("Password: ")
    problems = password_problems(password, 8)
    if problems:
        sys.exit("Password must contain " + ", ".join(problems))
    await init_db()
    async with AsyncSessionLocal() as db:
        await seed_roles(db)
        role = await admin_role(db)
        user = (await db.execute(select(User).where(func.lower(User.email) == email.lower()))).scalar_one_or_none()
        if user is None:
            user = User(email=email.lower(), full_name=name)
            db.add(user)
        user.password_hash = hash_password(password)
        user.password_changed_at = datetime.utcnow()
        user.status = UserStatus.active
        user.locked_until = None
        if role and role not in user.roles:
            user.roles = [*user.roles, role]
        await db.commit()
        print(f"Administrator ready: {user.email}")


async def create_api_client(name: str) -> None:
    await init_db()
    key = new_token("vsk_")
    async with AsyncSessionLocal() as db:
        db.add(ApiClient(name=name, key_prefix=key[:10], key_hash=hash_token(key), created_by="cli"))
        await db.commit()
    print(f"API client '{name}' created. Key (shown once):\n{key}")


async def rebuild_hazards() -> None:
    from app.modules.m06_hazards.hazard_service import rebuild_all
    await init_db()
    async with AsyncSessionLocal() as db:
        count = await rebuild_all(db)
    print(f"Hazards rebuilt: {count}")


async def recluster_detections(dry_run: bool, verbose: bool) -> None:
    import json
    from sqlalchemy import text
    from app.modules.m06_hazards import recluster
    from app.modules.m06_hazards.rejection_seeds import seed_rejection_reasons
    # No init_db(): it may ALTER tables, which must not run while a worker is
    # processing; the only object this needs is the candidate sequence.
    from app.core.db.registry import import_all_models
    import_all_models()
    async with AsyncSessionLocal() as db:
        await db.execute(text("CREATE SEQUENCE IF NOT EXISTS hazard_candidates_seq"))
        await db.commit()
        await seed_rejection_reasons(db)              # not_seen_again
        res = await recluster.run(db, dry_run=dry_run, verbose=verbose)
    print(("DRY RUN — nothing written. " if dry_run else "") +
          f"merged hazards: {res['merges']}, detections attached: {res['attached']} "
          f"(new candidate clusters: {res['candidates_created']}), re-sightings auto-validated: "
          f"{res['auto_validated']}, rejected detections detached: {res['detached']}")
    print("before:", json.dumps(res["before"], sort_keys=True))
    print("after: ", json.dumps(res["after"], sort_keys=True))


async def geocode_backfill(limit: int) -> None:
    from app.modules.m03_maps import geocoding_backfill
    await init_db()
    async with AsyncSessionLocal() as db:
        before = await geocoding_backfill.pending_count(db)
        st = await geocoding_backfill.run(db, limit)
        after = await geocoding_backfill.pending_count(db)
    print(f"Geocoding back-fill: {st.processed} looked up, {st.updated} updated, {st.failed} failed, "
          f"{st.hazards_updated} hazard(s) refreshed ({st.stopped_reason}); pending {before} → {after}")


async def _training_counts(db) -> dict:
    from sqlalchemy import func, select
    from app.modules.m06_hazards.models.training_sample import TrainingFrame, TrainingSample
    out: dict = {}
    for t, cls, n in (await db.execute(select(TrainingSample.detection_type, TrainingSample.classifier_class,
                                              func.count()).where(TrainingSample.state == "active",
                                                                  TrainingSample.crop_key.is_not(None))
                                       .group_by(TrainingSample.detection_type, TrainingSample.classifier_class)
                                       )).all():
        out.setdefault(t, {}).setdefault("crops", {})[cls] = n
    for t, n in (await db.execute(select(TrainingSample.detection_type, func.count()).where(
            TrainingSample.state == "active", TrainingSample.excluded.is_(True))
            .group_by(TrainingSample.detection_type))).all():
        out.setdefault(t, {})["excluded"] = n
    positive = TrainingFrame.n_boxes > 0
    for t, n_boxes, n in (await db.execute(select(TrainingFrame.detection_type, positive, func.count())
                                           .where(TrainingFrame.status == "active")
                                           .group_by(TrainingFrame.detection_type, positive))).all():
        out.setdefault(t, {})["positive_frames" if n_boxes else "background_frames"] = n
    return out


async def resync_training_rejections(only_unclassified: bool) -> None:
    """Re-file the training samples of rejected detections by reason kind and
    write the bucket objects (docs/TRAINING-DATA.md). Idempotent."""
    import json
    from app.modules.m06_hazards import training_data
    from app.modules.m06_hazards.training_data import sync
    await init_db()
    from app.modules.m06_hazards.rejection_seeds import seed_rejection_reasons
    async with AsyncSessionLocal() as db:
        await seed_rejection_reasons(db)              # built-in reasons and kinds up to date
        before = await _training_counts(db)
        res = await training_data.resync_rejections(db, only_unclassified=only_unclassified)
        synced = failed = 0
        while True:
            st = await sync.sync_dirty(db, limit=500)
            synced, failed = synced + st["synced"], failed + st["failed"]
            if not st["frames"] or st["synced"] == 0:
                break
        after = await _training_counts(db)
    print(f"Rejected samples checked: {res['checked']}, re-filed: {res['changed']}; "
          f"frames synced: {synced}, failed: {failed}")
    print("before:", json.dumps(before, sort_keys=True))
    print("after: ", json.dumps(after, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("create-admin")
    a.add_argument("--email", required=True)
    a.add_argument("--name", default="Administrator")
    c = sub.add_parser("create-api-client")
    c.add_argument("--name", required=True)
    sub.add_parser("rebuild-hazards")
    rc = sub.add_parser("recluster-detections",
                        help="attach detections to clusters, merge duplicate hazards (docs/CLUSTERS.md)")
    rc.add_argument("--dry-run", action="store_true")
    rc.add_argument("--verbose", action="store_true")
    g = sub.add_parser("geocode-backfill")
    g.add_argument("--limit", type=int, default=500)
    r = sub.add_parser("resync-training-rejections",
                       help="re-file rejected training samples by reason kind (idempotent)")
    r.add_argument("--only-unclassified", action="store_true")
    args = parser.parse_args()
    if args.cmd == "create-admin":
        asyncio.run(create_admin(args.email, args.name))
    elif args.cmd == "rebuild-hazards":
        asyncio.run(rebuild_hazards())
    elif args.cmd == "recluster-detections":
        asyncio.run(recluster_detections(args.dry_run, args.verbose))
    elif args.cmd == "resync-training-rejections":
        asyncio.run(resync_training_rejections(args.only_unclassified))
    elif args.cmd == "geocode-backfill":
        asyncio.run(geocode_backfill(args.limit))
    else:
        asyncio.run(create_api_client(args.name))


if __name__ == "__main__":
    main()
