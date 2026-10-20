"""
Append-only audit trail (RRAP-13-006/007/008): who did what, to which
object, when, from where, and whether it succeeded. Actor e-mail is copied
onto the row so history stays readable after a user is deleted.
"""
import uuid
from datetime import datetime
from sqlalchemy import String, DateTime, JSON, Index
from sqlalchemy.orm import Mapped, mapped_column
from app.db.session import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_created_at", "created_at"),
        Index("ix_audit_logs_action", "action"),
        Index("ix_audit_logs_object", "object_type", "object_id"),
    )

    id:          Mapped[str]             = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    created_at:  Mapped[datetime]        = mapped_column(DateTime, default=datetime.utcnow)
    actor_type:  Mapped[str]             = mapped_column(String)                 # user | client | anonymous | system
    actor_id:    Mapped[str | None]      = mapped_column(String, nullable=True, index=True)
    actor_label: Mapped[str | None]      = mapped_column(String, nullable=True)  # e-mail or client name
    action:      Mapped[str]             = mapped_column(String)                 # e.g. "auth.login", "user.update"
    outcome:     Mapped[str]             = mapped_column(String, default="success")  # success | failure
    object_type: Mapped[str | None]      = mapped_column(String, nullable=True)
    object_id:   Mapped[str | None]      = mapped_column(String, nullable=True)
    details:     Mapped[dict | None]     = mapped_column(JSON, nullable=True)
    ip:          Mapped[str | None]      = mapped_column(String, nullable=True)
    user_agent:  Mapped[str | None]      = mapped_column(String, nullable=True)
