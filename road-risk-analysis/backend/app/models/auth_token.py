"""
Server-side session and password-reset tokens.

Only the SHA-256 of a token is stored, so a database leak does not hand out
working sessions. Sessions are opaque (not JWTs) so that logout, user
deactivation and password resets revoke access immediately.
"""
import uuid
import enum
from datetime import datetime
from sqlalchemy import String, DateTime, ForeignKey, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column
from app.db.session import Base


class SessionStage(str, enum.Enum):
    full      = "full"        # fully authenticated
    mfa       = "mfa"         # password OK, waiting for the TOTP code
    mfa_setup = "mfa_setup"   # password OK, MFA is required but not enrolled yet


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id:           Mapped[str]             = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id:      Mapped[str]             = mapped_column(String, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash:   Mapped[str]             = mapped_column(String, unique=True, index=True)
    stage:        Mapped[SessionStage]    = mapped_column(SAEnum(SessionStage), default=SessionStage.full)
    created_at:   Mapped[datetime]        = mapped_column(DateTime, default=datetime.utcnow)
    expires_at:   Mapped[datetime]        = mapped_column(DateTime)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at:   Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ip:           Mapped[str | None]      = mapped_column(String, nullable=True)
    user_agent:   Mapped[str | None]      = mapped_column(String, nullable=True)


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id:         Mapped[str]             = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id:    Mapped[str]             = mapped_column(String, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str]             = mapped_column(String, unique=True, index=True)
    created_at: Mapped[datetime]        = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime]        = mapped_column(DateTime)
    used_at:    Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
