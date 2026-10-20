from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, require
from app.core.db.session import get_db
from app.modules.m13_admin.models.audit_log import AuditLog
from app.modules.m01_security.schemas import AuditLogListOut

router = APIRouter()


@router.get("/", response_model=AuditLogListOut, summary="Search the audit trail")
async def list_audit_logs(
    actor: str | None = Query(None, max_length=200, description="Matches actor e-mail/name"),
    action: str | None = Query(None, max_length=100, description="Prefix, e.g. 'auth.' or 'user.update'"),
    outcome: str | None = Query(None, pattern="^(success|failure)$", description="`success` or `failure`."),
    object_type: str | None = Query(None, description="e.g. `user`, `role`, `detection`, `settings`."),
    object_id: str | None = Query(None, description="Id of the object acted upon."),
    date_from: datetime | None = Query(None, description="From this instant (inclusive, UTC)."),
    date_to: datetime | None = Query(None, description="Up to this instant (inclusive, UTC)."),
    skip: int = Query(0, ge=0, description="Offset of the page."),
    limit: int = Query(50, ge=1, le=200, description="Page size (max 200)."),
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require("audit", "view")),
):
    """Audit entries matching the filters, newest first (RRAP-13-006): sign-ins and failures,
    sign-outs, password/MFA events, user/role/settings/API-key changes, validation decisions, every
    state-changing API call made by a user (`api.<method>`) and screen views (`ui.screen_view`)."""
    q = select(AuditLog)
    if actor:
        like = f"%{actor.lower()}%"
        q = q.where(or_(func.lower(AuditLog.actor_label).like(like), AuditLog.actor_id == actor))
    if action:
        q = q.where(AuditLog.action.like(f"{action}%"))
    if outcome:
        q = q.where(AuditLog.outcome == outcome)
    if object_type:
        q = q.where(AuditLog.object_type == object_type)
    if object_id:
        q = q.where(AuditLog.object_id == object_id)
    if date_from:
        q = q.where(AuditLog.created_at >= date_from.replace(tzinfo=None))
    if date_to:
        q = q.where(AuditLog.created_at <= date_to.replace(tzinfo=None))
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(
        q.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).offset(skip).limit(limit)
    )).scalars().all()
    return AuditLogListOut(total=total, items=rows)
