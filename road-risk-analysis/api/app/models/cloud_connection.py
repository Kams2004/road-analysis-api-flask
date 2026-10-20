"""
A linked Google Drive / OneDrive account or an SFTP server — an input source of the platform
(RRAP-07-002/004), next to manual upload and streams. Files are browsed and
imported on demand (app.models.cloud_import); optionally a watched folder is
auto-imported every cloud.sync_interval_minutes (`auto_import`). The OAuth refresh token is stored
encrypted (app.core.security.encrypt_secret) and never returned by the API.

SFTP servers (provider `sftp`) keep host / port / username / base path and the
*name* of their password key: the password itself lives only in the secrets
file api/secrets/sftp.env (app.services.sftp_core). The host key is trusted
on first use and stored here (`sftp_host_key`).
"""
import uuid
import enum
from datetime import datetime
from sqlalchemy import JSON, String, DateTime, Integer, Boolean
from sqlalchemy.orm import Mapped, mapped_column
from app.db.session import Base


class CloudProvider(str, enum.Enum):
    google_drive = "google_drive"
    onedrive     = "onedrive"
    sftp         = "sftp"


class ConnectionStatus(str, enum.Enum):
    pending_auth = "pending_auth"
    active       = "active"
    error        = "error"
    disabled     = "disabled"


class CloudConnection(Base):
    __tablename__ = "cloud_connections"

    id:                Mapped[str]             = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    provider:          Mapped[str]             = mapped_column(String)
    name:              Mapped[str]             = mapped_column(String)
    account_email:     Mapped[str | None]      = mapped_column(String, nullable=True)
    folder_id:         Mapped[str | None]      = mapped_column(String, nullable=True)   # None = drive root
    folder_name:       Mapped[str | None]      = mapped_column(String, nullable=True)
    status:            Mapped[str]             = mapped_column(String, default=ConnectionStatus.pending_auth.value)
    refresh_token_enc: Mapped[str | None]      = mapped_column(String, nullable=True)
    created_by:        Mapped[str | None]      = mapped_column(String, nullable=True)
    created_at:        Mapped[datetime]        = mapped_column(DateTime, default=datetime.utcnow)
    last_sync_at:      Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_sync_status:  Mapped[str | None]      = mapped_column(String, nullable=True)   # ok | error
    last_sync_error:   Mapped[str | None]      = mapped_column(String, nullable=True)
    last_sync_files:   Mapped[int]             = mapped_column(Integer, default=0)
    files_imported_total: Mapped[int]          = mapped_column(Integer, default=0)
    enabled:           Mapped[bool]            = mapped_column(Boolean, default=True)
    # Watched folder → automatic import (the former "sync"): off for new
    # connections, back-filled from `enabled` for connections made before.
    auto_import:            Mapped[bool]             = mapped_column(Boolean, default=False)
    auto_import_models:     Mapped[list | None]      = mapped_column(JSON, nullable=True)   # None = defaults
    auto_import_frame_skip: Mapped[int | None]       = mapped_column(Integer, nullable=True)
    # SFTP server profile (provider "sftp"); no secret is stored in the database.
    sftp_host:         Mapped[str | None]      = mapped_column(String, nullable=True)
    sftp_port:         Mapped[int | None]      = mapped_column(Integer, nullable=True)
    sftp_username:     Mapped[str | None]      = mapped_column(String, nullable=True)
    sftp_base_path:    Mapped[str | None]      = mapped_column(String, nullable=True)   # None = the user's home
    sftp_password_key: Mapped[str | None]      = mapped_column(String, nullable=True)   # SFTP_PASSWORD_<key>
    sftp_host_key:     Mapped[str | None]      = mapped_column(String, nullable=True)   # "<type> <base64>"
    sftp_host_key_fp:  Mapped[str | None]      = mapped_column(String, nullable=True)   # "<type> SHA256:…"
    sftp_host_key_at:  Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
