import re
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from app.models.user import Language, MapProvider, UserStatus


# ── Auth ─────────────────────────────────────────────────────────────────────

class CaptchaPublic(BaseModel):
    """CAPTCHA widget the login / forgot-password pages must render."""
    provider: str = Field(..., description="`turnstile`, `recaptcha`, `hcaptcha` or `disabled`.",
                          examples=["turnstile"])
    site_key: str = Field(..., description="Public site key of the widget (empty when disabled).",
                          examples=["0x4AAAAAAA…"])


class GooglePublic(BaseModel):
    """Public OAuth client IDs for "Sign in with Google" (empty when not configured)."""
    enabled: bool = Field(False, description="Google sign-in is configured and accepted by `POST /auth/google`.")
    web_client_id: str = Field("", description="OAuth client ID for web.")
    android_client_id: str = Field("", description="OAuth client ID for Android.")
    ios_client_id: str = Field("", description="OAuth client ID for iOS.")


class AuthConfigOut(BaseModel):
    """Public configuration the sign-in screens need before anyone is signed in."""
    captcha: CaptchaPublic
    languages: list[str] = Field(..., description="Supported interface languages.", examples=[["en", "fr", "es"]])
    password_min_length: int = Field(..., description="Minimum password length of the security policy.", examples=[10])
    google: GooglePublic = GooglePublic()


class LoginIn(BaseModel):
    """Web console sign-in."""
    identifier: str = Field(..., min_length=1, description="E-mail or username", examples=["admin@viasafe.local"])
    password: str = Field(..., min_length=1, examples=["ChangeMe123"])
    captcha_token: Optional[str] = Field(None, description="Token produced by the CAPTCHA widget of `GET /auth/config` "
                                                            "(ignored when the provider is `disabled`).")


class MobileLoginIn(BaseModel):
    """Mobile app sign-in — the API key replaces the CAPTCHA."""
    identifier: str = Field(..., min_length=1, description="E-mail or username", examples=["driver@example.com"])
    password: str = Field(..., min_length=1, examples=["S3cure-pass!"])


class MobileRegisterIn(BaseModel):
    """Self-registration from the mobile app (creates a `mobile` account without roles)."""
    full_name: str = Field(..., min_length=1, max_length=200, examples=["Jean Mbarga"])
    email: str = Field(..., max_length=254, description="Normalised to lower case; must be unique.",
                       examples=["jean.mbarga@example.com"])
    password: str = Field(..., min_length=1, max_length=256,
                          description="Must satisfy the password policy (`422 weak_password` otherwise).")
    phone: Optional[str] = Field(None, max_length=40, examples=["+237 6 99 00 00 00"])
    language: Optional[Language] = Field(None, description="Interface language; `en` when omitted.")

    _email = field_validator("email")(lambda cls, v: _clean_email(v))
    _name = field_validator("full_name")(lambda cls, v: v.strip())


class GoogleLoginIn(BaseModel):
    """Google ID token obtained by the app's Google Sign-In SDK."""
    id_token: str = Field(..., min_length=1, max_length=8192,
                          description="JWT whose `aud` must be one of the configured Google client IDs.")


class RoleRef(BaseModel):
    """Reference to a role."""
    id: str = Field(..., examples=["5f0c6c1e-3d4b-4f7e-9a51-1f2d3c4b5a69"])
    name: str = Field(..., examples=["Validator"])


class MeOut(BaseModel):
    """The signed-in user's profile and effective permissions."""
    id: str
    email: str = Field(..., examples=["admin@viasafe.local"])
    username: Optional[str] = Field(..., examples=["admin"])
    full_name: str = Field(..., examples=["Platform Administrator"])
    phone: Optional[str]
    whatsapp: Optional[str]
    status: UserStatus = Field(..., description="`active` or `inactive` (cannot sign in).")
    account_type: str = Field("staff", description="`staff` (created by an administrator) or `mobile` "
                                                   "(self-registered from the app, no roles).")
    language: Language = Field(..., description="Interface language: `en`, `fr` or `es`.")
    map_provider: MapProvider = Field(..., description="Base-map preference: `default` (platform default), "
                                                       "`osm` or `google`.")
    mfa_enabled: bool = Field(..., description="An authenticator app is enrolled.")
    mfa_required: bool = Field(..., description="A role or the security policy requires MFA for this user.")
    roles: list[RoleRef]
    permissions: dict[str, list[str]] = Field(..., description="Effective rights: module key → granted actions "
                                                               "(union of the active roles).",
                                              examples=[{"detections": ["view"], "validations": ["view", "edit"]}])
    last_login_at: Optional[datetime] = Field(..., description="UTC.")


