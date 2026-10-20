"""
Admin command line.

    python -m app.cli create-admin --email admin@camtrack.net --name "Jane Doe"
    python -m app.cli create-api-client --name "VIASafe mobile"
    python -m app.cli rebuild-hazards
    python -m app.cli geocode-backfill --limit 500

create-admin prompts for the password (or reads ADMIN_PASSWORD) and adds the
Administrator role to an existing user with that e-mail if there is one.
rebuild-hazards re-clusters every accepted/resolved detection into hazards
(after changing cluster radii, or to repair hazard aggregates).
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
from app.db.session import AsyncSessionLocal, init_db
from app.models.api_client import ApiClient
from app.models.user import User, UserStatus
from app.services.auth_service import admin_role, seed_roles


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
    from app.services.hazard_service import rebuild_all
    await init_db()
    async with AsyncSessionLocal() as db:
        count = await rebuild_all(db)
    print(f"Hazards rebuilt: {count}")


async def geocode_backfill(limit: int) -> None:
    from app.services import geocoding_backfill
    await init_db()
    async with AsyncSessionLocal() as db:
        before = await geocoding_backfill.pending_count(db)
        st = await geocoding_backfill.run(db, limit)
        after = await geocoding_backfill.pending_count(db)
    print(f"Geocoding back-fill: {st.processed} looked up, {st.updated} updated, {st.failed} failed, "
          f"{st.hazards_updated} hazard(s) refreshed ({st.stopped_reason}); pending {before} → {after}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("create-admin")
    a.add_argument("--email", required=True)
    a.add_argument("--name", default="Administrator")
    c = sub.add_parser("create-api-client")
    c.add_argument("--name", required=True)
    sub.add_parser("rebuild-hazards")
    g = sub.add_parser("geocode-backfill")
    g.add_argument("--limit", type=int, default=500)
    args = parser.parse_args()
    if args.cmd == "create-admin":
        asyncio.run(create_admin(args.email, args.name))
    elif args.cmd == "rebuild-hazards":
        asyncio.run(rebuild_hazards())
    elif args.cmd == "geocode-backfill":
        asyncio.run(geocode_backfill(args.limit))
    else:
        asyncio.run(create_api_client(args.name))


if __name__ == "__main__":
    main()
