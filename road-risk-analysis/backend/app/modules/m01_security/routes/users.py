"""
User administration (RRAP-01-010, 01-011, 01-012). Every change is audited.
Guard rails: you cannot deactivate or delete yourself, and the last active
Administrator can't be removed, deactivated or stripped of that role.
"""
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, api_error, require
from app.api.openapi import err, errors
from app.core.security import hash_password, password_problems
from app.core.db.session import get_db
from app.modules.m01_security.models.user import Role, User, UserRole, UserStatus
from app.modules.m01_security.schemas import (
    AdminPasswordIn, AdminPasswordOut, MessageOut, UserCreateIn, UserCreateOut,
    UserListOut, UserOut, UserUpdateIn,
)
from app.core import audit
from app.modules.m01_security.auth_service import (
    active_admin_count, issue_reset_link, revoke_sessions, security_settings, user_out,
)
from app.core.email_service import EmailNotConfigured, EmailSendError, render_reset, send_email

router = APIRouter()

_E_404 = err(404, "not_found", "User not found.")
_E_WEAK = err(422, "weak_password", "Password must contain at least 10 characters, a digit.",
              requirements=["at least 10 characters", "a digit"])
_E_LAST_ADMIN = err(409, "last_admin", "At least one active Administrator must remain.")

_AUDITED_FIELDS = ("email", "username", "full_name", "phone", "whatsapp", "status", "language", "map_provider")


def _snapshot(user: User) -> dict:
    data = {k: (getattr(user, k).value if hasattr(getattr(user, k), "value") else getattr(user, k))
            for k in _AUDITED_FIELDS}
    data["roles"] = sorted(r.name for r in user.roles)
    return data


async def _get_user(db: AsyncSession, user_id: str) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise api_error(404, "not_found", "User not found.")
    return user


async def _load_roles(db: AsyncSession, role_ids: list[str]) -> list[Role]:
    if not role_ids:
        return []
    roles = (await db.execute(select(Role).where(Role.id.in_(set(role_ids))))).scalars().all()
    if len(roles) != len(set(role_ids)):
        raise api_error(422, "unknown_role", "One or more roles do not exist.")
    return list(roles)


async def _check_unique(db: AsyncSession, email: str | None, username: str | None, exclude: str | None = None):
    if email:
        q = select(User.id).where(func.lower(User.email) == email.lower())
        if exclude:
            q = q.where(User.id != exclude)
        if (await db.execute(q)).first():
            raise api_error(409, "email_taken", "A user with this e-mail already exists.")
    if username:
        q = select(User.id).where(func.lower(User.username) == username.lower())
        if exclude:
            q = q.where(User.id != exclude)
        if (await db.execute(q)).first():
            raise api_error(409, "username_taken", "This username is already used.")


def _is_admin(user: User) -> bool:
    return user.status == UserStatus.active and any(r.is_system and r.is_active for r in user.roles)


async def _send_link(db: AsyncSession, user: User, invite: bool) -> tuple[str | None, str | None]:
    """Issue a reset/invite link and e-mail it. Returns (link, error); the link
    is handed back to the administrator only when e-mail could not be sent,
    so accounts can still be onboarded before SMTP is configured."""
    sec = await security_settings(db)
    minutes = sec.reset_token_minutes if not invite else max(sec.reset_token_minutes, 72 * 60)
    link = await issue_reset_link(db, user, minutes)
    await db.commit()
    subject, text = render_reset(user.language.value, invite, name=user.full_name, minutes=minutes, link=link)
    try:
        await send_email(db, user.email, subject, text)
        return None, None
    except (EmailNotConfigured, EmailSendError) as exc:
        return link, str(exc)


@router.get("/", response_model=UserListOut, summary="List users")
async def list_users(
    search: str | None = Query(None, max_length=200,
                               description="Case-insensitive match on e-mail, name, username or phone."),
    status: UserStatus | None = Query(None, description="`active` or `inactive`."),
    role_id: str | None = Query(None, description="Only holders of this role."),
    account_type: Literal["staff", "mobile"] | None = Query(
        None, description="`staff` (created by an administrator) or `mobile` (self-registered from the app)."),
    skip: int = Query(0, ge=0, description="Offset of the page."),
    limit: int = Query(50, ge=1, le=200, description="Page size (max 200)."),
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require("users", "view")),
):
    """Users matching the filters, sorted by name, with `total` before paging."""
    q = select(User)
    if search:
        like = f"%{search.lower()}%"
        q = q.where(or_(func.lower(User.email).like(like), func.lower(User.full_name).like(like),
                        func.lower(User.username).like(like), User.phone.like(like)))
    if status:
        q = q.where(User.status == status)
    if role_id:
        q = q.where(User.id.in_(select(UserRole.user_id).where(UserRole.role_id == role_id)))
    if account_type:
        q = q.where(User.account_type == account_type)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.order_by(User.full_name, User.id).offset(skip).limit(limit))).scalars().all()
    return UserListOut(total=total, items=[user_out(u) for u in rows])


