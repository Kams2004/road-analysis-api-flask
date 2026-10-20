"""
Create (or re-key) a named API client from a key given in the environment.

Used by deploy/server-v2/deploy.sh so the demo simulator can authenticate on a
stack where AUTH_ENFORCED is on, without anyone copying a key from the console:

    API_CLIENT_NAME=simulator API_CLIENT_KEY=vsk_… python -m app.demo.ensure_api_client

Idempotent: an existing client with that name gets the key's hash (and is
re-activated); the clear key is never printed.
"""
import asyncio
import os
from datetime import datetime

from sqlalchemy import select

from app.core.security import hash_token
from app.db.session import AsyncSessionLocal
from app.models.api_client import ApiClient


async def main() -> None:
    name = os.environ["API_CLIENT_NAME"]
    key = os.environ["API_CLIENT_KEY"]
    if not key.startswith("vsk_") or len(key) < 24:
        raise SystemExit("API_CLIENT_KEY must look like vsk_… (at least 24 characters)")
    async with AsyncSessionLocal() as db:
        client = (await db.execute(select(ApiClient).where(ApiClient.name == name))).scalars().first()
        if client is None:
            client = ApiClient(name=name, description="created by deploy/server-v2", key_prefix=key[:10],
                               key_hash=hash_token(key), created_by="deploy")
            db.add(client)
            action = "created"
        else:
            client.key_prefix, client.key_hash = key[:10], hash_token(key)
            client.is_active, client.revoked_at, client.rotated_at = True, None, datetime.utcnow()
            action = "re-keyed"
        await db.commit()
    print(f"API client '{name}' {action} (prefix {key[:10]})")


if __name__ == "__main__":
    asyncio.run(main())
