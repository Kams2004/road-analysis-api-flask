"""
Shared authentication logic used by the /auth, /users and startup code:
session issuing, reset/invite links, MFA policy and the /auth/me payload.
"""
import logging
from datetime import datetime, timedelta

from fastapi import Request
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.permissions import DEFAULT_ROLES, ADMIN_ROLE_NAME
from app.core.security import new_token, hash_token, hash_password
from app.api.deps import effective_permissions
from app.modules.m01_security.models.auth_token import AuthSession, SessionStage, PasswordResetToken
from app.modules.m01_security.models.user import User, Role, RolePermission, UserStatus
from app.modules.m01_security.schemas import MeOut, RoleRef, UserOut
from app.core.audit import client_ip
from app.core.settings_service import SecuritySettings, get_section

logger = logging.getLogger(__name__)

CHALLENGE_MINUTES = 10


async def security_settings(db: AsyncSession) -> SecuritySettings:
    return await get_section(db, "security")  # type: ignore[return-value]


def mfa_required_for(user: User, sec: SecuritySettings) -> bool:
    return sec.mfa_required_for_all or any(r.is_active and r.mfa_required for r in user.roles)


async def create_session(db: AsyncSession, user: User, stage: SessionStage,
                         request: Request | None, lifetime: timedelta) -> tuple[str, AuthSession]:
    token = new_token()
    sess = AuthSession(
        user_id=user.id,
        token_hash=hash_token(token),
        stage=stage,
        expires_at=datetime.utcnow() + lifetime,
        ip=client_ip(request),
        user_agent=(request.headers.get("user-agent", "")[:300] if request else None),
    )
    db.add(sess)
    return token, sess


async def revoke_sessions(db: AsyncSession, user_id: str, except_id: str | None = None) -> None:
    stmt = (
        update(AuthSession)
        .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=datetime.utcnow())
    )
    if except_id:
        stmt = stmt.where(AuthSession.id != except_id)
    await db.execute(stmt)


async def issue_reset_link(db: AsyncSession, user: User, minutes: int) -> str:
    token = new_token()
    db.add(PasswordResetToken(
        user_id=user.id,
        token_hash=hash_token(token),
        expires_at=datetime.utcnow() + timedelta(minutes=minutes),
    ))
    return f"{settings.WEB_BASE_URL.rstrip('/')}/reset-password?token={token}"


def role_refs(user: User) -> list[RoleRef]:
    return [RoleRef(id=r.id, name=r.name) for r in sorted(user.roles, key=lambda r: r.name)]


def me_out(user: User, sec: SecuritySettings) -> MeOut:
    return MeOut(
        id=user.id, email=user.email, username=user.username, full_name=user.full_name,
        phone=user.phone, whatsapp=user.whatsapp, status=user.status,
        account_type=user.account_type or "staff",
        language=user.language, map_provider=user.map_provider,
        mfa_enabled=user.mfa_enabled, mfa_required=mfa_required_for(user, sec),
        roles=role_refs(user),
        permissions={k: sorted(v) for k, v in effective_permissions(user).items()},
        last_login_at=user.last_login_at,
    )


def user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id, email=user.email, username=user.username, full_name=user.full_name,
        phone=user.phone, whatsapp=user.whatsapp, status=user.status,
        account_type=user.account_type or "staff",
        language=user.language, map_provider=user.map_provider,
        mfa_enabled=user.mfa_enabled, has_password=bool(user.password_hash),
        locked_until=user.locked_until, last_login_at=user.last_login_at,
        created_at=user.created_at, roles=role_refs(user),
    )


async def find_user(db: AsyncSession, identifier: str) -> User | None:
    ident = identifier.strip().lower()
    return (await db.execute(
        select(User).where((func.lower(User.email) == ident) | (func.lower(User.username) == ident))
    )).scalar_one_or_none()


async def active_admin_count(db: AsyncSession, excluding_user: str | None = None) -> int:
    """Active users holding the (active, system) Administrator role."""
    from app.modules.m01_security.models.user import UserRole
    stmt = (
        select(func.count(func.distinct(User.id)))
        .join(UserRole, UserRole.user_id == User.id)
        .join(Role, Role.id == UserRole.role_id)
        .where(User.status == UserStatus.active, Role.is_system.is_(True), Role.is_active.is_(True))
    )
    if excluding_user:
        stmt = stmt.where(User.id != excluding_user)
    return (await db.execute(stmt)).scalar_one()


# ── Startup seeding ──────────────────────────────────────────────────────────

async def seed_roles(db: AsyncSession) -> None:
    existing = (await db.execute(select(func.count(Role.id)))).scalar_one()
    if existing:
        return
    for spec in DEFAULT_ROLES:
        role = Role(name=spec["name"], description=spec["description"], is_system=spec.get("is_system", False))
        for module, actions in spec["permissions"].items():
            role.permissions.append(RolePermission(
                module=module,
                can_view="view" in actions, can_add="add" in actions,
                can_edit="edit" in actions, can_delete="delete" in actions,
            ))
        db.add(role)
    await db.commit()
    logger.info("Seeded %d default roles", len(DEFAULT_ROLES))


async def admin_role(db: AsyncSession) -> Role | None:
    return (await db.execute(select(Role).where(Role.is_system.is_(True)).limit(1))).scalar_one_or_none() \
        or (await db.execute(select(Role).where(Role.name == ADMIN_ROLE_NAME))).scalar_one_or_none()


async def bootstrap_admin(db: AsyncSession) -> None:
    """Create the first administrator from env vars when no user exists."""
    if not (settings.BOOTSTRAP_ADMIN_EMAIL and settings.BOOTSTRAP_ADMIN_PASSWORD):
        return
    if (await db.execute(select(func.count(User.id)))).scalar_one():
        return
    role = await admin_role(db)
    user = User(
        email=settings.BOOTSTRAP_ADMIN_EMAIL.strip().lower(),
        full_name="Administrator",
        password_hash=hash_password(settings.BOOTSTRAP_ADMIN_PASSWORD),
        password_changed_at=datetime.utcnow(),
        status=UserStatus.active,
    )
    if role:
        user.roles = [role]
    db.add(user)
    await db.commit()
    logger.warning("Bootstrap administrator %s created — change its password after first login", user.email)
