"""
Runtime-editable platform settings (CAPTCHA, SMTP, map providers, security
policy), one row per section holding a JSON document. Secret fields inside
a section are stored encrypted — see app.services.settings_service.
"""
from datetime import datetime
from sqlalchemy import String, DateTime, JSON
from sqlalchemy.orm import Mapped, mapped_column
from app.db.session import Base


class SystemSetting(Base):
    __tablename__ = "system_settings"

    section:    Mapped[str]             = mapped_column(String, primary_key=True)
    value:      Mapped[dict]            = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime]        = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by: Mapped[str | None]      = mapped_column(String, nullable=True)