class LoginOut(BaseModel):
    """Result of a sign-in step.

    * `ok` — `token` (send as `Authorization: Bearer`), `expires_at` and `user` are set;
    * `mfa_required` — continue with `POST /auth/mfa/verify` and `challenge_token`;
    * `mfa_setup_required` — enrol with `POST /auth/mfa/setup` + `POST /auth/mfa/enable` and `challenge_token`.
    """
    status: Literal["ok", "mfa_required", "mfa_setup_required"] = Field(..., examples=["ok"])
    token: Optional[str] = Field(None, description="Opaque session token (status `ok`).")
    expires_at: Optional[datetime] = Field(None, description="Session expiry, UTC (status `ok`).")
    challenge_token: Optional[str] = Field(None, description="Short-lived token of the pending MFA step.")
    user: Optional[MeOut] = None


class MfaVerifyIn(BaseModel):
    """Second sign-in step for an account with MFA enabled."""
    challenge_token: str = Field(..., description="From the `mfa_required` answer of a sign-in endpoint.")
    code: str = Field(..., description="6-digit code of the authenticator app.", examples=["123456"])


class MfaSetupIn(BaseModel):
    """Start authenticator enrolment."""
    challenge_token: Optional[str] = Field(None, description="Only during a sign-in that answered "
                                                             "`mfa_setup_required`; omit when signed in.")


class MfaSetupOut(BaseModel):
    """New TOTP secret, pending until confirmed by `POST /auth/mfa/enable`."""
    secret: str = Field(..., description="Base32 TOTP secret (manual entry).", examples=["JBSWY3DPEHPK3PXP"])
    otpauth_uri: str = Field(..., description="`otpauth://` URI to render as a QR code.")


class MfaEnableIn(BaseModel):
    """Confirm enrolment with a first code."""
    code: str = Field(..., description="Current code of the newly enrolled authenticator.", examples=["123456"])
    challenge_token: Optional[str] = Field(None, description="Same as in `POST /auth/mfa/setup` during a sign-in.")


class MfaDisableIn(BaseModel):
    """Turn MFA off (not allowed when a role requires it)."""
    password: str = Field(..., description="Current password.")
    code: str = Field(..., description="Current authenticator code.", examples=["123456"])


class ProfileUpdateIn(BaseModel):
    """Fields of the own profile to change; omitted fields are left unchanged."""
    full_name: Optional[str] = Field(None, min_length=1, max_length=200)
    phone: Optional[str] = Field(None, max_length=40)
    whatsapp: Optional[str] = Field(None, max_length=40)
    language: Optional[Language] = None
    map_provider: Optional[MapProvider] = Field(None, description="`default`, `osm` or `google`.")


class PasswordChangeIn(BaseModel):
    """Change the own password."""
    current_password: str
    new_password: str = Field(..., description="Must satisfy the password policy.")


class ForgotPasswordIn(BaseModel):
    """Request a password-reset e-mail."""
    email: str = Field(..., examples=["jean.mbarga@example.com"])
    captcha_token: Optional[str] = Field(None, description="CAPTCHA token, as for `POST /auth/login`.")


class ResetCheckIn(BaseModel):
    """Token from the reset / invitation link."""
    token: str


class ResetCheckOut(BaseModel):
    """Whether a reset link can still be used."""
    valid: bool
    email: Optional[str] = Field(None, description="Account the link belongs to (when the token exists).")


