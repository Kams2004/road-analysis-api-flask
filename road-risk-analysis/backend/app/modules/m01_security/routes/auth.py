"""
Authentication endpoints (Module 1, RRAP-01-001..006).

Login order is deliberate: the CAPTCHA is checked BEFORE the credentials so
the two failures can be reported distinctly (captcha_failed vs
invalid_credentials) without the CAPTCHA becoming a password oracle.
Errors are `{"detail": {"code": ..., "message": ...}}`.

Mobile app (docs/spec-mobile-app.md): /auth/mobile/register and
/auth/mobile/login replace the CAPTCHA with the app's API key; /auth/google
signs in (or creates a mobile account) from a Google ID token. All three
return the same LoginOut as /auth/login, MFA included.
"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, account_user, api_client, api_error, current_user, load_session
from app.api.openapi import err, errors
from app.core.config import settings
from app.core.security import (
    decrypt_secret, dummy_verify, encrypt_secret, hash_password, hash_token,
    new_totp_secret, password_problems, totp_uri, verify_password, verify_totp,
)
from app.core.db.session import get_db
from app.modules.m01_security.models.auth_token import PasswordResetToken, SessionStage
from app.modules.m01_security.models.user import AccountType, Language, User, UserStatus
from app.modules.m01_security.schemas import (
    ActivityIn, AuthConfigOut, CaptchaPublic, ForgotPasswordIn, GoogleLoginIn, GooglePublic, LoginIn,
    LoginOut, MeOut, MessageOut, MfaDisableIn, MfaEnableIn, MfaSetupIn, MfaSetupOut, MfaVerifyIn,
    MobileLoginIn, MobileRegisterIn, PasswordChangeIn, ProfileUpdateIn, ResetCheckIn, ResetCheckOut,
    ResetPasswordIn,
)
from app.core import audit, rate_limit
from app.modules.m01_security import google_signin
from app.modules.m01_security.auth_service import (
    CHALLENGE_MINUTES, create_session, find_user, issue_reset_link, me_out,
    mfa_required_for, revoke_sessions, security_settings,
)
from app.modules.m01_security.captcha import CaptchaUnavailable, verify_captcha
from app.core.email_service import EmailNotConfigured, EmailSendError, render_reset, send_email
from app.core.settings_service import CaptchaSettings, GoogleSigninSettings, get_section

router = APIRouter()

# Documented error responses shared by the sign-in endpoints.
_E_CAPTCHA = (err(400, "captcha_failed", "The security check did not pass. Please complete it again."),
              err(503, "captcha_unavailable",
                  "The security check service is unreachable. Please try again in a moment."))
_E_LOCKED = err(423, "account_locked", "Too many failed attempts. The account is temporarily locked.",
                locked_until="2026-10-15T10:30:00Z")
_E_INACTIVE = err(403, "account_inactive", "This account is deactivated. Contact an administrator.")
_E_CREDENTIALS = err(401, "invalid_credentials", "Incorrect e-mail/username or password.")
_E_WEAK = err(422, "weak_password", "Password must contain at least 10 characters, a digit.",
              requirements=["at least 10 characters", "a digit"])
_E_CHALLENGE = err(401, "challenge_expired", "The sign-in step expired. Please sign in again.")
_E_CODE = err(400, "invalid_code", "The authentication code is incorrect.")
_E_PASSWORD = err(400, "invalid_password", "Current password is incorrect.")

_GENERIC_FORGOT = "If an active account matches this e-mail, a reset link has been sent."


async def _check_captcha(db: AsyncSession, request: Request, token: str | None) -> None:
    cfg: CaptchaSettings = await get_section(db, "captcha")  # type: ignore[assignment]
    try:
        ok = await verify_captcha(cfg, token, audit.client_ip(request))
    except CaptchaUnavailable:
        raise api_error(503, "captcha_unavailable",
                        "The security check service is unreachable. Please try again in a moment.")
    if not ok:
        raise api_error(400, "captcha_failed", "The security check did not pass. Please complete it again.")


def _check_password_policy(password: str, min_length: int) -> None:
    problems = password_problems(password, min_length)
    if problems:
        raise api_error(422, "weak_password", "Password must contain " + ", ".join(problems) + ".",
                        requirements=problems)


async def _full_login(db: AsyncSession, request: Request, user: User, method: str) -> LoginOut:
    sec = await security_settings(db)
    token, sess = await create_session(db, user, SessionStage.full, request, timedelta(hours=sec.session_hours))
    user.last_login_at = datetime.utcnow()
    user.failed_login_count = 0
    user.locked_until = None
    audit.record(db, request, "auth.login", object_type="user", object_id=user.id,
                 actor_label=user.email, details={"method": method})
    await db.commit()
    return LoginOut(status="ok", token=token, expires_at=sess.expires_at, user=me_out(user, sec))


async def _register_failure(db: AsyncSession, request: Request, user: User, reason: str) -> None:
    sec = await security_settings(db)
    user.failed_login_count = (user.failed_login_count or 0) + 1
    locked = user.failed_login_count >= sec.lockout_threshold
    if locked:
        user.locked_until = datetime.utcnow() + timedelta(minutes=sec.lockout_minutes)
        user.failed_login_count = 0
    audit.record(db, request, "auth.login", outcome="failure", object_type="user", object_id=user.id,
                 actor_label=user.email, details={"reason": reason, "locked": locked})
    await db.commit()


def _locked_error(user: User):
    return api_error(423, "account_locked",
                     "Too many failed attempts. The account is temporarily locked.",
                     locked_until=user.locked_until.isoformat() + "Z")


# ── Public ───────────────────────────────────────────────────────────────────

@router.get("/config", response_model=AuthConfigOut, summary="Get the public sign-in configuration")
async def auth_config(db: AsyncSession = Depends(get_db)):
    """What the sign-in screens need before anyone is signed in: the CAPTCHA widget to render, the
    supported languages, the minimum password length and the Google sign-in client IDs."""
    cfg: CaptchaSettings = await get_section(db, "captcha")  # type: ignore[assignment]
    sec = await security_settings(db)
    google: GoogleSigninSettings = await get_section(db, "google_signin")  # type: ignore[assignment]
    return AuthConfigOut(
        captcha=CaptchaPublic(provider=cfg.provider, site_key=cfg.site_key if cfg.provider != "disabled" else ""),
        languages=["en", "fr", "es"],
        password_min_length=sec.password_min_length,
        google=GooglePublic(enabled=True, web_client_id=google.web_client_id.strip(),
                            android_client_id=google.android_client_id.strip(),
                            ios_client_id=google.ios_client_id.strip())
        if google.configured() else GooglePublic(),
    )


async def _complete_login(db: AsyncSession, request: Request, user: User, method: str) -> LoginOut:
    """Credentials are good: MFA challenge if the account needs one, else a session."""
    sec = await security_settings(db)
    if user.mfa_enabled or mfa_required_for(user, sec):
        stage = SessionStage.mfa if user.mfa_enabled else SessionStage.mfa_setup
        token, _ = await create_session(db, user, stage, request, timedelta(minutes=CHALLENGE_MINUTES))
        user.failed_login_count = 0
        await db.commit()
        return LoginOut(status="mfa_required" if user.mfa_enabled else "mfa_setup_required",
                        challenge_token=token)
    return await _full_login(db, request, user, method)


async def _password_login(db: AsyncSession, request: Request, identifier: str, password: str,
                          method: str) -> LoginOut:
    user = await find_user(db, identifier)
    if user is None:
        dummy_verify()
        await audit.record_now(request, "auth.login", outcome="failure", actor_label=identifier[:200],
                               details={"reason": "unknown_user", "method": method})
        raise api_error(401, "invalid_credentials", "Incorrect e-mail/username or password.")

    if user.locked_until and user.locked_until > datetime.utcnow():
        await audit.record_now(request, "auth.login", outcome="failure", object_type="user",
                               object_id=user.id, actor_label=user.email, details={"reason": "locked"})
        raise _locked_error(user)

    if not verify_password(password, user.password_hash):
        await _register_failure(db, request, user, "bad_password")
        if user.locked_until and user.locked_until > datetime.utcnow():
            raise _locked_error(user)
        raise api_error(401, "invalid_credentials", "Incorrect e-mail/username or password.")

    if user.status != UserStatus.active:
        await audit.record_now(request, "auth.login", outcome="failure", object_type="user",
                               object_id=user.id, actor_label=user.email, details={"reason": "inactive"})
        raise api_error(403, "account_inactive", "This account is deactivated. Contact an administrator.")

    return await _complete_login(db, request, user, method)


@router.post("/login", response_model=LoginOut, summary="Sign in (web console)",
             responses=errors(*_E_CAPTCHA, _E_CREDENTIALS, _E_INACTIVE, _E_LOCKED))
async def login(body: LoginIn, request: Request, db: AsyncSession = Depends(get_db)):
    """Password sign-in with CAPTCHA. The CAPTCHA is checked first, so `captcha_failed` and
    `invalid_credentials` are reported distinctly.

    Answers `status: ok` with a session `token`, or `mfa_required` / `mfa_setup_required` with a
    `challenge_token` (see *Authentication* in the introduction). Every failure counts towards the
    lockout (`security.lockout_threshold` failures → locked for `security.lockout_minutes`).
    Audited as `auth.login` (success and failure)."""
    await _check_captcha(db, request, body.captcha_token)
    return await _password_login(db, request, body.identifier, body.password, "password")


# ── Mobile app (API key instead of CAPTCHA) ──────────────────────────────────

@router.post("/mobile/login", response_model=LoginOut, summary="Sign in (mobile app)",
             responses=errors(_E_CREDENTIALS, _E_INACTIVE, _E_LOCKED))
async def mobile_login(body: MobileLoginIn, request: Request, db: AsyncSession = Depends(get_db),
                       _: Principal = Depends(api_client)):
    """Same as `POST /auth/login` for the mobile app: the app's API key replaces the CAPTCHA.
    Same answer, MFA steps and lockout rules. Audited as `auth.login`."""
    return await _password_login(db, request, body.identifier, body.password, "mobile_password")


@router.post("/mobile/register", response_model=LoginOut, status_code=201,
             summary="Register a mobile account",
             responses=errors(err(409, "email_taken", "An account already exists with this e-mail. Sign in instead."),
                              _E_WEAK,
                              err(429, "rate_limited", "Too many sign-up attempts. Please try again later.")))
async def mobile_register(body: MobileRegisterIn, request: Request, db: AsyncSession = Depends(get_db),
                          client: Principal = Depends(api_client)):
    """Self-registration from the app: creates an active `mobile` account with no roles and signs it
    in (`status: ok` with a session token).

    **Rate limit:** `MOBILE_REGISTER_PER_HOUR` attempts per client IP per hour (default 5) →
    `429 rate_limited`. Audited as `auth.register` and `auth.login`."""
    ip = audit.client_ip(request) or "unknown"
    if not await rate_limit.allow(f"mobile_register:{ip}", settings.MOBILE_REGISTER_PER_HOUR, 3600):
        await audit.record_now(request, "auth.register", outcome="failure", actor_label=body.email,
                               details={"reason": "rate_limited", "client": client.label})
        raise api_error(429, "rate_limited", "Too many sign-up attempts. Please try again later.")

    sec = await security_settings(db)
    _check_password_policy(body.password, sec.password_min_length)
    if await find_user(db, body.email) is not None:
        await audit.record_now(request, "auth.register", outcome="failure", actor_label=body.email,
                               details={"reason": "email_taken", "client": client.label})
        raise api_error(409, "email_taken", "An account already exists with this e-mail. Sign in instead.")

    user = User(
        email=body.email, full_name=body.full_name, phone=(body.phone or "").strip() or None,
        language=body.language or Language.en, account_type=AccountType.mobile,
        password_hash=hash_password(body.password), password_changed_at=datetime.utcnow(),
        status=UserStatus.active, roles=[],
    )
    db.add(user)
    await db.flush()
    audit.record(db, request, "auth.register", object_type="user", object_id=user.id, actor_label=user.email,
                 details={"method": "password", "client": client.label})
    return await _full_login(db, request, user, "mobile_register")


# ── Google sign-in (mobile app; usable by the web console later) ─────────────

@router.post("/google", response_model=LoginOut, summary="Sign in with Google",
             responses=errors(err(400, "google_not_configured", "Google sign-in is not configured."),
                              err(401, "invalid_google_token", "Google sign-in failed. Please try again."),
                              err(401, "invalid_google_token", "This e-mail is linked to another Google account.",
                                  summary="invalid_google_token — e-mail linked to another Google account"),
                              _E_INACTIVE, _E_LOCKED,
                              err(503, "google_unavailable",
                                  "Google could not be reached. Please try again in a moment.")))
async def google_login(body: GoogleLoginIn, request: Request, db: AsyncSession = Depends(get_db)):
    """Signs in with a Google ID token. An unknown e-mail creates a `mobile` account; an existing
    account with the same e-mail is linked to the Google identity on first use. MFA applies as for a
    password sign-in. Audited as `auth.login` / `auth.register` / `auth.google.link`."""
    cfg: GoogleSigninSettings = await get_section(db, "google_signin")  # type: ignore[assignment]
    if not cfg.configured():
        raise api_error(400, "google_not_configured", "Google sign-in is not configured.")
    try:
        identity = await google_signin.verify_id_token(cfg, body.id_token)
    except google_signin.GoogleUnavailable:
        raise api_error(503, "google_unavailable", "Google could not be reached. Please try again in a moment.")
    except google_signin.GoogleTokenInvalid as exc:
        await audit.record_now(request, "auth.login", outcome="failure",
                               details={"method": "google", "reason": f"invalid_token:{exc}"})
        raise api_error(401, "invalid_google_token", "Google sign-in failed. Please try again.")

    user = (await db.execute(select(User).where(User.google_sub == identity["sub"]))).scalar_one_or_none() \
        or await find_user(db, identity["email"])
    if user is not None and user.google_sub and user.google_sub != identity["sub"]:
        await audit.record_now(request, "auth.login", outcome="failure", object_type="user", object_id=user.id,
                               actor_label=user.email, details={"method": "google", "reason": "google_sub_mismatch"})
        raise api_error(401, "invalid_google_token", "This e-mail is linked to another Google account.")

    if user is None:
        user = User(
            email=identity["email"], full_name=identity["name"] or identity["email"].split("@")[0],
            account_type=AccountType.mobile, google_sub=identity["sub"], status=UserStatus.active, roles=[],
        )
        db.add(user)
        await db.flush()
        audit.record(db, request, "auth.register", object_type="user", object_id=user.id,
                     actor_label=user.email, details={"method": "google"})
        return await _full_login(db, request, user, "google")

    if user.status != UserStatus.active:
        await audit.record_now(request, "auth.login", outcome="failure", object_type="user", object_id=user.id,
                               actor_label=user.email, details={"method": "google", "reason": "inactive"})
        raise api_error(403, "account_inactive", "This account is deactivated. Contact an administrator.")
    if user.locked_until and user.locked_until > datetime.utcnow():
        await audit.record_now(request, "auth.login", outcome="failure", object_type="user", object_id=user.id,
                               actor_label=user.email, details={"method": "google", "reason": "locked"})
        raise _locked_error(user)
    if not user.google_sub:
        user.google_sub = identity["sub"]
        audit.record(db, request, "auth.google.link", object_type="user", object_id=user.id, actor_label=user.email)
    return await _complete_login(db, request, user, "google")


@router.post("/mfa/verify", response_model=LoginOut, summary="Complete sign-in with an MFA code",
             responses=errors(_E_CODE, _E_CHALLENGE, _E_LOCKED))
async def mfa_verify(body: MfaVerifyIn, request: Request, db: AsyncSession = Depends(get_db)):
    """Second step of a sign-in that answered `mfa_required`: checks the authenticator code (each code
    is accepted once) and returns `status: ok` with the session token. Wrong codes count towards the
    lockout; when the account locks, the challenge is revoked."""
    sess = await load_session(db, body.challenge_token, [SessionStage.mfa])
    if sess is None:
        raise api_error(401, "challenge_expired", "The sign-in step expired. Please sign in again.")
    user = await db.get(User, sess.user_id)
    if user is None or user.status != UserStatus.active:
        raise api_error(401, "challenge_expired", "The sign-in step expired. Please sign in again.")
    if user.locked_until and user.locked_until > datetime.utcnow():
        raise _locked_error(user)

    secret = decrypt_secret(user.mfa_secret_enc)
    step = verify_totp(secret, body.code, user.mfa_last_step) if secret else None
    if step is None:
        await _register_failure(db, request, user, "bad_mfa_code")
        if user.locked_until and user.locked_until > datetime.utcnow():
            sess.revoked_at = datetime.utcnow()
            await db.commit()
            raise _locked_error(user)
        raise api_error(400, "invalid_code", "The authentication code is incorrect.")

    user.mfa_last_step = step
    sess.revoked_at = datetime.utcnow()
    return await _full_login(db, request, user, "password+totp")


async def _mfa_subject(db: AsyncSession, request: Request, challenge_token: str | None) -> tuple[User, object]:
    """MFA enrolment works for a signed-in user or during a login whose role
    requires MFA (challenge stage mfa_setup)."""
    if challenge_token:
        sess = await load_session(db, challenge_token, [SessionStage.mfa_setup])
        if sess is None:
            raise api_error(401, "challenge_expired", "The sign-in step expired. Please sign in again.")
        user = await db.get(User, sess.user_id)
        if user is None or user.status != UserStatus.active:
            raise api_error(401, "challenge_expired", "The sign-in step expired. Please sign in again.")
        return user, sess
    from app.api.deps import get_principal
    principal = await get_principal(request, db)
    if principal.kind != "user":
        raise api_error(401, "not_authenticated", "Please sign in.")
    return principal.user, None


@router.post("/mfa/setup", response_model=MfaSetupOut, summary="Start authenticator enrolment",
             responses=errors(_E_CHALLENGE, err(401, "not_authenticated", "Please sign in.")),
             openapi_extra={"x-access": "challenge_or_user"})
async def mfa_setup(body: MfaSetupIn, request: Request, db: AsyncSession = Depends(get_db)):
    """Generates a new TOTP secret (pending until `POST /auth/mfa/enable`). Called by a signed-in user
    (My profile) or during a sign-in that answered `mfa_setup_required` (pass its `challenge_token`)."""
    user, _ = await _mfa_subject(db, request, body.challenge_token)
    secret = new_totp_secret()
    user.mfa_pending_enc = encrypt_secret(secret)
    await db.commit()
    return MfaSetupOut(secret=secret, otpauth_uri=totp_uri(secret, user.email))


@router.post("/mfa/enable", response_model=LoginOut, summary="Confirm authenticator enrolment",
             responses=errors(err(400, "mfa_not_started", "Start the authenticator setup first."), _E_CODE,
                              _E_CHALLENGE, err(401, "not_authenticated", "Please sign in.")),
             openapi_extra={"x-access": "challenge_or_user"})
async def mfa_enable(body: MfaEnableIn, request: Request, db: AsyncSession = Depends(get_db)):
    """Confirms the pending secret with a first code and enables MFA. During a sign-in
    (`challenge_token`) this completes it: `status: ok` with a session token. For a signed-in user the
    answer is `status: ok` without token. Audited as `auth.mfa.enable`."""
    user, challenge = await _mfa_subject(db, request, body.challenge_token)
    pending = decrypt_secret(user.mfa_pending_enc)
    if not pending:
        raise api_error(400, "mfa_not_started", "Start the authenticator setup first.")
    step = verify_totp(pending, body.code)
    if step is None:
        raise api_error(400, "invalid_code", "The authentication code is incorrect.")
    user.mfa_secret_enc = user.mfa_pending_enc
    user.mfa_pending_enc = None
    user.mfa_enabled = True
    user.mfa_last_step = step
    audit.record(db, request, "auth.mfa.enable", object_type="user", object_id=user.id, actor_label=user.email)
    if challenge is not None:
        challenge.revoked_at = datetime.utcnow()
        return await _full_login(db, request, user, "password+totp_enrolment")
    await db.commit()
    return LoginOut(status="ok")


@router.post("/mfa/disable", response_model=MessageOut, summary="Disable MFA on the own account",
             responses=errors(_E_PASSWORD, _E_CODE,
                              err(403, "mfa_required_by_role", "Your role requires multi-factor authentication.")))
async def mfa_disable(body: MfaDisableIn, request: Request, db: AsyncSession = Depends(get_db),
                      principal: Principal = Depends(current_user)):
    """Requires the current password and a valid code; refused when a role or the security policy
    requires MFA. Audited as `auth.mfa.disable`."""
    user = principal.user
    sec = await security_settings(db)
    if mfa_required_for(user, sec):
        raise api_error(403, "mfa_required_by_role", "Your role requires multi-factor authentication.")
    if not verify_password(body.password, user.password_hash):
        raise api_error(400, "invalid_password", "Current password is incorrect.")
    secret = decrypt_secret(user.mfa_secret_enc)
    if not secret or verify_totp(secret, body.code, user.mfa_last_step) is None:
        raise api_error(400, "invalid_code", "The authentication code is incorrect.")
    user.mfa_enabled = False
    user.mfa_secret_enc = None
    user.mfa_last_step = None
    audit.record(db, request, "auth.mfa.disable", principal=principal, object_type="user", object_id=user.id)
    await db.commit()
    return MessageOut(message="Multi-factor authentication disabled.")


# ── Session / profile ────────────────────────────────────────────────────────

@router.post("/logout", response_model=MessageOut, summary="Sign out")
async def logout(request: Request, db: AsyncSession = Depends(get_db),
                 principal: Principal = Depends(account_user)):
    """Revokes the current session token immediately. Audited as `auth.logout`."""
    principal.session.revoked_at = datetime.utcnow()
    audit.record(db, request, "auth.logout", principal=principal, object_type="user", object_id=principal.user.id)
    await db.commit()
    return MessageOut(message="Signed out.")


@router.get("/me", response_model=MeOut, summary="Get the signed-in user")
async def me(db: AsyncSession = Depends(get_db), principal: Principal = Depends(account_user)):
    """Profile, roles and effective permissions (`module → [actions]`) of the signed-in user — the web
    console builds its menu from them. The mobile app calls it with its API key + the user's token;
    `401` means the session is gone and the app should sign out."""
    return me_out(principal.user, await security_settings(db))


@router.patch("/me", response_model=MeOut, summary="Update the own profile")
async def update_me(body: ProfileUpdateIn, request: Request, db: AsyncSession = Depends(get_db),
                    principal: Principal = Depends(account_user)):
    """Changes name, phone, WhatsApp, language and/or map provider. Omitted fields are unchanged; an
    empty `full_name` is ignored. Audited as `user.profile.update` with a diff."""
    user = principal.user
    changes = body.model_dump(exclude_unset=True)
    before = {k: getattr(user, k) for k in changes}
    for k, v in changes.items():
        if k == "full_name" and not v:
            continue
        setattr(user, k, v)
    audit.record(db, request, "user.profile.update", principal=principal, object_type="user", object_id=user.id,
                 details=audit.diff({k: str(v) for k, v in before.items()},
                                    {k: str(getattr(user, k)) for k in changes}))
    await db.commit()
    return me_out(user, await security_settings(db))


@router.post("/me/password", response_model=MessageOut, summary="Change the own password",
             responses=errors(_E_PASSWORD, _E_WEAK))
async def change_password(body: PasswordChangeIn, request: Request, db: AsyncSession = Depends(get_db),
                          principal: Principal = Depends(account_user)):
    """Checks the current password and the password policy, then signs out every OTHER session of the
    user (the current one stays valid). Also reachable by the mobile app (API key + the user's Bearer
    token). Audited as `auth.password.change`."""
    user = principal.user
    if not verify_password(body.current_password, user.password_hash):
        raise api_error(400, "invalid_password", "Current password is incorrect.")
    sec = await security_settings(db)
    _check_password_policy(body.new_password, sec.password_min_length)
    user.password_hash = hash_password(body.new_password)
    user.password_changed_at = datetime.utcnow()
    await revoke_sessions(db, user.id, except_id=principal.session.id)
    audit.record(db, request, "auth.password.change", principal=principal, object_type="user", object_id=user.id)
    await db.commit()
    return MessageOut(message="Password changed. Other sessions were signed out.")


@router.post("/activity", status_code=204, summary="Record a screen view")
async def activity(body: ActivityIn, request: Request, db: AsyncSession = Depends(get_db),
                   principal: Principal = Depends(current_user)):
    """Screen-view trail from the web console (RRAP-13-006): writes a `ui.screen_view` audit entry."""
    audit.record(db, request, "ui.screen_view", principal=principal, details={"screen": body.screen})
    await db.commit()


# ── Password reset (RRAP-01-004) ─────────────────────────────────────────────

@router.post("/password/forgot", response_model=MessageOut, summary="Request a password-reset e-mail",
             responses=errors(*_E_CAPTCHA))
async def forgot_password(body: ForgotPasswordIn, request: Request, db: AsyncSession = Depends(get_db)):
    """E-mails a single-use reset link (valid `security.reset_token_minutes`) to an active account.
    The answer is the same whether or not the account exists (no account enumeration), and at most
    one e-mail per account is sent per minute. Audited as `auth.password.forgot`."""
    await _check_captcha(db, request, body.captcha_token)
    user = await find_user(db, body.email)
    if user is None or user.status != UserStatus.active or "@" not in body.email:
        await audit.record_now(request, "auth.password.forgot", outcome="failure",
                               actor_label=body.email[:200], details={"reason": "no_active_account"})
        return MessageOut(message=_GENERIC_FORGOT)

    recent = (await db.execute(
        select(PasswordResetToken).where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.created_at > datetime.utcnow() - timedelta(seconds=60),
        )
    )).first()
    if recent:
        return MessageOut(message=_GENERIC_FORGOT)

    sec = await security_settings(db)
    link = await issue_reset_link(db, user, sec.reset_token_minutes)
    await db.commit()
    subject, text = render_reset(user.language.value, False, name=user.full_name,
                                 minutes=sec.reset_token_minutes, link=link)
    outcome, reason = "success", None
    try:
        await send_email(db, user.email, subject, text)
    except (EmailNotConfigured, EmailSendError) as exc:
        outcome, reason = "failure", str(exc)
    await audit.record_now(request, "auth.password.forgot", outcome=outcome, object_type="user",
                           object_id=user.id, actor_label=user.email,
                           details={"email_error": reason} if reason else None)
    return MessageOut(message=_GENERIC_FORGOT)


async def _reset_token_row(db: AsyncSession, token: str) -> PasswordResetToken | None:
    row = (await db.execute(
        select(PasswordResetToken).where(PasswordResetToken.token_hash == hash_token(token))
    )).scalar_one_or_none()
    if row is None or row.used_at is not None or row.expires_at <= datetime.utcnow():
        return None
    return row


@router.post("/password/reset/check", response_model=ResetCheckOut, summary="Check a reset link")
async def check_reset_token(body: ResetCheckIn, db: AsyncSession = Depends(get_db)):
    """Tells the reset page whether the token is still usable (unused, unexpired, active account)."""
    row = await _reset_token_row(db, body.token)
    if row is None:
        return ResetCheckOut(valid=False)
    user = await db.get(User, row.user_id)
    return ResetCheckOut(valid=user is not None and user.status == UserStatus.active,
                         email=user.email if user else None)


@router.post("/password/reset", response_model=MessageOut, summary="Reset the password from a link",
             responses=errors(err(400, "invalid_reset_token", "This reset link is invalid or has expired."), _E_WEAK))
async def reset_password(body: ResetPasswordIn, request: Request, db: AsyncSession = Depends(get_db)):
    """Sets a new password from a reset or invitation link: consumes the token, clears the lockout and
    signs out every session of the user. Audited as `auth.password.reset`."""
    row = await _reset_token_row(db, body.token)
    user = await db.get(User, row.user_id) if row else None
    if row is None or user is None or user.status != UserStatus.active:
        raise api_error(400, "invalid_reset_token", "This reset link is invalid or has expired.")
    sec = await security_settings(db)
    _check_password_policy(body.new_password, sec.password_min_length)
    user.password_hash = hash_password(body.new_password)
    user.password_changed_at = datetime.utcnow()
    user.failed_login_count = 0
    user.locked_until = None
    row.used_at = datetime.utcnow()
    await revoke_sessions(db, user.id)
    audit.record(db, request, "auth.password.reset", object_type="user", object_id=user.id, actor_label=user.email)
    await db.commit()
    return MessageOut(message="Password updated. You can now sign in.")