@router.get("/{user_id}", response_model=UserOut, summary="Get a user", responses=errors(_E_404))
async def get_user(user_id: str, db: AsyncSession = Depends(get_db),
                   _: Principal = Depends(require("users", "view"))):
    return user_out(await _get_user(db, user_id))


@router.post("/", response_model=UserCreateOut, status_code=201, summary="Create a user",
             responses=errors(err(409, "email_taken", "A user with this e-mail already exists."),
                              err(409, "username_taken", "This username is already used."),
                              err(422, "password_or_invite", "Set an initial password or send an invitation e-mail."),
                              _E_WEAK, err(422, "unknown_role", "One or more roles do not exist.")))
async def create_user(body: UserCreateIn, request: Request, db: AsyncSession = Depends(get_db),
                      principal: Principal = Depends(require("users", "add"))):
    """Creates a staff account with an initial password and/or an invitation e-mail (link valid 72 h).
    When the e-mail cannot be sent (SMTP not configured) the link is returned in `invite_link` so the
    administrator can share it. Audited as `user.create` (and `user.invite`)."""
    if not body.password and not body.send_invite:
        raise api_error(422, "password_or_invite", "Set an initial password or send an invitation e-mail.")
    await _check_unique(db, body.email, body.username)
    sec = await security_settings(db)
    if body.password:
        problems = password_problems(body.password, sec.password_min_length)
        if problems:
            raise api_error(422, "weak_password", "Password must contain " + ", ".join(problems) + ".",
                            requirements=problems)
    user = User(
        email=body.email, username=body.username or None, full_name=body.full_name,
        phone=body.phone, whatsapp=body.whatsapp, status=body.status,
        language=body.language, map_provider=body.map_provider,
        password_hash=hash_password(body.password) if body.password else None,
        password_changed_at=datetime.utcnow() if body.password else None,
    )
    user.roles = await _load_roles(db, body.role_ids)
    db.add(user)
    await db.flush()
    audit.record(db, request, "user.create", principal=principal, object_type="user", object_id=user.id,
                 details=_snapshot(user))
    await db.commit()

    out = UserCreateOut(**user_out(user).model_dump())
    if body.send_invite:
        link, err = await _send_link(db, user, invite=True)
        out.invite_link, out.invite_error = link, err
        await audit.record_now(request, "user.invite", principal=principal, object_type="user", object_id=user.id,
                               outcome="failure" if err else "success",
                               details={"email_error": err} if err else None)
    return out


@router.patch("/{user_id}", response_model=UserOut, summary="Update a user",
              responses=errors(_E_404,
                               err(409, "email_taken", "A user with this e-mail already exists."),
                               err(409, "username_taken", "This username is already used."),
                               err(409, "cannot_deactivate_self", "You cannot deactivate your own account."),
                               _E_LAST_ADMIN, err(422, "unknown_role", "One or more roles do not exist.")))
async def update_user(user_id: str, body: UserUpdateIn, request: Request, db: AsyncSession = Depends(get_db),
                      principal: Principal = Depends(require("users", "edit"))):
    """Partial update; `role_ids` replaces the user's roles. Deactivating a user revokes their sessions.
    The last active Administrator cannot lose that status. Audited as `user.update` with a diff."""
    user = await _get_user(db, user_id)
    before = _snapshot(user)
    was_admin = _is_admin(user)
    changes = body.model_dump(exclude_unset=True)

    if "email" in changes or "username" in changes:
        await _check_unique(db, changes.get("email"), changes.get("username") or None, exclude=user.id)
    if user.id == principal.id and changes.get("status") == UserStatus.inactive:
        raise api_error(409, "cannot_deactivate_self", "You cannot deactivate your own account.")

    for key in ("email", "username", "full_name", "phone", "whatsapp", "status", "language", "map_provider"):
        if key in changes:
            value = changes[key]
            if key == "username":
                value = value or None
            if key in ("email", "full_name") and not value:
                continue
            setattr(user, key, value)
    if "role_ids" in changes and changes["role_ids"] is not None:
        user.roles = await _load_roles(db, changes["role_ids"])

    if was_admin and not _is_admin(user) and await active_admin_count(db, excluding_user=user.id) == 0:
        await db.rollback()
        raise api_error(409, "last_admin", "At least one active Administrator must remain.")

    if user.status == UserStatus.inactive and before["status"] != "inactive":
        await revoke_sessions(db, user.id)
    after = _snapshot(user)
    audit.record(db, request, "user.update", principal=principal, object_type="user", object_id=user.id,
                 details=audit.diff(before, after))
    await db.commit()
    return user_out(user)