class ResetPasswordIn(BaseModel):
    """Set a new password from a reset / invitation link."""
    token: str
    new_password: str = Field(..., description="Must satisfy the password policy.")


class ActivityIn(BaseModel):
    """A screen opened in the web console."""
    screen: str = Field(..., max_length=200, examples=["/validations/queue"])


class MessageOut(BaseModel):
    """Human-readable confirmation."""
    message: str = Field(..., examples=["Signed out."])


# ── Users ────────────────────────────────────────────────────────────────────

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _clean_email(v: str) -> str:
    v = v.strip().lower()
    if not _EMAIL_RE.match(v):
        raise ValueError("invalid e-mail address")
    return v


class UserOut(BaseModel):
    """A user account as seen by administrators."""
    id: str
    email: str
    username: Optional[str]
    full_name: str
    phone: Optional[str]
    whatsapp: Optional[str]
    status: UserStatus
    account_type: str = "staff"
    language: Language
    map_provider: MapProvider
    mfa_enabled: bool
    has_password: bool = Field(..., description="False for invited users who have not set a password yet "
                                                "and for Google-only accounts.")
    locked_until: Optional[datetime] = Field(..., description="Set while the account is locked after failed "
                                                              "sign-ins (UTC).")
    last_login_at: Optional[datetime]
    created_at: datetime
    roles: list[RoleRef]


class UserListOut(BaseModel):
    """One page of users."""
    total: int = Field(..., description="Number of matches before paging.")
    items: list[UserOut]


class UserCreateIn(BaseModel):
    """New user. Give an initial `password` and/or `send_invite` (e-mailed link valid 72 h)."""
    email: str = Field(..., max_length=254)
    username: Optional[str] = Field(None, min_length=3, max_length=60, pattern=r"^[A-Za-z0-9_.-]+$")
    full_name: str = Field(..., min_length=1, max_length=200)
    phone: Optional[str] = Field(None, max_length=40)
    whatsapp: Optional[str] = Field(None, max_length=40)
    status: UserStatus = UserStatus.active
    language: Language = Language.en
    map_provider: MapProvider = MapProvider.default
    role_ids: list[str] = Field(default_factory=list, description="Roles to assign (ids from `GET /roles/`).")
    password: Optional[str] = Field(None, description="Initial password (must satisfy the policy).")
    send_invite: bool = Field(False, description="E-mail an invitation link to set the password.")

    _email = field_validator("email")(lambda cls, v: _clean_email(v))


class UserUpdateIn(BaseModel):
    """Fields to change; omitted fields are left unchanged. `role_ids` replaces the role list."""
    email: Optional[str] = Field(None, max_length=254)
    username: Optional[str] = Field(None, max_length=60, pattern=r"^[A-Za-z0-9_.-]*$")
    full_name: Optional[str] = Field(None, min_length=1, max_length=200)
    phone: Optional[str] = Field(None, max_length=40)
    whatsapp: Optional[str] = Field(None, max_length=40)
    status: Optional[UserStatus] = None
    language: Optional[Language] = None
    map_provider: Optional[MapProvider] = None
    role_ids: Optional[list[str]] = None

    _email = field_validator("email")(lambda cls, v: _clean_email(v) if v else v)


class AdminPasswordIn(BaseModel):
    """Either set `new_password` directly or `send_reset_email`."""
    new_password: Optional[str] = Field(None, description="Set this password now (signs the user out).")
    send_reset_email: bool = Field(False, description="E-mail a reset link instead.")


class AdminPasswordOut(BaseModel):
    """Outcome of an administrator password action."""
    message: str
    reset_link: Optional[str] = Field(None, description="Only when the e-mail could not be sent — share it securely.")


class UserCreateOut(UserOut):
    """Created user; the invitation link is returned only when it could not be e-mailed."""
    invite_link: Optional[str] = Field(None, description="Invitation link when the e-mail could not be sent.")
    invite_error: Optional[str] = Field(None, description="Why the invitation e-mail failed.")


# ── Roles ────────────────────────────────────────────────────────────────────

