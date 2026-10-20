"""
Platform users, roles and the per-module View/Add/Edit/Delete permission
matrix (Module 1, RRAP-01-007..013).

A user may hold several roles; the effective permission for a module/action
is granted when ANY of the user's *active* roles grants it. The seeded
Administrator role is `is_system`: it always holds every permission and can
be neither deleted nor deactivated, so the platform can't be locked out.
"""
import uuid
import enum
from datetime import datetime
from sqlalchemy import String, DateTime, Boolean, Integer, ForeignKey, UniqueConstraint, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.db.session import Base


class UserStatus(str, enum.Enum):
    active   = "active"
    inactive = "inactive"


class MapProvider(str, enum.Enum):
    default = "default"   # follow the platform default (system settings)
    osm     = "osm"
    google  = "google"


class AccountType:
    """`staff` = created by an administrator (web console, roles);
    `mobile` = self-registered from the VIASafe app (no roles)."""
    staff  = "staff"
    mobile = "mobile"


class Language(str, enum.Enum):
    en = "en"
    fr = "fr"
    es = "es"


class UserRole(Base):
    __tablename__ = "user_roles"

    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role_id: Mapped[str] = mapped_column(String, ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True)


class User(Base):
    __tablename__ = "users"

    id:        Mapped[str]        = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    email:     Mapped[str]        = mapped_column(String, unique=True, index=True)   # stored lower-cased
    username:  Mapped[str | None] = mapped_column(String, unique=True, index=True, nullable=True)
    full_name: Mapped[str]        = mapped_column(String)
    phone:     Mapped[str | None] = mapped_column(String, nullable=True)
    whatsapp:  Mapped[str | None] = mapped_column(String, nullable=True)
    status:    Mapped[UserStatus] = mapped_column(SAEnum(UserStatus), default=UserStatus.active)
    account_type: Mapped[str]     = mapped_column(String, default=AccountType.staff, server_default=AccountType.staff)

    password_hash: Mapped[str | None] = mapped_column(String, nullable=True)  # None = invited, no password yet
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    google_sub: Mapped[str | None] = mapped_column(String, unique=True, index=True, nullable=True)  # Google sign-in

    # Preferences — per user, deliberately not tied to role (RRAP-01-012)
    map_provider: Mapped[MapProvider] = mapped_column(SAEnum(MapProvider), default=MapProvider.default)
    language:     Mapped[Language]    = mapped_column(SAEnum(Language), default=Language.en)

    # TOTP multi-factor (RRAP-01-002). Secrets are encrypted at rest.
    mfa_enabled:        Mapped[bool]       = mapped_column(Boolean, default=False)
    mfa_secret_enc:     Mapped[str | None] = mapped_column(String, nullable=True)
    mfa_pending_enc:    Mapped[str | None] = mapped_column(String, nullable=True)  # during enrolment
    mfa_last_step:      Mapped[int | None] = mapped_column(Integer, nullable=True)  # blocks code replay

    # Brute-force protection
    failed_login_count: Mapped[int]             = mapped_column(Integer, default=0)
    locked_until:       Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_login_at:      Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    roles: Mapped[list["Role"]] = relationship(secondary="user_roles", lazy="selectin")


class Role(Base):
    __tablename__ = "roles"

    id:           Mapped[str]        = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name:         Mapped[str]        = mapped_column(String, unique=True)
    description:  Mapped[str | None] = mapped_column(String, nullable=True)
    is_active:    Mapped[bool]       = mapped_column(Boolean, default=True)
    is_system:    Mapped[bool]       = mapped_column(Boolean, default=False)
    mfa_required: Mapped[bool]       = mapped_column(Boolean, default=False)
    created_at:   Mapped[datetime]   = mapped_column(DateTime, default=datetime.utcnow)
    updated_at:   Mapped[datetime]   = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    permissions: Mapped[list["RolePermission"]] = relationship(
        lazy="selectin", cascade="all, delete-orphan", back_populates="role",
    )
    # Not loaded implicitly (it would pull every user each time a role is
    # read); counts are queried explicitly in the roles routes.
    users: Mapped[list[User]] = relationship(secondary="user_roles", lazy="noload", viewonly=True)


class RolePermission(Base):
    __tablename__ = "role_permissions"
    __table_args__ = (UniqueConstraint("role_id", "module", name="uq_role_permission_module"),)

    id:         Mapped[str]  = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    role_id:    Mapped[str]  = mapped_column(String, ForeignKey("roles.id", ondelete="CASCADE"), index=True)
    module:     Mapped[str]  = mapped_column(String)
    can_view:   Mapped[bool] = mapped_column(Boolean, default=False)
    can_add:    Mapped[bool] = mapped_column(Boolean, default=False)
    can_edit:   Mapped[bool] = mapped_column(Boolean, default=False)
    can_delete: Mapped[bool] = mapped_column(Boolean, default=False)

    role: Mapped[Role] = relationship(back_populates="permissions")
