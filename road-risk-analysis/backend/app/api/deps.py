"""
Request authentication and authorisation.

Every request resolves to a Principal:
  - user       — `Authorization: Bearer <session token>` from /auth/login
  - client     — `X-API-Key: <key>` issued in Admin → API clients (mobile app,
                 simulator, integrations); limited to an explicit route list.
                 When the request ALSO carries a valid user Bearer token (a
                 signed-in mobile user), the client principal carries that
                 user/session too — still without the user's permissions, so
                 the key never reaches more than its route list.
  - anonymous  — nothing presented

`module_guard` protects a whole router: it maps the HTTP method to a
View/Add/Edit/Delete permission on one module, with per-route overrides for
POST endpoints that are really queries (/nearby, /along-route, …).
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.permissions import MODULES
from app.core.security import hash_token
from app.core.db.session import get_db
from app.modules.m12_integration.models.api_client import ApiClient
from app.modules.m01_security.models.auth_token import AuthSession, SessionStage
from app.modules.m01_security.models.user import User, UserStatus

_TOUCH_EVERY = timedelta(seconds=60)


def api_error(status: int, code: str, message: str, **extra) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message, **extra})


@dataclass
class Principal:
    kind: str                                   # user | client | anonymous
    id: str | None = None
    label: str | None = None
    user: User | None = None
    session: AuthSession | None = None
    permissions: dict[str, set[str]] = field(default_factory=dict)

    def can(self, module: str, action: str) -> bool:
        return action in self.permissions.get(module, set())


def effective_permissions(user: User) -> dict[str, set[str]]:
    perms: dict[str, set[str]] = {}
    for role in user.roles:
        if not role.is_active:
            continue
        if role.is_system:
            for m in MODULES:
                perms.setdefault(m.key, set()).update(m.actions)
            continue
        for p in role.permissions:
            granted = perms.setdefault(p.module, set())
            for action in ("view", "add", "edit", "delete"):
                if getattr(p, f"can_{action}"):
                    granted.add(action)
    return {k: v for k, v in perms.items() if v}


def bearer_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return None


async def load_session(db: AsyncSession, token: str, stages: Iterable[SessionStage]) -> AuthSession | None:
    row = (await db.execute(
        select(AuthSession).where(AuthSession.token_hash == hash_token(token))
    )).scalar_one_or_none()
    now = datetime.utcnow()
    if row is None or row.revoked_at is not None or row.expires_at <= now or row.stage not in set(stages):
        return None
    return row


async def _session_user(db: AsyncSession, token: str, now: datetime) -> tuple[User | None, AuthSession | None]:
    sess = await load_session(db, token, [SessionStage.full])
    if sess is None:
        return None, None
    user = await db.get(User, sess.user_id)
    if user is None or user.status != UserStatus.active:
        return None, None
    if sess.last_used_at is None or now - sess.last_used_at > _TOUCH_EVERY:
        sess.last_used_at = now
        await db.commit()
    return user, sess


async def get_principal(request: Request, db: AsyncSession = Depends(get_db)) -> Principal:
    cached = getattr(request.state, "principal", None)
    if cached is not None:
        return cached
    principal = Principal(kind="anonymous")
    now = datetime.utcnow()

    api_key = request.headers.get("x-api-key")
    token = bearer_token(request)
    if api_key:
        client = (await db.execute(
            select(ApiClient).where(ApiClient.key_hash == hash_token(api_key))
        )).scalar_one_or_none()
        if client is None or not client.is_active:
            raise api_error(401, "invalid_api_key", "Unknown or revoked API key.")
        if client.last_used_at is None or now - client.last_used_at > _TOUCH_EVERY * 5:
            client.last_used_at = now
            await db.commit()
        principal = Principal(kind="client", id=client.id, label=client.name)
        if token:
            # A stale token degrades to the plain client (anonymous app use
            # keeps working); /auth/me then answers 401 so the app signs out.
            user, sess = await _session_user(db, token, now)
            if user is not None:
                principal.user, principal.session = user, sess
    elif token:
        user, sess = await _session_user(db, token, now)
        if user is None:
            raise api_error(401, "session_expired", "Your session has expired. Please sign in again.")
        principal = Principal(
            kind="user", id=user.id, label=user.email, user=user, session=sess,
            permissions=effective_permissions(user),
        )
    request.state.principal = principal
    return principal


async def current_user(principal: Principal = Depends(get_principal)) -> Principal:
    """A signed-in user, regardless of AUTH_ENFORCED (profile, admin pages)."""
    if principal.kind != "user":
        raise api_error(401, "not_authenticated", "Please sign in.")
    return principal


current_user.openapi_access = {"kind": "user"}  # type: ignore[attr-defined]


async def account_user(principal: Principal = Depends(get_principal)) -> Principal:
    """A signed-in user, either directly (Bearer) or through an API client
    that also presents the user's Bearer token (the mobile app). For the
    user's own account only (/auth/me, /auth/logout) — never permissions."""
    if principal.user is None or principal.session is None:
        raise api_error(401, "not_authenticated", "Please sign in.")
    return principal


