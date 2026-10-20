"""
Helpers to append to the audit trail. `record` adds to the caller's session
(committed with the change it describes); `record_now` commits on its own
session, for events that must survive a failing request (e.g. bad logins).
"""
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.models.audit_log import AuditLog


def client_ip(request: Request | None) -> str | None:
    if request is None:
        return None
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def _build(request: Request | None, action: str, *, principal=None, outcome: str = "success",
           object_type: str | None = None, object_id: str | None = None,
           details: dict[str, Any] | None = None, actor_label: str | None = None) -> AuditLog:
    if principal is not None and principal.user is not None:
        # signed-in user, directly or through the mobile app's API key
        actor_type, actor_id, label = "user", principal.user.id, principal.user.email
    elif principal is not None:
        actor_type, actor_id, label = principal.kind, principal.id, principal.label
    else:
        actor_type, actor_id, label = "anonymous", None, None
    return AuditLog(
        actor_type=actor_type,
        actor_id=actor_id,
        actor_label=actor_label or label,
        action=action,
        outcome=outcome,
        object_type=object_type,
        object_id=object_id,
        details=details,
        ip=client_ip(request),
        user_agent=(request.headers.get("user-agent", "")[:300] if request else None),
    )


def record(db: AsyncSession, request: Request | None, action: str, **kw) -> None:
    db.add(_build(request, action, **kw))


async def record_now(request: Request | None, action: str, **kw) -> None:
    async with AsyncSessionLocal() as db:
        db.add(_build(request, action, **kw))
        await db.commit()


def diff(before: dict, after: dict) -> dict:
    """Only the fields that changed, as {field: [old, new]}."""
    return {k: [before.get(k), after.get(k)] for k in sorted(set(before) | set(after)) if before.get(k) != after.get(k)}


def record_system(db: AsyncSession, action: str, *, outcome: str = "success",
                  object_type: str | None = None, object_id: str | None = None,
                  details: dict[str, Any] | None = None) -> None:
    """Entry for an automatic decision (video processor rules, back-fills)."""
    db.add(AuditLog(actor_type="system", actor_label="system", action=action, outcome=outcome,
                    object_type=object_type, object_id=object_id, details=details))