@router.delete("/{user_id}", status_code=204, summary="Delete a user",
               responses=errors(_E_404, err(409, "cannot_delete_self", "You cannot delete your own account."),
                                _E_LAST_ADMIN))
async def delete_user(user_id: str, request: Request, db: AsyncSession = Depends(get_db),
                      principal: Principal = Depends(require("users", "delete"))):
    """Deletes the account permanently (prefer deactivation to keep history readable). Audited as
    `user.delete` with a snapshot."""
    user = await _get_user(db, user_id)
    if user.id == principal.id:
        raise api_error(409, "cannot_delete_self", "You cannot delete your own account.")
    if _is_admin(user) and await active_admin_count(db, excluding_user=user.id) == 0:
        raise api_error(409, "last_admin", "At least one active Administrator must remain.")
    snapshot = _snapshot(user)
    await db.delete(user)
    audit.record(db, request, "user.delete", principal=principal, object_type="user", object_id=user_id,
                 details=snapshot)
    await db.commit()


@router.post("/{user_id}/password", response_model=AdminPasswordOut, summary="Set or reset a user's password",
             responses=errors(_E_404, _E_WEAK,
                              err(422, "nothing_to_do", "Provide a new password or request a reset e-mail.")))
async def admin_set_password(user_id: str, body: AdminPasswordIn, request: Request,
                             db: AsyncSession = Depends(get_db),
                             principal: Principal = Depends(require("users", "edit"))):
    """Either sets `new_password` (clears the lockout and signs the user out — audited
    `user.password.set`) or e-mails a reset link (`send_reset_email`; an invitation link for a user
    without password — audited `user.password.reset_link`). If the e-mail fails, the link is returned
    in `reset_link`."""
    user = await _get_user(db, user_id)
    if body.new_password:
        sec = await security_settings(db)
        problems = password_problems(body.new_password, sec.password_min_length)
        if problems:
            raise api_error(422, "weak_password", "Password must contain " + ", ".join(problems) + ".",
                            requirements=problems)
        user.password_hash = hash_password(body.new_password)
        user.password_changed_at = datetime.utcnow()
        user.failed_login_count = 0
        user.locked_until = None
        await revoke_sessions(db, user.id)
        audit.record(db, request, "user.password.set", principal=principal, object_type="user", object_id=user.id)
        await db.commit()
        return AdminPasswordOut(message="Password updated; the user's sessions were signed out.")
    if body.send_reset_email:
        link, err = await _send_link(db, user, invite=not user.password_hash)
        await audit.record_now(request, "user.password.reset_link", principal=principal, object_type="user",
                               object_id=user.id, outcome="failure" if err else "success",
                               details={"email_error": err} if err else None)
        if err:
            return AdminPasswordOut(message=f"E-mail could not be sent ({err}). Share this link securely instead.",
                                    reset_link=link)
        return AdminPasswordOut(message=f"A reset link was e-mailed to {user.email}.")
    raise api_error(422, "nothing_to_do", "Provide a new password or request a reset e-mail.")


@router.post("/{user_id}/unlock", response_model=UserOut, summary="Unlock a user", responses=errors(_E_404))
async def unlock_user(user_id: str, request: Request, db: AsyncSession = Depends(get_db),
                      principal: Principal = Depends(require("users", "edit"))):
    """Clears a sign-in lockout and the failed-attempt counter. Audited as `user.unlock`."""
    user = await _get_user(db, user_id)
    user.locked_until = None
    user.failed_login_count = 0
    audit.record(db, request, "user.unlock", principal=principal, object_type="user", object_id=user.id)
    await db.commit()
    return user_out(user)


@router.post("/{user_id}/mfa/reset", response_model=MessageOut, summary="Reset a user's MFA",
             responses=errors(_E_404))
async def reset_mfa(user_id: str, request: Request, db: AsyncSession = Depends(get_db),
                    principal: Principal = Depends(require("users", "edit"))):
    """For a user who lost their authenticator: removes the enrolment and signs the user out; they
    re-enrol at next sign-in if their role requires MFA. Audited as `user.mfa.reset`."""
    user = await _get_user(db, user_id)
    user.mfa_enabled = False
    user.mfa_secret_enc = None
    user.mfa_pending_enc = None
    user.mfa_last_step = None
    await revoke_sessions(db, user.id)
    audit.record(db, request, "user.mfa.reset", principal=principal, object_type="user", object_id=user.id)
    await db.commit()
    return MessageOut(message="Multi-factor authentication reset.")