account_user.openapi_access = {"kind": "account"}  # type: ignore[attr-defined]


async def api_client(request: Request, db: AsyncSession = Depends(get_db)) -> Principal:
    """The call must come from a registered API client (the mobile app)."""
    if not request.headers.get("x-api-key"):
        raise api_error(401, "api_key_required", "This endpoint requires an API key.")
    return await get_principal(request, db)


api_client.openapi_access = {"kind": "client"}  # type: ignore[attr-defined]


def client_route_guard(client_routes: Iterable[tuple[str, str]]):
    """
    Router guard for routers that are otherwise public or check the user
    themselves (/auth): a request presenting an API key may only reach the
    (METHOD, path) pairs listed. Requests without a key are left alone.
    """
    client_routes = set(client_routes)

    async def guard(request: Request, db: AsyncSession = Depends(get_db)) -> None:
        if not request.headers.get("x-api-key"):
            return
        await get_principal(request, db)   # validates the key
        if (request.method.upper(), route_template(request)) not in client_routes:
            raise api_error(403, "forbidden", "This API key is not allowed to call this endpoint.")

    guard.openapi_access = {"kind": "client_routes", "client_routes": client_routes}  # type: ignore[attr-defined]
    return Depends(guard)


def require(module: str, action: str):
    async def dep(principal: Principal = Depends(current_user)) -> Principal:
        if not principal.can(module, action):
            raise api_error(403, "forbidden", "You do not have permission for this action.",
                            module=module, action=action)
        return principal
    dep.openapi_access = {"kind": "permission", "module": module, "action": action}  # type: ignore[attr-defined]
    return dep


def route_template(request: Request) -> str:
    """
    Full route template of the request, e.g. "/detections/{detection_id}/review".
    Recent FastAPI versions store the template WITHOUT the include_router
    prefix on scope["route"], older ones with it; the prefix is rebuilt from
    the actual path so both give the same key.
    """
    route = request.scope.get("route")
    template = getattr(route, "path", None)
    if template is None:
        return request.url.path
    tmpl_parts = [p for p in template.strip("/").split("/") if p]
    actual_parts = [p for p in request.url.path.strip("/").split("/") if p]
    prefix = actual_parts[:max(len(actual_parts) - len(tmpl_parts), 0)]
    return ("/" + "/".join(prefix) if prefix else "") + template


_METHOD_ACTION = {"GET": "view", "HEAD": "view", "POST": "add", "PUT": "edit", "PATCH": "edit", "DELETE": "delete"}


def module_guard(
    module: str,
    *,
    read_posts: Iterable[str] = (),
    overrides: dict[tuple[str, str], tuple[str, str]] | None = None,
    client_routes: Iterable[tuple[str, str]] = (),
):
    """
    Router-level guard. Paths are full route templates, e.g.
    "/detections/{detection_id}/review".
      read_posts    POST routes that only read data → "view"
      overrides     (METHOD, path) → (module, action) for routes that belong
                    to another module (e.g. review → validations/edit)
      client_routes (METHOD, path) reachable with an API key (mobile app)
    """
    read_posts = set(read_posts)
    overrides = overrides or {}
    client_routes = set(client_routes)

    def resolve(method: str, path: str) -> tuple[str, str]:
        if (method, path) in overrides:
            return overrides[(method, path)]
        return module, "view" if (method == "POST" and path in read_posts) else _METHOD_ACTION.get(method, "view")

    async def guard(request: Request, principal: Principal = Depends(get_principal)) -> None:
        path = route_template(request)
        method = request.method.upper()
        mod, action = resolve(method, path)

        if principal.kind == "client":
            if (method, path) in client_routes:
                return
            raise api_error(403, "forbidden", "This API key is not allowed to call this endpoint.")
        if not settings.AUTH_ENFORCED:
            return
        if principal.kind == "anonymous":
            raise api_error(401, "not_authenticated", "Please sign in.")
        if not principal.can(mod, action):
            raise api_error(403, "forbidden", "You do not have permission for this action.",
                            module=mod, action=action)

    # Read by app.api.openapi to document each operation's credentials and
    # permission from this very table.
    guard.openapi_access = {"kind": "module", "resolve": resolve,  # type: ignore[attr-defined]
                            "client_routes": client_routes}
    return Depends(guard)


def actor_name(principal: Principal, fallback: str | None = None) -> str | None:
    """Identity to stamp on reviewed_by / moderated_by: the signed-in user
    wins over whatever free text the client sent."""
    if principal.user is not None:
        return principal.user.email
    return fallback
