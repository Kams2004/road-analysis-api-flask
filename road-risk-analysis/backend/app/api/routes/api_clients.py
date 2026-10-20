"""
API clients — keys for the mobile app, simulator and integrations
(RRAP-12-015). The clear key is returned once, at creation or rotation.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, api_error, require
from app.api.openapi import err, errors
from app.core.security import hash_token, new_token
from app.db.session import get_db
from app.models.api_client import ApiClient
from app.schemas.auth import ApiClientCreateIn, ApiClientOut, ApiClientSecretOut
from app.services import audit

router = APIRouter()

_E_404 = errors(err(404, "not_found", "API client not found."))


def _new_key() -> tuple[str, str, str]:
    key = new_token("vsk_")
    return key, key[:10], hash_token(key)


async def _get(db: AsyncSession, client_id: str) -> ApiClient:
    client = await db.get(ApiClient, client_id)
    if client is None:
        raise api_error(404, "not_found", "API client not found.")
    return client


@router.get("/", response_model=list[ApiClientOut], summary="List API clients")
async def list_clients(db: AsyncSession = Depends(get_db),
                       _: Principal = Depends(require("api_clients", "view"))):
    """Every API client, newest first, with its key prefix and last use. Keys are never returned."""
    return (await db.execute(select(ApiClient).order_by(ApiClient.created_at.desc()))).scalars().all()


@router.post("/", response_model=ApiClientSecretOut, status_code=201, summary="Create an API client")
async def create_client(body: ApiClientCreateIn, request: Request, db: AsyncSession = Depends(get_db),
                        principal: Principal = Depends(require("api_clients", "add"))):
    """Issues a new key (`vsk_…`). The clear key is in `api_key` of this answer only — it is stored
    hashed. The key reaches only the API-key allow-list of endpoints. Audited as `api_client.create`."""
    key, prefix, digest = _new_key()
    client = ApiClient(name=body.name, description=body.description, key_prefix=prefix, key_hash=digest,
                       created_by=principal.label)
    db.add(client)
    await db.flush()
    audit.record(db, request, "api_client.create", principal=principal, object_type="api_client",
                 object_id=client.id, details={"name": client.name})
    await db.commit()
    return ApiClientSecretOut(**ApiClientOut.model_validate(client).model_dump(), api_key=key)


@router.post("/{client_id}/rotate", response_model=ApiClientSecretOut, summary="Rotate an API client's key",
             responses=_E_404)
async def rotate_client(client_id: str, request: Request, db: AsyncSession = Depends(get_db),
                        principal: Principal = Depends(require("api_clients", "edit"))):
    """Replaces the key (the old one stops working immediately) and re-activates a revoked client.
    The new clear key is returned once. Audited as `api_client.rotate`."""
    client = await _get(db, client_id)
    key, prefix, digest = _new_key()
    client.key_prefix, client.key_hash = prefix, digest
    client.rotated_at = datetime.utcnow()
    client.is_active, client.revoked_at = True, None
    audit.record(db, request, "api_client.rotate", principal=principal, object_type="api_client",
                 object_id=client.id, details={"name": client.name})
    await db.commit()
    return ApiClientSecretOut(**ApiClientOut.model_validate(client).model_dump(), api_key=key)


@router.post("/{client_id}/revoke", response_model=ApiClientOut, summary="Revoke an API client",
             responses=_E_404)
async def revoke_client(client_id: str, request: Request, db: AsyncSession = Depends(get_db),
                        principal: Principal = Depends(require("api_clients", "edit"))):
    """Disables the key (`401 invalid_api_key` from then on) while keeping the client for the record;
    rotating re-activates it. Audited as `api_client.revoke`."""
    client = await _get(db, client_id)
    client.is_active = False
    client.revoked_at = datetime.utcnow()
    audit.record(db, request, "api_client.revoke", principal=principal, object_type="api_client",
                 object_id=client.id, details={"name": client.name})
    await db.commit()
    return client


@router.delete("/{client_id}", status_code=204, summary="Delete an API client", responses=_E_404)
async def delete_client(client_id: str, request: Request, db: AsyncSession = Depends(get_db),
                        principal: Principal = Depends(require("api_clients", "delete"))):
    """Deletes the client and its key permanently. Audited as `api_client.delete`."""
    client = await _get(db, client_id)
    name = client.name
    await db.delete(client)
    audit.record(db, request, "api_client.delete", principal=principal, object_type="api_client",
                 object_id=client_id, details={"name": name})
    await db.commit()
