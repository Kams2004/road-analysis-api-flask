"""
Credentials for non-human API consumers — the VIASafe mobile app, the demo
simulator and, later, approved third parties (RRAP-12-015). Each client gets
its own key so it can be rotated or revoked without touching user accounts.
Only a SHA-256 of the key is stored; the clear key is shown once at creation.
"""
import uuid
from datetime import datetime
from sqlalchemy import String, DateTime, Boolean
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db.session import Base


class ApiClient(Base):
    __tablename__ = "api_clients"

    id:           Mapped[str]             = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name:         Mapped[str]             = mapped_column(String)
    description:  Mapped[str | None]      = mapped_column(String, nullable=True)
    key_prefix:   Mapped[str]             = mapped_column(String)            # first chars, to recognise a key
    key_hash:     Mapped[str]             = mapped_column(String, unique=True, index=True)
    is_active:    Mapped[bool]            = mapped_column(Boolean, default=True)
    created_by:   Mapped[str | None]      = mapped_column(String, nullable=True)
    created_at:   Mapped[datetime]        = mapped_column(DateTime, default=datetime.utcnow)
    rotated_at:   Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at:   Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
