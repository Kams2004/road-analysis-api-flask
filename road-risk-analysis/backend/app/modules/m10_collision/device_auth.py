"""
Per-device ownership of a live vehicle.

The mobile app's API key ships inside the app, so anyone can extract it. The key alone must not let
someone send positions as another driver's vehicle (fake vehicles → false collision alerts), read its
alerts or follow its predicted path. When a vehicle is created, POST /vehicles/register answers a
`device_secret` once; the app keeps it (secure storage) and sends it as `X-Device-Secret`.

With an API key (principal kind `client`) every call about a vehicle that has a secret must carry it.
Signed-in console users (permissions) and vehicles registered before secrets existed (null hash —
claimed at their next registration) are not checked.
"""
import hmac
import secrets
from typing import Optional

from fastapi import Depends, Path, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, api_error, get_principal
from app.core.db.session import get_db
from app.core.security import hash_token
from app.modules.m10_collision.models.vehicle import Vehicle

HEADER = "x-device-secret"


def new_secret() -> tuple[str, str]:
    """(secret, its hash)."""
    s = secrets.token_urlsafe(32)
    return s, hash_token(s)


def secret_matches(vehicle: Vehicle, provided: Optional[str]) -> bool:
    if not vehicle.device_secret_hash:
        return True
    return bool(provided) and hmac.compare_digest(hash_token(provided), vehicle.device_secret_hash)


async def check_owner(db: AsyncSession, principal: Principal, vehicle_id: str, headers) -> None:
    """403 `device_secret_invalid` when an API-key call about `vehicle_id` lacks its device secret.
    An unknown vehicle passes (the route answers its own 404)."""
    if principal.kind != "client":
        return
    vehicle = await db.get(Vehicle, vehicle_id)
    if vehicle is not None and not secret_matches(vehicle, headers.get(HEADER)):
        raise api_error(403, "device_secret_invalid",
                        "This vehicle belongs to another device (missing or wrong X-Device-Secret).")


async def vehicle_owner(
    request: Request,
    vehicle_id: str = Path(..., description="The vehicle."),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
) -> None:
    """Route dependency: the caller owns `{vehicle_id}` (see check_owner)."""
    await check_owner(db, principal, vehicle_id, request.headers)