class PermissionRow(BaseModel):
    """Rights of a role on one permission module. Any write right implies `can_view`."""
    module: str = Field(..., description="Module key (see `GET /roles/modules`).", examples=["validations"])
    can_view: bool = False
    can_add: bool = False
    can_edit: bool = False
    can_delete: bool = False


class RoleOut(BaseModel):
    """A role with its full permission matrix (one row per module)."""
    id: str
    name: str = Field(..., examples=["Validator"])
    description: Optional[str]
    is_active: bool = Field(..., description="Inactive roles grant nothing.")
    is_system: bool = Field(..., description="The built-in Administrator role (always every permission).")
    mfa_required: bool = Field(..., description="Members must use multi-factor authentication.")
    user_count: int = Field(..., description="Users holding the role.")
    active_user_count: int = Field(..., description="Active users holding the role.")
    permissions: list[PermissionRow]
    created_at: datetime
    updated_at: datetime


class RoleCreateIn(BaseModel):
    """New role. Modules not listed get no rights."""
    name: str = Field(..., min_length=1, max_length=80)
    description: Optional[str] = Field(None, max_length=500)
    is_active: bool = True
    mfa_required: bool = False
    permissions: list[PermissionRow] = Field(default_factory=list)


class RoleUpdateIn(BaseModel):
    """Fields to change. `permissions` rows are merged per module."""
    name: Optional[str] = Field(None, min_length=1, max_length=80)
    description: Optional[str] = Field(None, max_length=500)
    is_active: Optional[bool] = None
    mfa_required: Optional[bool] = None
    permissions: Optional[list[PermissionRow]] = None


class ModuleOut(BaseModel):
    """A permission module."""
    key: str = Field(..., examples=["validations"])
    label: str = Field(..., examples=["Human validation"])
    group: str = Field(..., examples=["Hazards"])
    actions: list[str] = Field(..., examples=[["view", "add", "edit", "delete"]])


# ── API clients ──────────────────────────────────────────────────────────────

class ApiClientOut(BaseModel):
    """A registered API client. The key itself is never returned again."""
    id: str
    name: str = Field(..., examples=["VIASafe mobile app"])
    description: Optional[str]
    key_prefix: str = Field(..., description="First characters of the key, to recognise it.", examples=["vsk_9fA2bC"])
    is_active: bool = Field(..., description="False once revoked.")
    created_by: Optional[str]
    created_at: datetime
    rotated_at: Optional[datetime]
    revoked_at: Optional[datetime]
    last_used_at: Optional[datetime]

    class Config:
        from_attributes = True


class ApiClientCreateIn(BaseModel):
    """New API client."""
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=500)


class ApiClientSecretOut(ApiClientOut):
    """API client with its clear key — shown only once."""
    api_key: str = Field(..., description="Send as `X-API-Key`. Store it now: it cannot be retrieved later.",
                         examples=["vsk_9fA2bC…"])


# ── Audit ────────────────────────────────────────────────────────────────────

class AuditLogOut(BaseModel):
    """One audit-trail entry."""
    id: str
    created_at: datetime = Field(..., description="UTC.")
    actor_type: str = Field(..., description="`user`, `client`, `anonymous` or `system`.", examples=["user"])
    actor_id: Optional[str]
    actor_label: Optional[str] = Field(..., description="E-mail of the user or name of the API client.")
    action: str = Field(..., description="Dotted action name, e.g. `auth.login`, `user.update`, "
                                         "`detection.accept`, `api.post`, `ui.screen_view`.",
                        examples=["auth.login"])
    outcome: str = Field(..., description="`success` or `failure`.", examples=["success"])
    object_type: Optional[str] = Field(..., examples=["user"])
    object_id: Optional[str]
    details: Optional[dict] = Field(..., description="Action-specific data (diffs have secrets redacted).")
    ip: Optional[str]
    user_agent: Optional[str]

    class Config:
        from_attributes = True


class AuditLogListOut(BaseModel):
    """One page of audit entries, newest first."""
    total: int = Field(..., description="Number of matches before paging.")
    items: list[AuditLogOut]
